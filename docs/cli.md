# CLI `wdb`

`wdb` runs inside the container and talks to the running service on `127.0.0.1` (the `/admin` endpoints are not reachable from outside). Use it via

```bash
docker exec -it <container> wdb status
docker compose exec wissensdatenbank-mcp wdb status
```

or the terminal of the resource in Coolify. Add `--json` before the command for machine-readable output (`wdb --json status`).

## Service

| Command | Purpose |
|---|---|
| `wdb status` | Repository, last sync, access mode, active index state, running update with progress |
| `wdb stats` | Notes per folder, sections, wikilinks, database indices, cache size, disk usage |
| `wdb versions` | Versions of the index table (the active one is marked) |
| `wdb sync` | Pull now; updates if anything changed |
| `wdb rebuild [--no-cache]` | Full rebuild in a new table; the current state stays searchable until the switch-over. `--no-cache` recomputes all embeddings. |
| `wdb cancel` | Cancel the running update. Computed embeddings are kept, the current state stays active. |
| `wdb pause` / `wdb resume` | Pause / resume the automatic sync (e.g. during maintenance of the vault) |
| `wdb cache prune` | Shrink the embedding cache to the current sections (also runs automatically when the cache is more than twice as large) |
| `wdb cache clear [-y]` | Delete the embedding cache |
| `wdb search "query" [-k 5] [--mode hybrid\|semantic\|fulltext] [--as ACCOUNT]` | Test search, optionally with an account's rights |
| `wdb eval [--file F] [-k 8] [--as ACCOUNT] [-v]` | Measure search quality with test questions, see [search](search.md#measuring-quality) |

Example:

```
$ wdb status
wissensdatenbank-mcp
  Repository:  directory /vault
  Commit:      dir-f1949e   last sync: 2026-11-11T15:06:48+00:00   OK
  Auto sync:   every 10 s
  Models:      offline from cache
  Access:      4 accounts (/data/access.yaml)
Active state: table chunks_20261111150628, version 7
  Commit dir-f1949e, updated 2026-11-11T15:06:28+00:00 in 0:03
  5 notes, 7 sections, 5 wikilinks
  Last update: full rebuild; embeddings 7 computed, 0 from cache
No task running.
```

## Access policy

Full reference with examples: [access-control.md](access-control.md#managing-accounts-with-wdb).

| Command | Purpose |
|---|---|
| `wdb access list` | Accounts with rules, roles, token ids and number of visible notes |
| `wdb access check ACCOUNT NOTE` | Is the note visible for the account, and which rule decides? |
| `wdb access show` | Print the policy file |
| `wdb access reload` | Reload now (pulls the access repository in Git mode) |
| `wdb account add NAME [--all] [--role R] [--admin] [--description D] [--no-token]` | Create an account; prints the first token |
| `wdb account remove\|disable\|enable NAME` | Remove or block / unblock an account |
| `wdb account permissions NAME search,read[,admin]` | Set permissions |
| `wdb role add NAME [--all]` / `wdb role remove NAME` | Manage roles |
| `wdb allow NAME (tag\|folder\|path\|field) VALUE [--remove]` | Add / remove an allow rule (`--role R` instead of `NAME` for a role) |
| `wdb allow NAME all [--remove]` | Allow all notes |
| `wdb deny NAME (tag\|folder\|path\|field) VALUE [--remove]` | Add / remove a deny rule |
| `wdb assign NAME ROLE [--remove]` | Assign / unassign a role |
| `wdb token create NAME` | New token (shown once; only its hash is stored) |
| `wdb token revoke NAME ID` | Revoke a token |

Field rules take `key=value`: `wdb deny staff field status=draft`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Clients get 401 | Token correct and sent as `Authorization: Bearer …`? Account disabled? `wdb access list`; the log shows the hash prefix of rejected tokens. |
| Search returns "index is being built" | First indexing still running: `wdb status` shows the phase and remaining time. |
| `last sync: FAILED` | `wdb status` shows the Git error; typical causes: deploy key not added to the repository, wrong host key, wrong branch. |
| Note missing from results | `wdb access check ACCOUNT NOTE`; folder excluded by `INDEX_EXCLUDE` or hidden (starts with `.`)? `wdb search "exact title" --mode fulltext`. |
| Policy change has no effect | `wdb status` shows `POLICY ERROR` if the file is invalid; in Git mode run `wdb access reload`. |
| Update takes long | The status line in the log and `wdb status` show phase, progress and remaining time; `wdb cancel` stops it without losing work. |
