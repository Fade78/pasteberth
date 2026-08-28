"""Tests de classification du contenu : image, texte, binaire."""
import struct
import unittest
import zlib

from pasteberth.content import classify, safe_extension
from tests.helpers import _png_chunk, make_png, make_jpeg, make_webp_lossy


class TestClassify(unittest.TestCase):
    def test_image_png_par_contenu(self):
        info = classify(make_png(10, 5), "application/octet-stream")
        self.assertEqual(info.kind, "image")
        self.assertEqual(info.ext, ".png")
        self.assertEqual(info.mime, "image/png")
        self.assertEqual((info.width, info.height), (10, 5))

    def test_image_jpeg_et_webp(self):
        self.assertEqual(classify(make_jpeg(4, 4), None).kind, "image")
        self.assertEqual(classify(make_webp_lossy(4, 4), None).kind, "image")

    def test_image_invalide_devient_binaire(self):
        info = classify(make_webp_lossy()[:-1], "image/webp", "capture.webp")
        self.assertEqual(info.kind, "binary")
        self.assertEqual(info.ext, ".webp")
        self.assertEqual(info.mime, "application/octet-stream")

    def test_image_trop_grande_devient_binaire(self):
        ihdr = struct.pack(">IIBBBBB", 20_000, 20_000, 8, 2, 0, 0, 0)
        data = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b""))
            + _png_chunk(b"IEND", b"")
        )
        info = classify(data, None, max_pixels=25_000_000)
        self.assertEqual(info.kind, "binary")
        self.assertEqual(info.ext, ".bin")

    def test_texte_utf8_par_defaut_txt(self):
        info = classify("hello world".encode(), "application/octet-stream")
        self.assertEqual(info.kind, "text")
        self.assertEqual(info.ext, ".txt")
        self.assertEqual(info.mime, "text/plain")

    def test_texte_markdown_selon_type_declare(self):
        info = classify("# Titre\n\n- item".encode(), "text/markdown")
        self.assertEqual(info.kind, "text")
        self.assertEqual(info.ext, ".md")
        self.assertEqual(info.mime, "text/markdown")

    def test_texte_json_selon_type_declare(self):
        info = classify(b'{"a": 1}', "application/json")
        self.assertEqual(info.ext, ".json")

    def test_texte_html_selon_type_declare(self):
        info = classify(b"<p>hi</p>", "text/html")
        self.assertEqual(info.ext, ".html")

    def test_binaire_avec_extension_dorigine(self):
        info = classify(b"\x00\x01\x02\x03", "application/octet-stream", "archive.zip")
        self.assertEqual(info.kind, "binary")
        self.assertEqual(info.ext, ".zip")
        self.assertEqual(info.mime, "application/octet-stream")

    def test_binaire_sans_hint_bin(self):
        info = classify(b"\x00\x01\x02\x03", None)
        self.assertEqual(info.kind, "binary")
        self.assertEqual(info.ext, ".bin")

    def test_binaire_extension_assainie(self):
        info = classify(b"\x00\x01", None, "fichier..exe")
        self.assertEqual(info.ext, ".exe")

    def test_texte_avec_nul_est_binaire(self):
        info = classify(b"abc\x00def", "text/plain")
        self.assertEqual(info.kind, "binary")


class TestSafeExtension(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(safe_extension("archive.tar.gz"), ".gz")
        self.assertEqual(safe_extension("fichier.PDF"), ".pdf")
        self.assertIsNone(safe_extension("sans-extension"))
        self.assertIsNone(safe_extension("..exe"))
        self.assertIsNone(safe_extension("fichier." + "x" * 20))


if __name__ == "__main__":
    unittest.main()
