"""Access control: accounts, roles, rules and tokens.

Policy file (YAML, default ``/data/access.yaml``) - managed with ``wdb``::

    version: 1
    roles:
      no-hr:
        deny: {tags: [hr, confidential]}
    accounts:
      admin:
        allow: all
        permissions: [admin]
        tokens: [{id: t1a2b3, sha256: "...", created: "2026-11-11T10:00:00Z"}]
      staff:
        allow: all
        roles: [no-hr]
      hr:
        allow: {tags: [hr], folders: ["30 HR"]}

Semantics
- ``allow: all`` - every note, minus whatever a ``deny`` rule matches.
- ``allow: {...}`` - only notes matching at least one criterion (OR).
- ``deny: {...}`` - always wins, also over allow rules and roles.
- Roles are merged into the account: allow criteria are added up, deny
  criteria are added up; ``allow: all`` in a role or account means all.
- Criteria: ``tags`` (hierarchical: ``area/production`` also matches
  ``area/production/welding``), ``folders`` (folder and everything below),
  ``paths`` (glob, ``*`` and ``?``, matched against the vault-relative path),
  ``fields`` (frontmatter, e.g. ``{status: [draft]}``; list values match if
  any element matches).
- A disabled account or an unknown token gets no access.

Every rule set is compiled twice: into a LanceDB SQL filter (prefilter for
vector and full-text search, scales to large vaults) and into a Python
predicate over note metadata (read_note, list_notes, links). Both are derived
from the same normalized rules and are cross-checked in the tests.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("wissensdatenbank-mcp.access")

CRITERIA = ("tags", "folders", "paths", "fields")
PERMISSIONS = ("search", "read", "admin")
DEFAULT_PERMISSIONS = ["search", "read"]


class PolicyError(ValueError):
    pass


def access_settings() -> dict:
    """Where the policy lives, from the environment (shared by server and CLI).

    - ``ACCESS_GIT_URL`` set: the policy is ``ACCESS_GIT_PATH`` (default
      ``access.yaml``) in a separate Git repository, pulled with every sync.
      It is read-only for ``wdb``; changes are made by committing to that repo.
    - otherwise: ``ACCESS_FILE`` (default ``<DATA_DIR>/access.yaml``), edited
      with ``wdb access ...`` or by hand.
    """
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    git_url = os.environ.get("ACCESS_GIT_URL", "").strip()
    repo_dir = data_dir / "access-repo"
    if os.environ.get("ACCESS_FILE"):
        path = Path(os.environ["ACCESS_FILE"])
    elif git_url:
        path = repo_dir / os.environ.get("ACCESS_GIT_PATH", "access.yaml")
    else:
        path = data_dir / "access.yaml"
    return {"git_url": git_url, "git_branch": os.environ.get("ACCESS_GIT_BRANCH", "main"),
            "repo_dir": repo_dir, "path": path}


# ---------------------------------------------------------------------------
# Normalization helpers (shared by indexer and policy)
# ---------------------------------------------------------------------------

def norm_tag(tag: str) -> str:
    return str(tag).strip().lstrip("#").strip("/").lower()


def expand_tags(tags: list[str]) -> list[str]:
    """``a/b/c`` -> ``a``, ``a/b``, ``a/b/c`` (so a rule on ``a/b`` matches)."""
    out: list[str] = []
    for t in tags:
        parts = [p for p in norm_tag(t).split("/") if p]
        for i in range(1, len(parts) + 1):
            v = "/".join(parts[:i])
            if v not in out:
                out.append(v)
    return out


def expand_folders(path: str) -> list[str]:
    """``a/b/note.md`` -> ``a``, ``a/b`` (lower-case)."""
    parts = path.lower().split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def norm_folder(folder: str) -> str:
    return str(folder).strip().strip("/").lower()


def field_entries(fields: dict[str, Any]) -> list[str]:
    """Frontmatter fields as ``key=value`` strings (lower-case) for filtering."""
    out: list[str] = []
    for k, v in (fields or {}).items():
        key = str(k).strip().lower()
        if key == "tags":
            continue
        for item in (v if isinstance(v, list) else [v]):
            if item is None or isinstance(item, (dict, list)):
                continue
            val = str(item).strip().lower()
            if val:
                entry = f"{key}={val}"
                if entry not in out:
                    out.append(entry)
    return out


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _glob_to_like(pattern: str) -> str:
    out = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in "%_\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass
class Criteria:
    tags: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)  # "key=value"

    @classmethod
    def parse(cls, raw: Any, where: str) -> "Criteria":
        c = cls()
        if raw in (None, {}):
            return c
        if not isinstance(raw, dict):
            raise PolicyError(f"{where}: expected a mapping with {', '.join(CRITERIA)}")
        unknown = set(raw) - set(CRITERIA)
        if unknown:
            raise PolicyError(f"{where}: unknown criteria {sorted(unknown)} (allowed: {', '.join(CRITERIA)})")
        c.tags = {norm_tag(t) for t in raw.get("tags") or [] if norm_tag(t)}
        c.folders = {norm_folder(f) for f in raw.get("folders") or [] if norm_folder(f)}
        c.paths = {str(p).strip() for p in raw.get("paths") or [] if str(p).strip()}
        flds = raw.get("fields") or {}
        if not isinstance(flds, dict):
            raise PolicyError(f"{where}.fields: expected a mapping, e.g. {{status: [draft]}}")
        for k, vals in flds.items():
            for v in (vals if isinstance(vals, list) else [vals]):
                c.fields.add(f"{str(k).strip().lower()}={str(v).strip().lower()}")
        return c

    def empty(self) -> bool:
        return not (self.tags or self.folders or self.paths or self.fields)

    def merge(self, other: "Criteria") -> None:
        self.tags |= other.tags
        self.folders |= other.folders
        self.paths |= other.paths
        self.fields |= other.fields

    def to_yaml(self) -> dict:
        out: dict[str, Any] = {}
        if self.tags:
            out["tags"] = sorted(self.tags)
        if self.folders:
            out["folders"] = sorted(self.folders)
        if self.paths:
            out["paths"] = sorted(self.paths)
        if self.fields:
            fl: dict[str, list[str]] = {}
            for e in sorted(self.fields):
                k, v = e.split("=", 1)
                fl.setdefault(k, []).append(v)
            out["fields"] = fl
        return out

    # -- evaluation -------------------------------------------------------

    def match(self, meta: "NoteMeta") -> str | None:
        """Return a description of the first matching criterion, else None."""
        for t in self.tags:
            if t in meta.tags:
                return f"tag '{t}'"
        for f in self.folders:
            if f in meta.folders:
                return f"folder '{f}'"
        for p in self.paths:
            if fnmatch.fnmatchcase(meta.path, p):
                return f"path '{p}'"
        for e in self.fields:
            if e in meta.fields:
                return f"field '{e}'"
        return None

    def sql(self) -> str | None:
        parts = []
        if self.tags:
            parts.append(f"array_has_any(acl_tags, [{', '.join(_sql_str(t) for t in sorted(self.tags))}])")
        if self.folders:
            parts.append(f"array_has_any(acl_folders, [{', '.join(_sql_str(f) for f in sorted(self.folders))}])")
        for p in sorted(self.paths):
            parts.append(f"note LIKE {_sql_str(_glob_to_like(p))} ESCAPE '\\'")
        if self.fields:
            parts.append(f"array_has_any(acl_fields, [{', '.join(_sql_str(e) for e in sorted(self.fields))}])")
        return " OR ".join(parts) if parts else None


@dataclass
class NoteMeta:
    path: str
    tags: list[str]
    folders: list[str]
    fields: list[str]

    @classmethod
    def from_note(cls, path: str, meta: dict) -> "NoteMeta":
        return cls(path=path, tags=meta.get("acl_tags", []), folders=expand_folders(path),
                   fields=meta.get("acl_fields", []))


@dataclass
class Access:
    """Effective rules of one account (roles merged)."""
    name: str
    allow_all: bool
    allow: Criteria
    deny: Criteria
    permissions: set[str]
    disabled: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.allow_all and self.deny.empty() and not self.disabled

    def can(self, permission: str) -> bool:
        return not self.disabled and (permission in self.permissions or "admin" in self.permissions)

    def explain(self, meta: NoteMeta) -> tuple[bool, str]:
        if self.disabled:
            return False, "account is disabled"
        d = self.deny.match(meta)
        if d:
            return False, f"hidden by deny rule: {d}"
        if self.allow_all:
            return True, "visible: allow all, no deny rule matches"
        a = self.allow.match(meta)
        if a:
            return True, f"visible by allow rule: {a}"
        return False, "hidden: no allow rule matches"

    def visible(self, meta: NoteMeta) -> bool:
        return self.explain(meta)[0]

    def sql_filter(self) -> str | None:
        """SQL prefilter for LanceDB, ``None`` = no restriction."""
        if self.disabled:
            return "false"
        clauses = []
        if not self.allow_all:
            a = self.allow.sql()
            if not a:
                return "false"
            clauses.append(f"({a})")
        d = self.deny.sql()
        if d:
            clauses.append(f"NOT ({d})")
        return " AND ".join(clauses) if clauses else None


FULL_ACCESS = Access("(full access)", True, Criteria(), Criteria(), {"admin", "search", "read"})


# ---------------------------------------------------------------------------
# Policy file
# ---------------------------------------------------------------------------

class Policy:
    """In-memory policy with automatic reload when the file changes."""

    def __init__(self, path: Path, legacy_token: str = "") -> None:
        self.path = path
        self.legacy_token = legacy_token.strip()
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self.raw: dict = {"version": 1, "roles": {}, "accounts": {}}
        self._accounts: dict[str, Access] = {}
        self._by_hash: dict[str, str] = {}
        self.error: str | None = None
        self.reload(force=True)

    # -- loading ----------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self.path.exists()

    def reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        with self._lock:
            if not force and mtime == self._mtime:
                return
            self._mtime = mtime
            if mtime is None:
                self.raw = {"version": 1, "roles": {}, "accounts": {}}
                self._accounts, self._by_hash = {}, {}
                self.error = None
                return
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                accounts, by_hash = compile_policy(raw)
            except Exception as exc:  # noqa: BLE001 - keep the last good policy
                self.error = str(exc)
                log.error("Access policy %s is invalid, keeping the previous one: %s", self.path, exc)
                return
            self.raw, self._accounts, self._by_hash, self.error = raw, accounts, by_hash, None
            log.info("Access policy loaded: %d accounts, %d roles", len(accounts), len(raw.get("roles") or {}))

    # -- lookup -----------------------------------------------------------

    def authenticate(self, token: str | None) -> Access | None:
        """Account for a bearer token. ``None`` = reject."""
        self.reload()
        if not self.configured:
            if not self.legacy_token:
                return FULL_ACCESS  # no policy, no token: open (documented)
            return FULL_ACCESS if token and secrets.compare_digest(token, self.legacy_token) else None
        if not token:
            return None
        name = self._by_hash.get(token_hash(token))
        if name is None and self.legacy_token and secrets.compare_digest(token, self.legacy_token):
            return FULL_ACCESS
        acc = self._accounts.get(name) if name else None
        return None if acc is None or acc.disabled else acc

    def account(self, name: str) -> Access:
        self.reload()
        if name in ("*", "(full access)", "admin*"):
            return FULL_ACCESS
        if name not in self._accounts:
            raise PolicyError(f"unknown account '{name}'")
        return self._accounts[name]

    def account_names(self) -> list[str]:
        self.reload()
        return sorted(self._accounts)

    @property
    def open_access(self) -> bool:
        return not self.configured and not self.legacy_token


def compile_policy(raw: dict) -> tuple[dict[str, Access], dict[str, str]]:
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a mapping")
    roles_raw = raw.get("roles") or {}
    accounts_raw = raw.get("accounts") or {}
    if not isinstance(roles_raw, dict) or not isinstance(accounts_raw, dict):
        raise PolicyError("'roles' and 'accounts' must be mappings")

    def parse_rules(obj: dict, where: str) -> tuple[bool, Criteria, Criteria]:
        unknown = set(obj) - {"allow", "deny", "roles", "permissions", "tokens", "disabled", "description"}
        if unknown:
            raise PolicyError(f"{where}: unknown keys {sorted(unknown)}")
        allow = obj.get("allow")
        allow_all = allow == "all"
        allow_c = Criteria() if allow_all else Criteria.parse(allow, f"{where}.allow")
        if allow is not None and not allow_all and not isinstance(allow, dict):
            raise PolicyError(f"{where}.allow: use 'all' or a mapping of criteria")
        return allow_all, allow_c, Criteria.parse(obj.get("deny"), f"{where}.deny")

    roles = {}
    for rname, robj in roles_raw.items():
        robj = robj or {}
        if "roles" in robj or "tokens" in robj:
            raise PolicyError(f"roles.{rname}: roles cannot contain roles or tokens")
        roles[rname] = parse_rules(robj, f"roles.{rname}")

    accounts: dict[str, Access] = {}
    by_hash: dict[str, str] = {}
    for aname, aobj in accounts_raw.items():
        aobj = aobj or {}
        allow_all, allow_c, deny_c = parse_rules(aobj, f"accounts.{aname}")
        for r in aobj.get("roles") or []:
            if r not in roles:
                raise PolicyError(f"accounts.{aname}: unknown role '{r}'")
            r_all, r_allow, r_deny = roles[r]
            allow_all = allow_all or r_all
            allow_c.merge(r_allow)
            deny_c.merge(r_deny)
        perms = set(aobj.get("permissions") or DEFAULT_PERMISSIONS)
        bad = perms - set(PERMISSIONS)
        if bad:
            raise PolicyError(f"accounts.{aname}: unknown permissions {sorted(bad)}")
        accounts[aname] = Access(aname, allow_all, allow_c, deny_c, perms, bool(aobj.get("disabled")))
        for tok in aobj.get("tokens") or []:
            h = (tok or {}).get("sha256")
            if not h:
                raise PolicyError(f"accounts.{aname}: token entry without sha256")
            if h in by_hash:
                raise PolicyError(f"token {tok.get('id')} is assigned to two accounts")
            by_hash[h] = aname
    return accounts, by_hash


# ---------------------------------------------------------------------------
# Editing (used by the CLI; writes atomically, the server reloads on change)
# ---------------------------------------------------------------------------

class PolicyEditor:
    def __init__(self, path: Path) -> None:
        self.path = path
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        self.raw: dict = loaded if isinstance(loaded, dict) else {"version": 1, "roles": {}, "accounts": {}}
        self.raw.setdefault("roles", {})
        self.raw.setdefault("accounts", {})

    def save(self) -> None:
        compile_policy(self.raw)  # validate before writing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# Managed with `wdb`. Manual edits are allowed; invalid files are ignored.\n"
                       + yaml.safe_dump(self.raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _target(self, kind: str, name: str) -> dict:
        group = self.raw["accounts" if kind == "account" else "roles"]
        if name not in group:
            raise PolicyError(f"unknown {kind} '{name}'")
        if group[name] is None:
            group[name] = {}
        return group[name]

    def add(self, kind: str, name: str, allow_all: bool = False, description: str | None = None) -> None:
        group = self.raw["accounts" if kind == "account" else "roles"]
        if name in group:
            raise PolicyError(f"{kind} '{name}' already exists")
        if not name or any(c.isspace() for c in name):
            raise PolicyError("names must not be empty or contain spaces")
        obj: dict[str, Any] = {}
        if description:
            obj["description"] = description
        if allow_all:
            obj["allow"] = "all"
        if kind == "account":
            obj["permissions"] = list(DEFAULT_PERMISSIONS)
            obj["tokens"] = []
        group[name] = obj

    def remove(self, kind: str, name: str) -> None:
        group = self.raw["accounts" if kind == "account" else "roles"]
        if name not in group:
            raise PolicyError(f"unknown {kind} '{name}'")
        if kind == "role":
            users = [a for a, o in self.raw["accounts"].items() if name in ((o or {}).get("roles") or [])]
            if users:
                raise PolicyError(f"role '{name}' is used by: {', '.join(users)}")
        del group[name]

    def set_allow_all(self, kind: str, name: str, value: bool) -> None:
        obj = self._target(kind, name)
        if value:
            obj["allow"] = "all"
        elif obj.get("allow") == "all":
            obj.pop("allow")

    def rule(self, kind: str, name: str, mode: str, criterion: str, value: str, remove: bool = False) -> None:
        if mode not in ("allow", "deny"):
            raise PolicyError("mode must be allow or deny")
        if criterion not in ("tag", "folder", "path", "field"):
            raise PolicyError("criterion must be tag, folder, path or field")
        obj = self._target(kind, name)
        block = obj.get(mode)
        if block == "all":
            if mode == "allow" and not remove:
                raise PolicyError(f"{kind} '{name}' already allows all notes; add deny rules or "
                                  f"run 'allow-all off' first")
            block = {}
        block = dict(block or {})
        key = criterion + "s"
        if criterion == "field":
            if "=" not in value:
                raise PolicyError("field rules need key=value, e.g. status=draft")
            k, v = (s.strip() for s in value.split("=", 1))
            flds = dict(block.get("fields") or {})
            vals = list(flds.get(k) or [])
            if remove:
                vals = [x for x in vals if str(x).lower() != v.lower()]
            elif v not in vals:
                vals.append(v)
            if vals:
                flds[k] = vals
            else:
                flds.pop(k, None)
            if flds:
                block["fields"] = flds
            else:
                block.pop("fields", None)
        else:
            vals = list(block.get(key) or [])
            if remove:
                vals = [x for x in vals if str(x) != value]
            elif value not in vals:
                vals.append(value)
            if vals:
                block[key] = vals
            else:
                block.pop(key, None)
        if block:
            obj[mode] = block
        else:
            obj.pop(mode, None)

    def set_roles(self, name: str, role: str, remove: bool = False) -> None:
        obj = self._target("account", name)
        if not remove and role not in self.raw["roles"]:
            raise PolicyError(f"unknown role '{role}'")
        roles = list(obj.get("roles") or [])
        if remove:
            roles = [r for r in roles if r != role]
        elif role not in roles:
            roles.append(role)
        if roles:
            obj["roles"] = roles
        else:
            obj.pop("roles", None)

    def set_permissions(self, name: str, perms: list[str]) -> None:
        bad = set(perms) - set(PERMISSIONS)
        if bad:
            raise PolicyError(f"unknown permissions {sorted(bad)} (allowed: {', '.join(PERMISSIONS)})")
        self._target("account", name)["permissions"] = perms

    def set_disabled(self, name: str, disabled: bool) -> None:
        obj = self._target("account", name)
        if disabled:
            obj["disabled"] = True
        else:
            obj.pop("disabled", None)

    def create_token(self, name: str) -> tuple[str, str]:
        obj = self._target("account", name)
        token = "wdb_" + secrets.token_urlsafe(32)
        tid = secrets.token_hex(3)
        obj.setdefault("tokens", []).append({
            "id": tid, "sha256": token_hash(token),
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return tid, token

    def revoke_token(self, name: str, tid: str) -> None:
        obj = self._target("account", name)
        toks = obj.get("tokens") or []
        keep = [t for t in toks if t.get("id") != tid]
        if len(keep) == len(toks):
            raise PolicyError(f"account '{name}' has no token '{tid}'")
        obj["tokens"] = keep
