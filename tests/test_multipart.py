"""Tests du parser multipart borné."""
import unittest

from pasteberth.multipart import MultipartError, extract_boundary, parse_multipart
from tests.helpers import build_multipart


class TestBoundary(unittest.TestCase):
    def test_extractions(self):
        self.assertEqual(extract_boundary("multipart/form-data; boundary=abc123"), "abc123")
        self.assertEqual(
            extract_boundary('multipart/form-data; boundary="quoted bound"'), "quoted bound"
        )
        self.assertIsNone(extract_boundary("multipart/form-data"))
        self.assertIsNone(extract_boundary("application/json"))
        self.assertIsNone(extract_boundary("multipart/form-data; boundary="))
        self.assertIsNone(extract_boundary('multipart/form-data; boundary="' + "x" * 80 + '"'))


class TestParsing(unittest.TestCase):
    def test_champ_image_simple(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        body, ctype = build_multipart(data=data)
        fields = parse_multipart(body, extract_boundary(ctype))
        self.assertIn("image", fields)
        filename, part_ctype, content = fields["image"]
        self.assertEqual(content, data)
        self.assertEqual(filename, "clipboard.png")
        self.assertEqual(part_ctype, "image/png")

    def test_plusieurs_champs_binaire_preserve(self):
        binary = bytes(range(256)) + b"\r\n--fake\r\n" + b"\x00"
        body = (
            b"--B\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhello\r\n"
            b"--B\r\nContent-Disposition: form-data; name=\"image\"; filename=\"c.png\"\r\n"
            b"Content-Type: image/png\r\n\r\n" + binary + b"\r\n"
            b"--B--\r\n"
        )
        fields = parse_multipart(body, "B")
        self.assertEqual(fields["note"], (None, None, b"hello"))
        self.assertEqual(fields["image"][2], binary)

    def test_filename_echappe(self):
        body = (
            b'--B\r\nContent-Disposition: form-data; name="image"; '
            b'filename="a\\"b.png"\r\n\r\nDATA\r\n--B--\r\n'
        )
        fields = parse_multipart(body, "B")
        self.assertEqual(fields["image"][0], 'a"b.png')

    def test_trop_de_parties(self):
        parts = []
        for i in range(40):
            parts.append(f'--B\r\nContent-Disposition: form-data; name="f{i}"\r\n\r\nx\r\n'.encode())
        body = b"".join(parts) + b"--B--\r\n"
        with self.assertRaises(MultipartError):
            parse_multipart(body, "B")

    def test_en_tetes_oversize(self):
        huge = b"x" * 20000
        body = f'--B\r\nContent-Disposition: form-data; name="{huge.decode()}"\r\n\r\nv\r\n--B--\r\n'.encode()
        with self.assertRaises(MultipartError):
            parse_multipart(body, "B")

    def test_marqueur_final_absent_tolerant(self):
        # Un client interrompu peut couper avant le marqueur final.
        body = b'--B\r\nContent-Disposition: form-data; name="image"\r\n\r\nPARTIAL'
        fields = parse_multipart(body, "B")
        self.assertEqual(fields["image"][2], b"PARTIAL")

    def test_corps_vide_rejete(self):
        with self.assertRaises(MultipartError):
            parse_multipart(b"", "B")


if __name__ == "__main__":
    unittest.main()
