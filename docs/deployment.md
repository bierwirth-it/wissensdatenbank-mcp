# Deployment

The server is a single container with three volumes:

| Volume | Content | Size |
|---|---|---|
| `/data` | clone of the vault repository, `access.yaml` (file mode), clone of the access repository (Git mode) | size of the vault |
| `/index` | LanceDB index and embedding cache | ~2× the vault text plus 4 KB per section |
| `/models` | Hugging Face model cache | ~3 GB with the default models |

All three should be persistent. Losing `/index` means a full rebuild (embeddings recomputed); losing `/models` means downloading the models again; losing `/data` in file mode loses the access policy – back it up, or keep the policy in Git.

The service listens on port **8000**: `/mcp` (MCP Streamable HTTP, token required unless the server is open), `/livez` (liveness, 200 while the process runs – used by the image's Docker health check) and `/health` (readiness, 200 once an index is active, 503 before; use it for monitoring). No token is needed for `/livez` and `/health`. The `/admin` endpoints only answer requests from inside the container.

While the first index is built the container is already healthy and routable; MCP searches answer "index is being built" until it is ready.

On the first start the models are downloaded and the whole vault is indexed – roughly 2–5 minutes per 1,000 notes on 8 CPU cores, plus the download. Progress appears in the log once a minute. Afterwards the models load offline and updates take seconds.

## Git access to the vault

| Repository | `GIT_REPO_URL` | Credentials |
|---|---|---|
| Private, SSH (recommended) | `ssh://git@github.com/acme/vault.git` or `git@github.com:acme/vault.git` | `GIT_SSH_PRIVATE_KEY` = read-only deploy key |
| Private, HTTPS | `https://x-access-token:<TOKEN>@github.com/acme/vault.git` | token in the URL (redacted in logs and `status`) |
| Public | `https://github.com/acme/vault.git` | none |
| No Git | leave empty, set `VAULT_DIR` to a mounted folder | none |

Create a deploy key without a passphrase and add the public part as a **read-only** deploy key to the vault repository (and to the access repository if you use one):

```bash
ssh-keygen -t ed25519 -N "" -C wissensdatenbank-mcp -f deploy_key
ssh-keyscan github.com            # value for GIT_SSH_KNOWN_HOSTS (add -p PORT for other ports)
```

`GIT_SSH_PRIVATE_KEY` may be multi-line or a single line (line breaks lost in a UI field, or written as `\n`) – the server restores the key format. Without `GIT_SSH_KNOWN_HOSTS` the host key is trusted on first use.

Hidden folders (`.obsidian`, `.trash`, `.git`, …) are never indexed. Skip more folders with `INDEX_EXCLUDE="Templates,Archive"`.

## Docker Compose

```bash
mkdir wissensdatenbank-mcp && cd wissensdatenbank-mcp
curl -fsSLO https://raw.githubusercontent.com/bierwirth-it/wissensdatenbank-mcp/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/bierwirth-it/wissensdatenbank-mcp/main/.env.example -o .env
# edit .env: GIT_REPO_URL, GIT_SSH_PRIVATE_KEY, GIT_SSH_KNOWN_HOSTS, FTS_LANGUAGE
docker compose up -d
docker compose logs -f                                     # wait for "New index state active"
docker compose exec wissensdatenbank-mcp wdb account add admin --all --admin
```

`docker-compose.yml` publishes port 8000 on the host (`WDB_PORT` changes it). Put a TLS-terminating reverse proxy in front of it for anything beyond a trusted network – see [Reverse proxy](#reverse-proxy-and-tls).

Pin a version instead of `latest` with `WDB_VERSION=0.1.0` in `.env`.

### Plain `docker run`

```bash
docker run -d --name wissensdatenbank-mcp --restart unless-stopped \
  -p 8000:8000 --env-file .env \
  -v wdb-data:/data -v wdb-index:/index -v wdb-models:/models \
  ghcr.io/bierwirth-it/wissensdatenbank-mcp:latest
```

### Build from source

```bash
git clone https://github.com/bierwirth-it/wissensdatenbank-mcp.git && cd wissensdatenbank-mcp
cp .env.example .env    # edit
docker compose -f docker-compose.build.yml up -d --build
```

`docker-compose.build.yml` does not publish a port (it is meant for proxies such as Coolify). For direct access add `ports: ["8000:8000"]` or combine it with `docker-compose.local.yml`.

### A local folder instead of Git

```bash
VAULT_DIR=~/Obsidian/MyVault docker compose -f docker-compose.build.yml -f docker-compose.local.yml up -d --build
```

The folder is mounted read-only and rescanned every `SYNC_INTERVAL` seconds (30 in this file); changes are detected from file sizes and modification times.

## Coolify from the repository

Coolify builds the image from this repository on its own server – no registry needed.

1. **New resource** → *Public Repository* → `https://github.com/bierwirth-it/wissensdatenbank-mcp` (for a fork in a private repository use the GitHub App or deploy-key option instead), branch `main`.
2. **Build pack:** *Docker Compose*, **Docker Compose location:** `/docker-compose.build.yml`.
3. **Domain:** on the service `wissensdatenbank-mcp` set e.g. `https://kb-mcp.example.com:8000` – the `:8000` tells Coolify's proxy which container port to use.
4. **Environment variables:** at least `GIT_REPO_URL`, `GIT_SSH_PRIVATE_KEY` (mark it *multiline* and *secret*/locked), `GIT_SSH_KNOWN_HOSTS`, `FTS_LANGUAGE`. Variables that the compose file declares appear automatically; others (see [configuration](configuration.md)) can be added in the UI.
5. **Deploy.** The volumes `data`, `index` and `models` are created as persistent Docker volumes and survive redeploys.
6. When the log shows `New index state active`, open the **Terminal** of the resource (container `wissensdatenbank-mcp`) and run `wdb account add admin --all --admin`.

Updates: *Redeploy* builds the current `main`. Enable *Auto Deploy* (GitHub App / webhook) to follow the repository, or set the branch to a release tag for controlled updates.

## Coolify with the prebuilt image

Faster deployments and no build on your server; updates are explicit.

1. **New resource** → *Docker Image* → `ghcr.io/bierwirth-it/wissensdatenbank-mcp:latest` (or a version tag such as `0.1.0`).
2. **Ports exposes:** `8000`. **Domain:** e.g. `https://kb-mcp.example.com`.
3. **Persistent storage** – add three volume mounts (*Storages* → *Add* → *Volume*):

   | Name | Destination path |
   |---|---|
   | `wdb-data` | `/data` |
   | `wdb-index` | `/index` |
   | `wdb-models` | `/models` |

   Without them the index and models are lost on every redeploy.
4. **Environment variables:** as above (`GIT_REPO_URL`, `GIT_SSH_PRIVATE_KEY` multiline, `GIT_SSH_KNOWN_HOSTS`, `FTS_LANGUAGE`, …).
5. **Health check:** the image has its own Docker health check on `/livez`. If you enable Coolify's health check instead, use path `/livez` and port `8000` – not `/health`, which returns 503 during the first indexing and would keep the proxy from routing to the container.
6. **Deploy**, then create the first account in the terminal as above.

Alternatively create a *Docker Compose (empty)* resource and paste [`docker-compose.yml`](../docker-compose.yml) (remove the `ports:` section; Coolify's proxy routes the domain).

Updates: change the tag and redeploy, or redeploy `latest` to pull the newest image.

## Reverse proxy and TLS

MCP uses ordinary HTTP POST requests plus server-sent events. Any reverse proxy works if it

- does not buffer responses (`proxy_buffering off;` in nginx),
- allows long requests (`proxy_read_timeout 300s;` – the first search after a start loads the models),
- passes the `Authorization` header.

Tokens travel in every request – use HTTPS outside trusted networks. Some MCP clients (Node.js based ones in particular) reject self-signed certificates; use a certificate from a trusted CA or plain HTTP inside a private network.

## Upgrades

- Index format or relevant settings changed (embedding model, chunking, full-text language): the server notices the mismatch and rebuilds in the background. Embeddings come from the cache when the model is unchanged, so this takes seconds to minutes. Searches return "index is being built" until the first state is active.
- `wdb status` shows the active state, `wdb eval` measures search quality before and after an upgrade (see [search](search.md#measuring-quality)).

## Monitoring

- `GET /health` → `{"ok": true, "commit": "…", "index_commit": "…", "building": false, "last_sync_ok": true}` – readiness, suitable for Uptime Kuma & co. Alert on non-200 or `last_sync_ok: false`.
- `GET /livez` → liveness only; use it for container health checks.
- The log shows every update with its changed files, a status line per minute during long tasks and checkpoints of the embedding cache. Idle periods are silent.
