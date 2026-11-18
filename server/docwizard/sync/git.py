"""Sync with a git repository over HTTPS (§12.7).

DocWizard keeps a clone of one branch in ``<data_dir>/sync-git/<target id>``. A run:

1. fetches the branch and resets the clone to it (``begin``);
2. syncs the project with the clone's files exactly like a plain folder (§12.6),
   so changes on both sides are reconciled with conflicts as in §12.3;
3. commits whatever DocWizard wrote and pushes it (``finish``). If someone pushed
   in the meantime, the commit is rebased onto theirs and pushed again. If that
   fails too, the run fails and its bases are rolled back, so the next run
   reconciles both sides again and nothing is lost.

git runs without hooks, system or user configuration, symlinks, submodules or a
terminal. Only https:// (and http:// for hosts an admin allowed) remotes are
accepted; the host is checked with the outbound request policy (§15) and git is
pinned to the address that passed, so a DNS change can't redirect it. The token
reaches git through its environment, never its command line.

Blocking: call from a thread.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from ..netpolicy import Policy, PolicyError
from .folder import FolderRemote

TIMEOUT = 180
_BRANCH_FORBIDDEN = (" ", "~", "^", ":", "?", "*", "[", "\\", "..", "@{", "//")


class GitError(Exception):
    pass


def available() -> bool:
    return shutil.which("git") is not None


def check_url(url: str) -> str:
    """Normalize a remote URL; raises ValueError for anything but http(s) without credentials."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Enter the repository's https:// address, as shown under Clone on GitHub, GitLab or Gitea.")
    if parts.username or parts.password:
        raise ValueError("Leave the username and token out of the address and enter them below.")
    if parts.query or parts.fragment:
        raise ValueError("The repository address can't have a ? or # part.")
    return url


def check_branch(branch: str) -> str:
    branch = (branch or "").strip() or "main"
    if (len(branch) > 200 or branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".", ".lock"))
            or any(s in branch for s in _BRANCH_FORBIDDEN) or any(ord(c) < 32 or ord(c) == 127 for c in branch)):
        raise ValueError("That isn't a valid branch name.")
    return branch


class GitRemote(FolderRemote):
    label = "Git"

    def __init__(self, policy: Policy, workdir: Path, url: str, branch: str, *, username: str = "",
                 token: str = "", author: tuple[str, str] = ("DocWizard", "docwizard@localhost"),
                 timeout: float = TIMEOUT):
        super().__init__(workdir, "")
        self.policy = policy
        self.workdir = Path(workdir)
        self.url = check_url(url)
        self.branch = check_branch(branch)
        self.username = username
        self.token = token
        self.author = author
        self.timeout = timeout
        self.fresh = False      # True when begin() made a new clone: the bases no longer apply
        self.has_remote_branch = False

    # -- running git ----------------------------------------------------------
    def _env(self) -> dict[str, str]:
        parts = urlsplit(self.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            ip = self.policy.check(parts.hostname, port)
        except PolicyError as exc:
            raise GitError(str(exc)) from None
        pinned = f"[{ip}]" if ":" in ip else ip
        config = [
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            ("protocol.http.allow", "always" if parts.scheme == "http" else "never"),
            ("http.followRedirects", "false"),
            ("http.curloptResolve", f"{parts.hostname}:{port}:{pinned}"),
            ("http.lowSpeedLimit", "1000"),
            ("http.lowSpeedTime", "30"),
            ("core.hooksPath", os.devnull),
            ("core.symlinks", "false"),
            ("core.fsmonitor", "false"),
            ("core.autocrlf", "false"),
            ("submodule.recurse", "false"),
            ("credential.helper", ""),
            ("init.defaultBranch", self.branch),
            ("advice.detachedHead", "false"),
            ("gc.auto", "0"),
        ]
        if self.username or self.token:
            pair = f"{self.username or 'git'}:{self.token}".encode()
            config.append(("http.extraHeader", "Authorization: Basic " + base64.b64encode(pair).decode()))
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.workdir.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_AUTHOR_NAME": self.author[0], "GIT_AUTHOR_EMAIL": self.author[1],
            "GIT_COMMITTER_NAME": self.author[0], "GIT_COMMITTER_EMAIL": self.author[1],
            "GIT_CONFIG_COUNT": str(len(config)),
            "LC_ALL": "C",
        }
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
                    "SSL_CERT_FILE", "GIT_SSL_CAINFO"):
            if key in os.environ:
                env[key] = os.environ[key]
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
        return env

    def _git(self, *args: str, env: dict[str, str], cwd: Path | None = None, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(["git", *args], cwd=cwd or self.workdir, env=env, capture_output=True,
                                  text=True, timeout=timeout or self.timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise GitError("git isn't installed on this server.") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} took too long.") from None
        if check and proc.returncode != 0:
            raise GitError(self._explain(args[0], proc.stderr))
        return proc

    def _explain(self, command: str, stderr: str) -> str:
        text = stderr.replace(self.token, "***") if self.token else stderr
        low = text.lower()
        if "authentication failed" in low or "401" in low or "403" in low or "could not read username" in low:
            return "The repository didn't accept the username and token. The token needs read and write access."
        if "not found" in low and command in ("clone", "fetch", "ls-remote"):
            return "The repository doesn't exist, or the token can't see it."
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("hint:")]
        return f"git {command} failed: {lines[-1] if lines else 'no details'}"[:400]

    # -- checking a new target ---------------------------------------------------
    def probe(self) -> bool:
        """Check the address and credentials; returns whether the branch exists yet."""
        self.workdir.parent.mkdir(parents=True, exist_ok=True)
        env = self._env()
        out = self._git("ls-remote", "--heads", "--", self.url, f"refs/heads/{self.branch}", env=env,
                        cwd=self.workdir.parent, timeout=60)
        return bool(out.stdout.strip())

    # -- one run -------------------------------------------------------------------
    def begin(self) -> None:
        env = self._env()
        if not (self.workdir / ".git").is_dir():
            if self.workdir.exists():
                shutil.rmtree(self.workdir)
            self.workdir.mkdir(parents=True)
            self._git("init", "--quiet", env=env)
            self._git("remote", "add", "origin", self.url, env=env)
            self.fresh = True
        else:
            self._git("remote", "set-url", "origin", self.url, env=env)
        fetched = self._git("fetch", "--quiet", "--depth", "1", "--no-tags", "origin",
                            f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", env=env, check=False)
        if fetched.returncode == 0:
            self.has_remote_branch = True
            self._git("checkout", "--quiet", "--force", "-B", self.branch, f"origin/{self.branch}", env=env)
            self._git("reset", "--quiet", "--hard", f"origin/{self.branch}", env=env)
        elif "couldn't find remote ref" in fetched.stderr.lower() or "could not find remote ref" in fetched.stderr.lower():
            # An empty repository or a new branch: the first push creates it.
            self.has_remote_branch = False
            head = self._git("rev-parse", "--verify", "--quiet", "HEAD", env=env, check=False)
            if head.returncode == 0:
                self._git("checkout", "--quiet", "--orphan", f"dw-new-{os.getpid()}", env=env)
                self._git("branch", "--quiet", "-D", self.branch, env=env, check=False)
                self._git("checkout", "--quiet", "--orphan", self.branch, env=env)
                self._git("rm", "-r", "--quiet", "--cached", "--ignore-unmatch", ".", env=env, check=False)
        else:
            raise GitError(self._explain("fetch", fetched.stderr))
        # Leftovers of a failed run; files the repository ignores stay, as they were never pushed.
        self._git("clean", "--quiet", "-fd", env=env)

    def finish(self, message: str) -> bool:
        """Commit and push what this run wrote; returns whether there was anything to push."""
        env = self._env()
        self._git("add", "--all", env=env)
        staged = self._git("diff", "--cached", "--quiet", env=env, check=False)
        if staged.returncode == 0:
            return False
        self._git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message, env=env)
        for attempt in range(3):
            pushed = self._git("push", "--quiet", "--porcelain", "origin", f"HEAD:refs/heads/{self.branch}",
                               env=env, check=False)
            if pushed.returncode == 0:
                self._git("update-ref", f"refs/remotes/origin/{self.branch}", "HEAD", env=env)
                self.has_remote_branch = True
                return True
            rejected = "rejected" in pushed.stdout + pushed.stderr or "fetch first" in pushed.stderr
            if not rejected or not self.has_remote_branch or attempt == 2:
                raise GitError(self._explain("push", pushed.stderr or pushed.stdout))
            # Someone pushed meanwhile: put this commit on top of theirs.
            self._git("fetch", "--quiet", "--depth", "1", "--no-tags", "origin",
                      f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}", env=env)
            rebased = self._git("rebase", "--quiet", "--onto", f"origin/{self.branch}", "HEAD~1", env=env,
                                check=False)
            if rebased.returncode != 0:
                self._git("rebase", "--abort", env=env, check=False)
                raise GitError("Someone pushed changes to the same lines meanwhile. The next sync merges them.")
        return False
