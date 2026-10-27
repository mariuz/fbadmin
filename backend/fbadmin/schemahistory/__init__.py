"""Schema history in git (milestone 2.0).

Per database, fbadmin keeps a local git repository on the fbadmin host and commits a *snapshot* of
the schema to it: the full DDL, written as one file per object (see :mod:`.layout`), so the history
of every table, view or procedure is a readable git log. Snapshots are taken on demand, by the
scheduler (schedule kind ``schema_snapshot``) and optionally a few seconds after DDL run through
fbadmin. A commit is only made when an object changed; its message lists the added, changed and
dropped objects. Optionally each commit is pushed to a remote (SSH keys or the host's credential
helper, or an HTTPS token given as ``env:NAME``).

Configurations live in ``<data dir>/schema_history.json``. Repository paths are resolved below
``FBADMIN_SCHEMA_HISTORY_ROOT`` (default ``<data dir>/schema-history``) unless absolute; in server
mode absolute paths must also be inside that root, and only administrators change configurations.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import settings
from ..metadata.types import quote_ident
from . import layout
from .gitrepo import GitError, GitMissing, Repo, check_branch, check_ref, check_sha, git_binary, git_version

__all__ = ["HistoryConfig", "GitError", "GitMissing", "get_store", "snapshot", "after_sql", "after_commit"]


class HistoryConfig(BaseModel):
    server_id: str
    database_id: str
    repo_path: str
    subdir: str = ""
    branch: str = "main"
    remote_url: str | None = None
    push: bool = True                 # push after each commit when a remote is set
    token: str | None = None          # HTTPS token for the remote, only as env:NAME
    token_user: str = "x-access-token"
    committer_name: str = "fbadmin"
    committer_email: str = "fbadmin@localhost"
    after_ddl: bool = False           # snapshot a few seconds after DDL run through fbadmin
    created_at: float = Field(default_factory=time.time)
    last_snapshot_at: float | None = None
    last_commit: str | None = None
    last_error: str | None = None
    last_push_error: str | None = None


# ------------------------------------------------------------------ paths and validation
def root_dir() -> Path:
    env = os.environ.get("FBADMIN_SCHEMA_HISTORY_ROOT")
    return Path(env) if env else Path(settings.data_dir) / "schema-history"


def _server_mode() -> bool:
    try:
        from ..api.deps import get_state

        return bool(get_state().security.server_mode)
    except Exception:  # noqa: BLE001
        return False


def _parts(p: str) -> list[str]:
    return [x for x in re.split(r"[/\\]+", p) if x]


def resolve_repo_path(p: str) -> Path:
    p = (p or "").strip()
    if not p:
        raise ValueError("The repository path is required")
    if ".." in _parts(p) or "\0" in p:
        raise ValueError("The repository path must not contain '..'")
    root = root_dir()
    path = Path(p)
    if not path.is_absolute():
        path = root / path
    path = Path(os.path.abspath(path))
    if _server_mode():
        real, real_root = Path(os.path.realpath(path)), Path(os.path.realpath(root))
        if real != real_root and real_root not in real.parents:
            raise ValueError(f"In server mode the repository must be inside {root} (FBADMIN_SCHEMA_HISTORY_ROOT)")
    return path


def check_subdir(p: str) -> str:
    p = (p or "").strip().replace("\\", "/").strip("/")
    if not p:
        return ""
    parts = _parts(p)
    if any(x in ("..", ".", ".git") for x in parts) or p.startswith("-") or "\0" in p or ":" in p:
        raise ValueError("Invalid sub-folder: use a relative path without '..'")
    return "/".join(parts)


_URL_CREDS = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@]*:[^/@]*@", re.IGNORECASE)


def check_remote(url: str | None) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("-") or "::" in url or any(c in url for c in "\0\n\r"):
        raise ValueError("Invalid remote URL")
    if _URL_CREDS.match(url):
        raise ValueError("Do not put credentials in the remote URL: use an SSH key, the host's git credential "
                         "helper, or a token given as env:NAME")
    return url


def check_token(token: str | None) -> str | None:
    token = (token or "").strip()
    if not token:
        return None
    if not token.startswith("env:") or not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", token):
        raise ValueError("The token must be a reference to an environment variable: env:NAME")
    return token


def _resolve_token(token: str | None) -> str | None:
    if not token:
        return None
    name = token[4:]
    if name not in os.environ:
        raise GitError(f"Environment variable {name} (the push token, referenced as {token}) is not set")
    return os.environ[name]


def _check_identity(name: str, email: str) -> None:
    for v in (name, email):
        if not v.strip() or any(c in v for c in "<>\n\r\0"):
            raise ValueError("Invalid committer name or e-mail")


# ------------------------------------------------------------------ configuration store
class ConfigStore:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or settings.data_dir)
        self.file = self.dir / "schema_history.json"
        self._lock = threading.RLock()
        self.items: dict[str, HistoryConfig] = {}
        if self.file.exists():
            for d in json.loads(self.file.read_text()).get("repos", []):
                c = HistoryConfig(**d)
                self.items[self.key(c.server_id, c.database_id)] = c

    @staticmethod
    def key(server_id: str, db_id: str) -> str:
        return f"{server_id}/{db_id}"

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"repos": [c.model_dump() for c in self.items.values()]}, indent=1))
        os.replace(tmp, self.file)

    def find(self, server_id: str, db_id: str) -> HistoryConfig | None:
        with self._lock:
            return self.items.get(self.key(server_id, db_id))

    def get(self, server_id: str, db_id: str) -> HistoryConfig:
        c = self.find(server_id, db_id)
        if c is None:
            raise KeyError("Schema history is not configured for this database")
        return c

    def put(self, server_id: str, db_id: str, data: dict[str, Any]) -> HistoryConfig:
        with self._lock:
            cur = self.items.get(self.key(server_id, db_id))
            base = cur.model_dump() if cur else {}
            merged = {**base, **{k: v for k, v in data.items() if v is not None or k in ("remote_url", "token")},
                      "server_id": server_id, "database_id": db_id}
            c = HistoryConfig(**merged)
            resolve_repo_path(c.repo_path)
            c.subdir = check_subdir(c.subdir)
            c.branch = check_branch(c.branch.strip())
            c.remote_url = check_remote(c.remote_url)
            c.token = check_token(c.token)
            _check_identity(c.committer_name, c.committer_email)
            self.items[self.key(server_id, db_id)] = c
            self._save()
            return c

    def remove(self, server_id: str, db_id: str) -> None:
        with self._lock:
            self.get(server_id, db_id)
            del self.items[self.key(server_id, db_id)]
            self._save()

    def record(self, c: HistoryConfig, **changes) -> None:
        with self._lock:
            cur = self.items.get(self.key(c.server_id, c.database_id))
            if cur is not None:
                for k, v in changes.items():
                    setattr(cur, k, v)
                self._save()


_store: ConfigStore | None = None
_repo_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def get_store() -> ConfigStore:
    global _store
    if _store is None or _store.dir != Path(settings.data_dir):
        _store = ConfigStore()
    return _store


def _lock_for(path: Path) -> threading.Lock:
    with _locks_lock:
        return _repo_locks.setdefault(str(path), threading.Lock())


def view(c: HistoryConfig) -> dict[str, Any]:
    d = c.model_dump()
    d["resolved_path"] = str(resolve_repo_path(c.repo_path))
    return d


def status() -> dict[str, Any]:
    try:
        git_binary()
        available, error = True, None
    except GitMissing as e:
        available, error = False, str(e)
    return {"git_available": available, "git_version": git_version() if available else None, "error": error,
            "root": str(root_dir()), "server_mode": _server_mode()}


# ------------------------------------------------------------------ repository helpers
def open_repo(c: HistoryConfig, *, create: bool = False) -> Repo:
    path = resolve_repo_path(c.repo_path)
    token = _resolve_token(c.token) if c.token and c.remote_url else None
    repo = Repo(path, token=token, token_user=c.token_user)
    if not repo.is_repo():
        if not create:
            raise KeyError(f"No git repository at {path} yet: take a snapshot first")
        if path.exists() and not path.is_dir():
            raise ValueError(f"{path} exists and is not a folder")
        repo.init(c.branch)
    return repo


def _pathspec(c: HistoryConfig) -> str:
    return c.subdir or "."


def _repo_path(c: HistoryConfig, rel: str) -> str:
    return f"{c.subdir}/{rel}" if c.subdir else rel


def _strip_sub(c: HistoryConfig, path: str) -> str:
    return path[len(c.subdir) + 1:] if c.subdir and path.startswith(c.subdir + "/") else path


def _write_tree(workdir: Path, files: dict[str, str]) -> None:
    """Write ``files`` below ``workdir``; remove .sql files (and the snapshot file) no longer produced.
    Other files (a README, CI configuration) are left alone."""
    workdir.mkdir(parents=True, exist_ok=True)
    wanted = {workdir / rel for rel in files}
    for p in sorted(workdir.rglob("*"), reverse=True):
        if ".git" in p.relative_to(workdir).parts:
            continue
        if p.is_file() and (p.suffix == ".sql" or p.name == "snapshot.json") and p not in wanted:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    for rel, text in files.items():
        p = workdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)


def collect_snapshots(conn) -> list[tuple[str | None, Any]]:
    """[(None, Snapshot)] for Firebird 3-5; [(schema, Snapshot), ...] for every user schema on Firebird 6."""
    from ..schema.extract import user_schemas
    from ..schema.model import Snapshot
    from ..schema_ctx import current_schema

    schemas = user_schemas(conn)
    if not schemas:
        return [(None, Snapshot(conn))]
    out: list[tuple[str | None, Any]] = []
    cur = conn.cursor()
    cur.execute("SELECT RDB$GET_CONTEXT('SYSTEM', 'SEARCH_PATH') FROM RDB$DATABASE")
    original = cur.fetchone()[0] or "PUBLIC, SYSTEM"
    for n, schema in enumerate(schemas):
        token = current_schema.set(schema)
        try:
            cur.execute(f"SET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM")
            out.append((schema, Snapshot(conn, include_global=n == 0)))
        finally:
            current_schema.reset(token)
            cur.execute(f"SET SEARCH_PATH TO {original}")
    return out


def _author() -> tuple[str, str] | None:
    """The fbadmin user of the current request (server mode)."""
    from ..security.sessions import current_principal

    p = current_principal.get()
    if p is None:
        return None
    email = None
    try:
        from ..api.deps import get_state

        u = get_state().security.db.get_user(p.user_id) or {}
        email = u.get("email")
    except Exception:  # noqa: BLE001
        pass
    clean = re.sub(r"[<>\n\r\0]", "", p.username) or "user"
    return clean, (email if email and "@" in email and not re.search(r"[<>\s]", email) else f"{clean}@fbadmin")


def _message(summary: dict[str, list[str]], db_name: str, trigger: str, actor: str) -> str:
    counts = ", ".join(f"{len(summary[k])} {k}" for k in ("added", "changed", "dropped") if summary[k])
    lines = [f"Schema snapshot of {db_name}: {counts or 'no objects'}", ""]
    for k in ("added", "changed", "dropped"):
        if summary[k]:
            lines.append(f"{k.capitalize()}:")
            lines += [f"  {x}" for x in summary[k]]
    lines += ["", f"Snapshot taken by {actor} ({trigger})"]
    return "\n".join(lines) + "\n"


def _changes(repo: Repo, *pathspec: str) -> list[tuple[str, str]]:
    out = repo.out("diff", "--cached", "--name-status", "--no-renames", "-z", "--", *pathspec)
    parts = out.split("\0")
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


# ------------------------------------------------------------------ snapshot
def snapshot(server_id: str, db_id: str, *, trigger: str = "manual", log=None) -> dict[str, Any]:
    """Extract the schema, write the files and commit when an object changed (then push)."""
    from ..api.deps import get_state
    from ..api.schema import db_conn

    log = log or (lambda _m: None)
    store = get_store()
    c = store.get(server_id, db_id)
    st = get_state()
    db = st.store.get_database(server_id, db_id)
    git_binary()
    author = _author()
    t0 = time.time()
    try:
        log(f"Extracting the schema of {db.name}")
        with db_conn(server_id, db_id) as conn:
            files = layout.build_files(collect_snapshots(conn), db.name)
        repo = open_repo(c, create=True)
        log(f"Writing {len(files)} files to {repo.path}" + (f"/{c.subdir}" if c.subdir else ""))
        with _lock_for(repo.path):
            result = _commit(repo, c, files, db.name, trigger, author, log)
        result["duration_ms"] = round((time.time() - t0) * 1000)
        store.record(c, last_snapshot_at=t0, last_error=None,
                     **({"last_commit": result["commit"]} if result["committed"] else {}),
                     **({"last_push_error": (result["push"] or {}).get("error")} if result["push"] else {}))
        return result
    except Exception as exc:
        store.record(c, last_snapshot_at=t0, last_error=str(exc) or exc.__class__.__name__)
        raise


def _commit(repo: Repo, c: HistoryConfig, files: dict[str, str], db_name: str, trigger: str,
            author: tuple[str, str] | None, log) -> dict[str, Any]:
    workdir = repo.path / c.subdir if c.subdir else repo.path
    _write_tree(workdir, files)
    spec = _pathspec(c)
    repo.run("add", "-A", "--", spec)
    changes = _changes(repo, spec)
    objects = [(s, p) for s, p in changes if layout.describe_path(_strip_sub(c, p))]
    head = repo.head()
    if not objects and head:
        # only derived files (full.sql, snapshot.json) differ, e.g. internal names: keep the last commit
        for s, p in changes:
            if s == "A":
                repo.run("rm", "-q", "--cached", "--", p)
                (repo.path / p).unlink(missing_ok=True)
            else:
                repo.run("reset", "-q", "--", p)
                repo.run("checkout", "-q", "--", p)
        log("No schema changes: nothing to commit")
        return {"committed": False, "commit": head, "changes": {"added": [], "changed": [], "dropped": []},
                "push": None, "files": len(files)}
    if not changes:
        log("No schema changes: nothing to commit")
        return {"committed": False, "commit": head, "changes": {"added": [], "changed": [], "dropped": []},
                "push": None, "files": len(files)}
    summary = layout.change_summary([(s, _strip_sub(c, p)) for s, p in changes])
    actor = author[0] if author else c.committer_name
    msg = _message(summary, db_name, trigger, actor)
    a_name, a_mail = author or (c.committer_name, c.committer_email)
    env = {"GIT_AUTHOR_NAME": a_name, "GIT_AUTHOR_EMAIL": a_mail,
           "GIT_COMMITTER_NAME": c.committer_name, "GIT_COMMITTER_EMAIL": c.committer_email}
    repo.run("commit", "-q", "--no-verify", "-F", "-", "--", spec, env=env, input=msg)
    sha = repo.head()
    log(f"Committed {sha[:10] if sha else '?'}: {msg.splitlines()[0]}")
    push = None
    if c.remote_url and c.push:
        push = push_repo(repo, c, log)
    return {"committed": True, "commit": sha, "message": msg, "changes": summary, "push": push, "files": len(files)}


def push_repo(repo: Repo, c: HistoryConfig, log=None) -> dict[str, Any]:
    log = log or (lambda _m: None)
    try:
        repo.set_remote(c.remote_url)
        out = repo.push(c.branch)
        log(f"Pushed to {c.remote_url} ({c.branch})")
        return {"ok": True, "output": out}
    except GitError as exc:
        log(f"Push failed: {exc}")
        return {"ok": False, "error": str(exc)}


def push_now(server_id: str, db_id: str) -> dict[str, Any]:
    c = get_store().get(server_id, db_id)
    if not c.remote_url:
        raise ValueError("No remote URL is configured")
    repo = open_repo(c)
    with _lock_for(repo.path):
        res = push_repo(repo, c)
    get_store().record(c, last_push_error=res.get("error"))
    return res


# ------------------------------------------------------------------ reading the history
_SEP, _END = "\x1f", "\x1e"


def log_entries(server_id: str, db_id: str, limit: int = 50, skip: int = 0) -> dict[str, Any]:
    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    if not repo.head():
        return {"commits": [], "has_more": False}
    fmt = _END + _SEP.join(["%H", "%an", "%ae", "%at", "%cn", "%s", "%b"]) + _SEP
    out = repo.out("log", f"--format={fmt}", "--name-status", "--no-renames", f"-n{int(limit) + 1}",
                   f"--skip={max(0, int(skip))}", "HEAD", "--", _pathspec(c))
    commits = []
    for chunk in out.split(_END)[1:]:
        fields = chunk.split(_SEP)
        sha, an, ae, at, cn, subject, body, rest = fields[:8]
        changes = []
        for line in rest.strip().splitlines():
            if "\t" in line:
                s, p = line.split("\t", 1)
                changes.append((s, _strip_sub(c, p)))
        commits.append({"sha": sha, "author": an, "author_email": ae, "time": int(at), "committer": cn,
                        "subject": subject, "body": body.strip(), "changes": layout.change_summary(changes)})
    return {"commits": commits[:limit], "has_more": len(commits) > limit}


def _check_rel(path: str) -> str:
    path = (path or "").strip().replace("\\", "/").strip("/")
    if not path or ".." in path.split("/") or path.startswith("-") or ":" in path or "\0" in path:
        raise ValueError(f"Invalid path: {path!r}")
    return path


def commit_diff(server_id: str, db_id: str, sha: str, context: int = 3) -> dict[str, Any]:
    """The per-file unified diffs of one commit (internal files left out)."""
    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    sha = check_sha(sha)
    meta = repo.out("show", "-s", "--format=%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%B", sha).split(_SEP)
    exclude = f":(exclude){_repo_path(c, layout.META_DIR)}"
    text = repo.out("show", "--format=", "--no-renames", "--no-color", f"-U{max(0, min(int(context), 50))}",
                    sha, "--", _pathspec(c), exclude)
    files = []
    for block in re.split(r"(?m)^(?=diff --git )", text):
        if not block.startswith("diff --git "):
            continue
        m = re.search(r"(?m)^\+\+\+ (?:b/(.*)|/dev/null)$", block)
        m2 = re.search(r"(?m)^--- (?:a/(.*)|/dev/null)$", block)
        path = (m.group(1) if m and m.group(1) else None) or (m2.group(1) if m2 and m2.group(1) else None)
        if path is None:
            path = block.split("\n", 1)[0].split(" b/", 1)[-1]
        status = "added" if re.search(r"(?m)^new file mode", block) else \
            "dropped" if re.search(r"(?m)^deleted file mode", block) else "changed"
        hunk = block.find("\n@@")
        rel = _strip_sub(c, path)
        files.append({"path": rel, "status": status, "object": layout.describe_path(rel),
                      "diff": block[hunk + 1:] if hunk >= 0 else ""})
    files.sort(key=lambda f: (f["object"] is None, f["path"]))
    parents = meta[1].split() if len(meta) > 1 else []
    return {"sha": meta[0], "parent": parents[0] if parents else None, "author": meta[2] if len(meta) > 2 else "",
            "author_email": meta[3] if len(meta) > 3 else "", "time": int(meta[4]) if len(meta) > 4 else 0,
            "message": meta[5].strip() if len(meta) > 5 else "", "files": files}


def file_at(server_id: str, db_id: str, sha: str, path: str) -> str:
    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    sha, path = check_sha(sha), _check_rel(path)
    p = repo.run("show", f"{sha}:{_repo_path(c, path)}", check=False)
    if p.returncode != 0:
        raise KeyError(f"{path} does not exist at commit {sha[:10]}")
    return p.stdout


def files_at(server_id: str, db_id: str, sha: str) -> list[dict[str, Any]]:
    """Every schema object file at a commit."""
    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    sha = check_sha(sha)
    out = repo.out("ls-tree", "-r", "--name-only", "-z", sha, "--", _pathspec(c))
    items = []
    for p in out.split("\0"):
        if not p:
            continue
        rel = _strip_sub(c, p)
        d = layout.describe_path(rel)
        if d or rel == "full.sql":
            items.append({"path": rel, "object": d})
    return items


def migration(server_id: str, db_id: str, sha_from: str, sha_to: str) -> dict[str, Any]:
    """A script that changes a database with the schema of ``sha_from`` into the schema of ``sha_to``
    (the schema diff applied to the two stored snapshots; no database connection needed)."""
    from ..schema.diff import compare

    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    snaps = []
    for sha in (check_sha(sha_from), check_sha(sha_to)):
        p = repo.run("show", f"{sha}:{_repo_path(c, layout.SNAPSHOT_FILE)}", check=False)
        if p.returncode != 0:
            raise KeyError(f"Commit {sha[:10]} has no {layout.SNAPSHOT_FILE}")
        snaps.append(layout.snapshots_from_json(p.stdout))
    old, new = snaps
    items: list[dict] = []
    parts: list[str] = []
    warnings: list[str] = []
    schemas = sorted(set(old) | set(new), key=lambda s: (s not in ("", "PUBLIC"), s))
    for schema in schemas:
        ref = new.get(schema) or old.get(schema)
        src = new.get(schema) or layout.empty_snapshot(ref.charset, ref.engine_major)
        tgt = old.get(schema) or layout.empty_snapshot(ref.charset, ref.engine_major)
        res = compare(src, tgt, set_term=True)
        for i in res["items"]:
            items.append({**i, "schema": schema or None})
        warnings += res["warnings"]
        if not res["items"]:
            continue
        head = ""
        if schema:
            if schema not in old:
                head += f"CREATE SCHEMA {quote_ident(schema)};\n"
            head += f"/* ===== Schema {schema} ===== */\nSET SEARCH_PATH TO {quote_ident(schema)}, SYSTEM;\n"
        parts.append(head + res["script"])
        if schema and schema not in new:
            parts.append(f"DROP SCHEMA {quote_ident(schema)};\n")
    header = f"/* Migration from {sha_from[:10]} to {sha_to[:10]}, generated by fbadmin schema history */\n"
    script = header + ("\n".join(parts) if parts else "/* The schemas are identical: nothing to do */\n")
    summary = {k: sum(1 for i in items if i["status"] == k) for k in ("create", "alter", "drop")}
    return {"items": items, "summary": summary, "warnings": warnings, "script": script}


def snapshots_at(server_id: str, db_id: str, ref: str | None = None, *, fetch: bool = False,
                 log=None) -> dict[str, Any]:
    """The schema stored at ``ref`` (default: the configured branch) in a database's history repository:
    ``{"commit", "ref", "subject", "time", "snapshots": {schema ("" before Firebird 6): Snapshot}}``.
    With ``fetch`` the remote is fetched first, so ``origin/<branch>`` is current."""
    log = log or (lambda _m: None)
    c = get_store().get(server_id, db_id)
    repo = open_repo(c)
    ref = check_ref(ref or c.branch)
    if fetch:
        if not c.remote_url:
            raise ValueError("The schema history repository has no remote to fetch from")
        log(f"Fetching {c.remote_url}")
        with _lock_for(repo.path):
            repo.set_remote(c.remote_url)
            repo.fetch()
    sha = repo.resolve(ref)
    p = repo.run("show", f"{sha}:{_repo_path(c, layout.SNAPSHOT_FILE)}", check=False)
    if p.returncode != 0:
        raise KeyError(f"Commit {sha[:10]} ({ref}) has no {layout.SNAPSHOT_FILE}")
    subject, when = (repo.out("log", "-1", "--format=%s%x00%ct", sha).rstrip("\n").split("\0") + ["", "0"])[:2]
    return {"commit": sha, "ref": ref, "subject": subject, "time": int(when or 0),
            "snapshots": layout.snapshots_from_json(p.stdout)}


# ------------------------------------------------------------------ after DDL (background)
DDL = re.compile(r"(?im)^\s*(CREATE|ALTER|DROP|RECREATE|COMMENT\s+ON|GRANT|REVOKE|DECLARE\s+EXTERNAL)\b")
_pending: set[str] = set()
_timers: dict[str, threading.Timer] = {}
_hook_lock = threading.Lock()


def _delay() -> float:
    try:
        return float(os.environ.get("FBADMIN_SCHEMA_HISTORY_DELAY", "5"))
    except ValueError:
        return 5.0


def after_sql(server_id: str | None, db_id: str | None, sql: str | None, transaction_open: bool = False) -> None:
    """Called after a script ran through fbadmin. Cheap: a regex and a dict lookup; the snapshot itself
    runs later in a background thread (debounced) once the DDL is committed. Never raises."""
    try:
        if not server_id or not db_id:
            return
        c = get_store().find(server_id, db_id)
        if c is None or not c.after_ddl:
            return
        key = ConfigStore.key(server_id, db_id)
        with _hook_lock:
            if sql and DDL.search(_strip_sql_comments(sql)):
                _pending.add(key)
            if transaction_open or key not in _pending:
                return
            _pending.discard(key)
            old = _timers.pop(key, None)
            if old:
                old.cancel()
            ctx = contextvars.copy_context()  # keeps the fbadmin user (server mode) for the snapshot

            def fire() -> None:
                with _hook_lock:
                    _timers.pop(key, None)
                ctx.run(_background_snapshot, server_id, db_id)

            t = threading.Timer(_delay(), fire)
            t.daemon = True
            _timers[key] = t
            t.start()
    except Exception:  # noqa: BLE001 - the hook must never break query execution
        pass


def after_commit(server_id: str | None, db_id: str | None) -> None:
    after_sql(server_id, db_id, None, False)


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.S)


def _background_snapshot(server_id: str, db_id: str) -> None:
    try:
        from ..api.deps import get_state

        st = get_state()
        db = st.store.get_database(server_id, db_id)
        st.jobs.start("schema-snapshot", f"Schema snapshot after DDL ({db.name})", server_id,
                      lambda log: _log_result(snapshot(server_id, db_id, trigger="after DDL", log=log), log))
    except Exception:  # noqa: BLE001
        pass


def _log_result(res: dict, log) -> None:
    if res.get("push") and not res["push"].get("ok"):
        raise GitError(f"Committed, but the push failed: {res['push'].get('error')}")


def wait_idle(timeout: float = 30) -> None:
    """Tests: wait until no after-DDL snapshot is scheduled."""
    end = time.time() + timeout
    while time.time() < end:
        with _hook_lock:
            if not _timers:
                return
        time.sleep(0.05)
