# Development

## Layout

| Path | Content |
|---|---|
| `app/server.py` | MCP server, tools, Git sync, authentication, `/admin` endpoints |
| `app/index.py` | LanceDB index: incremental updates, versions, search pipeline |
| `app/access.py` | Access policy: parsing, roles, SQL filter and predicate, editor for `wdb` |
| `app/textproc.py` | Markdown parsing: frontmatter, tags, wikilinks, sections |
| `app/progress.py` | Progress tracking and the periodic status line |
| `app/evaluate.py` | Search quality metrics for `wdb eval` |
| `app/wdb.py` | Administration CLI |
| `tests/test_access.py` | Unit tests for the access policy (no models, runs in seconds) |
| `tests/test_index.py` | Integration tests with the real models in the container |
| `examples/` | Example vault with test questions and an example policy |

## Tests

```bash
pip install ruff pytest pyyaml
ruff check app tests
pytest -q tests/test_access.py

docker build -t wissensdatenbank-mcp:dev .
docker run --rm -v "$PWD/tests:/opt/tests" -v wdb-models:/models \
  --entrypoint python wissensdatenbank-mcp:dev /opt/tests/test_index.py
```

The integration tests cover the first build, search with links and backlinks, German stemming, incremental changes, searching during an update, cancellation and resumption from the cache, rollback after errors and crashes, full rebuilds, related notes, cache pruning and access control (SQL filter and predicate agree; deny/allow rules in every search path, note-level tools, disabled accounts, filter overhead).

## Running locally

```bash
docker compose -f docker-compose.build.yml -f docker-compose.local.yml up --build
```

serves `examples/vault` on `http://localhost:8765/mcp`, rescanned every 30 seconds.

## CI and releases

- `.github/workflows/ci.yml` – lint, unit tests, image build, integration tests and a smoke test with the example vault on every push and pull request.
- `.github/workflows/docker.yml` – builds `linux/amd64` and `linux/arm64` images and pushes them to `ghcr.io/bierwirth-it/wissensdatenbank-mcp`: `main` and `sha-…` on every push to `main`; `X.Y.Z`, `X.Y` and `latest` for tags `vX.Y.Z`.

Release: update `version` in `pyproject.toml` and the changelog, then `git tag vX.Y.Z && git push --tags`.

After the first publication, make the package public once in GitHub (*Packages* → `wissensdatenbank-mcp` → *Package settings* → *Change visibility*), otherwise pulls require a login.
