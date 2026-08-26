"""Tests du stockage : noms uniques, sidecars, rétention, ownership."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pasteberth.images import ImageInfo
from pasteberth.storage import (
    DestinationError,
    LocalDestination,
    RetentionError,
    StorageLowError,
    valid_filename,
)
from tests.helpers import make_png

INFO = lambda w=4, h=3: ImageInfo(fmt="png", width=w, height=h)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "images"
        self.dest = LocalDestination(self.dir)
        self.addCleanup(self._tmp.cleanup)

    def save(self, n=1):
        return [self.dest.save(make_png(2, 2), INFO(2, 2)) for _ in range(n)]


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
        self.assertEqual(stat.S_IMODE((self.dir / stored.filename).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.dir / (stored.filename + ".json")).stat().st_mode), 0o600)

    def test_noms_uniques_rapides(self):
        names = {s.filename for s in self.save(20)}
        self.assertEqual(len(names), 20)

    def test_collision_forcee_resolue(self):
        import itertools

        suffixes = itertools.cycle(["cafe42", "abcd42"])
        saved = []
        with mock.patch("pasteberth.storage.secrets.token_hex",
                        lambda n: next(suffixes)):
            saved.append(self.dest.save(make_png(), INFO()).filename)
            # même seconde + premier suffixe déjà pris -> régénération
            saved.append(self.dest.save(make_png(), INFO()).filename)
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

    def test_sidecar_types_invalides_ignores(self):
        stored = self.save()[0]
        sidecar = self.dir / (stored.filename + ".json")
        raw = json.loads(sidecar.read_text())
        raw["filename"] = 123
        sidecar.write_text(json.dumps(raw))
        self.assertEqual(self.dest.list(), [])

    def test_image_symbolique_refusee_en_lecture(self):
        stored = self.save()[0]
        original = self.dir / stored.filename
        original.unlink()
        target = self.dir.parent / "outside.png"
        target.write_bytes(b"secret")
        original.symlink_to(target)
        with self.assertRaises(DestinationError):
            self.dest.read(stored.filename)


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
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_repertoire_existant_non_prive_accepte(self):
        target = self.tmp / "open"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        dest = LocalDestination(target)
        self.assertTrue(dest.save(make_png(), INFO(1, 1)).filename)

    def test_repertoire_group_writable_accepte(self):
        # Feature: un mode group-writable est accepté au runtime (warning au
        # démarrage seulement). Refuser au runtime casserait les zones partagées.
        target = self.tmp / "shared"
        target.mkdir(mode=0o775)
        os.chmod(target, 0o775)
        dest = LocalDestination(target)
        self.assertTrue(dest.save(make_png(), INFO(1, 1)).filename)

    def test_sans_creation_refuse(self):
        target = self.tmp / "absent"
        # Échec immédiat : la destination refuse un répertoire inexistant
        # quand la création n'est pas autorisée.
        with self.assertRaises(DestinationError):
            LocalDestination(target, create_directory=False)

    def test_lien_symbolique_parent_refuse(self):
        outside = self.tmp / "outside"
        outside.mkdir(mode=0o700)
        link = self.tmp / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(DestinationError, "symbolique"):
            LocalDestination(link / "images")

    def test_reference_path_absolu(self):
        dest = LocalDestination(self.tmp / "r", create_directory=True)
        stored = dest.save(make_png(), INFO(1, 1))
        ref = dest.reference_path(stored.filename)
        self.assertTrue(Path(ref).is_absolute())
        self.assertEqual(Path(ref), self.tmp / "r" / stored.filename)

    def test_valid_filename(self):
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c42.png"))
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c42.txt"))
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c42.pdf"))
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c42.gif"))
        self.assertFalse(valid_filename("../../etc/passwd"))
        self.assertFalse(valid_filename("random.png"))
        self.assertFalse(valid_filename("2026-08-25_01-22-31_a81c42.exe.bat"))

    def test_espace_libre_sous_seuil(self):
        dest = LocalDestination(self.tmp / "space")
        usage = type("Usage", (), {"f_blocks": 1000, "f_bavail": 10, "f_frsize": 1024})()
        with mock.patch("pasteberth.storage.os.statvfs", return_value=usage):
            with self.assertRaises(StorageLowError):
                dest.ensure_space(1024, 2.0)

    def test_reconciliation_supprime_un_orphelin_ancien(self):
        target = self.tmp / "reconcile"
        LocalDestination(target)
        orphan = target / "2026-01-01_00-00-00_abcdef.png"
        orphan.write_bytes(b"orphan")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        LocalDestination(target)
        self.assertFalse(orphan.exists())

    def test_retention_signale_les_echecs(self):
        dest = LocalDestination(self.tmp / "retention")
        dest.save(make_png(), INFO())
        with mock.patch.object(dest, "delete", side_effect=DestinationError("no")):
            with self.assertRaises(RetentionError):
                dest.apply_retention(0)


if __name__ == "__main__":
    unittest.main()
