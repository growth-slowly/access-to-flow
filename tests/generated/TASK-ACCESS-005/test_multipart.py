"""The bounded multipart reader."""

from __future__ import annotations

import unittest

from converter.web._multipart import MultipartError, boundary_of, parse_multipart


def body(boundary: str, *parts: bytes) -> bytes:
    chunks = []
    for part in parts:
        chunks.append(b"--" + boundary.encode() + b"\r\n" + part + b"\r\n")
    chunks.append(b"--" + boundary.encode() + b"--\r\n")
    return b"".join(chunks)


FILE_PART = (
    b'Content-Disposition: form-data; name="file"; filename="db.accdt"\r\n'
    b"Content-Type: application/octet-stream\r\n\r\nPK\x03\x04payload"
)
FIELD_PART = b'Content-Disposition: form-data; name="lang"\r\n\r\nja'


class BoundaryTests(unittest.TestCase):
    def test_unquoted_boundary(self) -> None:
        self.assertEqual(boundary_of("multipart/form-data; boundary=abc123"), b"abc123")

    def test_quoted_boundary(self) -> None:
        self.assertEqual(
            boundary_of('multipart/form-data; boundary="a b c"'), b"a b c"
        )

    def test_other_content_types_are_refused(self) -> None:
        with self.assertRaises(MultipartError):
            boundary_of("application/json")

    def test_missing_boundary_is_refused(self) -> None:
        with self.assertRaises(MultipartError):
            boundary_of("multipart/form-data")


class ParseTests(unittest.TestCase):
    def test_file_and_field_parts_are_separated(self) -> None:
        parts = parse_multipart(
            body("X", FILE_PART, FIELD_PART), "multipart/form-data; boundary=X"
        )
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].name, "file")
        self.assertEqual(parts[0].filename, "db.accdt")
        self.assertEqual(parts[0].content, b"PK\x03\x04payload")
        self.assertEqual(parts[1].name, "lang")
        self.assertIsNone(parts[1].filename)

    def test_binary_content_survives_intact(self) -> None:
        raw = bytes(range(256))
        part = (
            b'Content-Disposition: form-data; name="file"; filename="b.accdt"\r\n\r\n'
            + raw
        )
        parts = parse_multipart(body("Y", part), "multipart/form-data; boundary=Y")
        self.assertEqual(parts[0].content, raw)

    def test_body_without_the_boundary_is_refused(self) -> None:
        with self.assertRaises(MultipartError):
            parse_multipart(b"not multipart at all", "multipart/form-data; boundary=Z")

    def test_part_without_a_header_block_is_refused(self) -> None:
        with self.assertRaises(MultipartError):
            parse_multipart(
                b"--Z\r\nno-header-terminator\r\n--Z--\r\n",
                "multipart/form-data; boundary=Z",
            )

    def test_too_many_parts_is_refused(self) -> None:
        many = body("Z", *([FIELD_PART] * 40))
        with self.assertRaises(MultipartError):
            parse_multipart(many, "multipart/form-data; boundary=Z")


if __name__ == "__main__":
    unittest.main()
