"""Tests du module images : sniffing structurel, dimensions, refus."""
import struct
import unittest
import zlib
from unittest import mock

from tests.helpers import (
    _png_chunk,
    make_jpeg,
    make_png,
    make_webp_lossy,
    make_webp_vp8l,
    make_webp_vp8x,
)
from pasteberth.images import (
    InvalidImageError,
    inspect_image,
    mime_allowed,
    mime_syntax_allowed,
)


class TestPng(unittest.TestCase):
    def test_dimensions(self):
        for w, h in [(1, 1), (1920, 1080), (7, 3)]:
            info = inspect_image(make_png(w, h))
            self.assertEqual((info.fmt, info.width, info.height), ("png", w, h))

    def test_truncated(self):
        with self.assertRaises(InvalidImageError) as ctx:
            inspect_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        self.assertEqual(ctx.exception.code, "invalid_image")

    def test_absurd_dimensions_rejected(self):
        bad = make_png(4, 4)
        bad = bad[:16] + (200000).to_bytes(4, "big") + bad[20:]
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_dimensions_verifiees_avant_offsets(self):
        ihdr = struct.pack(">IIBBBBB", 20_000, 20_000, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b""))
            + _png_chunk(b"IEND", b"")
        )
        with mock.patch("pasteberth.images._png_filter_offsets") as offsets:
            with self.assertRaises(InvalidImageError):
                inspect_image(bad)
        offsets.assert_not_called()

    def test_adam7_valide(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 1)
        adam7 = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\x40\x80\xc0"))
            + _png_chunk(b"IEND", b"")
        )
        info = inspect_image(adam7)
        self.assertEqual((info.width, info.height), (1, 1))

    def test_ihdr_duplique_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        duplicate = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\x40\x80\xc0"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(duplicate)

    def test_chunk_critique_inconnu_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"ABCD", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_idat_non_contigus_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        idat = zlib.compress(b"\x00\x40\x80\xc0")
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", idat)
            + _png_chunk(b"tEXt", b"comment\x00value")
            + _png_chunk(b"IDAT", b"")
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_plte_dupliquee_rejetee(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        palette = b"\x00\x00\x00"
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"PLTE", palette)
            + _png_chunk(b"PLTE", palette)
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_plte_indexee_bornee_par_bit_depth(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 1, 3, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"PLTE", b"\x00\x00\x00" * 3)
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_compression_idat_invalide_rejetee(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", b"not-zlib")
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_filtre_scanline_invalide_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x05\x40\x80\xc0"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_chunk_png_trop_nombreux_rejete(self):
        with mock.patch("pasteberth.images._MAX_PNG_CHUNKS", 2):
            with self.assertRaises(InvalidImageError):
                inspect_image(make_png())

    def test_trns_png_invalide_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"tRNS", b"\x00")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_plte_interdite_pour_niveaux_de_gris(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"PLTE", b"\x00\x00\x00")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_trns_duplique_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"tRNS", b"\x00" * 6)
            + _png_chunk(b"tRNS", b"\x00" * 6)
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_bit_reserve_chunk_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"tExT", b"comment\x00value")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_indice_palette_png_hors_limites_rejete(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 1, 3, 0, 0, 0)
        bad = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"PLTE", b"\x00\x00\x00")
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\x80"))
            + _png_chunk(b"IEND", b"")
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)

    def test_bits_png_inutilises_doivent_etre_nuls(self):
        ihdr = struct.pack(">IIBBBBB", 1, 1, 1, 0, 0, 0, 0)
        valid = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"\x00\x80"))
            + _png_chunk(b"IEND", b"")
        )
        self.assertEqual(inspect_image(valid).width, 1)
        bad = valid.replace(zlib.compress(b"\x00\x80"), zlib.compress(b"\x00\x81"))
        with self.assertRaises(InvalidImageError):
            inspect_image(bad)


class TestJpeg(unittest.TestCase):
    def test_baseline(self):
        info = inspect_image(make_jpeg(640, 480))
        self.assertEqual((info.fmt, info.width, info.height), ("jpeg", 640, 480))

    def test_progressive_with_exif_and_comment(self):
        info = inspect_image(make_jpeg(320, 240, progressive=True, extra_segments=True))
        self.assertEqual((info.fmt, info.width, info.height), ("jpeg", 320, 240))

    def test_without_sof_rejected(self):
        data = b"\xff\xd8\xff\xd9"
        with self.assertRaises(InvalidImageError) as ctx:
            inspect_image(data)
        self.assertEqual(ctx.exception.code, "invalid_image")

    def test_desynchronized_rejected(self):
        with self.assertRaises(InvalidImageError):
            inspect_image(make_jpeg()[:6] + b"\x00garbage" + make_jpeg()[12:])

    def test_sof_incomplet_rejete(self):
        data = (
            b"\xff\xd8"
            + b"\xff\xc0\x00\x07\x08\x00\x01\x00\x01"
            + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00"
            + b"\xff\xd9"
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(data)

    def test_sos_sans_donnees_rejete(self):
        data = (
            b"\xff\xd8"
            + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
            + b"\xff\xd9"
        )
        with self.assertRaises(InvalidImageError):
            inspect_image(data)


class TestWebp(unittest.TestCase):
    def test_lossless_vp8l(self):
        info = inspect_image(make_webp_vp8l(1024, 768))
        self.assertEqual((info.fmt, info.width, info.height), ("webp", 1024, 768))

    def test_lossy_vp8(self):
        info = inspect_image(make_webp_lossy(800, 600))
        self.assertEqual((info.fmt, info.width, info.height), ("webp", 800, 600))

    def test_extended_vp8x(self):
        info = inspect_image(make_webp_vp8x(3000, 2000))
        self.assertEqual((info.fmt, info.width, info.height), ("webp", 3000, 2000))

    def test_extended_sans_payload_rejete(self):
        with self.assertRaises(InvalidImageError):
            inspect_image(make_webp_vp8x(300, 200, with_payload=False))

    def test_budget_pixels(self):
        with self.assertRaises(InvalidImageError):
            inspect_image(make_webp_vp8x(6000, 5000))
        info = inspect_image(make_webp_vp8x(6000, 5000), max_pixels=50_000_000)
        self.assertEqual((info.width, info.height), (6000, 5000))

    def test_truncated_payload_rejected(self):
        data = make_webp_vp8l(50, 40)
        with self.assertRaises(InvalidImageError):
            inspect_image(data[:-1])

    def test_versions_et_bits_reserves_rejetes(self):
        vp8 = bytearray(make_webp_lossy())
        vp8[20] |= 0x02
        with self.assertRaises(InvalidImageError):
            inspect_image(bytes(vp8))

        vp8l = bytearray(make_webp_vp8l())
        bits = struct.unpack("<I", vp8l[21:25])[0] | 0x20000000
        vp8l[21:25] = struct.pack("<I", bits)
        with self.assertRaises(InvalidImageError):
            inspect_image(bytes(vp8l))

        vp8x = bytearray(make_webp_vp8x())
        vp8x[20] |= 0x80
        with self.assertRaises(InvalidImageError):
            inspect_image(bytes(vp8x))

    def test_canvas_et_trame_doivent_coincider(self):
        bad = bytearray(make_webp_vp8x(300, 200))
        bad[24:27] = (301 - 1).to_bytes(3, "little")
        with self.assertRaises(InvalidImageError):
            inspect_image(bytes(bad))


class TestRejets(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(InvalidImageError) as ctx:
            inspect_image(b"")
        self.assertEqual(ctx.exception.code, "empty_upload")

    def test_text(self):
        with self.assertRaises(InvalidImageError) as ctx:
            inspect_image(b"hello world, definitely not an image")
        self.assertEqual(ctx.exception.code, "unsupported_format")

    def test_gif_rejected(self):
        with self.assertRaises(InvalidImageError) as ctx:
            inspect_image(b"GIF89a" + b"\x00" * 20)
        self.assertEqual(ctx.exception.code, "unsupported_format")

    def test_bmp_rejected(self):
        with self.assertRaises(InvalidImageError):
            inspect_image(b"BM" + b"\x00" * 30)

    def test_mime_declared_indicatif(self):
        # Le Content-Type déclaré ne fait jamais foi.
        self.assertTrue(mime_allowed("image/png"))
        self.assertTrue(mime_allowed("application/octet-stream"))
        self.assertTrue(mime_allowed(None))
        self.assertTrue(mime_allowed("image/png; charset=binary"))
        self.assertTrue(mime_allowed("text/plain"))
        self.assertFalse(mime_allowed("image/gif"))
        self.assertTrue(mime_allowed("text/html"))

    def test_mime_syntax_bornee(self):
        self.assertTrue(mime_syntax_allowed("text/plain; charset=utf-8"))
        self.assertFalse(mime_syntax_allowed("text/" + "a" * 121))


if __name__ == "__main__":
    unittest.main()
