"""Method and path routing.

Patterns use ``{name}`` for one path segment and ``{name:path}`` for the rest
of the path, e.g. ``/api/projects/{id}/assets/{path:path}``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .request import HTTPError, Request
from .response import Response

Handler = Callable[[Request], Awaitable[Response]]


@dataclass
class Route:
    method: str
    pattern: str
    regex: re.Pattern
    handler: Handler
    options: dict = field(default_factory=dict)


def compile_pattern(pattern: str) -> re.Pattern:
    out, pos = ["^"], 0
    for m in re.finditer(r"\{(\w+)(?::(path))?\}", pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>.+)" if m.group(2) else f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    out.append("$")
    return re.compile("".join(out))


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, **options) -> None:
        for m in method.split(","):
            self.routes.append(Route(m.strip().upper(), pattern, compile_pattern(pattern), handler, options))

    def route(self, method: str, pattern: str, **options):
        def deco(fn: Handler) -> Handler:
            self.add(method, pattern, fn, **options)
            return fn
        return deco

    def match(self, request: Request) -> tuple[Route, dict[str, str]]:
        allowed = set()
        for route in self.routes:
            m = route.regex.match(request.path)
            if not m:
                continue
            if route.method == request.method or (route.method == "GET" and request.method == "HEAD"):
                return route, m.groupdict()
            allowed.add(route.method)
        if allowed:
            raise HTTPError(405, "method_not_allowed", headers=[("allow", ", ".join(sorted(allowed)))])
        raise HTTPError(404, "not_found", "This page doesn't exist.")
