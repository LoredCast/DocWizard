"""Nextcloud Login Flow v2 (§12.4): an app password without typing the main password here.

1. ``start`` asks the server for a login URL and a poll token.
2. The user opens the login URL, logs in to Nextcloud and grants access.
3. ``poll`` returns the server URL, login name and app password once they did.

Blocking: call from a thread.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from ..netpolicy import Policy


class FlowError(Exception):
    pass


@dataclass
class Flow:
    login_url: str
    endpoint: str
    token: str


@dataclass
class Credentials:
    server: str
    login_name: str
    app_password: str


def normalize_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    for suffix in ("/index.php", "/remote.php/webdav", "/remote.php/dav"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FlowError("Enter the address of your Nextcloud, for example https://cloud.example.org.")
    return url


def start(policy: Policy, server: str) -> Flow:
    server = normalize_server(server)
    resp = policy.fetch("POST", f"{server}/index.php/login/v2", headers={"Accept": "application/json"},
                        body=b"", timeout=15, max_bytes=100_000)
    if resp.status != 200:
        raise FlowError(f"{server} doesn't look like a Nextcloud (HTTP {resp.status}).")
    try:
        data = json.loads(resp.body)
        flow = Flow(data["login"], data["poll"]["endpoint"], data["poll"]["token"])
    except (ValueError, KeyError, TypeError):
        raise FlowError(f"{server} answered the login request in an unexpected way.") from None
    # The poll endpoint must belong to the same server: it receives the token.
    if urlsplit(flow.endpoint).netloc.lower() != urlsplit(server).netloc.lower():
        raise FlowError("The Nextcloud server sent a login endpoint on another host.")
    return flow


def poll(policy: Policy, flow: Flow) -> Credentials | None:
    """None while the user hasn't granted access yet."""
    resp = policy.fetch("POST", flow.endpoint, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                        "Accept": "application/json"},
                        body=urlencode({"token": flow.token}).encode(), timeout=15, max_bytes=100_000)
    if resp.status == 404:
        return None
    if resp.status != 200:
        raise FlowError(f"Nextcloud answered HTTP {resp.status}.")
    try:
        data = json.loads(resp.body)
        return Credentials(data["server"].rstrip("/"), data["loginName"], data["appPassword"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise FlowError("Nextcloud answered the login in an unexpected way.") from None


def dav_root(server: str, login_name: str) -> str:
    return f"{server.rstrip('/')}/remote.php/dav/files/{quote(login_name, safe='')}"
