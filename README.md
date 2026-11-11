# wissensdatenbank-mcp

**Hybrid search over a Markdown/Obsidian vault as an MCP server – self-hosted, incremental, with per-account access control.**

*Wissensdatenbank* is German for *knowledge base*. The server pulls a vault from a Git repository (or reads a mounted folder), indexes it and lets AI assistants such as Claude, Hermes, Open WebUI or any other [MCP](https://modelcontextprotocol.io) client search it. Each client gets its own token, and each token only sees the notes its account is allowed to see.

```
 Git repository ──pull every 5 min──▶ ┌────────────────────────────┐ ◀── MCP (Streamable HTTP) ── AI assistants
 (Obsidian vault)                     │ wissensdatenbank-mcp       │      Bearer token per account
                                      │ LanceDB · bge-m3 · reranker│
 access.yaml (file or Git repo) ────▶ │ access filter on every tool│ ◀── wdb CLI (inside the container)
                                      └────────────────────────────┘
```

## Features

- **Hybrid search**: multilingual embeddings ([BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)) + full-text search with stemming and stop words, fused with reciprocal rank fusion, plus notes linked from the best hits (wikilinks), re-scored by a cross-encoder.
- **Obsidian-aware**: frontmatter, tags (also inline `#tags`, hierarchical `a/b`), `[[wikilinks]]` with aliases and headings; results include `links` and `backlinks`; `related_notes` walks the link graph.
- **Access control**: accounts with allow/deny rules on tags, folders, path patterns and frontmatter fields, reusable roles, several tokens per account. Rules are applied as a database prefilter in every search path and to every tool – hidden notes behave as if they did not exist. Policy changes apply immediately, without re-indexing. See [docs/access-control.md](docs/access-control.md).
- **Always available**: updates build a new table version in the background; searches keep running on the previous state until the switch-over. Failed or cancelled updates roll back.
- **Incremental**: only new, changed and deleted notes are processed. Embeddings are cached by text hash and checkpointed every 60 seconds, so a crash or restart never loses computed work.
- **Operations**: one status line per minute while long tasks run (silent when idle), `/health` endpoint, administration CLI `wdb` (status, stats, rebuild, cancel, pause, cache, test search, quality evaluation, account management).
- **Scales**: tested with 5,600 notes / 80,500 sections – rebuild from cache in 10 s, incremental update < 1 s, search 0.1–0.4 s on CPU. A vector index is created automatically above 50,000 sections.

## Quick start

Try it with the bundled example vault (no Git needed):

```bash
git clone https://github.com/bierwirth-it/wissensdatenbank-mcp.git
cd wissensdatenbank-mcp
docker compose -f docker-compose.build.yml -f docker-compose.local.yml up --build
```

The first start downloads the models (~3 GB, cached in a volume) and builds the index. When the log shows `New index state active`, test it:

```bash
docker compose -f docker-compose.build.yml -f docker-compose.local.yml exec wissensdatenbank-mcp wdb search "How do I request vacation?"
```

The MCP endpoint is `http://localhost:8765/mcp`. Without a token or access policy the server is **open** – fine for a local test, not for anything else.

## Deployment

| Option | Guide |
|---|---|
| Docker / Docker Compose with the prebuilt image (`ghcr.io/bierwirth-it/wissensdatenbank-mcp`) | [docs/deployment.md#docker-compose](docs/deployment.md#docker-compose) |
| Coolify, building from this repository | [docs/deployment.md#coolify-from-the-repository](docs/deployment.md#coolify-from-the-repository) |
| Coolify, using the prebuilt image | [docs/deployment.md#coolify-with-the-prebuilt-image](docs/deployment.md#coolify-with-the-prebuilt-image) |

Minimal `.env` for a private vault on GitHub/GitLab/Gitea:

```bash
GIT_REPO_URL=ssh://git@github.com/acme/knowledge-base.git
GIT_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----"
GIT_SSH_KNOWN_HOSTS="github.com ssh-ed25519 AAAA..."
FTS_LANGUAGE=English   # language of the vault for full-text stemming
```

Then create the first account inside the container:

```bash
docker compose exec wissensdatenbank-mcp wdb account add admin --all --admin
```

## MCP tools

| Tool | Purpose |
|---|---|
| `search(query, top_k=8, mode="hybrid", rerank=true)` | Best section per note: `file_path`, `title`, `heading`, `snippet`, `score`, `matched` (search paths), `links`, `backlinks` |
| `read_note(file_path)` | Full note with links and backlinks |
| `related_notes(file_path, depth=1)` | Linked notes, backlinks and (depth 2) notes two steps away |
| `list_notes(folder="")` | Notes visible to the account |
| `status()` | Index state; full details with the `admin` permission |
| `sync_now()` | Pull the repository now (`admin` permission) |

Client configuration (Claude Desktop, Claude Code, Hermes, Open WebUI, generic): [docs/clients.md](docs/clients.md).

## Documentation

- [Deployment](docs/deployment.md) – Docker, Docker Compose, Coolify (repository or image), Git access, reverse proxy, updates
- [Access control](docs/access-control.md) – accounts, roles, rules, tokens, policy in Git, how filtering works
- [Configuration](docs/configuration.md) – all environment variables
- [CLI `wdb`](docs/cli.md) – administration and troubleshooting
- [Search](docs/search.md) – how search works, tuning and measuring quality
- [Clients](docs/clients.md) – connecting MCP clients
- [Development](docs/development.md) – tests, CI, releases

## Requirements

- x86-64 or ARM64 host, 4 GB RAM (2 GB used at idle with the default models), ~6 GB disk for image and models.
- CPU only; no GPU needed. More cores speed up the first indexing (roughly 2–5 minutes per 1,000 notes on 8 cores).

## Licence

[MIT](LICENSE) © André Bierwirth (Bierwirth-IT). Third-party components and models: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) – note the licence remark on the default reranker model.
