"""Measure search quality with test questions.

Test file format (JSON list)::

    {"q": "question", "exp": ["note name", ...]}     expected notes (file name without .md)
    {"q": "question", "exp": [], "negative": true}   the answer is not in the vault

Metrics per run: hits at rank 1, within the top 3/5, misses, MRR (mean
reciprocal rank of the first expected note) and response time.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path


def run_eval(index, path: str, top_k: int = 8, **search_kw) -> dict:
    tests = json.loads(Path(path).read_text(encoding="utf-8"))
    index.search("warm-up", top_k=3, **{k: v for k, v in search_kw.items() if k == "access"})
    rows, secs = [], []
    for t in tests:
        t0 = time.monotonic()
        res = index.search(t["q"], top_k=top_k, **search_kw)
        secs.append(time.monotonic() - t0)
        notes = [Path(r["file_path"]).stem for r in res]
        rank = next((i + 1 for i, n in enumerate(notes) if n in t.get("exp", [])), None)
        rows.append({"q": t["q"], "rank": rank, "negative": bool(t.get("negative")),
                     "top": notes[:3], "top_score": res[0]["score"] if res else None,
                     "seconds": round(secs[-1], 3)})
    pos = [r for r in rows if not r["negative"]]
    n = len(pos) or 1
    return {
        "summary": {
            "questions": len(pos),
            "hit1": sum(r["rank"] == 1 for r in pos),
            "hit3": sum(bool(r["rank"]) and r["rank"] <= 3 for r in pos),
            "hit5": sum(bool(r["rank"]) and r["rank"] <= 5 for r in pos),
            "miss": sum(r["rank"] is None for r in pos),
            "mrr": round(sum(1 / r["rank"] for r in pos if r["rank"]) / n, 3),
            "median_s": round(statistics.median(secs), 3) if secs else None,
            "max_s": round(max(secs), 3) if secs else None,
        },
        "rows": rows,
    }
