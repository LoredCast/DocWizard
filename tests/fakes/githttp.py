"""A git server over smart HTTP (git http-backend) with Basic auth, for the git sync tests.

The repository lives in a bare folder; tests change it through an ordinary clone
next to it (``commit``), as someone working with git on their laptop would.
"""

from __future__ import annotations

import base64
import http.server
import os
import secrets
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent",
       "GIT_AUTHOR_NAME": "Laptop", "GIT_AUTHOR_EMAIL": "laptop@example.org",
       "GIT_COMMITTER_NAME": "Laptop", "GIT_COMMITTER_EMAIL": "laptop@example.org"}


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


class GitServer:
    def __init__(self, root: Path, user="jane", branch="main"):
        self.root = Path(root)
        # A new access token for every server, so no credential-like value is written in the tests.
        self.user, self.token, self.branch = user, secrets.token_hex(12), branch
        self.bare = self.root / "repo.git"
        git("init", "--quiet", "--bare", f"--initial-branch={branch}", str(self.bare))
        self.work = self.root / "laptop"
        self.requests: list[tuple[str, str, bool]] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://git.test:{self.port}/repo.git"

    def start(self) -> "GitServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- someone else working with the repository -----------------------------
    def checkout(self) -> Path:
        if not (self.work / ".git").is_dir():
            git("clone", "--quiet", str(self.bare), str(self.work))
        else:
            git("pull", "--quiet", "--ff-only", "origin", self.branch, cwd=self.work)
        return self.work

    def commit(self, files: dict[str, str | None], message="Laptop edit") -> None:
        work = self.checkout()
        git("checkout", "--quiet", "-B", self.branch, cwd=work)
        for path, text in files.items():
            target = work / path
            if text is None:
                git("rm", "--quiet", path, cwd=work)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            git("add", path, cwd=work)
        git("commit", "--quiet", "-m", message, cwd=work)
        git("push", "--quiet", "origin", f"HEAD:refs/heads/{self.branch}", cwd=work)

    def read(self, path: str) -> str | None:
        try:
            return git("show", f"{self.branch}:{path}", cwd=self.bare)
        except subprocess.CalledProcessError:
            return None

    def log(self) -> list[tuple[str, str]]:
        out = git("log", "--format=%an <%ae>%x09%s", self.branch, cwd=self.bare)
        return [tuple(line.split("\t", 1)) for line in out.splitlines()]

    # -- HTTP ---------------------------------------------------------------------
    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                expected = base64.b64encode(f"{server.user}:{server.token}".encode()).decode()
                authed = self.headers.get("Authorization") == f"Basic {expected}"
                server.requests.append((self.command, self.path, authed))
                if not authed:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlsplit(self.path)
                env = {**ENV, "GIT_PROJECT_ROOT": str(server.root), "GIT_HTTP_EXPORT_ALL": "1",
                       "PATH_INFO": url.path, "QUERY_STRING": url.query, "REQUEST_METHOD": self.command,
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                       "REMOTE_USER": server.user, "REMOTE_ADDR": "127.0.0.1",
                       "GATEWAY_INTERFACE": "CGI/1.1", "SERVER_PROTOCOL": "HTTP/1.1"}
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                if self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                proc = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                head, _, payload = proc.stdout.partition(b"\r\n\r\n")
                status, headers = 200, []
                for line in head.decode("latin-1").split("\r\n"):
                    if not line:
                        continue
                    key, _, value = line.partition(":")
                    if key.lower() == "status":
                        status = int(value.strip().split()[0])
                    else:
                        headers.append((key, value.strip()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve

        return Handler
