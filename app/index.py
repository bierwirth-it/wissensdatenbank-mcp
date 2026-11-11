"""Search index for a Markdown/Obsidian vault on LanceDB.

Storage (all below INDEX_DIR)
- Table ``chunks_<timestamp>``: one row per section (text, heading path, vector)
  plus access metadata of its note (expanded tags, folders, frontmatter
  ``key=value`` entries). Full-text index with stemming/stop words, label-list
  indices on the access columns, B-tree on ``note`` and, from
  VECTOR_INDEX_MIN_ROWS rows on, a vector index.
- Table ``emb_<model hash>``: embedding cache keyed by text hash. Rows are
  appended while an update runs (checkpoint), so a cancelled or crashed update
  never loses computed embeddings.
- ``notes-<table>-<version>.json``: per-note metadata (hash, title, tags,
  fields, resolved wikilinks).
- ``state.json``: the active state (table, version, commit). It is written only
  after an update succeeded and is the switch-over point.

Availability
- Searches read through a table handle pinned to the active LanceDB version.
  Updates write new versions on a separate handle; the read handle moves to the
  new version only when the update is complete. On failure the table is
  restored to the active version; on start-up a version mismatch (crash in the
  middle of a write) is rolled back the same way.

Incremental
- Only new and changed notes are parsed; their old rows and the rows of
  deleted notes are removed. Embeddings come from the cache where possible;
  unchanged notes are not touched.

Access control
- Every search, note lookup and link list takes an ``Access`` (see access.py).
  Searches use its SQL prefilter, note-level operations a cached set of
  visible notes. Hidden notes behave as if they did not exist.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

from access import FULL_ACCESS, Access, NoteMeta, expand_tags, field_entries
from progress import BuildCancelled, progress
from textproc import content_hash, parse_note, resolve_links

log = logging.getLogger("wissensdatenbank-mcp.index")
warnings.filterwarnings("ignore", message=r".*is deprecated as of 0\.25\.0.*")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    embed_model: str = _env("EMBED_MODEL", "BAAI/bge-m3")
    rerank_model: str = _env("RERANK_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    rerank_max_len: int = int(_env("RERANK_MAX_LENGTH", "256"))
    rerank_pool: int = int(_env("RERANK_POOL", "20"))
    rerank_fusion_weight: float = float(_env("RERANK_FUSION_WEIGHT", "0.3"))
    rrf_k: int = int(_env("RRF_K", "30"))
    semantic_weight: float = float(_env("SEMANTIC_WEIGHT", "1.0"))
    fulltext_weight: float = float(_env("FULLTEXT_WEIGHT", "0.5"))
    link_seeds: int = int(_env("LINK_SEEDS", "3"))
    link_max: int = int(_env("LINK_MAX", "5"))
    pool_per_note: int = int(_env("POOL_PER_NOTE", "0"))  # 0 = no limit
    chunk_max_tokens: int = int(_env("CHUNK_MAX_TOKENS", "512"))
    chunk_overlap_tokens: int = int(_env("CHUNK_OVERLAP_TOKENS", "50"))
    embed_batch: int = int(_env("EMBED_BATCH", "32"))
    fts_language: str = _env("FTS_LANGUAGE", "English")
    vector_index_min_rows: int = int(_env("VECTOR_INDEX_MIN_ROWS", "50000"))
    vector_nprobes: int = int(_env("VECTOR_NPROBES", "20"))
    vector_refine: int = int(_env("VECTOR_REFINE_FACTOR", "5"))
    vector_exact: bool = _env("VECTOR_EXACT", "0") == "1"
    # accounts that see less than this share of the notes search exactly (faster with narrow filters)
    exact_below_share: float = float(_env("VECTOR_EXACT_BELOW_SHARE", "0.1"))
    checkpoint_interval: float = float(_env("CHECKPOINT_INTERVAL", "60"))
    snippet_chars: int = int(_env("SNIPPET_CHARS", "400"))

    def index_signature(self) -> str:
        """Everything that shapes the stored index; a change forces a full rebuild."""
        return f"{self.embed_model}|{self.chunk_max_tokens}|{self.chunk_overlap_tokens}|{self.fts_language}|v2"


def _sql_list(values: list[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _and(*clauses: str | None) -> str | None:
    parts = [f"({c})" for c in clauses if c]
    return " AND ".join(parts) if parts else None


class NotReady(RuntimeError):
    pass


class NoteNotFound(ValueError):
    pass


# ---------------------------------------------------------------------------
# Models (loaded once per process)
# ---------------------------------------------------------------------------

class Models:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._embedder = None
        self._reranker = None
        self._qcache: OrderedDict[str, np.ndarray] = OrderedDict()
        # many threads slow down small computations (single queries)
        threads = int(os.environ.get("TORCH_THREADS", "0")) or min(os.cpu_count() or 1, 8)
        import torch
        torch.set_num_threads(threads)

    def embedder(self):
        with self._lock:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer
                t0 = time.monotonic()
                log.info("Loading embedding model %s ...", self.cfg.embed_model)
                self._embedder = SentenceTransformer(self.cfg.embed_model, device="cpu")
                log.info("Embedding model loaded (%.1f s)", time.monotonic() - t0)
            return self._embedder

    def reranker(self):
        with self._lock:
            if self._reranker is None:
                from sentence_transformers import CrossEncoder
                t0 = time.monotonic()
                log.info("Loading reranker %s ...", self.cfg.rerank_model)
                self._reranker = CrossEncoder(self.cfg.rerank_model, max_length=self.cfg.rerank_max_len,
                                              device="cpu")
                log.info("Reranker loaded (%.1f s)", time.monotonic() - t0)
            return self._reranker

    @property
    def dim(self) -> int:
        m = self.embedder()
        get = getattr(m, "get_embedding_dimension", None) or m.get_sentence_embedding_dimension
        return int(get())

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self.embedder().encode(texts, batch_size=self.cfg.embed_batch, normalize_embeddings=True,
                                      show_progress_bar=False, convert_to_numpy=True)
        return vecs.astype(np.float32)

    def encode_query(self, q: str) -> np.ndarray:
        with self._lock:
            if q in self._qcache:
                self._qcache.move_to_end(q)
                return self._qcache[q]
        v = self.encode([q])[0]
        with self._lock:
            self._qcache[q] = v
            if len(self._qcache) > 512:
                self._qcache.popitem(last=False)
        return v

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        return self.reranker().predict([(query, t) for t in texts], batch_size=32,
                                        show_progress_bar=False).tolist()


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    table: Any
    version: int
    notes: dict[str, dict]
    backlinks: dict[str, list[str]]


class VaultIndex:
    def __init__(self, root: Path, cfg: Config | None = None) -> None:
        self.root = root
        self.cfg = cfg or Config()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(root / "lance"))
        self.models = Models(self.cfg)
        self.state_path = root / "state.json"
        self.state: dict[str, Any] = {}
        self._snap: Snapshot | None = None
        self._snap_lock = threading.Lock()
        self._visible: OrderedDict[tuple, frozenset[str]] = OrderedDict()
        self.building: dict[str, Any] | None = None
        self.last_update: dict[str, Any] | None = None
        self._emb_table_name = "emb_" + content_hash(self.cfg.embed_model)[:10]
        self._remove_legacy()

    def _remove_legacy(self) -> None:
        """Remove files of an earlier index format from the volume."""
        for name in ("slots", "cache", "active", "lancedb", "graphdb", "metadata.db", "bm25_index.pkl"):
            p = self.root / name
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                log.info("Removed legacy index data: %s", p)
            elif p.exists():
                p.unlink(missing_ok=True)
                log.info("Removed legacy index data: %s", p)

    # -- state ---------------------------------------------------------------

    def _tables(self) -> list[str]:
        res = self.db.list_tables()
        return list(getattr(res, "tables", res))

    def _write_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        tmp.replace(self.state_path)
        self.state = state

    def _notes_path(self, table: str, version: int) -> Path:
        return self.root / f"notes-{table}-{version}.json"

    def open(self) -> None:
        """Load the active state; roll back a crashed update."""
        if not self.state_path.exists():
            log.info("No index yet")
            return
        state = json.loads(self.state_path.read_text())
        self.state = state
        if state.get("signature") != self.cfg.index_signature():
            log.warning("Index configuration changed (%s -> %s), a full rebuild is required",
                        state.get("signature"), self.cfg.index_signature())
            return
        name, version = state["table"], state["version"]
        if name not in self._tables():
            log.error("Table %s is missing, a full rebuild is required", name)
            return
        tbl = self.db.open_table(name)
        if tbl.version != version:
            log.warning("Found an interrupted update (version %s, active %s), rolling back",
                        tbl.version, version)
            tbl.restore(version)
            state["version"] = tbl.version
            os.replace(self._notes_path(name, version), self._notes_path(name, tbl.version))
            self._write_state(state)
        self._load_snapshot(name, state["version"])
        self._cleanup_files()
        s = state.get("stats", {})
        log.info("Index loaded: %s notes, %s sections, commit %s",
                 s.get("notes"), s.get("chunks"), str(state.get("commit"))[:10])

    def _load_snapshot(self, name: str, version: int) -> None:
        tbl = self.db.open_table(name)
        tbl.checkout(version)
        notes = json.loads(self._notes_path(name, version).read_text())
        back: dict[str, list[str]] = {}
        for p, meta in notes.items():
            for t in meta.get("links", []):
                back.setdefault(t, []).append(p)
        with self._snap_lock:
            self._snap = Snapshot(tbl, version, notes, back)
            self._visible.clear()

    def _cleanup_files(self) -> None:
        keep = self._notes_path(self.state.get("table", ""), self.state.get("version", -1)).name
        for f in self.root.glob("notes-*.json"):
            if f.name != keep:
                f.unlink(missing_ok=True)
        active = self.state.get("table")
        for t in self._tables():
            if t.startswith("chunks_") and t != active:
                self.db.drop_table(t)
                log.info("Removed orphaned table %s", t)

    def snapshot(self) -> Snapshot:
        with self._snap_lock:
            if self._snap is None:
                raise NotReady("the index is being built, please try again later")
            return self._snap

    def snapshot_ready(self) -> bool:
        return self._snap is not None

    # -- access control ------------------------------------------------------

    def visible_notes(self, access: Access, snap: Snapshot | None = None) -> frozenset[str] | None:
        """Visible note paths for an account; ``None`` = everything visible."""
        if access.unrestricted:
            return None
        snap = snap or self.snapshot()
        key = (id(access), access.name, snap.version, id(snap))
        with self._snap_lock:
            hit = self._visible.get(key)
            if hit is not None:
                self._visible.move_to_end(key)
                return hit
        vis = frozenset(p for p, m in snap.notes.items() if access.visible(NoteMeta.from_note(p, m)))
        with self._snap_lock:
            self._visible[key] = vis
            while len(self._visible) > 64:
                self._visible.popitem(last=False)
        return vis

    def is_visible(self, access: Access, path: str, snap: Snapshot | None = None) -> bool:
        snap = snap or self.snapshot()
        if path not in snap.notes:
            return False
        vis = self.visible_notes(access, snap)
        return vis is None or path in vis

    def explain(self, access: Access, path: str) -> dict:
        snap = self.snapshot()
        meta = snap.notes.get(path)
        if meta is None:
            return {"note": path, "indexed": False, "visible": False, "reason": "note is not in the index"}
        ok, reason = access.explain(NoteMeta.from_note(path, meta))
        return {"note": path, "indexed": True, "visible": ok, "reason": reason,
                "tags": meta.get("acl_tags", []), "fields": meta.get("acl_fields", [])}

    # -- embedding cache -----------------------------------------------------

    def _emb_table(self, dim: int):
        if self._emb_table_name in self._tables():
            return self.db.open_table(self._emb_table_name)
        schema = pa.schema([("hash", pa.string()), ("vector", pa.list_(pa.float32(), dim))])
        return self.db.create_table(self._emb_table_name, schema=schema)

    def _cache_lookup(self, emb, hashes: list[str]) -> dict[str, np.ndarray]:
        found: dict[str, np.ndarray] = {}
        uniq = list(dict.fromkeys(hashes))
        for i in range(0, len(uniq), 1000):
            part = uniq[i:i + 1000]
            tbl = emb.search().where(f"hash IN ({_sql_list(part)})").select(["hash", "vector"]) \
                .limit(len(part) * 2).to_arrow()
            for h, v in zip(tbl.column("hash").to_pylist(), tbl.column("vector").to_numpy(zero_copy_only=False)):
                found[h] = np.asarray(v, dtype=np.float32)
        return found

    def _flush_cache(self, emb, pending: list) -> None:
        if not pending:
            return
        t0 = time.monotonic()
        n = len(pending)
        emb.add(pa.Table.from_pylist([{"hash": k, "vector": v} for k, v in pending], schema=emb.schema))
        pending.clear()
        log.info("Checkpoint: %d embeddings saved to the cache (%.1f s)", n, time.monotonic() - t0)

    # -- update --------------------------------------------------------------

    def update(self, commit: str, files: dict[str, Path], force: bool = False, use_cache: bool = True) -> bool:
        """Bring the index in line with the files. Returns True if anything changed."""
        t0 = time.monotonic()
        snap = self._snap
        sig_ok = self.state.get("signature") == self.cfg.index_signature() and snap is not None
        full = force or not sig_ok

        hashes = {rel: content_hash(p.read_bytes()) for rel, p in files.items()}
        old = snap.notes if (snap and not full) else {}
        added = sorted(r for r in hashes if r not in old)
        changed = sorted(r for r in hashes if r in old and old[r]["hash"] != hashes[r])
        removed = sorted(r for r in old if r not in hashes)
        if not (added or changed or removed or full):
            if self.state and self.state.get("commit") != commit:
                self.state["commit"] = commit
                self._write_state(self.state)
            return False

        what = "full rebuild" if full else f"{len(added)} new, {len(changed)} changed, {len(removed)} deleted"
        log.info("Updating index for commit %s: %s", commit[:10], what)
        for label, items in (("new", added), ("changed", changed), ("deleted", removed)):
            for r in items[:20]:
                log.info("  %s: %s", label, r)
            if len(items) > 20:
                log.info("  %s: ... and %d more", label, len(items) - 20)
        progress.cancel_requested.clear()
        progress.start(f"Index update {commit[:8]} ({what})")
        self.building = {"commit": commit, "since": datetime.now(timezone.utc).isoformat(),
                         "new": len(added), "changed": len(changed), "deleted": len(removed), "full": full}
        pending: list[tuple[str, np.ndarray]] = []
        emb = None
        cache_stats = {"reused": 0, "computed": 0}
        try:
            # 1. parse notes
            todo = added + changed
            progress.set_phase("Parsing notes", len(todo))
            parsed = {}
            for rel in todo:
                raw = files[rel].read_text(encoding="utf-8", errors="replace")
                parsed[rel] = parse_note(rel, raw, self.cfg.chunk_max_tokens, self.cfg.chunk_overlap_tokens)
                progress.advance()
            progress.check_cancel()

            # 2. embeddings (cache, else compute; checkpoint into the cache)
            dim = self.models.dim
            emb = self._emb_table(dim)
            items = [(rel, i, s) for rel in todo for i, s in enumerate(parsed[rel].sections)]
            keys = [content_hash(self.cfg.embed_model + "\0" + s.text) for _, _, s in items]
            cached = self._cache_lookup(emb, keys) if (use_cache and keys) else {}
            missing = [i for i, k in enumerate(keys) if k not in cached]
            uniq_missing = list(dict.fromkeys(keys[i] for i in missing))  # compute equal texts once
            text_of = {keys[i]: items[i][2].text for i in missing}
            cache_stats["reused"] = len(keys) - len(missing)
            cache_stats["computed"] = len(uniq_missing)
            log.info("Embeddings: %d sections, %d from cache, %d to compute",
                     len(keys), cache_stats["reused"], len(uniq_missing))
            progress.set_phase("Embeddings", len(uniq_missing))
            last_cp = te = time.monotonic()
            # larger blocks: the model sorts by length within one call and saves padding
            step = max(self.cfg.embed_batch * 8, 64)
            for n in range(0, len(uniq_missing), step):
                progress.check_cancel()
                part = uniq_missing[n:n + step]
                vecs = self.models.encode([text_of[k] for k in part])
                for k, v in zip(part, vecs):
                    cached[k] = v
                    pending.append((k, v))
                progress.advance(len(part))
                if time.monotonic() - last_cp >= self.cfg.checkpoint_interval:
                    self._flush_cache(emb, pending)
                    last_cp = time.monotonic()
            self._flush_cache(emb, pending)
            if uniq_missing:
                log.info("Embeddings done: %d computed in %.0f s", len(uniq_missing), time.monotonic() - te)

            # 3. write rows
            progress.set_phase("Writing sections")
            note_acl = {}
            for rel in todo:
                n = parsed[rel]
                note_acl[rel] = (expand_tags(n.tags), [f for f in rel.lower().split("/")[:-1]],
                                 field_entries(n.fields))
            rows = []
            for (rel, i, s), k in zip(items, keys):
                tags, _, flds = note_acl[rel]
                parts = rel.lower().split("/")[:-1]
                rows.append({
                    "chunk_id": content_hash(f"{rel}#{i}"), "note": rel, "title": parsed[rel].title,
                    "heading": " > ".join(s.heading_path), "ord": i, "text": s.text, "body": s.body,
                    "hash": k, "acl_tags": tags,
                    "acl_folders": ["/".join(parts[:j]) for j in range(1, len(parts) + 1)],
                    "acl_fields": flds, "vector": cached[k],
                })
            if full:
                tbl = self._new_table(dim)
            else:
                tbl = self.db.open_table(self.state["table"])
                gone = changed + removed
                for i in range(0, len(gone), 500):
                    tbl.delete(f"note IN ({_sql_list(gone[i:i + 500])})")
            for i in range(0, len(rows), 5000):
                tbl.add(pa.Table.from_pylist(rows[i:i + 5000], schema=self._schema(dim)))
            progress.check_cancel()

            # 4. indices
            progress.set_phase("Updating indices")
            self._ensure_indices(tbl, full)

            # 5. metadata and switch-over
            notes = {r: dict(m) for r, m in old.items() if r not in removed}
            for rel in todo:
                n = parsed[rel]
                notes[rel] = {"hash": hashes[rel], "title": n.title, "tags": n.tags,
                              "acl_tags": note_acl[rel][0], "acl_fields": note_acl[rel][2],
                              "raw_links": n.link_targets, "chunks": len(n.sections)}
            links = resolve_links({r: m.get("raw_links", []) for r, m in notes.items()}, list(notes))
            for r, ls in links.items():
                notes[r]["links"] = ls
            version = tbl.version
            npath = self._notes_path(tbl.name, version)
            tmp = npath.with_suffix(".tmp")
            tmp.write_text(json.dumps(notes, ensure_ascii=False))
            tmp.replace(npath)
            n_chunks = tbl.count_rows()
            seconds = round(time.monotonic() - t0, 1)
            old_table = self.state.get("table")
            self._write_state({
                "signature": self.cfg.index_signature(), "table": tbl.name, "version": version,
                "commit": commit, "built_at": datetime.now(timezone.utc).isoformat(), "seconds": seconds,
                "changes": {"new": len(added), "changed": len(changed), "deleted": len(removed), "full": full},
                "stats": {"notes": len(notes), "chunks": n_chunks,
                          "links": sum(len(m.get("links", [])) for m in notes.values())},
                "embeddings": cache_stats,
            })
            self._load_snapshot(tbl.name, version)
            log.info("New index state active: %d notes, %d sections (%s) in %.1f s",
                     len(notes), n_chunks, what, seconds)
            self._after_switch(tbl, old_table)
            self.last_update = {"ok": True, "seconds": seconds}
            return True
        except Exception as exc:
            if emb is not None and pending:
                try:
                    self._flush_cache(emb, pending)
                except Exception:  # noqa: BLE001
                    log.warning("Could not save the embedding cache", exc_info=True)
            self._rollback()
            if isinstance(exc, BuildCancelled):
                log.warning("Update cancelled; computed embeddings are cached, the previous state stays active")
            else:
                log.exception("Update failed, the previous state stays active")
            self.last_update = {"ok": False, "error": str(exc)}
            raise
        finally:
            progress.cancel_requested.clear()
            self.building = None
            progress.end()

    def _schema(self, dim: int) -> pa.Schema:
        return pa.schema([
            ("chunk_id", pa.string()), ("note", pa.string()), ("title", pa.string()),
            ("heading", pa.string()), ("ord", pa.int32()), ("text", pa.string()), ("body", pa.string()),
            ("hash", pa.string()), ("acl_tags", pa.list_(pa.string())),
            ("acl_folders", pa.list_(pa.string())), ("acl_fields", pa.list_(pa.string())),
            ("vector", pa.list_(pa.float32(), dim)),
        ])

    def _new_table(self, dim: int):
        name = "chunks_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return self.db.create_table(name, schema=self._schema(dim))

    def _ensure_indices(self, tbl, fresh: bool) -> None:
        cols = {tuple(getattr(i, "columns", []) or []) for i in tbl.list_indices()}
        if fresh or ("text",) not in cols:
            tbl.create_fts_index("text", use_tantivy=False, language=self.cfg.fts_language, stem=True,
                                 remove_stop_words=True, ascii_folding=True, replace=True)
        if ("note",) not in cols:
            tbl.create_scalar_index("note", replace=True)
        for col in ("acl_tags", "acl_folders", "acl_fields"):
            if (col,) not in cols:
                tbl.create_scalar_index(col, index_type="LABEL_LIST", replace=True)
        rows = tbl.count_rows()
        if rows >= self.cfg.vector_index_min_rows and ("vector",) not in cols:
            log.info("Creating vector index (%d sections) ...", rows)
            t0 = time.monotonic()
            tbl.create_index(metric="cosine", vector_column_name="vector", index_type="IVF_HNSW_SQ",
                             replace=True)
            log.info("Vector index created (%.0f s)", time.monotonic() - t0)
        tbl.optimize(cleanup_older_than=timedelta(minutes=2))  # fold new rows into all indices

    def _after_switch(self, tbl, old_table: str | None) -> None:
        # delete older versions only after a grace period: searches still running
        # on the previous state must not lose their data
        try:
            tbl.cleanup_old_versions(older_than=timedelta(minutes=2))
        except Exception:  # noqa: BLE001
            log.debug("Could not clean up old versions", exc_info=True)
        if old_table and old_table != tbl.name and old_table in self._tables():
            self.db.drop_table(old_table)
            log.info("Removed old table %s", old_table)
        self._cleanup_files()
        self._maybe_prune_cache()

    def _rollback(self) -> None:
        name, version = self.state.get("table"), self.state.get("version")
        for t in self._tables():
            if t.startswith("chunks_") and t != name:
                self.db.drop_table(t)
        if name and name in self._tables() and version is not None:
            tbl = self.db.open_table(name)
            if tbl.version != version:
                tbl.restore(version)
                self.state["version"] = tbl.version
                os.replace(self._notes_path(name, version), self._notes_path(name, tbl.version))
                self._write_state(self.state)
                self._load_snapshot(name, tbl.version)
                log.info("Table rolled back to the active state")

    def _maybe_prune_cache(self) -> None:
        """Shrink the cache to the current sections when it is more than twice as large."""
        if self._emb_table_name not in self._tables():
            return
        emb = self.db.open_table(self._emb_table_name)
        n_cache, n_chunks = emb.count_rows(), self.state.get("stats", {}).get("chunks", 0)
        if n_chunks and n_cache > 2 * n_chunks + 1000:
            self.prune_cache()

    def prune_cache(self) -> dict[str, int]:
        snap = self.snapshot()
        data = snap.table.search().select(["hash", "vector"]).limit(snap.table.count_rows() + 1).to_arrow()
        seen, keep = set(), []
        for i, h in enumerate(data.column("hash").to_pylist()):
            if h not in seen:
                seen.add(h)
                keep.append(i)
        dedup = data.take(keep)
        before = self.db.open_table(self._emb_table_name).count_rows() if self._emb_table_name in self._tables() else 0
        self.db.create_table(self._emb_table_name, data=dedup, mode="overwrite")
        log.info("Embedding cache pruned: %d -> %d entries", before, dedup.num_rows)
        return {"before": before, "after": dedup.num_rows}

    # -- search --------------------------------------------------------------

    def _vq(self, q, exact: bool = False):
        """Vector query: refine results of the approximate index, or search exactly.

        ``exact`` is used for narrow access filters: a brute-force scan over the
        few permitted rows is faster than probing the approximate index."""
        if self.cfg.vector_exact or exact:
            return q.bypass_vector_index()
        try:
            return q.nprobes(self.cfg.vector_nprobes).refine_factor(self.cfg.vector_refine)
        except Exception:  # noqa: BLE001
            return q

    def search(self, query: str, top_k: int = 8, mode: str = "hybrid", rerank: bool = True,
               access: Access = FULL_ACCESS) -> list[dict]:
        snap = self.snapshot()
        cfg = self.cfg
        acl = access.sql_filter()
        if acl == "false":
            return []
        visible = self.visible_notes(access, snap)
        narrow = visible is not None and len(visible) < cfg.exact_below_share * max(len(snap.notes), 1)
        pool = cfg.rerank_pool if rerank else max(top_k * 3, 20)
        fetch = pool * 3  # headroom for the duplicate rule
        cols = ["chunk_id", "note", "title", "heading", "text", "body", "hash"]
        rows: dict[str, dict] = {}
        lists: dict[str, list[str]] = {}
        qvec = self.models.encode_query(query) if mode in ("hybrid", "semantic") or rerank else None

        if mode in ("hybrid", "semantic"):
            q = self._vq(snap.table.search(qvec, vector_column_name="vector").distance_type("cosine"), narrow)
            if acl:
                q = q.where(acl, prefilter=True)
            res = q.limit(fetch).select(cols + ["_distance"]).to_list()
            lists["semantic"] = [r["chunk_id"] for r in res]
            rows.update({r["chunk_id"]: r for r in res})
        if mode in ("hybrid", "fulltext"):
            text = re.sub(r"[^\w\s\-]", " ", query).strip()
            if text:
                try:
                    q = snap.table.search(text, query_type="fts", fts_columns="text")
                    if acl:
                        q = q.where(acl, prefilter=True)
                    res = q.limit(fetch).select(cols + ["_score"]).to_list()
                except Exception:  # noqa: BLE001
                    log.debug("Full-text search failed", exc_info=True)
                    res = []
                lists["fulltext"] = [r["chunk_id"] for r in res]
                for r in res:
                    rows.setdefault(r["chunk_id"], r)

        fused: dict[str, float] = {}
        engines: dict[str, set] = {}
        weights = {"semantic": cfg.semantic_weight, "fulltext": cfg.fulltext_weight}
        for name, ids in lists.items():
            for rank, cid in enumerate(ids):
                fused[cid] = fused.get(cid, 0.0) + weights.get(name, 1.0) / (cfg.rrf_k + rank + 1)
                engines.setdefault(cid, set()).add(name)

        # Diversity: identical section texts (templates, copies) count once;
        # optionally at most pool_per_note sections per note.
        ranked, seen_hash, per_note = [], set(), {}
        for cid in sorted(fused, key=lambda c: -fused[c]):
            r = rows[cid]
            if r["hash"] in seen_hash or (cfg.pool_per_note and per_note.get(r["note"], 0) >= cfg.pool_per_note):
                continue
            seen_hash.add(r["hash"])
            per_note[r["note"]] = per_note.get(r["note"], 0) + 1
            ranked.append(cid)
            if len(ranked) >= pool:
                break

        # Linked notes of the best hits join the candidate pool
        if mode == "hybrid" and cfg.link_seeds > 0 and cfg.link_max > 0 and qvec is not None:
            seeds: list[str] = []
            for cid in ranked:
                n = rows[cid]["note"]
                if n not in seeds:
                    seeds.append(n)
                if len(seeds) >= cfg.link_seeds:
                    break
            in_pool = {rows[c]["note"] for c in ranked}
            neigh: list[str] = []
            for n in seeds:
                for m in snap.notes.get(n, {}).get("links", []) + snap.backlinks.get(n, []):
                    if m not in in_pool and m not in neigh and m in snap.notes \
                            and (visible is None or m in visible):
                        neigh.append(m)
            if neigh:
                q = snap.table.search(qvec, vector_column_name="vector").distance_type("cosine") \
                    .where(_and(f"note IN ({_sql_list(neigh[:200])})", acl), prefilter=True)
                res = q.limit(min(len(neigh) * 4, 400)).select(cols + ["_distance"]).to_list()
                best: dict[str, dict] = {}
                for r in res:
                    best.setdefault(r["note"], r)
                for r in sorted(best.values(), key=lambda r: r["_distance"])[:cfg.link_max]:
                    if r["hash"] in seen_hash:
                        continue
                    seen_hash.add(r["hash"])
                    rows[r["chunk_id"]] = r
                    ranked.append(r["chunk_id"])
                    engines.setdefault(r["chunk_id"], set()).add("wikilink")

        if not ranked:
            return []
        if rerank:
            scores = self.models.rerank(query, [rows[c]["text"] for c in ranked])
            fvals = [fused.get(c, 0.0) for c in ranked]
            fmin, fmax = min(fvals), max(fvals)
            rmin, rmax = min(scores), max(scores)
            wf = cfg.rerank_fusion_weight
            final = {
                c: wf * ((fused.get(c, 0.0) - fmin) / ((fmax - fmin) or 1))
                + (1 - wf) * ((s - rmin) / ((rmax - rmin) or 1))
                for c, s in zip(ranked, scores)
            }
        else:
            final = {c: fused.get(c, 0.0) for c in ranked}

        def vis(paths: list[str]) -> list[str]:
            return paths if visible is None else [p for p in paths if p in visible]

        out, seen = [], {}
        for cid in sorted(final, key=lambda c: -final[c]):
            r = rows[cid]
            if r["note"] in seen:
                seen[r["note"]]["more_sections"] += 1
                continue
            item = {
                "file_path": r["note"], "title": r["title"], "heading": r["heading"],
                "snippet": self._snippet(r["body"] or r["text"]),
                "score": round(float(final[cid]), 4), "matched": sorted(engines.get(cid, ())),
                "links": vis(snap.notes.get(r["note"], {}).get("links", []))[:15],
                "backlinks": vis(snap.backlinks.get(r["note"], []))[:15],
                "more_sections": 0,
            }
            seen[r["note"]] = item
            out.append(item)
            if len(out) >= top_k:
                break
        return out

    def _snippet(self, text: str) -> str:
        text = " ".join(text.split())
        n = self.cfg.snippet_chars
        return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"

    # -- note-level operations ----------------------------------------------

    def note_info(self, path: str, access: Access = FULL_ACCESS) -> dict:
        snap = self.snapshot()
        if not self.is_visible(access, path, snap):
            raise NoteNotFound(f"note not found: {path}")
        meta = snap.notes[path]
        visible = self.visible_notes(access, snap)
        keep = (lambda ps: ps) if visible is None else (lambda ps: [p for p in ps if p in visible])
        return {"title": meta.get("title"), "tags": meta.get("tags", []),
                "links": keep(meta.get("links", [])), "backlinks": keep(snap.backlinks.get(path, []))}

    def related(self, path: str, depth: int = 1, access: Access = FULL_ACCESS) -> dict:
        snap = self.snapshot()
        if not self.is_visible(access, path, snap):
            raise NoteNotFound(f"note not found: {path}")
        visible = self.visible_notes(access, snap)

        def ok(p: str) -> bool:
            return p in snap.notes and (visible is None or p in visible)

        def describe(p: str) -> dict:
            m = snap.notes.get(p, {})
            return {"file_path": p, "title": m.get("title"), "tags": m.get("tags", [])}

        links = [p for p in snap.notes[path].get("links", []) if ok(p)]
        back = [p for p in snap.backlinks.get(path, []) if ok(p)]
        out = {"note": describe(path), "links": [describe(p) for p in links],
               "backlinks": [describe(p) for p in back]}
        if depth >= 2:
            first = {path} | set(links) | set(back)
            second: dict[str, int] = {}
            for p in first - {path}:
                for q in snap.notes.get(p, {}).get("links", []) + snap.backlinks.get(p, []):
                    if q not in first and ok(q):
                        second[q] = second.get(q, 0) + 1
            out["second_degree"] = [dict(describe(p), via=n) for p, n in
                                    sorted(second.items(), key=lambda x: -x[1])[:20]]
        return out

    def manifest(self, access: Access = FULL_ACCESS) -> list[str]:
        snap = self._snap
        if snap is None:
            return []
        visible = self.visible_notes(access, snap)
        return sorted(snap.notes if visible is None else visible)

    # -- administration ------------------------------------------------------

    def info(self) -> dict:
        snap = self._snap
        return {
            "active": None if snap is None else {
                "table": self.state.get("table"), "version": self.state.get("version"),
                "commit": self.state.get("commit"), "built_at": self.state.get("built_at"),
                "seconds": self.state.get("seconds"), "changes": self.state.get("changes"),
                "stats": self.state.get("stats"), "embeddings": self.state.get("embeddings"),
            },
            "building": self.building,
            "progress": progress.snapshot(),
            "last_update": self.last_update,
            "config": dict(self.cfg.__dict__),
        }

    def cache_info(self) -> dict:
        if self._emb_table_name not in self._tables():
            return {"embeddings": {"entries": 0}}
        return {"embeddings": {"entries": self.db.open_table(self._emb_table_name).count_rows(),
                               "table": self._emb_table_name}}

    def clear_cache(self) -> list[str]:
        if self.building:
            raise RuntimeError("the cache cannot be cleared while an update is running")
        removed = []
        for t in self._tables():
            if t.startswith("emb_"):
                self.db.drop_table(t)
                removed.append(t)
        log.info("Cache cleared: %s", ", ".join(removed) or "nothing to clear")
        return removed

    def versions(self) -> list[dict]:
        name = self.state.get("table")
        if not name or name not in self._tables():
            return []
        tbl = self.db.open_table(name)
        out = []
        for v in tbl.list_versions():
            vv = v["version"] if isinstance(v, dict) else getattr(v, "version", v)
            ts = v.get("timestamp") if isinstance(v, dict) else getattr(v, "timestamp", None)
            out.append({"version": vv, "timestamp": str(ts), "active": vv == self.state.get("version")})
        return out

    def indices(self) -> list[dict]:
        snap = self.snapshot()
        return [{"name": getattr(i, "name", None), "type": str(getattr(i, "index_type", "")),
                 "columns": list(getattr(i, "columns", []) or [])} for i in snap.table.list_indices()]
