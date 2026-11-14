"""Outbound request policy (§15): the only way the app talks to user-supplied hosts.

Each host is resolved once. Private, loopback, link-local, multicast and other
non-global addresses are refused unless an admin listed the host (or the address)
in ``security.allow_private_outbound``. The connection then goes to the validated
address itself, while SNI, certificate verification and the Host header keep the
original name, so a DNS change between check and connect (DNS rebinding) has no
effect. Redirects go through the same check, and timeouts and size limits apply.

Blocking: call it from a thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 50_000_000
MAX_REDIRECTS = 5
USER_AGENT = "DocWizard"


class PolicyError(Exception):
    """The request was refused before anything was sent."""


class FetchError(Exception):
    """The request failed: connection, TLS, timeout or a response over the size limit."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    header_list: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Policy:
    def __init__(self, allow: list[str] | None = None, *, resolver=None):
        self.allow = {a.strip().lower() for a in (allow or []) if a.strip()}
        self.resolver = resolver or _resolve

    def _allowed_private(self, host: str, ip: ipaddress._BaseAddress) -> bool:
        if host.lower() in self.allow or str(ip) in self.allow:
            return True
        for entry in self.allow:
            if "/" in entry:
                try:
                    if ip in ipaddress.ip_network(entry, strict=False):
                        return True
                except ValueError:
                    continue
        return False

    def check(self, host: str, port: int) -> str:
        """Resolve *host* and return one address that the policy allows."""
        if not host:
            raise PolicyError("The URL has no host.")
        addresses = self.resolver(host, port)
        if not addresses:
            raise PolicyError(f"{host} doesn't resolve.")
        for addr in addresses:
            ip = ipaddress.ip_address(addr)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not ip.is_global or ip.is_multicast:
                if not self._allowed_private(host, ip):
                    raise PolicyError(f"{host} resolves to a private address ({ip}). An admin can allow it.")
        # Every address passed: connect to the first one.
        return addresses[0]

    def fetch(self, method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None,
              timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES,
              follow_redirects: bool = True) -> Response:
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._once(method, url, hdrs, body, timeout, max_bytes)
            if not follow_redirects or resp.status not in (301, 302, 303, 307, 308):
                return resp
            location = resp.header("location")
            if not location:
                return resp
            new_url = urljoin(url, location)
            if urlsplit(new_url).netloc.lower() != urlsplit(url).netloc.lower():
                # Credentials never follow a redirect to another host.
                hdrs = {k: v for k, v in hdrs.items() if k.lower() not in ("authorization", "cookie")}
            if resp.status == 303 or (resp.status in (301, 302) and method == "POST"):
                method, body = "GET", None
            url = new_url
        raise FetchError("Too many redirects.")

    def _once(self, method, url, headers, body, timeout, max_bytes) -> Response:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise PolicyError("Only http and https URLs are allowed.")
        if parts.username or parts.password:
            raise PolicyError("Put credentials in the credential fields, not in the URL.")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = self.check(host, port)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if parts.scheme == "https":
            conn = _PinnedHTTPS(host, ip, port, timeout)
        else:
            conn = _PinnedHTTP(host, ip, port, timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            r = conn.getresponse()
            length = r.getheader("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise FetchError("The response is too large.")
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError("The response is too large.")
            header_list = [(k.lower(), v) for k, v in r.getheaders()]
            return Response(r.status, dict(header_list), data, url, header_list)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise FetchError(f"Couldn't reach {host}: {exc}") from exc
        finally:
            conn.close()


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PolicyError(f"{host} doesn't resolve.") from exc
    out = []
    for info in infos:
        addr = info[4][0]
        if addr not in out:
            out.append(addr)
    return out


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to the validated address; SNI and certificate checks use the hostname."""

    def __init__(self, host, ip, port, timeout, context: ssl.SSLContext | None = None):
        super().__init__(host, port, timeout=timeout, context=context or ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
