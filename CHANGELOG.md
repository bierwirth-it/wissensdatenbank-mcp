# Changelog

## 0.1.4 – 2026-11-17

- Fix: after the first index build the server switched to offline model loading although the reranker had not been downloaded yet (it is only used by searches). After a restart every search failed with `LocalEntryNotFoundError`. Now all models are loaded (and downloaded if missing) right after the first build before offline mode is enabled, and every start checks that all models are actually on disk – missing models are always fetched online. Installations in the broken state repair themselves on the next start.
- `wdb search` reports errors as a message instead of an HTTP 500 traceback.
- CI restarts the smoke-test container and searches offline.

## 0.1.3 – 2026-11-17

- Fix: version cleanup during a long update (longer than 2 minutes) could delete the active index version. Searches on the previous state and the rollback after a crash in the middle of an update then failed ("Version … no longer exists"). The active version is now protected by a LanceDB tag; new test covers it.
- Removed the `cleanup_old_versions` call that required the optional `pylance` package and logged a traceback at DEBUG level after every update; `optimize()` already removes old versions.
- New setting `VERSION_RETENTION_S` (default 120).

## 0.1.2 – 2026-11-17

- Embedding progress advances every batch (32 sections) instead of every 256: sections are sorted by length once, so small batches are as fast as large calls (measured 186 s vs. 206 s for 1,610 sections). Checkpoints are written more often as well. Early remaining-time estimates are high because the longest sections are computed first.
- `/livez` health-check requests are no longer written to the access log.

## 0.1.1 – 2026-11-11

- Fix: the Docker health check used the readiness endpoint `/health`, so reverse proxies (e.g. Traefik in Coolify) did not route to the container until the first index was built. The health check now uses the new liveness endpoint `/livez`; `/health` keeps its readiness meaning for monitoring.

## 0.1.0 – 2026-11-11

First public release.

- Hybrid search (bge-m3 embeddings, full text with stemming, reciprocal rank fusion, wikilink neighbours, cross-encoder reranking) over a Markdown/Obsidian vault on LanceDB.
- Git sync with deploy keys or a mounted folder (`VAULT_DIR`); incremental updates over table versions, searches stay available during updates, rollback on errors.
- Embedding cache with checkpoints, periodic status line, `/health`.
- Access control: accounts, roles, allow/deny rules on tags, folders, paths and frontmatter fields, several tokens per account, policy as file or in a Git repository, applied to every tool as a database prefilter.
- MCP tools `search`, `read_note`, `related_notes`, `list_notes`, `status`, `sync_now`.
- Administration CLI `wdb`.
- Multi-architecture image on GHCR (amd64, arm64).
