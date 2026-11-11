#!/usr/bin/env python3
"""wdb - administration CLI for wissensdatenbank-mcp (run inside the container).

Talks to the running service via http://127.0.0.1:<MCP_PORT>/admin/.
Access policy commands edit the policy file directly (file mode); the
service reloads it automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('MCP_PORT', '8000')}/admin"


def call(method: str, path: str, params: dict | None = None, body: dict | None = None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get("error", exc.reason)
        except Exception:  # noqa: BLE001
            msg = exc.reason
        sys.exit(f"Error ({exc.code}): {msg}")
    except urllib.error.URLError as exc:
        sys.exit(f"Service not reachable ({exc.reason}). Is the server running or still starting?")


def out_json(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fmt_bytes(size: int | None) -> str:
    if size is None:
        return "-"
    n = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def fmt_s(s: int | float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def show_progress(p: dict | None) -> None:
    if not p:
        return
    print(f"  Task:      {p['task']}")
    if p.get("phase"):
        ph = p["phase"]
        if p.get("total"):
            ph += f" {p['done']}/{p['total']} ({p['percent']} %)"
        print(f"  Phase:     {ph}")
    rest = f", phase remaining ~{fmt_s(p['phase_remaining_s'])}" if "phase_remaining_s" in p else ""
    print(f"  Elapsed:   phase {fmt_s(p['phase_elapsed_s'])}, total {fmt_s(p['elapsed_s'])}{rest}")


# ---------------------------------------------------------------------------
# Service commands
# ---------------------------------------------------------------------------

def cmd_status(a) -> None:
    st = call("GET", "/status")
    if a.json:
        return out_json(st)
    act = st.get("active")
    acc = st.get("access") or {}
    ok = "OK" if st.get("last_sync_ok") else ("FAILED" if st.get("last_sync_ok") is False else "")
    print("wissensdatenbank-mcp")
    print(f"  Repository:  {st['repo']}" + (f" ({st['branch']})" if st.get("branch") else ""))
    print(f"  Commit:      {(st.get('commit') or '-')[:10]}   last sync: {st.get('last_sync') or '-'}   {ok}")
    if st.get("last_error"):
        print(f"  Last error:  {st['last_error']}")
    print(f"  Auto sync:   every {st['sync_interval_s']} s{'  (PAUSED)' if st.get('sync_paused') else ''}")
    print(f"  Models:      {'offline from cache' if st.get('offline_models') else 'with network access'}")
    if acc.get("open"):
        print("  Access:      OPEN - no policy and no MCP_AUTH_TOKEN")
    else:
        src = f"git {acc.get('repo')}" if acc.get("mode") == "git" else acc.get("file")
        print(f"  Access:      {acc.get('accounts', 0)} accounts ({src})"
              f"{', MCP_AUTH_TOKEN = full access' if acc.get('legacy_token') else ''}")
    if acc.get("policy_error"):
        print(f"  POLICY ERROR (previous policy still active): {acc['policy_error']}")
    if act:
        s = act.get("stats") or {}
        ch = act.get("changes") or {}
        e = act.get("embeddings") or {}
        print(f"Active state: table {act['table']}, version {act['version']}")
        print(f"  Commit {str(act.get('commit'))[:10]}, updated {act.get('built_at')} in {fmt_s(act.get('seconds'))}")
        print(f"  {s.get('notes')} notes, {s.get('chunks')} sections, {s.get('links')} wikilinks")
        kind = "full rebuild" if ch.get("full") else \
            f"{ch.get('new', 0)} new, {ch.get('changed', 0)} changed, {ch.get('deleted', 0)} deleted"
        print(f"  Last update: {kind}; embeddings {e.get('computed', 0)} computed, {e.get('reused', 0)} from cache")
    else:
        print("Active state: none (first build not finished yet)")
    lu = st.get("last_update") or {}
    if lu and not lu.get("ok"):
        print(f"  Last update failed: {lu.get('error')}")
    b = st.get("building")
    if b:
        kind = "full rebuild" if b.get("full") else f"{b['new']} new, {b['changed']} changed, {b['deleted']} deleted"
        print(f"Update running since {b['since']}: {kind}")
    show_progress(st.get("progress"))
    if not b and not st.get("progress"):
        print("No task running.")


def cmd_stats(a) -> None:
    st = call("GET", "/stats")
    if a.json:
        return out_json(st)
    s = (st.get("index") or {}).get("stats") or {}
    print("Index")
    print(f"  Notes in index:  {s.get('notes', '-')} ({st['repo_notes_total']} Markdown files in the repository)")
    print(f"  Sections:        {s.get('chunks', '-')}")
    print(f"  Wikilinks:       {s.get('links', '-')}")
    for i in st.get("indices", []):
        print(f"  Index {i['name']}: {i['type']} on {', '.join(i['columns'])}")
    print("Notes per folder")
    for k, v in st["notes_per_folder"].items():
        print(f"  {v:5d}  {k}")
    print("Cache")
    for k, v in st["cache"].items():
        print(f"  {k:12s} {v.get('entries', 0):>8} entries")
    print("Disk")
    for k, v in st["disk_bytes"].items():
        print(f"  {k:8s} {fmt_bytes(v)}")


def cmd_versions(a) -> None:
    vers = call("GET", "/versions")
    if a.json:
        return out_json(vers)
    if not vers:
        print("No index versions.")
    for v in vers:
        print(f"  version {v['version']:>5}  {v['timestamp']}  {'active' if v['active'] else ''}")


def cmd_sync(a) -> None:
    r = call("POST", "/sync")
    print("Sync started. Follow it with 'wdb status' or in the log." if r.get("started")
          else f"Not started: {r.get('reason')}")


def cmd_rebuild(a) -> None:
    r = call("POST", "/rebuild", params={"no_cache": "1"} if a.no_cache else None)
    if r.get("started"):
        print("Full rebuild started" + (" without cache" if a.no_cache else " (using the cache)")
              + ". Searches keep using the current state until the switch-over.")
    else:
        print(f"Not started: {r.get('reason')}")


def cmd_cancel(a) -> None:
    r = call("POST", "/cancel")
    if not r.get("cancelled"):
        print(r.get("reason"))
        return
    print("Cancellation requested. Work done so far is kept in the cache, the current state stays active.")
    if not r.get("sync_paused"):
        print("Note: on a new commit the automatic sync starts the update again (mostly from the cache). "
              "Stop it with 'wdb pause'.")


def cmd_pause(a) -> None:
    call("POST", "/pause")
    print("Automatic sync paused. Resume with 'wdb resume'.")


def cmd_resume(a) -> None:
    call("POST", "/resume")
    print("Automatic sync resumed.")


def cmd_cache(a) -> None:
    if a.action == "prune":
        r = call("POST", "/cache/prune")
        print(f"Cache pruned: {r['before']} -> {r['after']} entries")
        return
    if not a.yes:
        ans = input("Really clear the embedding cache? The next full rebuild recomputes everything. [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    r = call("POST", "/cache/clear")
    print("Removed: " + (", ".join(r["removed"]) or "nothing"))


def cmd_eval(a) -> None:
    r = call("POST", "/eval", body={"file": a.file, "top_k": a.k, "account": a.account})
    if a.json:
        return out_json(r)
    s = r["summary"]
    print(f"{s['questions']} questions: rank 1: {s['hit1']}, top 3: {s['hit3']}, top 5: {s['hit5']}, "
          f"missed: {s['miss']}, MRR {s['mrr']}, time median {s['median_s']} s, max {s['max_s']} s")
    if a.verbose:
        for row in r["rows"]:
            mark = "neg" if row["negative"] else (row["rank"] or "-")
            print(f"  [{mark}] {row['q']}  ->  {', '.join(row['top'])}")


def cmd_search(a) -> None:
    res = call("POST", "/search", body={"query": a.query, "top_k": a.k, "mode": a.mode, "account": a.account})
    if a.json:
        return out_json(res)
    if not res:
        print("No results.")
    for i, r in enumerate(res, 1):
        print(f"{i}. {r['file_path']}  ({r['score']}, {', '.join(r['matched'])})")
        print(f"   {r['heading']}")
        snippet = " ".join(r["snippet"].split())
        print(f"   {snippet[:200]}{'…' if len(snippet) > 200 else ''}")
        if r.get("links") or r.get("backlinks"):
            print(f"   links: {len(r.get('links', []))}, backlinks: {len(r.get('backlinks', []))}")


# ---------------------------------------------------------------------------
# Access policy
# ---------------------------------------------------------------------------

def _editor():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from access import PolicyEditor, access_settings
    st = access_settings()
    return PolicyEditor(st["path"]), st


def _edit(a, fn) -> None:
    """Run an edit on the policy file and save it (file mode only)."""
    from_git = bool(os.environ.get("ACCESS_GIT_URL", "").strip())
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from access import PolicyError
    if from_git:
        sys.exit("The access policy is managed in a Git repository (ACCESS_GIT_URL). Edit access.yaml there "
                 "and commit; the service picks it up with the next sync ('wdb access reload' for now).")
    ed, st = _editor()
    try:
        msg = fn(ed)
        ed.save()
    except PolicyError as exc:
        sys.exit(f"Error: {exc}")
    if msg:
        print(msg)
    print(f"Saved {st['path']}; the service reloads it automatically.")


def cmd_access_list(a) -> None:
    r = call("GET", "/access")
    if a.json:
        return out_json(r)
    src = f"git {r.get('repo')}" if r.get("mode") == "git" else r.get("file")
    print(f"Access policy: {src}{'' if r.get('policy_loaded') else ' (not present)'}")
    if r.get("policy_error"):
        print(f"  POLICY ERROR (previous policy still active): {r['policy_error']}")
    if r.get("legacy_token"):
        print("  MCP_AUTH_TOKEN is set and grants full access")
    if r.get("open"):
        print("  ACCESS IS OPEN: no policy and no MCP_AUTH_TOKEN")
    if r.get("roles"):
        print(f"  Roles: {', '.join(r['roles'])}")
    total = r.get("total_notes")
    for acc in r.get("account_list", []):
        vis = acc.get("visible_notes")
        share = f"{vis}/{total} notes" if vis is not None else "index not ready"
        state = "  DISABLED" if acc.get("disabled") else ""
        print(f"\n{acc['name']}{state}  ({share}; permissions: {', '.join(acc['permissions'])})")
        if acc.get("description"):
            print(f"  {acc['description']}")
        allow = acc["allow"]
        shown = "all" if allow == "all" else (json.dumps(allow, ensure_ascii=False) if allow else "nothing")
        print(f"  allow: {shown}")
        if acc.get("deny"):
            print(f"  deny:  {json.dumps(acc['deny'], ensure_ascii=False)}")
        if acc.get("roles"):
            print(f"  roles: {', '.join(acc['roles'])}")
        toks = acc.get("tokens") or []
        print(f"  tokens: {', '.join(t['id'] + ' (' + str(t.get('created')) + ')' for t in toks) or 'none'}")


def cmd_access_check(a) -> None:
    r = call("POST", "/access/check", body={"account": a.account, "note": a.note})
    if a.json:
        return out_json(r)
    print(f"{r['note']}: {'VISIBLE' if r['visible'] else 'HIDDEN'} for {a.account} - {r['reason']}")
    if r.get("indexed"):
        print(f"  tags:   {', '.join(r.get('tags') or []) or '-'}")
        print(f"  fields: {', '.join(r.get('fields') or []) or '-'}")


def cmd_access_reload(a) -> None:
    r = call("POST", "/access/reload")
    print(f"Policy reloaded: {r.get('accounts')} accounts"
          + (f"; ERROR: {r['policy_error']}" if r.get("policy_error") else ""))


def cmd_access_show(a) -> None:
    ed, st = _editor()
    import yaml
    print(f"# {st['path']}")
    print(yaml.safe_dump(ed.raw, sort_keys=False, allow_unicode=True), end="")


def cmd_account_add(a) -> None:
    def fn(ed):
        ed.add("account", a.name, allow_all=a.all, description=a.description)
        for r in a.role or []:
            ed.set_roles(a.name, r)
        if a.admin:
            ed.set_permissions(a.name, ["search", "read", "admin"])
        if a.no_token:
            return f"Account '{a.name}' created (no token yet: 'wdb token create {a.name}')."
        tid, token = ed.create_token(a.name)
        return _token_msg(a.name, tid, token)
    _edit(a, fn)


def _token_msg(name: str, tid: str, token: str) -> str:
    return (f"Token {tid} for '{name}' (shown only once, store it now):\n\n    {token}\n\n"
            f"Clients send it as header 'Authorization: Bearer <token>'.")


def cmd_account_remove(a) -> None:
    _edit(a, lambda ed: ed.remove("account", a.name) or f"Account '{a.name}' removed.")


def cmd_account_enable(a, disabled: bool) -> None:
    _edit(a, lambda ed: ed.set_disabled(a.name, disabled)
          or f"Account '{a.name}' {'disabled' if disabled else 'enabled'}.")


def cmd_role_add(a) -> None:
    _edit(a, lambda ed: ed.add("role", a.name, allow_all=a.all, description=a.description)
          or f"Role '{a.name}' created.")


def cmd_role_remove(a) -> None:
    _edit(a, lambda ed: ed.remove("role", a.name) or f"Role '{a.name}' removed.")


def cmd_rule(a, mode: str) -> None:
    kind = "role" if a.role_target else "account"
    name = a.role_target or a.name

    def fn(ed):
        if a.value == "all" and a.criterion is None:
            if mode != "allow":
                raise SystemExit("Use 'deny' with a criterion, e.g. 'deny NAME tag hr'.")
            ed.set_allow_all(kind, name, not a.remove)
            return f"{kind} '{name}': allow all {'removed' if a.remove else 'set'}."
        if a.criterion is None:
            raise SystemExit("Give a criterion and value, e.g. 'tag hr', 'folder \"30 HR\"', "
                             "'path \"Archive/*\"' or 'field status=draft'.")
        ed.rule(kind, name, mode, a.criterion, a.value, remove=a.remove)
        return f"{kind} '{name}': {mode} {a.criterion} '{a.value}' {'removed' if a.remove else 'added'}."
    _edit(a, fn)


def cmd_assign(a) -> None:
    _edit(a, lambda ed: ed.set_roles(a.name, a.role, remove=a.remove)
          or f"Account '{a.name}': role '{a.role}' {'removed' if a.remove else 'added'}.")


def cmd_permissions(a) -> None:
    perms = [p.strip() for p in a.permissions.split(",") if p.strip()]
    _edit(a, lambda ed: ed.set_permissions(a.name, perms)
          or f"Account '{a.name}': permissions {', '.join(perms)}.")


def cmd_token_create(a) -> None:
    if os.environ.get("ACCESS_GIT_URL", "").strip():
        # git mode: print a token entry to commit to the access repository
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import secrets
        from datetime import datetime, timezone

        from access import token_hash
        token = "wdb_" + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(3), "sha256": token_hash(token),
                 "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        print(_token_msg(a.name, entry["id"], token))
        print(f"\nAdd this entry under accounts.{a.name}.tokens in the access repository and commit:\n")
        print(f"      - {{id: {entry['id']}, sha256: \"{entry['sha256']}\", created: \"{entry['created']}\"}}")
        return

    def fn(ed):
        tid, token = ed.create_token(a.name)
        return _token_msg(a.name, tid, token)
    _edit(a, fn)


def cmd_token_revoke(a) -> None:
    _edit(a, lambda ed: ed.revoke_token(a.name, a.id) or f"Token {a.id} of '{a.name}' revoked.")


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="wdb", description="Administration of wissensdatenbank-mcp")
    ap.add_argument("--json", action="store_true", help="output as JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="active state, running update, last sync").set_defaults(fn=cmd_status)
    sub.add_parser("stats", help="notes, sections, cache, disk usage").set_defaults(fn=cmd_stats)
    sub.add_parser("versions", help="versions of the index table").set_defaults(fn=cmd_versions)
    sub.add_parser("sync", help="pull the repository now, update on changes").set_defaults(fn=cmd_sync)
    p = sub.add_parser("rebuild", help="full rebuild (the current state stays active meanwhile)")
    p.add_argument("--no-cache", action="store_true", help="recompute embeddings")
    p.set_defaults(fn=cmd_rebuild)
    sub.add_parser("cancel", help="cancel the running update").set_defaults(fn=cmd_cancel)
    sub.add_parser("pause", help="pause the automatic sync").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume the automatic sync").set_defaults(fn=cmd_resume)
    p = sub.add_parser("cache", help="manage the embedding cache")
    p.add_argument("action", choices=["clear", "prune"])
    p.add_argument("-y", "--yes", action="store_true", help="do not ask")
    p.set_defaults(fn=cmd_cache)
    p = sub.add_parser("search", help="test search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5, help="number of results")
    p.add_argument("--mode", default="hybrid", choices=["hybrid", "semantic", "fulltext"])
    p.add_argument("--as", dest="account", default=None, help="search with the rights of an account")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("eval", help="measure search quality with test questions")
    p.add_argument("--file", default=None, help="test file inside the container (default: EVAL_FILE)")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--as", dest="account", default=None, help="evaluate with the rights of an account")
    p.add_argument("-v", "--verbose", action="store_true", help="show every question")
    p.set_defaults(fn=cmd_eval)

    # access policy
    acc = sub.add_parser("access", help="access policy: list, check, show, reload")
    asub = acc.add_subparsers(dest="access_cmd", required=True)
    asub.add_parser("list", help="accounts, rules and visible notes").set_defaults(fn=cmd_access_list)
    p = asub.add_parser("check", help="does an account see a note, and why?")
    p.add_argument("account")
    p.add_argument("note", help="path relative to the vault or file name")
    p.set_defaults(fn=cmd_access_check)
    asub.add_parser("show", help="print the policy file").set_defaults(fn=cmd_access_show)
    asub.add_parser("reload", help="reload the policy now (pulls the access repository in git mode)") \
        .set_defaults(fn=cmd_access_reload)

    acct = sub.add_parser("account", help="create, remove, enable or disable accounts")
    tsub = acct.add_subparsers(dest="account_cmd", required=True)
    p = tsub.add_parser("add", help="create an account (prints its first token)")
    p.add_argument("name")
    p.add_argument("--all", action="store_true", help="allow all notes (add deny rules afterwards)")
    p.add_argument("--role", action="append", help="assign a role (repeatable)")
    p.add_argument("--admin", action="store_true", help="grant the admin permission")
    p.add_argument("--description", default=None)
    p.add_argument("--no-token", action="store_true", help="do not create a token")
    p.set_defaults(fn=cmd_account_add)
    p = tsub.add_parser("remove", help="remove an account and its tokens")
    p.add_argument("name")
    p.set_defaults(fn=cmd_account_remove)
    p = tsub.add_parser("disable", help="block an account without deleting it")
    p.add_argument("name")
    p.set_defaults(fn=lambda a: cmd_account_enable(a, True))
    p = tsub.add_parser("enable", help="unblock an account")
    p.add_argument("name")
    p.set_defaults(fn=lambda a: cmd_account_enable(a, False))
    p = tsub.add_parser("permissions", help="set permissions, e.g. search,read or search,read,admin")
    p.add_argument("name")
    p.add_argument("permissions")
    p.set_defaults(fn=cmd_permissions)

    role = sub.add_parser("role", help="create or remove roles (reusable rule sets)")
    rsub = role.add_subparsers(dest="role_cmd", required=True)
    p = rsub.add_parser("add")
    p.add_argument("name")
    p.add_argument("--all", action="store_true", help="the role allows all notes")
    p.add_argument("--description", default=None)
    p.set_defaults(fn=cmd_role_add)
    p = rsub.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(fn=cmd_role_remove)

    for mode in ("allow", "deny"):
        p = sub.add_parser(mode, help=f"add/remove an {mode} rule: {mode} NAME (tag|folder|path|field) VALUE"
                           + (" or 'allow NAME all'" if mode == "allow" else ""))
        p.add_argument("name", nargs="?", help="account name")
        p.add_argument("criterion_or_all", nargs="?",
                       metavar="tag|folder|path|field" + ("|all" if mode == "allow" else ""))
        p.add_argument("value", nargs="?", default=None)
        p.add_argument("--role", dest="role_target", default=None, help="edit a role instead of an account")
        p.add_argument("--remove", action="store_true", help="remove the rule instead of adding it")
        p.set_defaults(fn=lambda a, m=mode: _rule_entry(a, m))

    p = sub.add_parser("assign", help="assign a role to an account (--remove to unassign)")
    p.add_argument("name")
    p.add_argument("role")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(fn=cmd_assign)

    tok = sub.add_parser("token", help="create or revoke tokens")
    ksub = tok.add_subparsers(dest="token_cmd", required=True)
    p = ksub.add_parser("create", help="new token for an account (shown once)")
    p.add_argument("name")
    p.set_defaults(fn=cmd_token_create)
    p = ksub.add_parser("revoke", help="revoke a token by id (see 'wdb access list')")
    p.add_argument("name")
    p.add_argument("id")
    p.set_defaults(fn=cmd_token_revoke)

    a = ap.parse_args()
    a.fn(a)


def _rule_entry(a, mode: str) -> None:
    """Normalize 'allow NAME all' / 'deny NAME tag hr' / 'deny --role R tag hr'."""
    args = [x for x in (a.name, a.criterion_or_all, a.value) if x is not None]
    if a.role_target:
        # with --role the account name is absent: shift arguments
        args = args if len(args) <= 2 else args[1:]
        a.name = None
    else:
        if len(args) < 2:
            sys.exit(f"Usage: wdb {mode} NAME (tag|folder|path|field) VALUE")
        a.name, args = args[0], args[1:]
    if args == ["all"]:
        a.criterion, a.value = None, "all"
    elif len(args) == 2 and args[0] in ("tag", "folder", "path", "field"):
        a.criterion, a.value = args
    else:
        sys.exit(f"Usage: wdb {mode} NAME (tag|folder|path|field) VALUE   (or with --role ROLE instead of NAME)")
    cmd_rule(a, mode)


if __name__ == "__main__":
    main()
