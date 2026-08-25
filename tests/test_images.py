"""Tests du module images : sniffing structurel, dimensions, refus."""
import unittest

from tests.helpers import make_jpeg, make_png, make_webp_lossy, make_webp_vp8l, make_webp_vp8x
from pasteberth.images import InvalidImageError, inspect_image, mime_allowed


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
        self.assertFalse(mime_allowed("text/plain"))
        self.assertFalse(mime_allowed("image/gif"))
        self.assertFalse(mime_allowed("text/html"))


if __name__ == "__main__":
    unittest.main()
