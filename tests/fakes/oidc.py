"""A fake OpenID provider: discovery, JWKS, token and userinfo endpoints."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import threading
import time
from urllib.parse import parse_qs

from joserfc import jwt
from joserfc.jwk import RSAKey


class FakeOIDC:
    def __init__(self, client_id="docwizard", client_secret="s3cret"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
        self.codes: dict[str, dict] = {}   # code -> {claims, challenge}
        self.override: dict = {}            # claim changes for the next token
        self.header_override: dict = {}
        self.sign_with = None
        self.userinfo: dict = {}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def issuer(self) -> str:
        return f"http://id.test:{self.port}/realms/main"

    def stop(self):
        self.server.shutdown()

    def rotate_key(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k2"}, private=True)

    def issue(self, code: str, sub: str, nonce: str, challenge: str, **claims) -> None:
        self.codes[code] = {"claims": {"sub": sub, "nonce": nonce, **claims}, "challenge": challenge}

    def _token(self, claims: dict) -> str:
        now = int(time.time())
        body = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300, **claims, **self.override}
        header = {"alg": "RS256", "kid": self.key.kid, **self.header_override}
        return jwt.encode(header, body, self.sign_with or self.key)

    def _handler(self):
        op = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = "/realms/main"
                if self.path == f"{base}/.well-known/openid-configuration":
                    return self.reply(200, {
                        "issuer": op.issuer, "authorization_endpoint": f"{op.issuer}/auth",
                        "token_endpoint": f"{op.issuer}/token", "jwks_uri": f"{op.issuer}/certs",
                        "userinfo_endpoint": f"{op.issuer}/userinfo",
                        "end_session_endpoint": f"{op.issuer}/logout"})
                if self.path == f"{base}/certs":
                    return self.reply(200, {"keys": [op.key.as_dict(private=False)]})
                if self.path == f"{base}/userinfo":
                    return self.reply(200, op.userinfo)
                self.reply(404, {})

            def do_POST(self):
                if self.path != "/realms/main/token":
                    return self.reply(404, {})
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                expected = base64.b64encode(f"{op.client_id}:{op.client_secret}".encode()).decode()
                if self.headers.get("Authorization") != f"Basic {expected}":
                    return self.reply(401, {"error": "invalid_client"})
                grant = op.codes.pop(form.get("code"), None)
                if grant is None:
                    return self.reply(400, {"error": "invalid_grant"})
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                if digest.decode() != grant["challenge"]:
                    return self.reply(400, {"error": "invalid_grant", "error_description": "PKCE"})
                self.reply(200, {"id_token": op._token(grant["claims"]), "access_token": "at", "token_type": "Bearer"})

        return Handler
