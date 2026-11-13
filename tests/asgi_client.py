"""A small ASGI test client (no extra dependencies)."""

from __future__ import annotations

import json as _json
from http.cookies import SimpleCookie
from urllib.parse import urlencode, urlsplit


class Result:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        for k, v in self.headers:
            if k == name.lower():
                return v
        return None

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return _json.loads(self.body)


class Client:
    def __init__(self, app, origin="http://localhost:8080"):
        self.app = app
        self.origin = origin
        self.cookies: dict[str, str] = {}
        self.csrf = None

    async def request(self, method, url, *, body=b"", json=None, form=None, headers=None, origin=None):
        parts = urlsplit(url)
        origin = origin or self.origin
        host = urlsplit(origin).netloc
        hdrs = {"host": host}
        if json is not None:
            body = _json.dumps(json).encode()
            hdrs["content-type"] = "application/json"
        if form is not None:
            body = urlencode(form).encode()
            hdrs["content-type"] = "application/x-www-form-urlencoded"
        if method not in ("GET", "HEAD"):
            hdrs["origin"] = origin
            if self.csrf:
                hdrs["x-csrf-token"] = self.csrf
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update(headers or {})
        scope = {
            "type": "http", "method": method, "path": parts.path, "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(), "scheme": urlsplit(origin).scheme,
            "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()], "client": ("127.0.0.1", 1234),
        }
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        status, headers, chunks = None, [], []

        async def send(message):
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k.decode(), v.decode()) for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        for k, v in headers:
            if k == "set-cookie":
                c = SimpleCookie()
                c.load(v)
                for name, morsel in c.items():
                    if morsel["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = morsel.value
        return Result(status, headers, b"".join(chunks))

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)

    async def login(self, username, password):
        r = await self.post("/login", form={"username": username, "password": password, "next": "/"})
        assert r.status == 303, r.text
        me = await self.get("/api/me")
        self.csrf = me.json()["csrf"]
        return me.json()


class WebSocket:
    """Drive the app's websocket handler in-process."""

    def __init__(self, app, client: Client, origin=None):
        import asyncio

        self.app = app
        self.client = client
        self.origin = origin or client.origin
        self.to_app: asyncio.Queue = asyncio.Queue()
        self.from_app: asyncio.Queue = asyncio.Queue()
        self.task = None
        self.closed = None

    async def connect(self):
        import asyncio

        hdrs = {"host": urlsplit(self.client.origin).netloc, "origin": self.origin}
        if self.client.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items())
        scope = {"type": "websocket", "path": "/ws", "raw_path": b"/ws", "query_string": b"",
                 "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()], "client": ("127.0.0.1", 1),
                 "scheme": "ws"}
        await self.to_app.put({"type": "websocket.connect"})

        async def receive():
            return await self.to_app.get()

        async def send(message):
            await self.from_app.put(message)

        self.task = asyncio.create_task(self.app(scope, receive, send))
        first = await asyncio.wait_for(self.from_app.get(), 5)
        if first["type"] == "websocket.close":
            self.closed = first.get("code")
            return False
        assert first["type"] == "websocket.accept"
        return True

    async def send(self, msg):
        await self.to_app.put({"type": "websocket.receive", "text": _json.dumps(msg)})

    async def recv(self, timeout=5):
        import asyncio

        message = await asyncio.wait_for(self.from_app.get(), timeout)
        if message["type"] == "websocket.close":
            self.closed = message.get("code")
            return None
        return _json.loads(message["text"])

    async def recv_until(self, t, timeout=5):
        while True:
            msg = await self.recv(timeout)
            if msg is None or msg.get("t") == t:
                return msg

    async def close(self):
        import asyncio

        await self.to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self.task:
            await asyncio.wait_for(self.task, 5)
