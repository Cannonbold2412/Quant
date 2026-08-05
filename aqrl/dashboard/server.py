"""The whole HTTP layer — stdlib only (Implementation_Plan §15 / UI-UX-Brief
§12: "local web app... server-rendered pages; minimal client JS"). A
single-user, localhost, read-only-except-two-buttons page does not earn a
runtime dependency; this is the same call Stage 10 made for `urllib` over
`httpx`.

One SQLite connection **per request** (`aqrl.db.connect()`) — `sqlite3`
connections are not thread-safe, and WAL (`connection.py`) makes concurrent
readers free, so there is nothing to gain from sharing one.

Routes are a decorator-populated table (`route`), one call per view module at
import time — `views/__init__.py` imports every screen module for exactly its
side effect of registering routes; nothing else in this file needs to know
`decisions.py` exists.

POST/Redirect/GET everywhere a decision control writes (UI-UX-Brief §10.6's
deep-linking intent, extended to writes): every POST handler returns a
303 redirect on success, so refreshing the result page never resubmits a
capital decision.
"""
from __future__ import annotations

import re
import sqlite3
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from ..db import connect
from .html import escape, page

__all__ = ["Response", "connect_request", "redirect", "route", "serve"]


@dataclass
class Response:
    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    location: str | None = None


Handler = Callable[[sqlite3.Connection, "re.Match[str]", dict[str, str]], Response]


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: "re.Pattern[str]"
    func: Handler


ROUTES: list[_Route] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    """Register `func` for `method` on `pattern` (a `re.fullmatch` regex,
    e.g. `r"/health/(?P<uid>[^/]+)"`). Matched groups reach the handler as
    `match.groupdict()`'s equivalent — passed pre-extracted as `params`."""
    compiled = re.compile(pattern)

    def decorator(func: Handler) -> Handler:
        ROUTES.append(_Route(method, compiled, func))
        return func

    return decorator


def redirect(location: str) -> Response:
    """303 See Other — the POST/Redirect/GET response every write handler
    returns on success."""
    return Response(status=303, location=location)


def connect_request() -> sqlite3.Connection:
    return connect()


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length).decode("utf-8") if length else ""
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def _error_page(title: str, message: str) -> str:
    return page(title, f'<h1>{escape(title)}</h1><p class="against">{escape(message)}</p>')


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "AQRL-Dashboard/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature; quiet by default (§9.1)
        pass

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        path = split.path
        # Query-string params (`?group=market`) reach every GET handler this
        # way — the Pipeline screen's grouping toggle (§5.2) depends on it.
        # Route-pattern groups win on a name collision: they are the more
        # specific source (e.g. a `uid` path segment over a same-named query
        # param), so they are applied second, after the query string.
        query_params = {key: values[0] for key, values in parse_qs(split.query).items()}
        for candidate in ROUTES:
            if candidate.method != method:
                continue
            match = candidate.pattern.fullmatch(path)
            if match is None:
                continue
            params = {**query_params, **match.groupdict()}
            conn = connect_request()
            try:
                if method == "POST":
                    params = {**params, **_read_form(self)}
                response = candidate.func(conn, match, params)
            except (ValueError, LookupError, PermissionError) as exc:
                response = Response(status=400, body=_error_page("Request failed", str(exc)))
            except Exception:  # pragma: no cover - defensive, keeps the server up
                traceback.print_exc()
                response = Response(status=500, body=_error_page("Internal error", "see server log"))
            finally:
                conn.close()
            self._send(response)
            return
        self._send(Response(status=404, body=_error_page("Not found", f"no route for {method} {path}")))

    def _send(self, response: Response) -> None:
        body_bytes = response.body.encode("utf-8")
        self.send_response(response.status)
        if response.location:
            self.send_header("Location", response.location)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_bytes)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        self._dispatch("POST")


@route("GET", r"/")
def _root(conn: sqlite3.Connection, match: "re.Match[str]", params: dict[str, str]) -> Response:
    return redirect("/decisions")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Block, serving until interrupted. Imports every screen module first —
    each import's only job is populating `ROUTES` via the `@route` decorator."""
    from . import views  # noqa: F401 - side effect: registers every screen's routes

    server = ThreadingHTTPServer((host, port), _RequestHandler)
    print(f"AQRL dashboard on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
