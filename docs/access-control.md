# Access control

Every request to `/mcp` carries a bearer token. The token belongs to an **account**; the account's rules decide which notes exist for that request. One server and one index serve all accounts.

## Modes

| Situation | Behaviour |
|---|---|
| No policy file and no `MCP_AUTH_TOKEN` | **Open**: everybody who reaches the port sees everything. The log warns at start-up. |
| Only `MCP_AUTH_TOKEN` | One shared token with full access (simple setups). |
| Policy file present | Accounts and tokens from the policy. `MCP_AUTH_TOKEN`, if still set, additionally grants full access – remove it once all clients use account tokens. |

The policy lives either in a **file** in the data volume (`/data/access.yaml`, managed with `wdb`) or in a separate **Git repository** (`ACCESS_GIT_URL`, versioned and reviewable). See [policy in Git](#policy-in-git).

## The policy file

```yaml
version: 1

roles:                      # reusable rule sets
  no-hr:
    deny:
      tags: [hr, confidential]
  no-drafts:
    deny:
      fields: {status: [draft]}
  production:
    allow:
      folders: ["20 Work instructions", "40 Glossary"]
      tags: [area/production]

accounts:
  admin:                    # sees everything, may administrate
    allow: all
    permissions: [search, read, admin]
    tokens:
      - {id: 3f9a1c, sha256: "…", created: "2026-11-11T10:00:00Z"}

  staff:                    # everything except …
    allow: all
    roles: [no-hr, no-drafts]

  hr:                       # only …
    description: HR department
    allow:
      tags: [hr]
      folders: ["30 HR"]

  workshop:                 # role plus own exception
    roles: [production, no-drafts]
    deny:
      paths: ["20 Work instructions/WI - CNC*"]

  contractor:
    allow: {folders: ["50 Public"]}
    disabled: true          # blocked, kept for later
```

### Rules

| Rule | Meaning |
|---|---|
| `allow: all` | Every note – minus those matched by a `deny` rule. This is "everything except …". |
| `allow: {…}` | Only notes matching **at least one** criterion. This is "only …". No `allow` at all means nothing is visible. |
| `deny: {…}` | Hides matching notes. **Deny always wins** – over allow rules and over roles. |
| `roles: […]` | Roles are merged into the account: allow criteria add up, deny criteria add up, `allow: all` in any of them means all. |
| `permissions` | `search`, `read` (default for every account) and `admin` (`sync_now`, full `status`). |
| `disabled: true` | The account's tokens are rejected. |

### Criteria

| Criterion | Matches | Example |
|---|---|---|
| `tags` | frontmatter `tags` and inline `#tags`, case-insensitive. Hierarchical: `area/production` also matches `area/production/welding`. | `tags: [hr]` |
| `folders` | the folder and everything below it, case-insensitive. Not a string prefix: `30 HR` does not match `30 HR-Archive`. | `folders: ["30 HR"]` |
| `paths` | glob on the path relative to the vault (`*`, `?`), case-sensitive | `paths: ["Archive/2019*"]` |
| `fields` | any frontmatter field (scalar or list), value case-insensitive | `fields: {status: [draft], owner: [legal]}` |

Several criteria in one block are OR-combined. Combine allow and deny to express AND-NOT: "folder `20 Work instructions`, but not drafts".

### What "hidden" means for each tool

A hidden note is treated as non-existent – there is no "access denied" that would reveal it.

| Tool | Effect |
|---|---|
| `search` | The filter runs *before* the search (vector, full text and linked notes), so results are never thinned out after the fact. |
| `links` / `backlinks` in results | Only visible targets. |
| `read_note`, `related_notes` | Hidden note → "note not found"; neighbourhoods contain only visible notes. |
| `list_notes` | Only visible notes. |
| `status` | Without `admin`: account name and number of visible notes only. |
| `sync_now` | Requires `admin`. |

**Limit:** filtering works per note. If a visible note quotes confidential text, that text is visible. Wikilinks in note content are returned as written, so the *name* of a hidden linked note can appear inside a visible note's text.

## Managing accounts with `wdb`

Run inside the container (`docker exec -it <container> sh`, or the terminal of the resource in Coolify):

```bash
# roles (reusable rule sets)
wdb role add no-hr
wdb deny --role no-hr tag hr
wdb deny --role no-hr tag confidential

# accounts
wdb account add admin --all --admin            # prints the first token once
wdb account add staff --all --role no-hr
wdb account add hr --description "HR department"
wdb account disable contractor
wdb account permissions staff search,read

# rules
wdb allow hr tag hr
wdb allow hr folder "30 HR"
wdb deny staff field status=draft
wdb deny workshop path "20 Work instructions/WI - CNC*"
wdb deny staff tag hr --remove                 # remove a rule
wdb allow staff all                            # allow everything (then add deny rules)

# tokens (only the SHA-256 hash is stored; the token is shown once)
wdb token create staff
wdb token revoke staff 3f9a1c

# check
wdb access list                                # accounts, rules, number of visible notes
wdb access check staff "30 HR/Leave.md"        # visible? which rule decides?
wdb search "vacation request" --as staff       # search with an account's rights
wdb eval --as staff                            # quality measurement with an account's rights
wdb access show                                # print the policy file
```

Every command validates the whole policy before saving and writes the file atomically. The server reloads the file when it changes; an invalid file (e.g. after a manual edit) is ignored, the previous policy stays active and `wdb status` shows the error.

Hand-editing `/data/access.yaml` is fine as well. Generate token hashes with `wdb token create`, or `printf %s "$TOKEN" | sha256sum`.

## Policy in Git

For reviewable, versioned permissions keep `access.yaml` in its own private repository:

```bash
ACCESS_GIT_URL=ssh://git@git.example.com/acme/knowledge-base-access.git
ACCESS_GIT_BRANCH=main
ACCESS_GIT_PATH=access.yaml          # path inside that repository
```

- The repository is pulled before every vault sync (default every 5 minutes) with the same deploy key (`GIT_SSH_PRIVATE_KEY`); add the key to both repositories. `wdb access reload` pulls immediately.
- `wdb` cannot edit the policy in this mode. `wdb token create NAME` prints a new token and the entry to commit:

  ```
  Token 5be21d for 'staff' (shown only once, store it now):

      wdb_Lr1…

  Add this entry under accounts.staff.tokens in the access repository and commit:
        - {id: 5be21d, sha256: "9c1e…", created: "2026-11-11T10:20:00Z"}
  ```
- If the repository cannot be pulled, the last policy stays active. If the file is missing from the repository, all account requests are rejected (fail closed).
- Never commit plain tokens – only their hashes.

## How filtering works (performance)

- While indexing, every section stores its note's access metadata in separate columns: expanded tags, folders and frontmatter `key=value` entries. These columns have label-list indices.
- When the policy is loaded, each account is compiled once into an SQL filter, for example `NOT (array_has_any(acl_tags, ['hr','confidential'])) AND NOT (array_has_any(acl_fields, ['status=draft']))`, and into an equivalent predicate for note-level operations. The tests check that both always select the same notes.
- Searches pass the filter to LanceDB as a **prefilter**. Note-level operations use a per-account set of visible notes, computed once per index version.
- Policy changes therefore need no re-indexing. Measured with 5,600 notes / 80,500 sections: computing an account's visible set 1–20 ms, vector or full-text search with filter 1–8 ms; the total search time (0.1–0.4 s) is dominated by the reranker, with or without filters.

## Typical setups

**One assistant per department** – one account per department, each client or bot configured with its account's token:

| Client | Account |
|---|---|
| Company chat for all employees | `staff` (all except HR and drafts) |
| HR assistant | `hr` |
| Admin / maintenance agent | `admin` |

**Open WebUI with groups** – add the MCP server several times as tool servers, each with a different token, and restrict each one to the matching user group if your Open WebUI version offers access control for tool servers.

Rights belong to the token, not to a person: everybody who can use a client configured with the `hr` token sees what `hr` sees.
