# Configuration

All settings are environment variables. Values marked *rebuild* are part of the index signature: changing them triggers a full rebuild in the background (from the embedding cache where possible).

## Source

| Variable | Default | Description |
|---|---|---|
| `GIT_REPO_URL` | – | Vault repository (SSH, HTTPS or local path). Required unless `VAULT_DIR` is set. |
| `GIT_BRANCH` | `main` | Branch to follow. |
| `VAULT_DIR` | – | Read a mounted folder instead of a Git repository. |
| `SYNC_INTERVAL` | `300` | Seconds between pulls / folder scans. |
| `GIT_SSH_PRIVATE_KEY` | – | Deploy key for SSH URLs (also used for the access repository). |
| `GIT_SSH_KNOWN_HOSTS` | – | Host keys (`ssh-keyscan` output). Empty: trust on first use. |
| `INDEX_EXCLUDE` | – | Comma-separated folders relative to the vault that are not indexed. Hidden folders are always skipped. |
| `MAX_NOTE_CHARS` | `50000` | `read_note` truncates longer notes. |

## Access control

See [access-control.md](access-control.md).

| Variable | Default | Description |
|---|---|---|
| `MCP_AUTH_TOKEN` | – | Shared token with full access. |
| `ACCESS_FILE` | `/data/access.yaml` | Policy file (file mode). |
| `ACCESS_GIT_URL` | – | Repository containing the policy (Git mode). |
| `ACCESS_GIT_BRANCH` | `main` | Branch of the access repository. |
| `ACCESS_GIT_PATH` | `access.yaml` | Path of the policy inside the access repository. |

## Search

| Variable | Default | Description |
|---|---|---|
| `FTS_LANGUAGE` | `English` | Stemming and stop words of the full-text search (`German`, `French`, `Spanish`, `Italian`, `Dutch`, `Portuguese`, `Swedish`, …). *rebuild* |
| `EMBED_MODEL` | `BAAI/bge-m3` | Sentence-transformers embedding model (multilingual, 8k context). *rebuild* |
| `RERANK_MODEL` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | Cross-encoder for re-scoring. See the licence note in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). |
| `RERANK_MAX_LENGTH` | `256` | Tokens per section given to the reranker. |
| `RERANK_POOL` | `20` | Candidates re-scored per search. |
| `RERANK_FUSION_WEIGHT` | `0.3` | Share of the fused search rank in the final score (rest: reranker). |
| `RRF_K` | `30` | Reciprocal rank fusion constant. |
| `SEMANTIC_WEIGHT` | `1.0` | Weight of the vector search in the fusion. |
| `FULLTEXT_WEIGHT` | `0.5` | Weight of the full-text search in the fusion. |
| `LINK_SEEDS` | `3` | Number of best notes whose wikilink neighbours join the candidates. |
| `LINK_MAX` | `5` | Maximum neighbour notes added. |
| `POOL_PER_NOTE` | `0` | Maximum sections per note in the candidate pool (0 = unlimited). |
| `SNIPPET_CHARS` | `400` | Snippet length in results. |
| `CHUNK_MAX_TOKENS` | `512` | Maximum section size (approx. tokens). *rebuild* |
| `CHUNK_OVERLAP_TOKENS` | `50` | Overlap when a long section is split. *rebuild* |
| `VECTOR_INDEX_MIN_ROWS` | `50000` | Create an approximate vector index (IVF_HNSW_SQ) from this many sections; below it the search is exact. |
| `VECTOR_NPROBES` | `20` | Partitions probed with a vector index. |
| `VECTOR_REFINE_FACTOR` | `5` | Re-rank approximate vector results with exact distances. |
| `VECTOR_EXACT` | `0` | `1` = always search exactly. |
| `VECTOR_EXACT_BELOW_SHARE` | `0.1` | Accounts that see less than this share of all notes search exactly. |

## Runtime

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `8000` | Port inside the container. |
| `MCP_INSTRUCTIONS` | (built in) | Server instructions sent to MCP clients – describe your vault here. |
| `EMBED_BATCH` | `32` | Embedding batch size during indexing. |
| `TORCH_THREADS` | min(CPU cores, 8) | CPU threads for the models. |
| `STATUS_INTERVAL` | `60` | Status line every n seconds while a long task runs (`0` = off). |
| `CHECKPOINT_INTERVAL` | `60` | Save computed embeddings every n seconds during an update. |
| `HF_OFFLINE_WHEN_CACHED` | `1` | Load models without network access once they are cached. |
| `EVAL_FILE` | `<vault>/.wdb/eval.json` | Test questions for `wdb eval`. |
| `LOG_LEVEL` | `INFO` | `DEBUG` for details. |
| `DATA_DIR`, `INDEX_DIR`, `HF_HOME` | `/data`, `/index`, `/models` | Volume paths. |
