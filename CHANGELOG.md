# Changelog

## 0.1.0 – 2026-11-11

First public release.

- Hybrid search (bge-m3 embeddings, full text with stemming, reciprocal rank fusion, wikilink neighbours, cross-encoder reranking) over a Markdown/Obsidian vault on LanceDB.
- Git sync with deploy keys or a mounted folder (`VAULT_DIR`); incremental updates over table versions, searches stay available during updates, rollback on errors.
- Embedding cache with checkpoints, periodic status line, `/health`.
- Access control: accounts, roles, allow/deny rules on tags, folders, paths and frontmatter fields, several tokens per account, policy as file or in a Git repository, applied to every tool as a database prefilter.
- MCP tools `search`, `read_note`, `related_notes`, `list_notes`, `status`, `sync_now`.
- Administration CLI `wdb`.
- Multi-architecture image on GHCR (amd64, arm64).
