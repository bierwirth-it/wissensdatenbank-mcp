# Search

## How a query is answered

1. **Candidates.** The query is embedded with `EMBED_MODEL` and searched in two ways, both restricted by the account's access filter:
   - vector search over all sections (cosine similarity),
   - full-text search with stemming and stop words in `FTS_LANGUAGE` (BM25).
2. **Fusion.** Both result lists are merged with reciprocal rank fusion (`RRF_K`, weights `SEMANTIC_WEIGHT` / `FULLTEXT_WEIGHT`). Sections with identical text (templates, copies) count once.
3. **Linked notes.** The wikilink neighbours (links and backlinks) of the best `LINK_SEEDS` notes contribute their most similar section, up to `LINK_MAX` notes. This finds the work instruction behind a process description even if the query does not mention it.
4. **Re-ranking.** A cross-encoder scores the top `RERANK_POOL` candidates against the query; the final score blends the reranker (70 %) with the fused rank (`RERANK_FUSION_WEIGHT` = 30 %).
5. **Result.** One result per note (its best section) with snippet, score, the search paths that found it, and the note's visible links and backlinks.

Sections are the parts of a note between headings, each prefixed with its heading path (`Note title > Chapter > Section`) so that context is part of the text. Long sections are split at sentence boundaries with overlap.

## Choosing models

| Model | Role | Notes |
|---|---|---|
| `BAAI/bge-m3` (default) | embeddings | Multilingual (100+ languages), MIT. ~2.2 GB. |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` (default) | reranker | Multilingual, fast (~0.2 s per query on 8 cores). Trained on MS MARCO, whose licence restricts use to non-commercial research – check before commercial use. |
| `BAAI/bge-reranker-v2-m3` | reranker | Apache-2.0, better quality (+5 % MRR in our tests), ~4× slower on CPU. |

Any sentence-transformers compatible model can be used. Changing `EMBED_MODEL` rebuilds the index (embeddings are recomputed); changing the reranker takes effect after a restart.

## Measuring quality

`wdb eval` runs a set of test questions against the live index and reports how often the expected note is ranked first, in the top 3 / top 5, the mean reciprocal rank (MRR) and response times. Use it before and after changing models or settings, and with `--as ACCOUNT` to see what an account can find.

Put the questions into the vault as `.wdb/eval.json` (hidden folders are not indexed) or point `EVAL_FILE` / `--file` elsewhere:

```json
[
  {"q": "How do I request vacation?", "exp": ["Vacation request"]},
  {"q": "What do I do if a delivery arrives damaged?", "exp": ["Goods receipt", "Visual inspection"]},
  {"q": "What is the canteen subsidy?", "exp": [], "negative": true}
]
```

`exp` lists acceptable note file names without `.md`; `negative` questions have no answer in the vault and are reported separately (their top score helps to choose a "nothing found" threshold in a client).

```
$ wdb eval -v
32 questions: rank 1: 25, top 3: 30, top 5: 31, missed: 1, MRR 0.861, time median 0.39 s, max 0.62 s
  [1] How do I request vacation?  ->  Vacation request, Absence, Sick leave
  ...
```

Reference values from our German test vault (112 notes, 32 questions, 8 CPU cores) with the default settings: MRR 0.861, 25 of 32 questions answered with the expected note at rank 1, median 0.39 s per query. The linked-notes step raised the MRR by about 0.03, German stemming by about 0.06 compared with plain tokenisation.
