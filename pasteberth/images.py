"""Validation structurelle des images : PNG, JPEG, WebP.

Le serveur ne décode pas les pixels. Il vérifie toutefois les conteneurs
complets et impose un budget de pixels afin de limiter les décodages côté
navigateur/harness lors de l'affichage des previews.
"""
from __future__ import annotations

import binascii
import re
import struct
import zlib
from dataclasses import dataclass

MAX_DIMENSION = 16_384
MAX_PIXELS = 25_000_000
HARD_MAX_PIXELS = 50_000_000
MAX_MIME_LENGTH = 120
_MAX_PNG_RAW_BYTES = 256 * 1024 * 1024
_MAX_PNG_CHUNKS = 100_000
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

# Formats acceptés -> (extension, MIME)
FORMATS: dict[str, tuple[str, str]] = {
    "png": (".png", "image/png"),
    "jpeg": (".jpg", "image/jpeg"),
    "webp": (".webp", "image/webp"),
}

# MIME déclarés acceptés à l'upload (indicatif : le contenu fait foi).
ALLOWED_DECLARED_MIMES = {
    "image/png", "image/jpeg", "image/webp", "application/octet-stream",
    "text/plain", "text/markdown", "text/html", "text/css",
    "text/javascript", "application/json", "application/xml", "text/csv",
    "application/x-yaml", "application/x-sh", "text/x-python",
    "text/x-shellscript",
}


class InvalidImageError(Exception):
    """Upload refusé : contenu vide, non reconnu ou corrompu."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageInfo:
    fmt: str  # clé de FORMATS
    width: int
    height: int
    kind: str = "image"
    mime: str = "image/png"
    ext: str = ".png"


def _check_dims(width: int, height: int, fmt: str, max_pixels: int) -> ImageInfo:
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise InvalidImageError(
            "invalid_image",
            f"dimensions {fmt} irréalistes : {width}x{height}",
        )
    if width * height > max_pixels:
        raise InvalidImageError(
            "invalid_image",
            f"image {fmt} trop grande à décoder : {width}x{height} "
            f"(maximum {max_pixels} pixels)",
        )
    return ImageInfo(fmt=fmt, width=width, height=height, mime=mime_for(fmt), ext=FORMATS[fmt][0])


def _parse_png(data: bytes, max_pixels: int) -> ImageInfo:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) or not data.startswith(signature):
        raise InvalidImageError("invalid_image", "signature PNG absente")

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
        if chunk_count > _MAX_PNG_CHUNKS:
            raise InvalidImageError("invalid_image", "trop de chunks PNG")
        if pos + 12 > len(data):
            raise InvalidImageError("invalid_image", "chunk PNG tronqué")
        chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        if not all(0x41 <= value <= 0x5A or 0x61 <= value <= 0x7A for value in chunk_type):
            raise InvalidImageError("invalid_image", "nom de chunk PNG invalide")
        if chunk_type[2] & 0x20:
            raise InvalidImageError("invalid_image", "bit réservé de chunk PNG invalide")
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_len
        if chunk_end + 4 > len(data):
            raise InvalidImageError("invalid_image", "chunk PNG tronqué")
        payload = data[chunk_start:chunk_end]
        expected_crc = struct.unpack(">I", data[chunk_end:chunk_end + 4])[0]
        actual_crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise InvalidImageError("invalid_image", "CRC PNG invalide")

        if not saw_ihdr:
            if chunk_type != b"IHDR" or chunk_len != 13:
                raise InvalidImageError("invalid_image", "premier chunk PNG != IHDR")
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
                raise InvalidImageError("invalid_image", "type PNG invalide")
            if compression != 0 or filter_method != 0 or interlace not in (0, 1):
                raise InvalidImageError("invalid_image", "paramètres PNG invalides")
            dimensions = (width, height)
            # Bound dimensions before deriving any row-level structures from
            # attacker-controlled 32-bit values.
            info = _check_dims(width, height, "png", max_pixels)
            png_raw_size = _png_raw_size(width, height, bit_depth, color_type, interlace)
            png_filter_offsets = _png_filter_offsets(
                width, height, bit_depth, color_type, interlace
            )
            png_row_layout = _png_row_layout(width, height, bit_depth, color_type, interlace)
            saw_ihdr = True
        elif chunk_type == b"IHDR":
            raise InvalidImageError("invalid_image", "chunk IHDR PNG dupliqué")
        elif chunk_type == b"IDAT":
            if idat_closed:
                raise InvalidImageError("invalid_image", "chunks IDAT PNG non contigus")
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
                raise InvalidImageError("invalid_image", "palette PNG invalide")
            if color_type == 3 and bit_depth is not None and chunk_len // 3 > 1 << bit_depth:
                raise InvalidImageError("invalid_image", "palette PNG trop grande")
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
                raise InvalidImageError("invalid_image", "chunk tRNS PNG invalide")
            saw_trns = True
        elif chunk_type == b"IEND":
            if chunk_len != 0 or not saw_idat:
                raise InvalidImageError("invalid_image", "PNG incomplet")
            saw_iend = True
            pos = chunk_end + 4
            if pos != len(data):
                raise InvalidImageError("invalid_image", "données après IEND PNG")
            break
        else:
            if saw_idat:
                idat_closed = True
            if chunk_type[0] & 0x20 == 0:
                raise InvalidImageError("invalid_image", "chunk critique PNG inconnu")
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
        raise InvalidImageError("invalid_image", "PNG incomplet")
    if color_type == 3 and not saw_plte:
        raise InvalidImageError("invalid_image", "palette PNG absente")
    if png_raw_size > _MAX_PNG_RAW_BYTES:
        raise InvalidImageError("invalid_image", "données PNG décompressées trop volumineuses")
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


def _parse_jpeg(data: bytes, max_pixels: int) -> ImageInfo:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise InvalidImageError("invalid_image", "signature JPEG absente")
    pos = 2
    end = len(data)
    dimensions: tuple[int, int] | None = None
    while pos + 1 < end:
        if data[pos] != 0xFF:
            raise InvalidImageError("invalid_image", "flux JPEG désynchronisé")
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if dimensions is None or pos + 2 > end:
                raise InvalidImageError("invalid_image", "trame JPEG incomplète")
            seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
            if seg_len < 2 or pos + seg_len > end:
                raise InvalidImageError("invalid_image", "segment SOS JPEG tronqué")
            entropy_start = pos + seg_len
            eoi = data.find(b"\xff\xd9", entropy_start)
            if eoi <= entropy_start or eoi + 2 != end:
                raise InvalidImageError("invalid_image", "fin JPEG absente ou incohérente")
            return _check_dims(*dimensions, "jpeg", max_pixels)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > end:
            raise InvalidImageError("invalid_image", "segment JPEG tronqué")
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        if seg_len < 2 or pos + seg_len > end:
            raise InvalidImageError("invalid_image", "segment JPEG tronqué")
        if marker in _SOF_MARKERS:
            if seg_len < 8:
                raise InvalidImageError("invalid_image", "segment SOF JPEG tronqué")
            precision = data[pos + 2]
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            components = data[pos + 7]
            if precision == 0 or components == 0 or seg_len != 8 + components * 3:
                raise InvalidImageError("invalid_image", "segment SOF JPEG invalide")
            dimensions = (width, height)
        pos += seg_len
    raise InvalidImageError("invalid_image", "JPEG incomplet ou en-tête de trame absent")


def _parse_webp(data: bytes, max_pixels: int) -> ImageInfo:
    if len(data) < 20 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise InvalidImageError("invalid_image", "signature WebP absente")
    riff_size = struct.unpack("<I", data[4:8])[0]
    if riff_size != len(data) - 8:
        raise InvalidImageError("invalid_image", "taille RIFF WebP incohérente")

    pos = 12
    end = len(data)
    canvas_dimensions: tuple[int, int] | None = None
    frame_dimensions: tuple[int, int] | None = None
    saw_image_payload = False
    saw_vp8x = False
    payload_count = 0
    while pos < end:
        if pos + 8 > end:
            raise InvalidImageError("invalid_image", "chunk WebP tronqué")
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        chunk_end = body + size
        padded_end = chunk_end + (size & 1)
        if chunk_end > end or padded_end > end:
            raise InvalidImageError("invalid_image", "chunk WebP tronqué")
        if size & 1 and data[chunk_end] != 0:
            raise InvalidImageError("invalid_image", "bourrage WebP invalide")
        if fourcc == b"VP8X":
            if saw_vp8x or size != 10:
                raise InvalidImageError("invalid_image", "chunk VP8X WebP tronqué")
            flags = data[body]
            if flags & 0xC1 or any(data[body + 1:body + 4]):
                raise InvalidImageError("invalid_image", "bits réservés VP8X invalides")
            if flags & 0x02:
                raise InvalidImageError("invalid_image", "animation WebP non supportée")
            w = int.from_bytes(data[body + 4:body + 7], "little") + 1
            h = int.from_bytes(data[body + 7:body + 10], "little") + 1
            canvas_dimensions = (w, h)
        elif fourcc == b"VP8 ":
            if payload_count or size < 10:
                raise InvalidImageError("invalid_image", "chunk VP8 WebP tronqué")
            frame_tag = data[body]
            if frame_tag & 0x01 or frame_tag & 0x0E:
                raise InvalidImageError("invalid_image", "version VP8 invalide")
            if data[body + 3:body + 6] != b"\x9d\x01\x2a":
                raise InvalidImageError("invalid_image", "code de synchronisation VP8 invalide")
            saw_image_payload = True
            payload_count += 1
            w, h = struct.unpack("<HH", data[body + 6:body + 10])
            frame_dimensions = (w & 0x3FFF, h & 0x3FFF)
        elif fourcc == b"VP8L":
            if payload_count or size < 5:
                raise InvalidImageError("invalid_image", "chunk VP8L WebP tronqué")
            if data[body] != 0x2F:
                raise InvalidImageError("invalid_image", "signature VP8L invalide")
            saw_image_payload = True
            payload_count += 1
            bits = struct.unpack("<I", data[body + 1:body + 5])[0]
            if bits & 0xE0000000:
                raise InvalidImageError("invalid_image", "version VP8L invalide")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            frame_dimensions = (w, h)
        pos = padded_end
    if canvas_dimensions is not None and frame_dimensions is not None:
        if canvas_dimensions != frame_dimensions:
            raise InvalidImageError("invalid_image", "canvas et trame WebP incohérents")
    dimensions = canvas_dimensions or frame_dimensions
    if dimensions is None:
        raise InvalidImageError("invalid_image", "chunk de dimension WebP introuvable")
    if not saw_image_payload:
        raise InvalidImageError("invalid_image", "payload image WebP introuvable")
    return _check_dims(*dimensions, "webp", max_pixels)


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
                raise InvalidImageError("invalid_image", "filtre PNG invalide")
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
            raise InvalidImageError("invalid_image", "indice de palette PNG invalide")

    def check_padding(raw_row: bytes, pixel_width: int) -> None:
        if bits_per_pixel >= 8:
            return
        unused_bits = len(raw_row) * 8 - pixel_width * bits_per_pixel
        if unused_bits and raw_row[-1] & ((1 << unused_bits) - 1):
            raise InvalidImageError("invalid_image", "bits PNG inutilisés non nuls")

    def consume(output: bytes) -> None:
        nonlocal produced, filter_index, row_index, previous_row
        start = produced
        produced += len(output)
        if produced > raw_size:
            raise InvalidImageError("invalid_image", "données PNG décompressées incohérentes")
        while filter_index < len(filter_offsets) and filter_offsets[filter_index] < produced:
            offset = filter_offsets[filter_index]
            if output[offset - start] > 4:
                raise InvalidImageError("invalid_image", "filtre PNG invalide")
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
                    raise InvalidImageError("invalid_image", "flux zlib PNG incohérent")
                pending = data[chunk_start:chunk_end]
                while pending:
                    output = decompressor.decompress(pending, 64 * 1024)
                    consume(output)
                    pending = decompressor.unconsumed_tail
                    if decompressor.eof:
                        if decompressor.unused_data or pending:
                            raise InvalidImageError("invalid_image", "flux zlib PNG incohérent")
                        stream_ended = True
                        break
            if chunk_type == b"IEND":
                break
            pos = chunk_end + 4
        if not decompressor.eof:
            raise InvalidImageError("invalid_image", "flux zlib PNG incomplet")
        consume(decompressor.flush())
    except zlib.error as exc:
        raise InvalidImageError("invalid_image", "compression PNG invalide") from exc
    if (
        produced != raw_size
        or filter_index != len(filter_offsets)
        or row_index != len(row_layout)
        or row_buffer
    ):
        raise InvalidImageError("invalid_image", "taille PNG décompressée incohérente")


def inspect_image(data: bytes, *, max_pixels: int = MAX_PIXELS) -> ImageInfo:
    """Identifie et valide une image à partir de son contenu."""
    if not (1 <= max_pixels <= HARD_MAX_PIXELS):
        raise ValueError(f"max_pixels doit être entre 1 et {HARD_MAX_PIXELS}")
    if not data:
        raise InvalidImageError("empty_upload", "upload vide")
    fmt = None
    for sig, candidate in _SIGNATURES:
        if data.startswith(sig):
            fmt = candidate
            break
    if fmt is None:
        raise InvalidImageError(
            "unsupported_format",
            "contenu non reconnu (formats acceptés : PNG, JPEG, WebP)",
        )
    return _PARSERS[fmt](data, max_pixels)


def mime_allowed(declared: str | None) -> bool:
    """Vérifie le Content-Type déclaré (indicatif, jamais déterminant)."""
    if not declared:
        return True  # traité comme application/octet-stream
    return declared.split(";")[0].strip().lower() in ALLOWED_DECLARED_MIMES


def mime_syntax_allowed(declared: str | None) -> bool:
    """Vérifie la syntaxe d'un MIME avant de le laisser influencer le type."""
    if not declared:
        return True
    value = declared.split(";", 1)[0].strip().lower()
    return len(value) <= MAX_MIME_LENGTH and bool(
        re.fullmatch(
            r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+",
            value,
        )
    )


def extension_for(fmt: str) -> str:
    return FORMATS[fmt][0]


def mime_for(fmt: str) -> str:
    return FORMATS[fmt][1]
