"""OpenID Connect login (§13.4): authorization code flow with PKCE, state and nonce.

DocWizard is a confidential client. Discovery and the JWKS are cached; the JWKS is
fetched again when a token names an unknown key. Users are matched by
``(issuer, sub)``, never by email alone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from ..netpolicy import FetchError, PolicyError
from . import jwt

DISCOVERY_TTL = 3600
FLOW_TTL = 600


class OIDCError(Exception):
    pass


@dataclass
class Pending:
    provider: str
    state: str
    nonce: str
    verifier: str
    next: str
    link_user: int | None
    created: float = field(default_factory=time.time)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = _b64(secrets.token_bytes(32))
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


class Providers:
    def __init__(self, app):
        self.app = app
        self.by_id = {p.id: p for p in app.cfg.oidc}
        self._discovery: dict[str, tuple[float, dict]] = {}
        self._jwks: dict[str, dict] = {}
        self._jwks_at: dict[str, float] = {}
        self.pending: dict[str, Pending] = {}

    def list(self):
        return list(self.by_id.values())

    def get(self, provider_id: str):
        provider = self.by_id.get(provider_id)
        if provider is None:
            raise OIDCError("Unknown login provider.")
        return provider

    def redirect_uri(self, provider) -> str:
        return f"{self.app.cfg.server.public_url.rstrip('/')}/login/oidc/{provider.id}/callback"

    # -- discovery and keys -------------------------------------------------
    async def _get_json(self, url: str) -> dict:
        try:
            resp = await asyncio.to_thread(self.app.net.fetch, "GET", url, headers={"Accept": "application/json"},
                                           timeout=15, max_bytes=1_000_000)
        except (FetchError, PolicyError) as exc:
            raise OIDCError(str(exc)) from None
        if resp.status != 200:
            raise OIDCError(f"The login provider answered HTTP {resp.status}.")
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise OIDCError("The login provider sent JSON that can't be read.") from None
        if not isinstance(data, dict):
            raise OIDCError("The login provider sent unexpected JSON.")
        return data

    async def discovery(self, provider) -> dict:
        cached = self._discovery.get(provider.id)
        if cached and time.time() - cached[0] < DISCOVERY_TTL:
            return cached[1]
        data = await self._get_json(provider.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if data.get("issuer") != provider.issuer:
            raise OIDCError("The provider's discovery document names another issuer.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(key), str) or urlsplit(data[key]).scheme not in ("https", "http"):
                raise OIDCError(f"The provider's discovery document has no {key}.")
        self._discovery[provider.id] = (time.time(), data)
        return data

    async def jwks(self, provider, refresh: bool = False) -> dict:
        # Unknown key ids trigger a refresh, at most once a minute per provider.
        stale = refresh and time.time() - self._jwks_at.get(provider.id, 0) > 60
        if stale or provider.id not in self._jwks:
            disc = await self.discovery(provider)
            self._jwks[provider.id] = await self._get_json(disc["jwks_uri"])
            self._jwks_at[provider.id] = time.time()
        return self._jwks[provider.id]

    # -- the flow -------------------------------------------------------------
    async def start(self, provider_id: str, next_url: str, link_user: int | None = None) -> tuple[str, Pending]:
        provider = self.get(provider_id)
        disc = await self.discovery(provider)
        now = time.time()
        for key in [k for k, p in self.pending.items() if now - p.created > FLOW_TTL]:
            del self.pending[key]
        verifier, challenge = pkce_pair()
        pending = Pending(provider.id, secrets.token_urlsafe(24), secrets.token_urlsafe(24), verifier, next_url,
                          link_user)
        self.pending[pending.state] = pending
        params = {"response_type": "code", "client_id": provider.client_id, "redirect_uri": self.redirect_uri(provider),
                  "scope": " ".join(provider.scopes), "state": pending.state, "nonce": pending.nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        sep = "&" if "?" in disc["authorization_endpoint"] else "?"
        return disc["authorization_endpoint"] + sep + urlencode(params), pending

    async def finish(self, provider_id: str, state: str, code: str) -> tuple[Pending, dict]:
        """Exchange the code and verify the ID token. Returns (pending login, claims)."""
        pending = self.pending.pop(state, None)
        if pending is None or pending.provider != provider_id or time.time() - pending.created > FLOW_TTL:
            raise OIDCError("This login expired or didn't start here. Try again.")
        provider = self.get(provider_id)
        disc = await self.discovery(provider)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri(provider),
                "code_verifier": pending.verifier}
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if provider.auth_method == "client_secret_post":
            form.update(client_id=provider.client_id, client_secret=provider.client_secret)
        else:
            from urllib.parse import quote

            basic = f"{quote(provider.client_id, safe='')}:{quote(provider.client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
        try:
            resp = await asyncio.to_thread(self.app.net.fetch, "POST", disc["token_endpoint"], headers=headers,
                                           body=urlencode(form).encode(), timeout=15, max_bytes=1_000_000)
        except (FetchError, PolicyError) as exc:
            raise OIDCError(str(exc)) from None
        if resp.status != 200:
            raise OIDCError(f"The login provider refused the login (HTTP {resp.status}).")
        try:
            tokens = json.loads(resp.body)
            id_token = tokens["id_token"]
        except (ValueError, KeyError, TypeError):
            raise OIDCError("The login provider sent no ID token.") from None
        try:
            claims = jwt.verify(id_token, await self.jwks(provider), issuer=provider.issuer,
                                client_id=provider.client_id, nonce=pending.nonce)
        except jwt.UnknownKey:
            try:
                claims = jwt.verify(id_token, await self.jwks(provider, refresh=True), issuer=provider.issuer,
                                    client_id=provider.client_id, nonce=pending.nonce)
            except jwt.TokenError as exc:
                raise OIDCError(str(exc)) from None
        except jwt.TokenError as exc:
            raise OIDCError(str(exc)) from None
        # Groups often come only from the userinfo endpoint.
        if provider.groups_claim not in claims and (provider.allowed_groups or provider.admin_groups) \
                and disc.get("userinfo_endpoint") and isinstance(tokens.get("access_token"), str):
            try:
                info = await asyncio.to_thread(
                    self.app.net.fetch, "GET", disc["userinfo_endpoint"],
                    headers={"Authorization": f"Bearer {tokens['access_token']}", "Accept": "application/json"},
                    timeout=15, max_bytes=1_000_000)
                data = json.loads(info.body) if info.status == 200 else {}
                if isinstance(data, dict) and data.get("sub") == claims["sub"]:
                    claims = {**data, **claims}
            except (FetchError, PolicyError, ValueError):
                pass
        return pending, claims

    async def logout_url(self, provider_id: str) -> str | None:
        provider = self.by_id.get(provider_id)
        if provider is None:
            return None
        try:
            disc = await self.discovery(provider)
        except OIDCError:
            return None
        end = disc.get("end_session_endpoint")
        if not isinstance(end, str):
            return None
        params = {"client_id": provider.client_id,
                  "post_logout_redirect_uri": self.app.cfg.server.public_url.rstrip("/") + "/login"}
        return end + ("&" if "?" in end else "?") + urlencode(params)


def groups_of(provider, claims: dict) -> set[str]:
    value = claims.get(provider.groups_claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {g for g in value if isinstance(g, str)}
    return set()
