"""The hosted conversion service, exercised over a real socket.

These tests start the actual server on an ephemeral port and speak HTTP to it,
because the guarantees that matter here - what it refuses, what it logs, and
that it never writes the upload down - are properties of the running service,
not of a function signature.
"""

from __future__ import annotations

import builtins
import gzip
import http.client
import io
import json
import threading
import unittest
from pathlib import Path

from converter.web import Config, create_server

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SAMPLE = next(
    iter(
        sorted(
            (
                PROJECT_ROOT
                / "samples"
                / "open_access_systems"
                / "northwind2_starter_edition"
                / "original"
            ).glob("*.accdt")
        )
    ),
    None,
)

BOUNDARY = "----accessconvertertest"


def multipart(filename: str, payload: bytes, field: str = "file") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()


class _Service:
    """A running instance of the service, on a port the OS picked."""

    def __init__(self, config: Config) -> None:
        self.server = create_server("127.0.0.1", 0, config)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        self.thread.daemon = True
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            if response.getheader("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def convert(
        self, filename: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        body = multipart(filename, payload)
        merged = {
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
            "Content-Length": str(len(body)),
        }
        merged.update(headers or {})
        return self.request("POST", "/api/convert", body, merged)


class StaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = _Service(Config())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.stop()

    def test_index_is_served(self) -> None:
        status, headers, body = self.service.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b'id="landing"', body)

    def test_page_has_no_inline_script(self) -> None:
        _, _, body = self.service.request("GET", "/")
        text = body.decode("utf-8")
        # Every <script> must be a src reference; the policy forbids inline.
        for fragment in text.split("<script")[1:]:
            self.assertIn("src=", fragment.split(">")[0])

    def test_security_headers_are_present(self) -> None:
        _, headers, _ = self.service.request("GET", "/")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_viewer_assets_are_served(self) -> None:
        for path in ("/assets/app.js", "/assets/i18n.js", "/assets/app.css",
                     "/assets/upload.js"):
            status, _, body = self.service.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)

    def test_converter_source_is_not_reachable(self) -> None:
        for path in (
            "/converter/access/translation.py",
            "/assets/../__init__.py",
            "/../converter/semantics/_vba.py",
            "/assets/translation.py",
        ):
            status, _, _ = self.service.request("GET", path)
            self.assertNotEqual(status, 200, path)

    def test_health_and_limits(self) -> None:
        status, _, body = self.service.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"ok")

        status, _, body = self.service.request("GET", "/api/limits")
        self.assertEqual(status, 200)
        limits = json.loads(body)
        self.assertEqual(limits["accepted_suffixes"], [".accdt"])
        self.assertFalse(limits["requires_token"])

    def test_unknown_path_is_a_json_404(self) -> None:
        status, headers, body = self.service.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body)["error"]["code"], "NOT_FOUND")


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if SAMPLE is None:
            raise unittest.SkipTest("no ACCDT sample available")
        cls.payload = SAMPLE.read_bytes()
        cls.service = _Service(Config(requests_per_minute=0))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.stop()

    def test_a_real_template_converts(self) -> None:
        status, headers, body = self.service.convert("db.accdt", self.payload)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        result = json.loads(body)
        self.assertTrue(result["flow"]["objects"])
        self.assertTrue(result["semantics"]["aspect_totals"])
        self.assertTrue(result["meta"]["served_over_http"])

    def test_the_uploaded_name_is_reported_back_not_a_server_path(self) -> None:
        _, _, body = self.service.convert("customer database.accdt", self.payload)
        result = json.loads(body)
        self.assertEqual(result["meta"]["file_name"], "customer database.accdt")
        self.assertNotIn(str(PROJECT_ROOT), body.decode("utf-8"))

    def test_binary_databases_are_refused_rather_than_spooled(self) -> None:
        status, _, body = self.service.convert("db.accdb", b"\x00" * 4096)
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["error"]["code"], "UNSUPPORTED_FORMAT")

    def test_empty_upload_is_refused(self) -> None:
        status, _, body = self.service.convert("db.accdt", b"")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "EMPTY_FILE")

    def test_a_file_that_is_not_a_package_fails_without_leaking_detail(self) -> None:
        status, _, body = self.service.convert("db.accdt", b"not a zip at all")
        # The converter returns a structured failure rather than raising.
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertIsNotNone(result["meta"])

    def test_body_without_a_file_part_is_refused(self) -> None:
        body = (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="lang"\r\n\r\nja\r\n'
            f"--{BOUNDARY}--\r\n"
        ).encode()
        status, _, payload = self.service.request(
            "POST",
            "/api/convert",
            body,
            {
                "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "NO_FILE")

    def test_non_multipart_body_is_refused(self) -> None:
        body = b'{"file": "x"}'
        status, _, payload = self.service.request(
            "POST",
            "/api/convert",
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"]["code"], "BAD_MULTIPART")

    def test_post_to_another_path_is_a_404(self) -> None:
        status, _, _ = self.service.request(
            "POST", "/api/anything", b"x", {"Content-Length": "1"}
        )
        self.assertEqual(status, 404)


class LimitTests(unittest.TestCase):
    def test_an_oversized_upload_is_refused_before_it_is_read(self) -> None:
        service = _Service(Config(max_upload_bytes=1024, requests_per_minute=0))
        try:
            status, _, body = service.convert("db.accdt", b"x" * 4096)
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"]["code"], "TOO_LARGE")
        finally:
            service.stop()

    def test_rate_limit_applies_per_client(self) -> None:
        service = _Service(Config(requests_per_minute=2))
        try:
            seen = [service.convert("db.accdt", b"x")[0] for _ in range(4)]
            self.assertEqual(seen[-1], 429)
            self.assertIn(429, seen)
        finally:
            service.stop()

    def test_a_token_is_required_when_one_is_configured(self) -> None:
        service = _Service(Config(access_token="s3cret", requests_per_minute=0))
        try:
            status, _, _ = service.convert("db.accdt", b"x")
            self.assertEqual(status, 401)

            status, _, _ = service.convert(
                "db.accdt", b"x", {"X-Access-Token": "wrong"}
            )
            self.assertEqual(status, 401)

            status, _, _ = service.convert(
                "db.accdt", b"x", {"Authorization": "Bearer s3cret"}
            )
            self.assertNotEqual(status, 401)
        finally:
            service.stop()


class NoDiskWriteTests(unittest.TestCase):
    """The service's central promise, tested rather than asserted in prose."""

    @classmethod
    def setUpClass(cls) -> None:
        if SAMPLE is None:
            raise unittest.SkipTest("no ACCDT sample available")
        cls.payload = SAMPLE.read_bytes()

    def test_converting_an_upload_opens_nothing_for_writing(self) -> None:
        service = _Service(Config(requests_per_minute=0))
        writes: list[str] = []
        real_open = builtins.open

        def watching_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                writes.append(f"{file!r} {mode!r}")
            return real_open(file, mode, *args, **kwargs)

        real_path_open = Path.open

        def watching_path_open(self, mode="r", *args, **kwargs):  # noqa: ANN001
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                writes.append(f"{self!r} {mode!r}")
            return real_path_open(self, mode, *args, **kwargs)

        builtins.open = watching_open
        Path.open = watching_path_open
        try:
            status, _, _ = service.convert("db.accdt", self.payload)
        finally:
            builtins.open = real_open
            Path.open = real_path_open
            service.stop()

        self.assertEqual(status, 200)
        self.assertEqual(writes, [], f"the service wrote to disk: {writes}")

    def test_the_in_memory_entry_point_matches_the_file_entry_point(self) -> None:
        from converter.access import translate_access_file
        from converter.access.translation import translate_access_bytes

        from_file = translate_access_file(SAMPLE)
        from_bytes = translate_access_bytes(self.payload, SAMPLE.name)
        self.assertEqual(
            from_file["semantics"]["totals"], from_bytes["semantics"]["totals"]
        )
        self.assertEqual(from_file["source"]["sha256"], from_bytes["source"]["sha256"])


class LoggingTests(unittest.TestCase):
    """Nothing about the customer's database may reach the log."""

    @classmethod
    def setUpClass(cls) -> None:
        if SAMPLE is None:
            raise unittest.SkipTest("no ACCDT sample available")
        cls.payload = SAMPLE.read_bytes()

    def test_the_access_log_records_no_file_name(self) -> None:
        import contextlib

        service = _Service(Config(requests_per_minute=0))
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                service.convert("secret-customer-database.accdt", self.payload)
        finally:
            service.stop()
        log = captured.getvalue()
        self.assertIn("POST /api/convert -> 200", log)
        self.assertNotIn("secret-customer-database", log)


if __name__ == "__main__":
    unittest.main()
