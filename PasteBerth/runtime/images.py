"""Configurable structural validation for PNG, JPEG, and WebP images.

The server does not decode pixels or codec bitstreams. It checks containers,
dimensions, and required budgets before a browser/harness preview; the
classifier then decides whether to fall back to binary content.

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

# Accepted formats -> (extension, MIME type).
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "webp": (".webp", "image/webp"),
}

# Declared MIME types accepted for upload (advisory: content is authoritative).
ALLOWED_DECLARED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "application/octet-stream",
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
    width: int
    height: int
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


_PARSERS = {"png": _parse_png, "jpeg": _parse_jpeg, "webp": _parse_webp}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"RIFF", "webp"),
)


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
    fmt = None
    for sig, candidate in _SIGNATURES:
        if data.startswith(sig):
            fmt = candidate
            break
    if fmt is None:
        raise InvalidImageError(
            "unsupported_format",
            "unrecognized content (accepted formats: PNG, JPEG, WebP)",
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
    return _parse_webp(data, max_pixels, max_dimension, max_webp_chunks)


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
