"""A thin wrapper around the ``git`` command-line tool (no Python git dependency).

Every call runs ``git -C <repo>`` with prompts disabled (``GIT_TERMINAL_PROMPT=0``, no controlling
terminal, empty stdin), commit signing off and a C locale. An HTTPS token for pushing is passed as an ``http.extraHeader`` through the
``GIT_CONFIG_*`` environment variables, never on the command line, in the repository config or in a
URL; it is also scrubbed from any error message.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 60
PUSH_TIMEOUT = 180
SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,100}(?<![./])$")
REF = BRANCH  # branches, tags, origin/<branch> and commit ids share the same safe characters


class GitError(ValueError):
    """A git command failed (shown to the user as a 400 with git's message)."""


class GitMissing(GitError):
    pass


def git_binary() -> str:
    exe = os.environ.get("FBADMIN_GIT") or shutil.which("git")
    if not exe or not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        raise GitMissing("git is not installed on the fbadmin host (or not on its PATH). Install git to use "
                         "schema history.")
    return exe


def git_version() -> str | None:
    try:
        out = subprocess.run([git_binary(), "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().removeprefix("git version ").strip() or None
    except (GitError, OSError, subprocess.SubprocessError):
        return None


def check_branch(name: str) -> str:
    if not BRANCH.match(name or "") or name.endswith(".lock"):
        raise ValueError(f"Invalid branch name: {name!r}")
    return name


def check_ref(ref: str) -> str:
    """A branch, tag, remote branch (``origin/main``) or commit id; no options or revision syntax."""
    if not REF.match(ref or "") or ".." in ref or ref.endswith((".lock", "/")) or "//" in ref:
        raise ValueError(f"Invalid git reference: {ref!r}")
    return ref


def check_sha(sha: str) -> str:
    if not SHA.match(sha or ""):
        raise ValueError(f"Invalid commit id: {sha!r}")
    return sha.lower()


class Repo:
    def __init__(self, path: Path, *, token: str | None = None, token_user: str = "x-access-token"):
        self.path = Path(path)
        self._token = token or None
        self._token_user = token_user or "x-access-token"

    # ------------------------------------------------------------------ plumbing
    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C", "GIT_PAGER": "cat",
                    "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
        if extra:
            env.update(extra)
        return env

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "********")
            text = text.replace(self._auth_header_value(), "Basic ********")
        return text

    def _auth_header_value(self) -> str:
        raw = f"{self._token_user}:{self._token}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def run(self, *args: str, check: bool = True, timeout: float = TIMEOUT, auth: bool = False,
            env: dict[str, str] | None = None, input: str | None = None) -> subprocess.CompletedProcess:
        extra = dict(env or {})
        if auth and self._token:
            n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
            extra.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "http.extraHeader",
                          f"GIT_CONFIG_VALUE_{n}": "Authorization: " + self._auth_header_value()})
        cmd = [git_binary(), "-C", str(self.path), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
               "-c", "core.quotepath=off", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args]
        try:
            # no terminal: a new session without a controlling tty, so ssh or a credential prompt
            # fails at once instead of waiting for input
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, env=self._env(extra), input=input if input is not None else "",
                               start_new_session=True)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {int(timeout)} s") from exc
        except OSError as exc:
            raise GitMissing(f"Cannot run git: {exc}") from exc
        if check and p.returncode != 0:
            msg = self._scrub((p.stderr or p.stdout or "").strip()) or f"exit code {p.returncode}"
            raise GitError(f"git {args[0]} failed: {msg}")
        return p

    def out(self, *args: str, **kw) -> str:
        return self.run(*args, **kw).stdout

    # ------------------------------------------------------------------ operations
    def is_repo(self) -> bool:
        if not (self.path / ".git").exists():
            return False
        p = self.run("rev-parse", "--is-inside-work-tree", check=False)
        return p.returncode == 0 and p.stdout.strip() == "true"

    def init(self, branch: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.run("init", "-q")
        # works with every git version (init -b needs 2.28)
        self.run("symbolic-ref", "HEAD", f"refs/heads/{check_branch(branch)}")

    def head(self) -> str | None:
        p = self.run("rev-parse", "--verify", "-q", "HEAD", check=False)
        return p.stdout.strip() or None if p.returncode == 0 else None

    def current_branch(self) -> str | None:
        p = self.run("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return p.stdout.strip() or None

    def set_remote(self, url: str | None) -> None:
        has = self.run("remote", check=False).stdout.split()
        if url:
            self.run("remote", "set-url" if "origin" in has else "add", "origin", url)
        elif "origin" in has:
            self.run("remote", "remove", "origin")

    def resolve(self, ref: str) -> str:
        """The commit id ``ref`` points to."""
        p = self.run("rev-parse", "--verify", "-q", f"{check_ref(ref)}^{{commit}}", check=False)
        sha = p.stdout.strip()
        if p.returncode != 0 or not sha:
            raise KeyError(f"No commit {ref!r} in the schema history repository")
        return sha

    def fetch(self) -> str:
        p = self.run("fetch", "--quiet", "--prune", "origin", auth=True, timeout=PUSH_TIMEOUT)
        return self._scrub((p.stdout + p.stderr).strip())

    def push(self, branch: str) -> str:
        p = self.run("push", "--porcelain", "origin", f"HEAD:refs/heads/{check_branch(branch)}", auth=True,
                     timeout=PUSH_TIMEOUT)
        return self._scrub((p.stdout + p.stderr).strip())
