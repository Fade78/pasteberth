"""Tests du stockage : noms uniques, sidecars, rétention, ownership."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pasteberth.images import ImageInfo
from pasteberth.storage import DestinationError, LocalDestination, valid_filename
from tests.helpers import make_png

INFO = lambda w=4, h=3: ImageInfo(fmt="png", width=w, height=h)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "images"
        self.dest = LocalDestination(self.dir)
        self.addCleanup(self._tmp.cleanup)

    def save(self, n=1):
        return [self.dest.save(make_png(2, 2), INFO()) for _ in range(n)]


class TestSauvegarde(Base):
    def test_fichier_et_sidecar(self):
        stored = self.save()[0]
        self.assertRegex(stored.filename, r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.png$")
        self.assertTrue((self.dir / stored.filename).is_file())
        meta = json.loads((self.dir / (stored.filename + ".json")).read_text())
        self.assertEqual(meta["filename"], stored.filename)
        self.assertEqual(meta["width"], 2)
        self.assertEqual(meta["size"], len(make_png(2, 2)))
        self.assertIn("T", meta["created_at"])

    def test_noms_uniques_rapides(self):
        names = {s.filename for s in self.save(20)}
        self.assertEqual(len(names), 20)

    def test_collision_forcee_resolue(self):
        saved = []
        with mock.patch("pasteberth.storage.secrets.token_hex", return_value="cafe42"):
            saved.append(self.dest.save(make_png(), INFO()).filename)
            saved.append(self.dest.save(make_png(), INFO()).filename)
        # Le second save a détecté la collision et régénéré un suffixe.
        self.assertNotEqual(saved[0], saved[1])
        self.assertEqual(len(list(self.dir.glob("*.png"))), 2)


class TestListe(Base):
    def test_plus_recent_en_premier(self):
        items = self.save(3)
        listed = self.dest.list()
        self.assertEqual([i.filename for i in listed], [i.filename for i in reversed(items)])

    def test_reconstruction_apres_restart(self):
        items = self.save(4)
        fresh = LocalDestination(self.dir)
        self.assertEqual(
            [i.filename for i in fresh.list()],
            [i.filename for i in reversed(items)],
        )
        original = fresh.list()[0]
        self.assertEqual(original.width, 2)
        self.assertEqual(original.fmt, "png")


class TestRetention(Base):
    def test_suppression_exacte_des_plus_anciennes(self):
        all_saved = [s.filename for s in self.save(5)]
        deleted = self.dest.apply_retention(3)
        self.assertEqual(sorted(deleted), sorted(all_saved[:2]))
        remaining = {i.filename for i in self.dest.list()}
        self.assertEqual(remaining, set(all_saved[2:]))
        for name in all_saved[:2]:
            self.assertFalse((self.dir / name).exists())
            self.assertFalse((self.dir / (name + ".json")).exists())

    def test_retain_1(self):
        saved = [s.filename for s in self.save(3)]
        self.dest.apply_retention(1)
        self.assertEqual({i.filename for i in self.dest.list()}, {saved[-1]})


class TestOwnership(Base):
    def test_fichier_etranger_jamais_touche(self):
        stranger = self.dir / "vacation_photo.png"
        stranger.write_bytes(b"not pasteberth data")
        note = self.dir / "notes.txt"
        note.write_text("hello")
        self.save(3)
        self.dest.apply_retention(1)
        self.assertTrue(stranger.exists())
        self.assertTrue(note.exists())
        self.assertEqual([i.filename for i in self.dest.list() if i.filename == "vacation_photo.png"], [])

    def test_delete_exige_sidecar(self):
        with self.assertRaises(DestinationError):
            self.dest.delete("2026-01-01_00-00-00_abcdef.png")
        victim = self.dir / "2026-01-01_00-00-00_abcdef.png"
        victim.write_bytes(b"x")
        with self.assertRaises(DestinationError):
            self.dest.delete(victim.name)

    def test_traversal_refuse(self):
        for bad in ["../evil.png", "sub/dir.png", "..\\evil.png", ".hidden.json"]:
            with self.assertRaises(DestinationError):
                self.dest.delete(bad)
            with self.assertRaises(DestinationError):
                self.dest.read(bad)

    def test_sidecar_orphelin_nettoye(self):
        stored = self.save()[0]
        (self.dir / stored.filename).unlink()
        self.assertEqual(self.dest.list(), [])
        self.assertFalse((self.dir / (stored.filename + ".json")).exists())

    def test_sidecar_corrompu_conservé_et_ignore(self):
        self.save()
        bad = self.dir / "1999-01-01_00-00-00_dead00.png.json"
        bad.write_text("{corrompu")
        self.assertEqual(len(self.dest.list()), 1)
        self.assertTrue(bad.exists())


class TestRepertoires(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_creation_auto(self):
        target = self.tmp / "a" / "b"
        dest = LocalDestination(target, create_directory=True)
        dest.save(make_png(), INFO(1, 1))
        self.assertTrue(target.is_dir())

    def test_sans_creation_refuse(self):
        target = self.tmp / "absent"
        dest = LocalDestination(target, create_directory=False)
        with self.assertRaises(DestinationError):
            dest.save(make_png(), INFO(1, 1))

    def test_reference_path_absolu(self):
        dest = LocalDestination(self.tmp / "r", create_directory=True)
        stored = dest.save(make_png(), INFO(1, 1))
        ref = dest.reference_path(stored.filename)
        self.assertTrue(Path(ref).is_absolute())
        self.assertEqual(Path(ref), self.tmp / "r" / stored.filename)

    def test_valid_filename(self):
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c.png"))
        self.assertFalse(valid_filename("../../etc/passwd"))
        self.assertFalse(valid_filename("random.png"))
        self.assertFalse(valid_filename("2026-08-25_01-22-31_a81c.gif"))


if __name__ == "__main__":
    unittest.main()
