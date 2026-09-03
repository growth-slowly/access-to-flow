"""Parser for the Access ``SaveAsText`` definition format.

Access writes queries, forms, reports and macros as a nested, line oriented
text format.  Every construct in that format is one of:

``Begin``/``Begin <Type>``
    Opens an anonymous or typed block, closed by a line containing ``End``.
``<Key> =<Value>``
    A scalar property.  ``<Value>`` is either a bare token (number, keyword)
    or a quoted string that may continue on following lines.
``<DbType> "<Key>" =<Value>``
    A typed property, used by query definitions (``dbText "Name" ="x"``).
``<Key> = Begin`` / ``<DbType> "<Key>" = Begin``
    Opens a value block whose body is hexadecimal payload lines.

Nothing here interprets Access semantics; this module only turns bytes that
were already extracted offline into a faithful tree.  No file is opened, no
code is executed, and the input text is never modified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = [
    "AccessTextError",
    "Block",
    "Property",
    "parse_access_text",
    "unescape_access_string",
]


class AccessTextError(Exception):
    """Raised when a definition cannot be parsed as Access text format."""


_OCTAL = re.compile(r"\\([0-7]{3})")


def unescape_access_string(raw: str) -> str:
    """Decode the escapes Access uses inside quoted definition strings.

    ``\\015\\012`` is CRLF, ``\\"`` is a literal quote and ``\\\\`` a literal
    backslash.  Unknown escapes are preserved verbatim rather than guessed at.
    """
    out: list[str] = []
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        rest = raw[index:]
        match = _OCTAL.match(rest)
        if match is not None:
            out.append(chr(int(match.group(1), 8)))
            index += 4
            continue
        if index + 1 < length:
            following = raw[index + 1]
            if following == '"':
                out.append('"')
                index += 2
                continue
            if following == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


@dataclass(frozen=True)
class Property:
    """One ``Key =Value`` line, with its optional Access storage type."""

    key: str
    value: str
    db_type: str | None = None
    quoted: bool = False
    #: Hexadecimal payload lines for ``Key = Begin`` value blocks.
    binary_lines: tuple[str, ...] = ()

    @property
    def is_binary(self) -> bool:
        return bool(self.binary_lines)


@dataclass
class Block:
    """A ``Begin``/``End`` block with ordered properties and child blocks."""

    type_name: str | None
    properties: list[Property] = field(default_factory=list)
    children: list["Block"] = field(default_factory=list)
    #: Ordered items, so a caller can reconstruct the original sequence.
    order: list[tuple[str, int]] = field(default_factory=list)

    def get(self, key: str, default: str | None = None) -> str | None:
        for item in self.properties:
            if item.key.casefold() == key.casefold():
                return item.value
        return default

    def get_all(self, key: str) -> list[str]:
        folded = key.casefold()
        return [i.value for i in self.properties if i.key.casefold() == folded]

    def blocks(self, type_name: str) -> list["Block"]:
        folded = type_name.casefold()
        return [
            child
            for child in self.children
            if child.type_name is not None
            and child.type_name.casefold() == folded
        ]

    def walk(self) -> Iterator["Block"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type_name,
            "properties": [
                {
                    "key": item.key,
                    "value": item.value,
                    "db_type": item.db_type,
                    "binary": item.is_binary,
                }
                for item in self.properties
            ],
            "children": [child.to_dict() for child in self.children],
        }


_BEGIN = re.compile(r"^Begin(?:\s+(?P<type>[A-Za-z_][\w]*))?\s*$")
_END = re.compile(r"^End\s*$")
_TYPED = re.compile(
    r'^(?P<db>db[A-Za-z]+)\s+"(?P<key>(?:[^"\\]|\\.)*)"\s*=\s*(?P<value>.*)$'
)
_PLAIN = re.compile(r"^(?P<key>[A-Za-z_][\w]*)\s*=\s*(?P<value>.*)$")
_HEX = re.compile(r"^0x[0-9A-Fa-f]+\s*,?\s*$")

_MAX_DEPTH = 200


def _split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _read_quoted(
    first: str, lines: list[str], index: int
) -> tuple[str, int]:
    """Read a quoted value that may continue over following lines."""
    parts: list[str] = []
    remainder = first
    while True:
        if not remainder.startswith('"'):
            raise AccessTextError("expected a quoted value")
        cursor = 1
        closed = False
        buffer: list[str] = []
        while cursor < len(remainder):
            char = remainder[cursor]
            if char == "\\" and cursor + 1 < len(remainder):
                buffer.append(remainder[cursor : cursor + 2])
                cursor += 2
                continue
            if char == '"':
                closed = True
                cursor += 1
                break
            buffer.append(char)
            cursor += 1
        parts.append("".join(buffer))
        if not closed:
            raise AccessTextError("unterminated quoted value")
        # A continuation line is an indented string literal on its own.
        peek = index
        while peek < len(lines) and not lines[peek].strip():
            peek += 1
        if peek < len(lines) and lines[peek].strip().startswith('"'):
            remainder = lines[peek].strip()
            index = peek + 1
            continue
        return unescape_access_string("".join(parts)), index


def parse_access_text(text: str) -> Block:
    """Parse a full Access text definition into a root :class:`Block`.

    The root block carries every top level property (``Version``, ``Operation``
    and friends) plus the top level ``Begin`` blocks.
    """
    lines = _split_lines(text)
    root = Block(type_name=None)
    stack: list[Block] = [root]
    index = 0
    total = len(lines)
    while index < total:
        raw = lines[index]
        index += 1
        stripped = raw.strip()
        if not stripped:
            continue
        begin = _BEGIN.match(stripped)
        if begin is not None:
            if len(stack) > _MAX_DEPTH:
                raise AccessTextError("definition nesting is unreasonably deep")
            block = Block(type_name=begin.group("type"))
            parent = stack[-1]
            parent.order.append(("block", len(parent.children)))
            parent.children.append(block)
            stack.append(block)
            continue
        if _END.match(stripped) is not None:
            if len(stack) == 1:
                # A stray End closes nothing; tolerate it rather than losing
                # the rest of a real definition.
                continue
            stack.pop()
            continue
        typed = _TYPED.match(stripped)
        plain = None if typed is not None else _PLAIN.match(stripped)
        match = typed or plain
        if match is None:
            continue
        key = match.group("key")
        if typed is not None:
            key = unescape_access_string(key)
        value = match.group("value").strip()
        db_type = typed.group("db") if typed is not None else None
        if value == "Begin":
            payload: list[str] = []
            while index < total:
                candidate = lines[index].strip()
                index += 1
                if _END.match(candidate) is not None:
                    break
                if _HEX.match(candidate):
                    payload.append(candidate.rstrip(",").strip())
            prop = Property(key, "", db_type, False, tuple(payload))
        elif value.startswith('"'):
            decoded, index = _read_quoted(value, lines, index)
            prop = Property(key, decoded, db_type, True, ())
        else:
            prop = Property(key, value, db_type, False, ())
        target = stack[-1]
        target.order.append(("property", len(target.properties)))
        target.properties.append(prop)
    return root


def split_code_behind(text: str) -> tuple[str, str | None]:
    """Split a form/report definition from its trailing VBA code-behind.

    Access appends the class module source after the last ``End`` of the
    object definition.  The split is anchored on the module header, which is
    the only line that can legally start VBA source in this position.
    """
    for marker in ("\nOption Compare Database", "\nOption Explicit"):
        position = text.replace("\r\n", "\n").find(marker)
        if position >= 0:
            normalized = text.replace("\r\n", "\n")
            return normalized[:position], normalized[position + 1 :]
    return text, None
