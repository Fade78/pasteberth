"""Minimal ``multipart/form-data`` parser (standard library).

The caller supplies any operational budgets. The filename is returned as a
hint; the storage layer validates it before use.
"""
from __future__ import annotations

import re

from .config import LimitsConfig

_DEFAULT_LIMITS = LimitsConfig()

_PARAM_RE = {
    "name": re.compile(r'\bname="((?:[^"\\]|\\.)*)"'),
    "filename": re.compile(r'\bfilename="((?:[^"\\]|\\.)*)"'),
}


class MultipartError(Exception):
    """Malformed multipart body."""


def extract_boundary(
    content_type: str,
    *,
    max_length: int | None = _DEFAULT_LIMITS.max_multipart_boundary_length,
) -> str | None:
    match = re.search(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', content_type or "", re.I)
    if not match:
        return None
    boundary = match.group(1) or match.group(2)
    boundary = boundary.rstrip()
    if not boundary or (max_length is not None and len(boundary) > max_length) or '"' in boundary:
        return None
    return boundary


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _boundary_at(body: bytes, delimiter: bytes, start: int) -> int:
    """Return the next delimiter positioned at the start of a line."""
    cursor = start
    while True:
        position = body.find(delimiter, cursor)
        if position < 0:
            return -1
        if position == 0 or body[position - 2:position] == b"\r\n" or body[position - 1:position] == b"\n":
            after = position + len(delimiter)
            if body[after:after + 2] in (b"--", b"\r\n") or body[after:after + 1] == b"\n":
                return position
        cursor = position + len(delimiter)


def parse_multipart(
    body: bytes,
    boundary: str,
    *,
    max_parts: int | None = _DEFAULT_LIMITS.max_multipart_parts,
    max_header_bytes: int | None = _DEFAULT_LIMITS.max_multipart_header_bytes,
    max_field_name_length: int | None = _DEFAULT_LIMITS.max_multipart_field_name_length,
) -> dict[str, tuple[str | None, str | None, bytes]]:
    """Return {field name: (client filename|None, content type|None, content)}."""
    delimiter = b"--" + boundary.encode("utf-8")
    fields: dict[str, tuple[str | None, str | None, bytes]] = {}
    part_count = 0
    position = _boundary_at(body, delimiter, 0)
    if position < 0:
        raise MultipartError("multipart delimiter not found")

    while True:
        after_delimiter = position + len(delimiter)
        if body[after_delimiter:after_delimiter + 2] == b"--":
            break
        if body[after_delimiter:after_delimiter + 2] == b"\r\n":
            headers_start = after_delimiter + 2
        elif body[after_delimiter:after_delimiter + 1] == b"\n":
            headers_start = after_delimiter + 1
        else:
            raise MultipartError("multipart delimiter is improperly terminated")

        part_count += 1
        if max_parts is not None and part_count > max_parts:
            raise MultipartError("too many parts in multipart body")

        header_end = body.find(b"\r\n\r\n", headers_start)
        sep_len = 4
        if header_end < headers_start:
            header_end = body.find(b"\n\n", headers_start)
            sep_len = 2
        if header_end < headers_start:
            raise MultipartError("header/content separator not found")
        if max_header_bytes is not None and header_end - headers_start > max_header_bytes:
            raise MultipartError("header block is too large")

        content_start = header_end + sep_len
        next_position = _boundary_at(body, delimiter, content_start)
        if next_position < 0:
            raise MultipartError("final multipart delimiter is missing")
        content_end = next_position
        if body[content_end - 2:content_end] == b"\r\n":
            content_end -= 2
        elif body[content_end - 1:content_end] == b"\n":
            content_end -= 1
        content = body[content_start:content_end]

        header_blob = body[headers_start:header_end].decode("utf-8", "replace")
        disposition = ""
        part_ctype: str | None = None
        for line in header_blob.splitlines():
            lower = line.lower()
            if not disposition and lower.startswith("content-disposition:"):
                disposition = line
            elif lower.startswith("content-type:"):
                part_ctype = line.split(":", 1)[1].strip() or None
        name_match = _PARAM_RE["name"].search(disposition)
        if name_match:
            name = _unescape(name_match.group(1))
            if not name or (
                max_field_name_length is not None and len(name) > max_field_name_length
            ):
                raise MultipartError("invalid field name")
            file_match = _PARAM_RE["filename"].search(disposition)
            filename = _unescape(file_match.group(1)) if file_match else None
            fields[name] = (filename, part_ctype, content)

        position = next_position

    if not fields:
        raise MultipartError("multipart body contains no fields")
    return fields
