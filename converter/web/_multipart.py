"""A small, bounded ``multipart/form-data`` reader.

Python 3.13 removed :mod:`cgi`, and the replacements in the ecosystem are
whole web frameworks.  This service needs exactly one thing: pull a single
uploaded file out of a request body that is already in memory, refusing
anything malformed rather than guessing.  That is small enough to own.

The parser never writes to disk and never allocates more than the body it was
given.  Every limit is explicit, because the alternative to an explicit limit
is a limit discovered in production.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = ["MultipartError", "Part", "parse_multipart", "boundary_of"]

_MAX_PARTS = 16
_MAX_HEADER_BYTES = 8 * 1024
_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_BOUNDARY = re.compile(
    r';\s*boundary\s*=\s*(?:"([^"]{1,200})"|(' + _TOKEN + r"))", re.IGNORECASE
)
_DISPOSITION_NAME = re.compile(r';\s*name\s*=\s*"([^"]*)"', re.IGNORECASE)
_DISPOSITION_FILENAME = re.compile(r';\s*filename\s*=\s*"([^"]*)"', re.IGNORECASE)


class MultipartError(Exception):
    """The body is not a well-formed multipart payload."""


class Part(NamedTuple):
    name: str
    filename: str | None
    content: bytes


def boundary_of(content_type: str) -> bytes:
    """Extract the boundary from a ``Content-Type`` header."""
    if not content_type or "multipart/form-data" not in content_type.casefold():
        raise MultipartError("request is not multipart/form-data")
    match = _BOUNDARY.search(content_type)
    if match is None:
        raise MultipartError("multipart body declares no boundary")
    boundary = match.group(1) or match.group(2)
    if not boundary:
        raise MultipartError("multipart boundary is empty")
    return boundary.encode("ascii", errors="strict")


def _split_headers(chunk: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    if len(chunk) > _MAX_HEADER_BYTES:
        raise MultipartError("multipart part headers are unreasonably large")
    for line in chunk.split(b"\r\n"):
        if not line:
            continue
        if b":" not in line:
            raise MultipartError("malformed multipart part header")
        name, _, value = line.partition(b":")
        try:
            headers[name.decode("ascii").strip().casefold()] = value.decode(
                "utf-8"
            ).strip()
        except UnicodeDecodeError as error:
            raise MultipartError("undecodable multipart part header") from error
    return headers


def parse_multipart(body: bytes, content_type: str) -> list[Part]:
    """Parse a multipart body that is already fully in memory."""
    boundary = boundary_of(content_type)
    delimiter = b"--" + boundary
    if not body.startswith(delimiter):
        # Some clients prepend a preamble; find the first delimiter instead.
        index = body.find(b"\r\n" + delimiter)
        if index < 0:
            raise MultipartError("multipart body does not contain its boundary")
        body = body[index + 2 :]

    parts: list[Part] = []
    segments = body.split(delimiter)
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break  # closing delimiter
        if not segment.startswith(b"\r\n"):
            raise MultipartError("malformed multipart segment")
        segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        head, separator, content = segment.partition(b"\r\n\r\n")
        if not separator:
            raise MultipartError("multipart part has no header block")
        headers = _split_headers(head)
        disposition = headers.get("content-disposition", "")
        name_match = _DISPOSITION_NAME.search(disposition)
        file_match = _DISPOSITION_FILENAME.search(disposition)
        parts.append(
            Part(
                name=name_match.group(1) if name_match else "",
                filename=file_match.group(1) if file_match else None,
                content=content,
            )
        )
        if len(parts) > _MAX_PARTS:
            raise MultipartError("multipart body has too many parts")
    if not parts:
        raise MultipartError("multipart body contains no parts")
    return parts
