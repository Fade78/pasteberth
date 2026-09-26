"""Configurable structural validation for browser-renderable images.

The server does not decode pixels or codec bitstreams. It checks containers,
dimensions, and required budgets before a browser preview; the classifier then
decides whether to fall back to binary content. Browser-native formats that do
not expose a useful bounded decoder in the standard library are checked only
as containers, never decoded by the server.

Contract: validation is structural, not complete codec decoding. A structurally
valid but undecodable file (truncated WebP, minimal JPEG) may be stored and
produce a broken preview; it is never executed by the server. Complete decoding
is a V2 candidate.
"""
from __future__ import annotations

import binascii
import re
import struct
import zlib
from dataclasses import dataclass

from .config import DEFAULT_MAX_IMAGE_PIXELS, LimitsConfig

_DEFAULT_LIMITS = LimitsConfig()

_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

# Accepted formats -> (extension, MIME type).  These are formats that the web
# client can render in an <img> element in current mainstream browsers.
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "webp": (".webp", "image/webp"),
    "gif": (".gif", "image/gif"),
    "bmp": (".bmp", "image/bmp"),
    "ico": (".ico", "image/x-icon"),
    "avif": (".avif", "image/avif"),
    "svg": (".svg", "image/svg+xml"),
}

# Declared MIME types accepted for upload (advisory: content is authoritative).
ALLOWED_DECLARED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
    "image/x-icon", "image/vnd.microsoft.icon", "image/avif", "image/apng",
    "image/svg+xml", "application/octet-stream",
    "text/plain", "text/markdown", "text/html", "text/css",
    "text/javascript", "application/json", "application/xml", "text/csv",
    "application/x-yaml", "application/x-sh", "text/x-python",
    "text/x-shellscript",
}


class InvalidImageError(Exception):
    """Rejected upload: empty, unrecognized, or corrupt content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageInfo:
    fmt: str  # FORMATS key.
    width: int | None
    height: int | None
    kind: str = "image"
    mime: str = "image/png"
    ext: str = ".png"


def _check_dims(
    width: int,
    height: int,
    fmt: str,
    max_pixels: int | None,
    max_dimension: int | None,
) -> ImageInfo:
    if width < 1 or height < 1 or (
        max_dimension is not None
        and (width > max_dimension or height > max_dimension)
    ):
        raise InvalidImageError(
            "invalid_image",
            f"unrealistic {fmt} dimensions: {width}x{height}",
        )
    if max_pixels is not None and width * height > max_pixels:
        raise InvalidImageError(
            "invalid_image",
            f"{fmt} image is too large to decode: {width}x{height} "
            f"(maximum {max_pixels} pixels)",
        )
    return ImageInfo(fmt=fmt, width=width, height=height, mime=mime_for(fmt), ext=FORMATS[fmt][0])


def _parse_png(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
    max_raw_bytes: int | None,
    max_chunks: int | None,
) -> ImageInfo:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) or not data.startswith(signature):
        raise InvalidImageError("invalid_image", "PNG signature is missing")

    pos = len(signature)
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    idat_closed = False
    dimensions: tuple[int, int] | None = None
    png_raw_size: int | None = None
    png_filter_offsets: tuple[int, ...] | None = None
    png_row_layout: tuple[tuple[int, int, bool], ...] | None = None
    color_type: int | None = None
    bit_depth: int | None = None
    saw_plte = False
    plte_entries: int | None = None
    saw_trns = False
    chunk_count = 0
    while pos < len(data):
        chunk_count += 1
        if max_chunks is not None and chunk_count > max_chunks:
            raise InvalidImageError("invalid_image", "too many PNG chunks")
        if pos + 12 > len(data):
            raise InvalidImageError("invalid_image", "truncated PNG chunk")
        chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        if not all(0x41 <= value <= 0x5A or 0x61 <= value <= 0x7A for value in chunk_type):
            raise InvalidImageError("invalid_image", "invalid PNG chunk name")
        if chunk_type[2] & 0x20:
            raise InvalidImageError("invalid_image", "invalid PNG chunk reserved bit")
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_len
        if chunk_end + 4 > len(data):
            raise InvalidImageError("invalid_image", "truncated PNG chunk")
        payload = data[chunk_start:chunk_end]
        expected_crc = struct.unpack(">I", data[chunk_end:chunk_end + 4])[0]
        actual_crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise InvalidImageError("invalid_image", "invalid PNG CRC")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_len != 13:
                raise InvalidImageError("invalid_image", "first PNG chunk is not IHDR")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if color_type not in valid_depths or bit_depth not in valid_depths[color_type]:
                raise InvalidImageError("invalid_image", "invalid PNG color type")
            if compression != 0 or filter_method != 0 or interlace not in (0, 1):
                raise InvalidImageError("invalid_image", "invalid PNG parameters")
            dimensions = (width, height)
            # Bound dimensions before deriving any row-level structures from
            # attacker-controlled 32-bit values.
            info = _check_dims(width, height, "png", max_pixels, max_dimension)
            png_raw_size = _png_raw_size(width, height, bit_depth, color_type, interlace)
            png_filter_offsets = _png_filter_offsets(
                width, height, bit_depth, color_type, interlace
            )
            png_row_layout = _png_row_layout(width, height, bit_depth, color_type, interlace)
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise InvalidImageError("invalid_image", "duplicate PNG IHDR chunk")
        elif chunk_type == b"IDAT":
            if idat_closed:
                raise InvalidImageError("invalid_image", "PNG IDAT chunks are not contiguous")
            saw_idat = True
        elif chunk_type == b"PLTE":
            if (
                saw_idat
                or saw_plte
                or color_type in (0, 4)
                or chunk_len == 0
                or chunk_len % 3
                or chunk_len > 768
            ):
                raise InvalidImageError("invalid_image", "invalid PNG palette")
            if color_type == 3 and bit_depth is not None and chunk_len // 3 > 1 << bit_depth:
                raise InvalidImageError("invalid_image", "PNG palette is too large")
            saw_plte = True
            plte_entries = chunk_len // 3
        elif chunk_type == b"tRNS":
            if color_type == 0:
                valid = chunk_len == 2
            elif color_type == 2:
                valid = chunk_len == 6
            elif color_type == 3:
                valid = saw_plte and plte_entries is not None and chunk_len <= plte_entries
            else:
                valid = False
            if color_type == 3:
                valid = valid and chunk_len > 0
            if saw_idat or saw_trns or not valid:
                raise InvalidImageError("invalid_image", "invalid PNG tRNS chunk")
            saw_trns = True
        elif chunk_type == b"IEND":
            if chunk_len != 0 or not saw_idat:
                raise InvalidImageError("invalid_image", "incomplete PNG")
            saw_iend = True
            pos = chunk_end + 4
            if pos != len(data):
                raise InvalidImageError("invalid_image", "data follows PNG IEND")
            break
        else:
            if saw_idat:
                idat_closed = True
            if chunk_type[0] & 0x20 == 0:
                raise InvalidImageError("invalid_image", "unknown critical PNG chunk")
        pos = chunk_end + 4

    if (
        dimensions is None
        or png_raw_size is None
        or png_filter_offsets is None
        or png_row_layout is None
        or not saw_idat
        or not saw_iend
        or pos != len(data)
    ):
        raise InvalidImageError("invalid_image", "incomplete PNG")
    if color_type == 3 and not saw_plte:
        raise InvalidImageError("invalid_image", "PNG palette is missing")
    if max_raw_bytes is not None and png_raw_size > max_raw_bytes:
        raise InvalidImageError("invalid_image", "decompressed PNG data is too large")
    filter_bpp = max(1, (_PNG_CHANNELS[color_type] * bit_depth + 7) // 8)
    _validate_png_data(
        data,
        png_raw_size,
        png_filter_offsets,
        png_row_layout,
        filter_bpp,
        _PNG_CHANNELS[color_type] * bit_depth,
        plte_entries if color_type == 3 else None,
        bit_depth,
    )
    return info


_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def _parse_jpeg(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
    max_segments: int | None,
) -> ImageInfo:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise InvalidImageError("invalid_image", "JPEG signature is missing")
    pos = 2
    end = len(data)
    dimensions: tuple[int, int] | None = None
    segment_count = 0
    while pos + 1 < end:
        if data[pos] != 0xFF:
            raise InvalidImageError("invalid_image", "desynchronized JPEG stream")
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = data[pos]
        pos += 1
        segment_count += 1
        if max_segments is not None and segment_count > max_segments:
            raise InvalidImageError("invalid_image", "too many JPEG segments")
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if dimensions is None or pos + 2 > end:
                raise InvalidImageError("invalid_image", "incomplete JPEG frame")
            seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
            if seg_len < 2 or pos + seg_len > end:
                raise InvalidImageError("invalid_image", "truncated JPEG SOS segment")
            entropy_start = pos + seg_len
            eoi = data.find(b"\xff\xd9", entropy_start)
            if eoi <= entropy_start or eoi + 2 != end:
                raise InvalidImageError("invalid_image", "JPEG end marker is missing or inconsistent")
            return _check_dims(*dimensions, "jpeg", max_pixels, max_dimension)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > end:
            raise InvalidImageError("invalid_image", "truncated JPEG segment")
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        if seg_len < 2 or pos + seg_len > end:
            raise InvalidImageError("invalid_image", "truncated JPEG segment")
        if marker in _SOF_MARKERS:
            if seg_len < 8:
                raise InvalidImageError("invalid_image", "truncated JPEG SOF segment")
            precision = data[pos + 2]
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            components = data[pos + 7]
            if precision == 0 or components == 0 or seg_len != 8 + components * 3:
                raise InvalidImageError("invalid_image", "invalid JPEG SOF segment")
            dimensions = (width, height)
        pos += seg_len
    raise InvalidImageError("invalid_image", "incomplete JPEG or missing frame header")


def _parse_webp(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
    max_chunks: int | None,
) -> ImageInfo:
    if len(data) < 20 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("invalid_image", "WebP signature is missing")
    riff_size = struct.unpack("<I", data[4:8])[0]
    if riff_size != len(data) - 8:
        raise InvalidImageError("invalid_image", "inconsistent WebP RIFF size")

    pos = 12
    end = len(data)
    canvas_dimensions: tuple[int, int] | None = None
    frame_dimensions: tuple[int, int] | None = None
    saw_image_payload = False
    saw_vp8x = False
    payload_count = 0
    chunk_count = 0
    while pos < end:
        chunk_count += 1
        if max_chunks is not None and chunk_count > max_chunks:
            raise InvalidImageError("invalid_image", "too many WebP chunks")
        if pos + 8 > end:
            raise InvalidImageError("invalid_image", "truncated WebP chunk")
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        chunk_end = body + size
        padded_end = chunk_end + (size & 1)
        if chunk_end > end or padded_end > end:
            raise InvalidImageError("invalid_image", "truncated WebP chunk")
        if size & 1 and data[chunk_end] != 0:
            raise InvalidImageError("invalid_image", "invalid WebP padding")
        if fourcc == b"VP8X":
            if saw_vp8x or size != 10:
                raise InvalidImageError("invalid_image", "truncated WebP VP8X chunk")
            flags = data[body]
            if flags & 0xC1 or any(data[body + 1:body + 4]):
                raise InvalidImageError("invalid_image", "invalid VP8X reserved bits")
            if flags & 0x02:
                raise InvalidImageError("invalid_image", "WebP animation is not supported")
            w = int.from_bytes(data[body + 4:body + 7], "little") + 1
            h = int.from_bytes(data[body + 7:body + 10], "little") + 1
            canvas_dimensions = (w, h)
        elif fourcc == b"VP8 ":
            if payload_count or size < 10:
                raise InvalidImageError("invalid_image", "truncated WebP VP8 chunk")
            frame_tag = data[body]
            if frame_tag & 0x01 or frame_tag & 0x0E:
                raise InvalidImageError("invalid_image", "invalid VP8 version")
            if data[body + 3:body + 6] != b"\x9d\x01\x2a":
                raise InvalidImageError("invalid_image", "invalid VP8 synchronization code")
            saw_image_payload = True
            payload_count += 1
            w, h = struct.unpack("<HH", data[body + 6:body + 10])
            frame_dimensions = (w & 0x3FFF, h & 0x3FFF)
        elif fourcc == b"VP8L":
            if payload_count or size < 5:
                raise InvalidImageError("invalid_image", "truncated WebP VP8L chunk")
            if data[body] != 0x2F:
                raise InvalidImageError("invalid_image", "invalid VP8L signature")
            saw_image_payload = True
            payload_count += 1
            bits = struct.unpack("<I", data[body + 1:body + 5])[0]
            if bits & 0xE0000000:
                raise InvalidImageError("invalid_image", "invalid VP8L version")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            frame_dimensions = (w, h)
        pos = padded_end
    if canvas_dimensions is not None and frame_dimensions is not None:
        if canvas_dimensions != frame_dimensions:
            raise InvalidImageError("invalid_image", "inconsistent WebP canvas and frame")
    dimensions = canvas_dimensions or frame_dimensions
    if dimensions is None:
        raise InvalidImageError("invalid_image", "WebP dimension chunk is missing")
    if not saw_image_payload:
        raise InvalidImageError("invalid_image", "WebP image payload is missing")
    return _check_dims(*dimensions, "webp", max_pixels, max_dimension)


def _skip_gif_sub_blocks(data: bytes, pos: int) -> int:
    """Skip GIF data sub-blocks and return the position after the terminator."""
    while True:
        if pos >= len(data):
            raise InvalidImageError("invalid_image", "truncated GIF data sub-block")
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        if pos + size > len(data):
            raise InvalidImageError("invalid_image", "truncated GIF data sub-block")
        pos += size


def _parse_gif(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
) -> ImageInfo:
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise InvalidImageError("invalid_image", "GIF signature is missing")
    width, height = struct.unpack("<HH", data[6:10])
    info = _check_dims(width, height, "gif", max_pixels, max_dimension)
    packed = data[10]
    pos = 13
    if packed & 0x80:
        pos += 3 * (1 << ((packed & 0x07) + 1))
        if pos > len(data):
            raise InvalidImageError("invalid_image", "truncated GIF color table")

    frames = 0
    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:  # trailer
            if frames == 0 or pos + 1 != len(data):
                raise InvalidImageError("invalid_image", "incomplete GIF")
            return info
        if marker == 0x2C:  # image descriptor
            if pos + 10 > len(data):
                raise InvalidImageError("invalid_image", "truncated GIF image descriptor")
            left, top, frame_width, frame_height = struct.unpack(
                "<HHHH", data[pos + 1:pos + 9]
            )
            descriptor_flags = data[pos + 9]
            if (
                frame_width < 1
                or frame_height < 1
                or left + frame_width > width
                or top + frame_height > height
            ):
                raise InvalidImageError("invalid_image", "invalid GIF frame dimensions")
            _check_dims(frame_width, frame_height, "gif", max_pixels, max_dimension)
            pos += 10
            if descriptor_flags & 0x80:
                pos += 3 * (1 << ((descriptor_flags & 0x07) + 1))
                if pos > len(data):
                    raise InvalidImageError("invalid_image", "truncated GIF color table")
            if pos >= len(data) or not 2 <= data[pos] <= 8:
                raise InvalidImageError("invalid_image", "invalid GIF LZW code size")
            pos = _skip_gif_sub_blocks(data, pos + 1)
            frames += 1
            continue
        if marker == 0x21:  # extension
            if pos + 2 > len(data):
                raise InvalidImageError("invalid_image", "truncated GIF extension")
            label = data[pos + 1]
            pos += 2
            if label == 0xF9:  # graphic control extension
                if pos + 6 > len(data) or data[pos] != 4 or data[pos + 5] != 0:
                    raise InvalidImageError("invalid_image", "invalid GIF graphic control extension")
                pos += 6
            elif label == 0x01:  # plain text extension
                if pos >= len(data) or data[pos] != 12 or pos + 13 > len(data):
                    raise InvalidImageError("invalid_image", "invalid GIF plain text extension")
                pos = _skip_gif_sub_blocks(data, pos + 13)
            else:
                pos = _skip_gif_sub_blocks(data, pos)
            continue
        raise InvalidImageError("invalid_image", "invalid GIF block")
    raise InvalidImageError("invalid_image", "GIF trailer is missing")


def _parse_bmp(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
) -> ImageInfo:
    if len(data) < 26 or data[:2] != b"BM":
        raise InvalidImageError("invalid_image", "BMP signature is missing")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    dib_size = struct.unpack_from("<I", data, 14)[0]
    if dib_size == 12:
        if len(data) < 26:
            raise InvalidImageError("invalid_image", "truncated BMP header")
        width, height, planes, bits = struct.unpack_from("<HHHH", data, 18)
        compression = 0
        header_end = 26
    elif dib_size >= 40:
        header_end = 14 + dib_size
        if header_end > len(data):
            raise InvalidImageError("invalid_image", "truncated BMP header")
        width, height, planes, bits, compression = struct.unpack_from(
            "<iiHHI", data, 18
        )
        height = abs(height)
    else:
        raise InvalidImageError("invalid_image", "unsupported BMP header")
    if (
        width < 1
        or height < 1
        or planes != 1
        or bits not in (1, 4, 8, 16, 24, 32)
        or compression not in (0, 3, 6)
        or pixel_offset < header_end
        or pixel_offset >= len(data)
    ):
        raise InvalidImageError("invalid_image", "invalid BMP structure")
    return _check_dims(width, height, "bmp", max_pixels, max_dimension)


def _parse_ico(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
) -> ImageInfo:
    if len(data) < 6 or data[:2] != b"\x00\x00":
        raise InvalidImageError("invalid_image", "ICO signature is missing")
    icon_type, count = struct.unpack_from("<HH", data, 2)
    if icon_type not in (1, 2) or count < 1 or 6 + count * 16 > len(data):
        raise InvalidImageError("invalid_image", "invalid ICO directory")
    largest: tuple[int, int] | None = None
    for index in range(count):
        pos = 6 + index * 16
        raw_width, raw_height = data[pos], data[pos + 1]
        width = raw_width or 256
        height = raw_height or 256
        image_size, image_offset = struct.unpack_from("<II", data, pos + 8)
        if (
            image_size < 1
            or image_offset < 6 + count * 16
            or image_offset + image_size > len(data)
        ):
            raise InvalidImageError("invalid_image", "invalid ICO image entry")
        _check_dims(width, height, "ico", max_pixels, max_dimension)
        if largest is None or width * height > largest[0] * largest[1]:
            largest = (width, height)
    assert largest is not None
    return _check_dims(*largest, "ico", max_pixels, max_dimension)


def _read_isobmff_boxes(
    data: bytes,
    start: int,
    end: int,
) -> list[tuple[bytes, int, int]]:
    """Read bounded ISO-BMFF boxes as (type, body start, body end)."""
    boxes: list[tuple[bytes, int, int]] = []
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise InvalidImageError("invalid_image", "truncated ISO-BMFF box")
        size = struct.unpack_from(">I", data, pos)[0]
        box_type = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise InvalidImageError("invalid_image", "truncated ISO-BMFF extended size")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or size > end - pos:
            raise InvalidImageError("invalid_image", "invalid ISO-BMFF box size")
        body_start = pos + header
        boxes.append((box_type, body_start, pos + size))
        pos += size
    return boxes


def _find_ispe(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    for box_type, body_start, body_end in _read_isobmff_boxes(data, start, end):
        if box_type == b"ispe":
            if body_end - body_start < 12:
                raise InvalidImageError("invalid_image", "truncated AVIF dimensions")
            return struct.unpack_from(">II", data, body_start + 4)
        if box_type in (b"iprp", b"ipco"):
            dimensions = _find_ispe(data, body_start, body_end)
            if dimensions is not None:
                return dimensions
    return None


def _avif_brand(data: bytes) -> bool:
    try:
        boxes = _read_isobmff_boxes(data, 0, len(data))
    except InvalidImageError:
        return False
    if not boxes or boxes[0][0] != b"ftyp":
        return False
    _, body_start, body_end = boxes[0]
    if body_end - body_start < 8:
        return False
    brands = [data[body_start:body_start + 4]]
    brands.extend(
        data[pos:pos + 4]
        for pos in range(body_start + 8, body_end - 3, 4)
    )
    return any(brand in (b"avif", b"avis") for brand in brands)


def _parse_avif(
    data: bytes,
    max_pixels: int | None,
    max_dimension: int | None,
) -> ImageInfo:
    try:
        boxes = _read_isobmff_boxes(data, 0, len(data))
    except InvalidImageError:
        raise
    if not boxes or boxes[0][0] != b"ftyp" or not _avif_brand(data):
        raise InvalidImageError("invalid_image", "AVIF file type is missing")
    meta = next((box for box in boxes if box[0] == b"meta"), None)
    if meta is None or meta[2] - meta[1] < 4:
        raise InvalidImageError("invalid_image", "AVIF metadata is missing")
    dimensions = _find_ispe(data, meta[1] + 4, meta[2])
    if dimensions is None:
        raise InvalidImageError("invalid_image", "AVIF dimensions are missing")
    return _check_dims(*dimensions, "avif", max_pixels, max_dimension)


def _looks_like_svg(data: bytes) -> bool:
    prefix = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if prefix.startswith(b"<?xml"):
        declaration_end = prefix.find(b"?>")
        if declaration_end < 0:
            return False
        prefix = prefix[declaration_end + 2:].lstrip(b" \t\r\n")
    while True:
        if prefix.startswith(b"<!--"):
            comment_end = prefix.find(b"-->")
            if comment_end < 0:
                return False
            prefix = prefix[comment_end + 3:].lstrip(b" \t\r\n")
        elif prefix.startswith(b"<!DOCTYPE"):
            doctype_end = prefix.find(b">")
            if doctype_end < 0:
                return False
            prefix = prefix[doctype_end + 1:].lstrip(b" \t\r\n")
        elif prefix.startswith(b"<?"):
            instruction_end = prefix.find(b"?>")
            if instruction_end < 0:
                return False
            prefix = prefix[instruction_end + 2:].lstrip(b" \t\r\n")
        else:
            break
    return bool(re.match(rb"<svg(?:[\s>])", prefix, re.I))


def _parse_svg(data: bytes) -> ImageInfo:
    if not _looks_like_svg(data):
        raise InvalidImageError("invalid_image", "SVG root is missing")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidImageError("invalid_image", "SVG is not UTF-8") from exc
    lowered = text.lower()
    if "</svg" not in lowered and not re.search(r"<svg\b[^>]*/>", lowered):
        raise InvalidImageError("invalid_image", "SVG closing tag is missing")
    # SVG dimensions are optional and its intrinsic size may come from a
    # viewBox. The browser remains the authority for the rendered dimensions.
    return ImageInfo(fmt="svg", width=None, height=None, mime=mime_for("svg"), ext=extension_for("svg"))


_PARSERS = {
    "png": _parse_png,
    "jpeg": _parse_jpeg,
    "webp": _parse_webp,
    "gif": _parse_gif,
    "bmp": _parse_bmp,
    "ico": _parse_ico,
    "avif": _parse_avif,
}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"\x00\x00\x01\x00", "ico"),
)


def detect_image_format(data: bytes) -> str | None:
    """Return a recognized image format without validating its contents."""
    for signature, fmt in _SIGNATURES:
        if data.startswith(signature):
            return fmt
    if _avif_brand(data):
        return "avif"
    if _looks_like_svg(data):
        return "svg"
    return None


def _png_row_layout(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace: int,
) -> tuple[tuple[int, int, bool], ...]:
    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth
    passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    if interlace == 0:
        passes = ((0, 0, 1, 1),)
    rows: list[tuple[int, int, bool]] = []
    for x_start, y_start, x_step, y_step in passes:
        pass_width = width if interlace == 0 else max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = height if interlace == 0 else max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width == 0 or pass_height == 0:
            continue
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        for row_index in range(pass_height):
            rows.append((row_bytes, pass_width, row_index == 0))
    return tuple(rows)


def _png_raw_size(width: int, height: int, bit_depth: int, color_type: int,
                  interlace: int) -> int:
    return sum(row_bytes + 1 for row_bytes, _, _ in _png_row_layout(
        width, height, bit_depth, color_type, interlace
    ))


def _png_filter_offsets(width: int, height: int, bit_depth: int, color_type: int,
                        interlace: int) -> tuple[int, ...]:
    offsets: list[int] = []
    offset = 0
    for row_bytes, _, _ in _png_row_layout(width, height, bit_depth, color_type, interlace):
        offsets.append(offset)
        offset += row_bytes + 1
    return tuple(offsets)


def _validate_png_data(
    data: bytes,
    raw_size: int,
    filter_offsets: tuple[int, ...],
    row_layout: tuple[tuple[int, int, bool], ...],
    filter_bpp: int,
    bits_per_pixel: int,
    palette_entries: int | None,
    bit_depth: int,
) -> None:
    pos = 8
    decompressor = zlib.decompressobj()
    produced = 0
    filter_index = 0
    stream_ended = False
    row_index = 0
    row_buffer = bytearray()
    previous_row = b""

    def unfilter(row: bytes, previous: bytes) -> bytes:
        filter_type = row[0]
        filtered = row[1:]
        restored = bytearray(len(filtered))
        for index, value in enumerate(filtered):
            left = restored[index - filter_bpp] if index >= filter_bpp else 0
            up = previous[index] if index < len(previous) else 0
            up_left = previous[index - filter_bpp] if index >= filter_bpp and index - filter_bpp < len(previous) else 0
            if filter_type == 0:
                restored[index] = value
            elif filter_type == 1:
                restored[index] = (value + left) & 0xFF
            elif filter_type == 2:
                restored[index] = (value + up) & 0xFF
            elif filter_type == 3:
                restored[index] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + up - up_left
                pa = abs(predictor - left)
                pb = abs(predictor - up)
                pc = abs(predictor - up_left)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                restored[index] = (value + predictor) & 0xFF
            else:
                raise InvalidImageError("invalid_image", "invalid PNG filter")
        return bytes(restored)

    def check_palette(raw_row: bytes, pixel_width: int) -> None:
        if palette_entries is None:
            return
        if bit_depth == 8:
            samples = (raw_row[index] for index in range(pixel_width))
        else:
            mask = (1 << bit_depth) - 1
            per_byte = 8 // bit_depth
            samples = (
                (raw_row[index // per_byte] >> (8 - bit_depth * (index % per_byte + 1))) & mask
                for index in range(pixel_width)
            )
        if any(sample >= palette_entries for sample in samples):
            raise InvalidImageError("invalid_image", "invalid PNG palette index")

    def check_padding(raw_row: bytes, pixel_width: int) -> None:
        if bits_per_pixel >= 8:
            return
        unused_bits = len(raw_row) * 8 - pixel_width * bits_per_pixel
        if unused_bits and raw_row[-1] & ((1 << unused_bits) - 1):
            raise InvalidImageError("invalid_image", "unused PNG bits are not zero")

    def consume(output: bytes) -> None:
        nonlocal produced, filter_index, row_index, previous_row
        start = produced
        produced += len(output)
        if produced > raw_size:
            raise InvalidImageError("invalid_image", "inconsistent decompressed PNG data")
        while filter_index < len(filter_offsets) and filter_offsets[filter_index] < produced:
            offset = filter_offsets[filter_index]
            if output[offset - start] > 4:
                raise InvalidImageError("invalid_image", "invalid PNG filter")
            filter_index += 1
        row_buffer.extend(output)
        while row_index < len(row_layout):
            row_bytes, pixel_width, reset_previous = row_layout[row_index]
            row_length = row_bytes + 1
            if len(row_buffer) < row_length:
                break
            row = bytes(row_buffer[:row_length])
            del row_buffer[:row_length]
            if reset_previous:
                previous_row = b""
            previous_row = unfilter(row, previous_row)
            check_padding(previous_row, pixel_width)
            check_palette(previous_row, pixel_width)
            row_index += 1

    try:
        while pos + 12 <= len(data):
            chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
            chunk_type = data[pos + 4:pos + 8]
            chunk_start = pos + 8
            chunk_end = chunk_start + chunk_len
            if chunk_end + 4 > len(data):
                break
            if chunk_type == b"IDAT":
                if stream_ended:
                    raise InvalidImageError("invalid_image", "inconsistent PNG zlib stream")
                pending = data[chunk_start:chunk_end]
                while pending:
                    output = decompressor.decompress(pending, 64 * 1024)
                    consume(output)
                    pending = decompressor.unconsumed_tail
                    if decompressor.eof:
                        if decompressor.unused_data or pending:
                            raise InvalidImageError("invalid_image", "inconsistent PNG zlib stream")
                        stream_ended = True
                        break
            if chunk_type == b"IEND":
                break
            pos = chunk_end + 4
        if not decompressor.eof:
            raise InvalidImageError("invalid_image", "incomplete PNG zlib stream")
        consume(decompressor.flush())
    except zlib.error as exc:
        raise InvalidImageError("invalid_image", "invalid PNG compression") from exc
    if (
        produced != raw_size
        or filter_index != len(filter_offsets)
        or row_index != len(row_layout)
        or row_buffer
    ):
        raise InvalidImageError("invalid_image", "inconsistent decompressed PNG size")


def inspect_image(
    data: bytes,
    *,
    max_pixels: int | None = DEFAULT_MAX_IMAGE_PIXELS,
    max_dimension: int | None = _DEFAULT_LIMITS.max_image_dimension,
    max_raw_bytes: int | None = _DEFAULT_LIMITS.max_image_raw_bytes,
    max_png_chunks: int | None = _DEFAULT_LIMITS.max_png_chunks,
    max_jpeg_segments: int | None = _DEFAULT_LIMITS.max_jpeg_segments,
    max_webp_chunks: int | None = _DEFAULT_LIMITS.max_webp_chunks,
) -> ImageInfo:
    """Identify and validate an image from its content."""
    if not data:
        raise InvalidImageError("empty_upload", "upload is empty")
    fmt = detect_image_format(data)
    if fmt is None:
        raise InvalidImageError(
            "unsupported_format",
            "unrecognized content (accepted formats: "
            + ", ".join(name.upper() for name in FORMATS)
            + ")",
        )
    if fmt == "png":
        return _parse_png(
            data,
            max_pixels,
            max_dimension,
            max_raw_bytes,
            max_png_chunks,
        )
    if fmt == "jpeg":
        return _parse_jpeg(data, max_pixels, max_dimension, max_jpeg_segments)
    if fmt == "webp":
        return _parse_webp(data, max_pixels, max_dimension, max_webp_chunks)
    if fmt == "gif":
        return _parse_gif(data, max_pixels, max_dimension)
    if fmt == "bmp":
        return _parse_bmp(data, max_pixels, max_dimension)
    if fmt == "ico":
        return _parse_ico(data, max_pixels, max_dimension)
    if fmt == "avif":
        return _parse_avif(data, max_pixels, max_dimension)
    return _parse_svg(data)


def mime_allowed(declared: str | None) -> bool:
    """Check the declared Content-Type (advisory, never authoritative)."""
    if not declared:
        return True  # Treat as application/octet-stream.
    return declared.split(";")[0].strip().lower() in ALLOWED_DECLARED_MIMES


def mime_syntax_allowed(
    declared: str | None,
    *,
    max_length: int | None = _DEFAULT_LIMITS.max_mime_length,
) -> bool:
    """Check MIME syntax before letting it influence classification."""
    if not declared:
        return True
    value = declared.split(";", 1)[0].strip().lower()
    return (max_length is None or len(value) <= max_length) and bool(
        re.fullmatch(
            r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+",
            value,
        )
    )


def extension_for(fmt: str) -> str:
    return FORMATS[fmt][0]


def mime_for(fmt: str) -> str:
    return FORMATS[fmt][1]
