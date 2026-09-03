"""A dependency-free HTTP service that converts an uploaded Access template.

Design constraints, in the order they mattered:

1. **The upload never touches disk.**  The request body is read into memory,
   converted from those bytes, and released.  There is no temporary file to
   leak, to forget to delete, or to be read by anything else on the host.
2. **Nothing about the customer's database is logged.**  The access log records
   method, path, status, byte count and duration.  Never a file name, never a
   table name, never a fragment of content.
3. **No third-party code runs on the server.**  The whole service is the Python
   standard library, so "what runs on the machine that touches my database" has
   a short and checkable answer.
4. **The converter's own source is never served.**  The browser receives the
   conversion result as JSON and the viewer as static assets; the translator
   stays on the server.

None of this makes uploading a confidential database risk-free.  It makes the
residual risk small, bounded and explainable, which is the most a hosted
service can honestly offer.  For a database that must not leave the building,
the offline command-line converter produces the identical result.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import mimetypes
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..access.translation import translate_access_bytes
from ..ui import build_payload
from ._multipart import MultipartError, parse_multipart

__all__ = ["Config", "build_handler", "create_server", "STATIC_ROOT"]

STATIC_ROOT = Path(__file__).parent / "static"
_VIEWER_ASSETS = Path(__file__).parent.parent / "ui" / "assets"

_SERVER_TOKEN = "access-converter"


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip().isdigit():
        return default
    value = int(raw.strip())
    return value if value > 0 else default


@dataclasses.dataclass(frozen=True)
class Config:
    """Everything the service will refuse, stated once and up front."""

    max_upload_bytes: int = 48 * 1024 * 1024
    requests_per_minute: int = 10
    include_source_text: bool = True
    access_token: str | None = None
    #: Extensions this endpoint will convert.  Binary Jet/ACE databases are
    #: excluded because reading them needs a real file on disk.
    accepted_suffixes: tuple[str, ...] = (".accdt",)

    @classmethod
    def from_environment(cls) -> "Config":
        token = os.environ.get("ACCESS_CONVERTER_TOKEN") or None
        return cls(
            max_upload_bytes=_positive_int("ACCESS_CONVERTER_MAX_UPLOAD_MB", 48)
            * 1024
            * 1024,
            requests_per_minute=_positive_int("ACCESS_CONVERTER_RATE_PER_MINUTE", 10),
            include_source_text=_flag("ACCESS_CONVERTER_INCLUDE_SOURCE", True),
            access_token=token.strip() if token else None,
        )


class _RateLimiter:
    """A fixed-window counter per client, held in memory.

    Deliberately simple: this bounds accidental hammering and trivial abuse.
    It is not a defence against a distributed attacker, and the deployment
    notes say so rather than implying otherwise.
    """

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        if self._per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._hits.get(client, []) if now - t < 60.0]
            if len(recent) >= self._per_minute:
                self._hits[client] = recent
                return False
            recent.append(now)
            self._hits[client] = recent
            if len(self._hits) > 4096:
                # Bound the table: drop clients with nothing in the window.
                self._hits = {
                    key: value
                    for key, value in self._hits.items()
                    if any(now - t < 60.0 for t in value)
                }
            return True


#: ``script-src 'self'`` is the directive that matters here: no inline script
#: exists anywhere in the page, so injected script cannot run even if some
#: string escaped its escaping. Inline *style attributes* are allowed, because
#: the viewer positions gauge bars, screen sketches and diagram shapes from
#: computed values; a style attribute cannot execute code, and forbidding it
#: would buy nothing but a rewrite.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

_STATIC_FILES = {
    "/": (STATIC_ROOT / "index.html", "text/html; charset=utf-8"),
    "/index.html": (STATIC_ROOT / "index.html", "text/html; charset=utf-8"),
    "/assets/upload.js": (STATIC_ROOT / "upload.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": (_VIEWER_ASSETS / "app.js", "text/javascript; charset=utf-8"),
    "/assets/i18n.js": (_VIEWER_ASSETS / "i18n.js", "text/javascript; charset=utf-8"),
    "/assets/app.css": (_VIEWER_ASSETS / "app.css", "text/css; charset=utf-8"),
}


def build_handler(config: Config) -> type[BaseHTTPRequestHandler]:
    """Create the request handler class bound to one configuration."""
    limiter = _RateLimiter(config.requests_per_minute)
    cache = {}

    def static(path: str) -> tuple[bytes, str] | None:
        entry = _STATIC_FILES.get(path)
        if entry is None:
            return None
        if path not in cache:
            file_path, content_type = entry
            cache[path] = (file_path.read_bytes(), content_type)
        return cache[path]

    class Handler(BaseHTTPRequestHandler):
        server_version = _SERVER_TOKEN
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------

        def log_message(self, format: str, *args: Any) -> None:
            """Log the shape of a request and nothing about its contents."""
            # BaseHTTPRequestHandler would log the raw request line, which for
            # this service is harmless, but the override keeps that guarantee
            # explicit rather than incidental.
            return

        def _note(self, status: int, sent: int, started: float) -> None:
            print(
                "%s %s -> %d %dB %.0fms"
                % (
                    self.command,
                    self.path.split("?", 1)[0],
                    status,
                    sent,
                    (time.monotonic() - started) * 1000,
                ),
                flush=True,
            )

        def _send(
            self,
            status: HTTPStatus | int,
            body: bytes,
            content_type: str,
            *,
            cacheable: bool = False,
            extra: dict[str, str] | None = None,
        ) -> int:
            payload = body
            encoding = None
            accepted = (self.headers.get("Accept-Encoding") or "").casefold()
            if "gzip" in accepted and len(body) > 1024:
                payload = gzip.compress(body, 6)
                encoding = "gzip"
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
                self.send_header("Vary", "Accept-Encoding")
            self.send_header(
                "Cache-Control",
                "public, max-age=300" if cacheable else "no-store",
            )
            for key, value in _SECURITY_HEADERS.items():
                self.send_header(key, value)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return len(payload)

        def _error(self, status: HTTPStatus, code: str, message: str) -> int:
            return self._send(
                status,
                json.dumps(
                    {"error": {"code": code, "message": message}}, ensure_ascii=False
                ).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _authorised(self) -> bool:
            if not config.access_token:
                return True
            header = self.headers.get("Authorization") or ""
            if header.startswith("Bearer "):
                supplied = header[len("Bearer ") :].strip()
            else:
                supplied = (self.headers.get("X-Access-Token") or "").strip()
            # Length-independent comparison: the token is a shared secret.
            import hmac

            return hmac.compare_digest(supplied, config.access_token)

        # -- routes -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - required by the base class
            started = time.monotonic()
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                sent = self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
                self._note(200, sent, started)
                return
            if path == "/api/limits":
                body = json.dumps(
                    {
                        "max_upload_bytes": config.max_upload_bytes,
                        "accepted_suffixes": list(config.accepted_suffixes),
                        "requires_token": bool(config.access_token),
                    }
                ).encode("utf-8")
                sent = self._send(
                    HTTPStatus.OK, body, "application/json; charset=utf-8"
                )
                self._note(200, sent, started)
                return
            asset = static(path)
            if asset is None:
                sent = self._error(
                    HTTPStatus.NOT_FOUND, "NOT_FOUND", "no such resource"
                )
                self._note(404, sent, started)
                return
            body, content_type = asset
            sent = self._send(
                HTTPStatus.OK,
                body,
                content_type,
                cacheable=not path.endswith((".html", "/")),
            )
            self._note(200, sent, started)

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802 - required by the base class
            started = time.monotonic()
            if self.path.split("?", 1)[0] != "/api/convert":
                sent = self._error(
                    HTTPStatus.NOT_FOUND, "NOT_FOUND", "no such resource"
                )
                self._note(404, sent, started)
                return
            if not self._authorised():
                sent = self._error(
                    HTTPStatus.UNAUTHORIZED,
                    "UNAUTHORIZED",
                    "a valid access token is required",
                )
                self._note(401, sent, started)
                return
            client = self.client_address[0] if self.client_address else "?"
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                client = forwarded.split(",")[0].strip() or client
            if not limiter.allow(client):
                sent = self._error(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "RATE_LIMITED",
                    "too many conversions from this address; try again shortly",
                )
                self._note(429, sent, started)
                return

            raw_length = self.headers.get("Content-Length")
            if raw_length is None or not raw_length.strip().isdigit():
                sent = self._error(
                    HTTPStatus.LENGTH_REQUIRED,
                    "LENGTH_REQUIRED",
                    "a Content-Length header is required",
                )
                self._note(411, sent, started)
                return
            length = int(raw_length)
            if length > config.max_upload_bytes:
                sent = self._error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "TOO_LARGE",
                    f"the upload limit is {config.max_upload_bytes} bytes",
                )
                self._note(413, sent, started)
                return

            body = self.rfile.read(length)
            if len(body) != length:
                sent = self._error(
                    HTTPStatus.BAD_REQUEST, "TRUNCATED", "the request body was short"
                )
                self._note(400, sent, started)
                return

            try:
                parts = parse_multipart(body, self.headers.get("Content-Type") or "")
            except MultipartError as error:
                sent = self._error(
                    HTTPStatus.BAD_REQUEST, "BAD_MULTIPART", str(error)
                )
                self._note(400, sent, started)
                return
            finally:
                # The body is released as soon as it has been split; only the
                # file part survives into the conversion.
                del body

            upload = next(
                (part for part in parts if part.name == "file" and part.filename),
                None,
            )
            if upload is None:
                sent = self._error(
                    HTTPStatus.BAD_REQUEST,
                    "NO_FILE",
                    "the request contains no file part named 'file'",
                )
                self._note(400, sent, started)
                return
            suffix = Path(upload.filename).suffix.casefold()
            if suffix not in config.accepted_suffixes:
                sent = self._error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "UNSUPPORTED_FORMAT",
                    "this endpoint converts "
                    + ", ".join(config.accepted_suffixes)
                    + " files only",
                )
                self._note(415, sent, started)
                return
            if not upload.content:
                sent = self._error(
                    HTTPStatus.BAD_REQUEST, "EMPTY_FILE", "the uploaded file is empty"
                )
                self._note(400, sent, started)
                return

            try:
                result = translate_access_bytes(
                    upload.content,
                    upload.filename,
                    include_source_text=config.include_source_text,
                )
                payload = build_payload(
                    result, include_source=config.include_source_text
                )
            except Exception as error:  # noqa: BLE001 - the boundary of the service
                # The message is deliberately generic: an exception's text can
                # quote the customer's data, and this response leaves the host.
                sent = self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "CONVERSION_FAILED",
                    "the file could not be converted",
                )
                print(
                    "conversion failed: %s" % type(error).__name__,
                    flush=True,
                )
                self._note(500, sent, started)
                return

            payload["meta"]["served_over_http"] = True
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            sent = self._send(
                HTTPStatus.OK, encoded, "application/json; charset=utf-8"
            )
            self._note(200, sent, started)

    return Handler


def create_server(
    host: str = "0.0.0.0", port: int = 8080, config: Config | None = None
) -> ThreadingHTTPServer:
    """Create the threaded HTTP server, ready to ``serve_forever()``."""
    resolved = config or Config.from_environment()
    server = ThreadingHTTPServer((host, port), build_handler(resolved))
    server.daemon_threads = True
    return server
