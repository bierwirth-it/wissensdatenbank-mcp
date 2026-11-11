"""wissensdatenbank-mcp: MCP server for searching a Markdown/Obsidian vault.

Serves hybrid search over a vault in a Git repository via MCP (Streamable
HTTP). A background thread pulls the repository every SYNC_INTERVAL seconds;
only new, changed and deleted notes are processed and searches keep running on
the previous state meanwhile (details in index.py). Access control per account
and token is described in access.py.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (before importing model libraries: offline mode)
# ---------------------------------------------------------------------------

GIT_REPO_URL = os.environ.get("GIT_REPO_URL", "").strip()
# alternative to Git: a plain directory (e.g. a mounted Obsidian folder), read in place
VAULT_DIR = os.environ.get("VAULT_DIR", "").strip()
GIT_BRANCH = os.environ.get("GIT_BRANCH", "main")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "300"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
REPO_DIR = Path(VAULT_DIR) if VAULT_DIR else Path(os.environ.get("REPO_DIR", str(DATA_DIR / "repo")))
INDEX_DIR = Path(os.environ.get("INDEX_DIR", "/index"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MAX_NOTE_CHARS = int(os.environ.get("MAX_NOTE_CHARS", "50000"))
# folders (relative to the vault) that are not indexed, comma-separated
INDEX_EXCLUDE = [e.strip().strip("/") for e in os.environ.get("INDEX_EXCLUDE", "").split(",") if e.strip()]
SSH_DIR = Path.home() / ".ssh"
# test questions for `wdb eval`; a hidden folder in the vault is not indexed
EVAL_FILE = os.environ.get("EVAL_FILE", str(REPO_DIR / ".wdb" / "eval.json"))

# model signature: once all models were loaded successfully, start without network
SIGNATURE = "|".join([
    os.environ.get("EMBED_MODEL", "BAAI/bge-m3"),
    os.environ.get("RERANK_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"),
])
MODELS_DIR = Path(os.environ.get("HF_HOME", "/models"))
MODELS_READY = MODELS_DIR / ".models-ready"
if os.environ.get("HF_OFFLINE_WHEN_CACHED", "1") == "1" and MODELS_READY.exists() \
        and MODELS_READY.read_text().strip() == SIGNATURE:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import anyio  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wissensdatenbank-mcp")
# silence noisy libraries: HTTP requests of the model libraries, health checks
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
# in offline mode huggingface_hub reports "Could not reach the Hub" on every load
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


class _NoHealth(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_NoHealth())

from access import FULL_ACCESS, Access, Policy, PolicyError, access_settings, token_hash  # noqa: E402

# access policy: a file in the data volume or a file in a separate Git repository
_acs = access_settings()
ACCESS_GIT_URL, ACCESS_GIT_BRANCH = _acs["git_url"], _acs["git_branch"]
ACCESS_REPO_DIR, ACCESS_FILE = _acs["repo_dir"], _acs["path"]
from index import NoteNotFound, NotReady, VaultIndex  # noqa: E402
from progress import progress  # noqa: E402

# huggingface_hub sets its own log levels on import; silence it afterwards
try:
    from huggingface_hub.utils import logging as _hf_logging

    _hf_logging.set_verbosity_error()
except Exception:  # noqa: BLE001
    pass

if os.environ.get("HF_HUB_OFFLINE") == "1":
    log.info("Models are cached, loading without network access (HF_HUB_OFFLINE=1)")

_sync_lock = threading.Lock()  # prevents parallel syncs
_index = VaultIndex(INDEX_DIR)
_policy = Policy(ACCESS_FILE, legacy_token=MCP_AUTH_TOKEN)
_paused = threading.Event()  # automatic sync paused (wdb pause)
_state: dict = {"commit": None, "access_commit": None, "last_sync": None, "last_sync_ok": None,
                "last_error": None}
_current_access: contextvars.ContextVar[Access] = contextvars.ContextVar("access", default=FULL_ACCESS)

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

_KH_ENTRY = re.compile(
    r"(\S+)\s+(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com)\s+([A-Za-z0-9+/]+={0,2})"
)
_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)


def normalize_known_hosts(raw: str) -> str:
    """Extract known_hosts entries from arbitrarily formatted text.

    Environment variables entered without line breaks (e.g. in a single-line
    UI field) lose their newlines; entries are therefore matched by pattern
    instead of line by line. Comments (e.g. from ssh-keyscan) are dropped.
    """
    raw = raw.replace("\\n", "\n")
    return "".join(f"{h} {t} {k}\n" for h, t, k in _KH_ENTRY.findall(raw))


def normalize_private_key(raw: str) -> str:
    """Restore a private key in PEM/OpenSSH format, also when its line
    breaks were turned into spaces."""
    raw = raw.replace("\\n", "\n").strip()
    m = _PEM.search(raw)
    if not m:
        return raw + "\n"
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 70] for i in range(0, len(body), 70)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def _setup_ssh() -> dict[str, str]:
    """Write the deploy key from the environment and set GIT_SSH_COMMAND."""
    env = dict(os.environ)
    key = os.environ.get("GIT_SSH_PRIVATE_KEY", "").strip()
    if not key:
        return env
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = SSH_DIR / "deploy_key"
    key_path.write_text(normalize_private_key(key))
    key_path.chmod(0o600)
    fp = subprocess.run(["ssh-keygen", "-lf", str(key_path)], capture_output=True, text=True)
    if fp.returncode == 0:
        log.info("Deploy key loaded: %s", fp.stdout.strip())
    else:
        log.error("Deploy key is unreadable (check GIT_SSH_PRIVATE_KEY): %s", fp.stderr.strip())
    known_hosts = SSH_DIR / "known_hosts"
    kh = normalize_known_hosts(os.environ.get("GIT_SSH_KNOWN_HOSTS", ""))
    if kh:
        known_hosts.write_text(kh)
        strict = "yes"
        log.info("known_hosts: %d entries (%s)", kh.count("\n"),
                 ", ".join(sorted({line.split()[0] for line in kh.splitlines()})))
    else:
        strict = "accept-new"
        log.warning("GIT_SSH_KNOWN_HOSTS is empty or unreadable, the host key is trusted on first use")
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes "
        f"-o StrictHostKeyChecking={strict} -o UserKnownHostsFile={known_hosts}"
    )
    return env


GIT_ENV = _setup_ssh()


def _git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


def _redact(url: str) -> str:
    """Repository URL without credentials (user:password@ or token@)."""
    return re.sub(r"(?<=://)[^/@]+@", "", url)


def sync_repo(url: str, branch: str, target: Path) -> str:
    """Clone the repository or reset it to origin/<branch>. Returns the HEAD commit."""
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        log.info("Cloning %s (%s) to %s", _redact(url), branch, target)
        _git("clone", "--branch", branch, "--single-branch", url, str(target))
    else:
        _git("remote", "set-url", "origin", url, cwd=target)
        _git("fetch", "--prune", "origin", branch, cwd=target)
        _git("reset", "--hard", f"origin/{branch}", cwd=target)
        _git("clean", "-fdx", cwd=target)
    return _git("rev-parse", "HEAD", cwd=target)


# ---------------------------------------------------------------------------
# Sync and index
# ---------------------------------------------------------------------------

def _is_indexed(rel: Path) -> bool:
    if any(part.startswith(".") for part in rel.parts):
        return False
    rel_s = rel.as_posix()
    return not any(rel_s == e or rel_s.startswith(e + "/") for e in INDEX_EXCLUDE)


def indexed_files() -> dict[str, Path]:
    return {
        p.relative_to(REPO_DIR).as_posix(): p
        for p in sorted(REPO_DIR.rglob("*.md"))
        if _is_indexed(p.relative_to(REPO_DIR))
    }


def directory_state(root: Path) -> str:
    """Pseudo commit id of a plain vault directory (paths, sizes, modification times)."""
    h = hashlib.sha1()
    for p in sorted(root.rglob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{p.relative_to(root).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return "dir-" + h.hexdigest()


def sync_access() -> None:
    """Pull the access policy repository (if configured); errors keep the last policy."""
    if not ACCESS_GIT_URL:
        return
    try:
        commit = sync_repo(ACCESS_GIT_URL, ACCESS_GIT_BRANCH, ACCESS_REPO_DIR)
        if commit != _state["access_commit"]:
            _state["access_commit"] = commit
            _policy.reload(force=True)
            if not ACCESS_FILE.exists():
                log.error("Access repository has no file %s - all requests are rejected",
                          ACCESS_FILE.relative_to(ACCESS_REPO_DIR))
    except Exception:  # noqa: BLE001
        log.exception("Could not pull the access repository, keeping the current policy")


def sync_once(force: bool = False, use_cache: bool = True) -> bool:
    """Pull the repositories and build a new index state if anything changed."""
    if not _sync_lock.acquire(blocking=False):
        log.debug("Sync already running, skipping")
        return False
    try:
        _state["last_sync"] = datetime.now(timezone.utc).isoformat()
        progress.start("Git sync")
        progress.set_phase("Pulling repository")
        try:
            sync_access()
            commit = directory_state(REPO_DIR) if VAULT_DIR else sync_repo(GIT_REPO_URL, GIT_BRANCH, REPO_DIR)
        finally:
            progress.end()
        _state["commit"] = commit
        # Excluded folders count as deleted files: when INDEX_EXCLUDE changes,
        # they leave or join the index on the next sync
        built = _index.update(commit, indexed_files(), force=force, use_cache=use_cache)
        _state["last_sync_ok"] = True
        _state["last_error"] = None
        # after a successful build all models are cached: load them without
        # network access from the next start on
        if _index.snapshot_ready() and (not MODELS_READY.exists()
                                        or MODELS_READY.read_text().strip() != SIGNATURE):
            MODELS_READY.write_text(SIGNATURE + "\n")
        return built
    except Exception as exc:  # noqa: BLE001 - the service keeps running
        log.exception("Sync failed")
        _state["last_sync_ok"] = False
        _state["last_error"] = str(exc)
        return False
    finally:
        _sync_lock.release()


def sync_loop() -> None:
    while True:
        if not _paused.is_set():
            sync_once()
        time.sleep(SYNC_INTERVAL)


def startup() -> None:
    if not GIT_REPO_URL and not VAULT_DIR:
        raise SystemExit("Set GIT_REPO_URL (Git repository) or VAULT_DIR (mounted folder)")
    if VAULT_DIR and not REPO_DIR.is_dir():
        raise SystemExit(f"VAULT_DIR {VAULT_DIR} is not a directory")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _index.open()
    if ACCESS_GIT_URL:
        sync_access()
    if _policy.configured:
        log.info("Access control: %s (%d accounts)%s", ACCESS_FILE, len(_policy.account_names()),
                 ", MCP_AUTH_TOKEN additionally grants full access" if MCP_AUTH_TOKEN else "")
    elif MCP_AUTH_TOKEN:
        log.info("Access control: single token (MCP_AUTH_TOKEN), full access")
    else:
        log.warning("Access control is OFF: no access policy and no MCP_AUTH_TOKEN - anyone who can "
                    "reach the port can read the whole vault")
    threading.Thread(target=sync_loop, name="git-sync", daemon=True).start()
    log.info("Ready, syncing every %s s", SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "wissensdatenbank",
    instructions=os.environ.get("MCP_INSTRUCTIONS") or (
        "Searches the knowledge base (a Markdown/Obsidian vault). Use 'search' first, then load "
        "the full note with 'read_note' if needed. 'links' and 'backlinks' in results point to "
        "related notes. Always cite the file path of your sources."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
)


def _access(ctx: Context) -> Access:
    """Account of the current MCP request (set by the auth middleware)."""
    try:
        req = ctx.request_context.request if ctx is not None else None
        acc = req.scope.get("wdb.access") if req is not None else None
        if acc is not None:
            return acc
    except (AttributeError, ValueError, LookupError):
        pass
    return _current_access.get()


def _require(access: Access, permission: str) -> None:
    if not access.can(permission):
        raise ValueError(f"permission '{permission}' is required for this tool")


@mcp.tool()
async def search(ctx: Context, query: str, top_k: int = 8, mode: str = "hybrid",
                 rerank: bool = True) -> list[dict]:
    """Search the knowledge base.

    Args:
        query: Natural-language query, e.g. "How does goods receipt work?".
        top_k: Maximum number of results (one result per note).
        mode: "hybrid" (default: meaning + full text + linked notes),
            "semantic" or "fulltext".
        rerank: Re-score results with a cross-encoder.

    Returns:
        The best section per note: file_path (relative to the vault), title,
        heading, snippet, score, matched (search paths), links (links to) and
        backlinks (linked from). links/backlinks show where more information
        is; read them with read_note or related_notes.
    """
    access = _access(ctx)
    _require(access, "search")
    if mode not in {"hybrid", "semantic", "fulltext"}:
        raise ValueError("mode must be hybrid, semantic or fulltext")
    top_k = max(1, min(int(top_k), 50))
    try:
        return await anyio.to_thread.run_sync(
            lambda: _index.search(query, top_k=top_k, mode=mode, rerank=rerank, access=access))
    except NotReady as exc:
        raise ValueError(str(exc)) from exc


def _resolve_note(file_path: str, access: Access) -> tuple[Path, str]:
    root = REPO_DIR.resolve()
    target = (root / file_path).resolve()
    if root not in target.parents or target.suffix.lower() != ".md" or not target.is_file():
        raise ValueError(f"note not found: {file_path}")
    rel = target.relative_to(root).as_posix()
    if not access.unrestricted:
        # restricted accounts only see indexed notes they are allowed to see
        try:
            if not _index.is_visible(access, rel):
                raise ValueError(f"note not found: {file_path}")
        except NotReady as exc:
            raise ValueError(str(exc)) from exc
    return target, rel


@mcp.tool()
async def read_note(ctx: Context, file_path: str) -> dict:
    """Read the full content of a note, including its links.

    Args:
        file_path: Path relative to the vault as returned by 'search',
            e.g. "10 Processes/Goods receipt.md".
    """
    access = _access(ctx)
    _require(access, "read")
    target, rel = _resolve_note(file_path, access)
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        meta = _index.note_info(rel, access)
    except (NotReady, NoteNotFound):
        meta = {"links": [], "backlinks": []}
    return {
        "file_path": rel,
        "content": text[:MAX_NOTE_CHARS],
        "truncated": len(text) > MAX_NOTE_CHARS,
        "links": meta.get("links", []),
        "backlinks": meta.get("backlinks", []),
    }


@mcp.tool()
async def related_notes(ctx: Context, file_path: str, depth: int = 1) -> dict:
    """Related notes via wikilinks: what the note links to (links) and which
    notes link to it (backlinks). With depth=2 also notes two steps away,
    sorted by the number of connections.

    Args:
        file_path: Path relative to the vault.
        depth: 1 or 2.
    """
    access = _access(ctx)
    _require(access, "read")
    _, rel = _resolve_note(file_path, access)
    try:
        return _index.related(rel, depth=2 if int(depth) >= 2 else 1, access=access)
    except (NotReady, NoteNotFound) as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
async def list_notes(ctx: Context, folder: str = "") -> list[str]:
    """List notes (Markdown files), optionally only below one folder.

    Args:
        folder: Folder relative to the vault, empty for all.
    """
    access = _access(ctx)
    _require(access, "read")
    prefix = folder.strip().strip("/")
    if ".." in prefix.split("/"):
        raise ValueError("invalid folder")
    if access.unrestricted:
        root = REPO_DIR.resolve()
        base = (root / prefix).resolve()
        notes = sorted(p.relative_to(root).as_posix() for p in base.rglob("*.md")
                       if not any(part.startswith(".") for part in p.relative_to(root).parts))
    else:
        notes = _index.manifest(access)
        if prefix:
            notes = [n for n in notes if n.startswith(prefix + "/")]
    return notes


@mcp.tool()
async def status(ctx: Context) -> dict:
    """State of the index: active state (commit, notes, sections), running
    update with progress and last Git sync. Accounts without the admin
    permission get a short summary."""
    access = _access(ctx)
    if access.can("admin"):
        return _status_payload()
    active = _index.info()["active"]
    return {
        "account": access.name,
        "ready": active is not None,
        "visible_notes": len(_index.manifest(access)),
        "commit": active["commit"] if active else None,
        "built_at": active["built_at"] if active else None,
        "updating": _index.building is not None,
    }


@mcp.tool()
async def sync_now(ctx: Context) -> dict:
    """Pull the repository now; an update starts in the background if anything
    changed. Searches keep running on the previous state. Requires the admin
    permission."""
    _require(_access(ctx), "admin")
    threading.Thread(target=sync_once, name="sync-now", daemon=True).start()
    return {"started": True, "building": _index.building}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    info = _index.info()
    active = info["active"]
    ok = active is not None
    return JSONResponse(
        {"ok": ok, "commit": _state["commit"], "index_commit": active["commit"] if active else None,
         "building": info["building"] is not None, "last_sync_ok": _state["last_sync_ok"]},
        status_code=200 if ok else 503,
    )


# ---------------------------------------------------------------------------
# Administration (only from inside the container: CLI `wdb`)
# ---------------------------------------------------------------------------

def _local(request: Request) -> bool:
    return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": "administration is only available inside the container"}, status_code=403)


def _bg(fn, *args, **kwargs) -> None:
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def _status_payload() -> dict:
    return {
        "repo": f"directory {VAULT_DIR}" if VAULT_DIR else _redact(GIT_REPO_URL),
        "branch": None if VAULT_DIR else GIT_BRANCH,
        "sync_interval_s": SYNC_INTERVAL,
        "sync_paused": _paused.is_set(),
        "index_exclude": INDEX_EXCLUDE,
        "signature": SIGNATURE,
        "offline_models": os.environ.get("HF_HUB_OFFLINE") == "1",
        "access": _access_summary(),
        **_state,
        **_index.info(),
    }


def _access_summary() -> dict:
    return {
        "mode": "git" if ACCESS_GIT_URL else "file",
        "file": str(ACCESS_FILE),
        "repo": _redact(ACCESS_GIT_URL) if ACCESS_GIT_URL else None,
        "policy_loaded": _policy.configured,
        "policy_error": _policy.error,
        "accounts": len(_policy.account_names()),
        "legacy_token": bool(MCP_AUTH_TOKEN),
        "open": _policy.open_access,
    }


def _account_for(name: str | None) -> Access:
    return FULL_ACCESS if not name else _policy.account(name)


@mcp.custom_route("/admin/status", methods=["GET"])
async def admin_status(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    return JSONResponse(_status_payload())


def dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


@mcp.custom_route("/admin/stats", methods=["GET"])
async def admin_stats(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()

    def collect():
        folders: dict[str, int] = {}
        for rel in _index.manifest():
            top = rel.split("/")[0] if "/" in rel else "(root)"
            folders[top] = folders.get(top, 0) + 1
        try:
            indices = _index.indices()
        except NotReady:
            indices = []
        return {
            "index": _index.info()["active"],
            "notes_per_folder": dict(sorted(folders.items())),
            "repo_notes_total": len(list(REPO_DIR.rglob("*.md"))) if REPO_DIR.exists() else 0,
            "cache": _index.cache_info(),
            "indices": indices,
            "disk_bytes": {
                "index": dir_size(INDEX_DIR),
                "models": dir_size(MODELS_DIR),
                "repo": dir_size(REPO_DIR) if REPO_DIR.exists() else 0,
            },
        }
    return JSONResponse(await anyio.to_thread.run_sync(collect))


@mcp.custom_route("/admin/versions", methods=["GET"])
async def admin_versions(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    return JSONResponse(await anyio.to_thread.run_sync(_index.versions))


@mcp.custom_route("/admin/sync", methods=["POST"])
async def admin_sync(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    if _index.building:
        return JSONResponse({"started": False, "reason": "an update is already running"})
    _bg(sync_once)
    return JSONResponse({"started": True})


@mcp.custom_route("/admin/rebuild", methods=["POST"])
async def admin_rebuild(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    if _index.building:
        return JSONResponse({"started": False, "reason": "an update is already running"})
    use_cache = request.query_params.get("no_cache") not in ("1", "true", "yes")
    log.info("Admin: full rebuild requested (%s)", "with cache" if use_cache else "without cache")
    _bg(sync_once, force=True, use_cache=use_cache)
    return JSONResponse({"started": True, "use_cache": use_cache})


@mcp.custom_route("/admin/cancel", methods=["POST"])
async def admin_cancel(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    if not _index.building:
        return JSONResponse({"cancelled": False, "reason": "no update is running"})
    progress.cancel_requested.set()
    log.info("Admin: cancellation of the running update requested")
    return JSONResponse({"cancelled": True, "sync_paused": _paused.is_set()})


@mcp.custom_route("/admin/pause", methods=["POST"])
async def admin_pause(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    _paused.set()
    log.info("Admin: automatic sync paused")
    return JSONResponse({"sync_paused": True})


@mcp.custom_route("/admin/resume", methods=["POST"])
async def admin_resume(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    _paused.clear()
    log.info("Admin: automatic sync resumed")
    return JSONResponse({"sync_paused": False})


@mcp.custom_route("/admin/cache/clear", methods=["POST"])
async def admin_cache_clear(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    try:
        removed = await anyio.to_thread.run_sync(_index.clear_cache)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"removed": removed})


@mcp.custom_route("/admin/cache/prune", methods=["POST"])
async def admin_cache_prune(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    if _index.building:
        return JSONResponse({"error": "an update is running"}, status_code=409)
    try:
        return JSONResponse(await anyio.to_thread.run_sync(_index.prune_cache))
    except NotReady as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@mcp.custom_route("/admin/eval", methods=["POST"])
async def admin_eval(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    body = await request.json()
    from evaluate import run_eval
    path = body.get("file") or EVAL_FILE
    if not path:
        return JSONResponse({"error": "no test file: pass --file or set EVAL_FILE"}, status_code=400)
    try:
        access = _account_for(body.get("account"))
        res = await anyio.to_thread.run_sync(
            lambda: run_eval(_index, path, int(body.get("top_k", 8)), access=access))
    except (NotReady, FileNotFoundError, PolicyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(res)


@mcp.custom_route("/admin/search", methods=["POST"])
async def admin_search(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    body = await request.json()
    try:
        access = _account_for(body.get("account"))
        res = await anyio.to_thread.run_sync(lambda: _index.search(
            body["query"], top_k=int(body.get("top_k", 5)), mode=body.get("mode", "hybrid"),
            rerank=bool(body.get("rerank", True)), access=access))
    except (NotReady, PolicyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(res)


@mcp.custom_route("/admin/access", methods=["GET"])
async def admin_access(request: Request) -> JSONResponse:
    """Accounts with their effective rules and number of visible notes."""
    if not _local(request):
        return _forbidden()
    _policy.reload()

    def collect():
        total = len(_index.manifest()) if _index.snapshot_ready() else None
        accounts = []
        raw = _policy.raw.get("accounts") or {}
        for name in _policy.account_names():
            a = _policy.account(name)
            accounts.append({
                "name": name,
                "description": (raw.get(name) or {}).get("description"),
                "disabled": a.disabled,
                "allow": "all" if a.allow_all else a.allow.to_yaml(),
                "deny": a.deny.to_yaml(),
                "roles": (raw.get(name) or {}).get("roles") or [],
                "permissions": sorted(a.permissions),
                "tokens": [{k: t.get(k) for k in ("id", "created")}
                           for t in (raw.get(name) or {}).get("tokens") or []],
                "visible_notes": len(_index.manifest(a)) if total is not None else None,
            })
        return {**_access_summary(), "total_notes": total, "roles": list((_policy.raw.get("roles") or {})),
                "account_list": accounts}
    return JSONResponse(await anyio.to_thread.run_sync(collect))


@mcp.custom_route("/admin/access/check", methods=["POST"])
async def admin_access_check(request: Request) -> JSONResponse:
    """Explain whether an account sees a note and which rule decides."""
    if not _local(request):
        return _forbidden()
    body = await request.json()
    try:
        access = _policy.account(body["account"])
        note = body["note"].strip().strip("/")
        if not note.endswith(".md"):
            note += ".md"
        snap_notes = _index.manifest()
        if note not in snap_notes:  # allow file names without folder
            cands = [n for n in snap_notes if n.rsplit("/", 1)[-1] == note.rsplit("/", 1)[-1]]
            if len(cands) == 1:
                note = cands[0]
        return JSONResponse(_index.explain(access, note))
    except (PolicyError, NotReady, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/admin/access/reload", methods=["POST"])
async def admin_access_reload(request: Request) -> JSONResponse:
    if not _local(request):
        return _forbidden()
    if ACCESS_GIT_URL:
        await anyio.to_thread.run_sync(sync_access)
    _policy.reload(force=True)
    return JSONResponse(_access_summary())


class BearerAuth:
    """Authenticates /mcp requests and attaches the account to the request."""

    def __init__(self, app, policy: Policy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            access = self.policy.authenticate(token)
            if access is None:
                if token:
                    log.warning("Rejected MCP request: unknown or disabled token (%s...)", token_hash(token)[:8])
                resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
            scope["wdb.access"] = access
            reset = _current_access.set(access)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_access.reset(reset)
            return
        await self.app(scope, receive, send)


def main() -> None:
    startup()
    app = BearerAuth(mcp.streamable_http_app(), _policy)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
