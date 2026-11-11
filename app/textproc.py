"""Read Markdown/Obsidian notes: frontmatter, tags, wikilinks, sections."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_CODE_FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
# [[target]], [[target|alias]], [[target#section|alias]], ![[embed]]
_WIKILINK = re.compile(r"(!?)\[\[([^\]\|#]*)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"„“(]?\w)|\n{2,}")
# inline tags: letter first (any script), then letters, digits, _, -, / (not "#1" or headings)
_TAG_INLINE = re.compile(r"(?<![\w/#])#([^\W\d_][\w/\-]*)")


@dataclass
class Section:
    heading_path: list[str]
    text: str   # text for embedding, full-text search and reranking (with heading path)
    body: str   # text without heading path (for snippets)


@dataclass
class Note:
    path: str
    title: str
    tags: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)        # frontmatter without tags
    link_targets: list[str] = field(default_factory=list)       # raw, as written
    sections: list[Section] = field(default_factory=list)


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER.match(raw)
    if not m:
        return {}, raw
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    # YAML turns dates into date objects; keep scalars as strings
    clean: dict[str, Any] = {}
    for k, v in meta.items():
        if isinstance(v, list):
            clean[str(k)] = [str(x) for x in v if x is not None and not isinstance(x, (dict, list))]
        elif v is not None and not isinstance(v, dict):
            clean[str(k)] = str(v)
    return clean, raw[m.end():]


def _link_text(m: re.Match) -> str:
    if m.group(1):  # embeds (images, PDFs, notes) are not part of the text
        return ""
    alias = m.group(4)
    if alias:
        return alias.strip()
    target = (m.group(2) or "").strip()
    return target.rsplit("/", 1)[-1] if target else (m.group(3) or "").lstrip("#")


def clean_text(text: str) -> str:
    text = _WIKILINK.sub(_link_text, text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_long(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    parts = [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]
    if not parts:
        return [text]
    pieces, cur, cur_tok = [], [], 0
    for s in parts:
        t = estimate_tokens(s)
        if cur and cur_tok + t > max_tokens:
            pieces.append(" ".join(cur))
            ov, ov_tok = [], 0
            for prev in reversed(cur):
                pt = estimate_tokens(prev)
                if ov and ov_tok + pt > overlap_tokens:
                    break
                ov.insert(0, prev)
                ov_tok += pt
            cur, cur_tok = ov, ov_tok
        cur.append(s)
        cur_tok += t
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def parse_note(path: str, raw: str, max_tokens: int = 512, overlap_tokens: int = 50) -> Note:
    meta, body = parse_frontmatter(raw)
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")

    tags = meta.pop("tags", [])
    tags = [tags] if isinstance(tags, str) and tags else list(tags or [])
    tags = [t.lstrip("#") for t in tags if t]
    no_code = _CODE_FENCE.sub("", body)
    tags += [t for t in _TAG_INLINE.findall(no_code) if t not in tags]

    links = []
    for m in _WIKILINK.finditer(raw):
        target = (m.group(2) or "").strip()
        if target and target not in links:
            links.append(target)

    h1 = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    title = h1.group(1).strip() if h1 else stem

    # split at headings, ignoring headings inside code blocks
    blocks: list[tuple[int, str, str]] = []
    spans = [(m.start(), m.end()) for m in _CODE_FENCE.finditer(body)]
    heads = [m for m in _HEADING.finditer(body) if not any(a <= m.start() < b for a, b in spans)]
    last_end, level, heading = 0, 0, ""
    for m in heads:
        blocks.append((level, heading, body[last_end:m.start()]))
        level, heading, last_end = len(m.group(1)), m.group(2).strip(), m.end()
    blocks.append((level, heading, body[last_end:]))

    hierarchy = [""] * 7
    sections: list[Section] = []
    for lvl, head, text in blocks:
        if lvl > 0:
            hierarchy[lvl] = clean_text(head)
            for i in range(lvl + 1, 7):
                hierarchy[i] = ""
        path_h = [h for h in hierarchy[1:lvl + 1] if h] if lvl > 0 else []
        if not path_h or path_h[0] != title:
            path_h = [title] + path_h
        text = clean_text(text)
        if not text and lvl <= 1:
            continue  # empty title line
        prefix = " > ".join(path_h)
        full = f"{prefix}\n\n{text}" if text else prefix
        if estimate_tokens(full) <= max_tokens:
            sections.append(Section(path_h, full, text))
        else:
            for piece in _split_long(text, max_tokens - estimate_tokens(prefix), overlap_tokens):
                sections.append(Section(path_h, f"{prefix}\n\n{piece}", piece))
    return Note(path=path, title=title, tags=tags, fields=meta, link_targets=links, sections=sections)


def resolve_links(notes: dict[str, list[str]], paths: list[str]) -> dict[str, list[str]]:
    """Resolve wikilink targets to note paths like Obsidian (path, else file name)."""
    by_path = {p.removesuffix(".md").lower(): p for p in paths}
    by_name: dict[str, str] = {}
    for p in sorted(paths, key=lambda x: (x.count("/"), x)):  # shortest path wins
        by_name.setdefault(p.rsplit("/", 1)[-1].removesuffix(".md").lower(), p)
    out: dict[str, list[str]] = {}
    for src, targets in notes.items():
        res = []
        for t in targets:
            key = t.strip().removesuffix(".md").lower()
            p = by_path.get(key) or by_name.get(key.rsplit("/", 1)[-1])
            if p and p != src and p not in res:
                res.append(p)
        out[src] = res
    return out
