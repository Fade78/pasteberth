"""Tests du stockage : noms uniques, sidecars, rétention, ownership."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pasteberth.storage as storage_module
from pasteberth.images import ImageInfo
from pasteberth.storage import (
    DestinationError,
    LocalDestination,
    RetentionError,
    StorageConflictError,
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

    def test_sidecar_ancien_sans_kind_mime_accepte(self):
        # Compatibilité : les sidecars v1.0.1/v1.0.2 (6 clés) restent lisibles.
        stored = self.save()[0]
        meta = json.loads((self.dir / (stored.filename + ".json")).read_text())
        del meta["kind"]
        del meta["mime"]
        (self.dir / (stored.filename + ".json")).write_text(json.dumps(meta))
        items = self.dest.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "image")
        self.assertEqual(items[0].mime, "image/png")
        self.assertEqual(len(self.dest.read(stored.filename)), len(make_png(2, 2)))

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

    def test_nom_explicit_et_ecrasement_atomique(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        first = self.dest.save(b"old", info, filename="archive final.zip")
        second = self.dest.save(b"new", info, filename="archive final.zip")

        self.assertEqual(first.filename, "archive final.zip")
        self.assertEqual(second.filename, first.filename)
        self.assertEqual(self.dest.read(first.filename), b"new")
        self.assertEqual([item.filename for item in self.dest.list()], [first.filename])
        self.assertEqual(list(self.dir.glob(".pb*")), [])

    def test_nom_explicit_necrase_pas_un_fichier_etranger(self):
        foreign = self.dir / "archive.zip"
        foreign.write_bytes(b"foreign")
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )

        with self.assertRaises(StorageConflictError):
            self.dest.save(b"replacement", info, filename=foreign.name)
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_nom_explicit_abandonne_si_cible_etrangere_apparait(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        target = self.dir / "race.zip"

        original_install = self.dest._install_new

        def appear_then_install(directory_fd, temp_name, target_name):
            if target_name == target.name and not target.exists():
                target.write_bytes(b"foreign")
            return original_install(directory_fd, temp_name, target_name)

        with mock.patch.object(self.dest, "_install_new", side_effect=appear_then_install):
            with self.assertRaises(StorageConflictError):
                self.dest.save(b"replacement", info, filename=target.name)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_nom_explicit_echec_apres_installation_est_recupere(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        target = self.dir / "transaction.zip"
        original_move = storage_module._rename_noreplace

        def fail_after_data_install(directory_fd, source, destination):
            result = original_move(directory_fd, source, destination)
            if destination == target.name and source.startswith(".pbdata-"):
                raise OSError("coupure simulée")
            return result

        with mock.patch(
            "pasteberth.storage._rename_noreplace",
            side_effect=fail_after_data_install,
        ):
            with self.assertRaises(OSError):
                self.dest.save(b"replacement", info, filename=target.name)
        self.assertFalse(target.exists())
        self.assertFalse((self.dir / (target.name + ".json")).exists())
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_nom_explicit_abandonne_si_cible_existante_change(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        target = self.dir / "race-existing.zip"
        self.dest.save(b"old", info, filename=target.name)
        original_write = self.dest._write_data_temp

        def swap_then_write(directory_fd, data):
            target.unlink()
            target.write_bytes(b"foreign")
            return original_write(directory_fd, data)

        with mock.patch.object(self.dest, "_write_data_temp", side_effect=swap_then_write):
            with self.assertRaises(StorageConflictError):
                self.dest.save(b"replacement", info, filename=target.name)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_nom_explicit_ne_remplace_pas_un_etranger_apparu_avant_deplacement(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        target = self.dir / "race-before-move.zip"
        self.dest.save(b"old", info, filename=target.name)
        original_move = storage_module._rename_noreplace

        def swap_before_move(directory_fd, source, destination):
            if source == target.name and destination.startswith(".pbbackup-"):
                target.unlink()
                target.write_bytes(b"foreign")
            return original_move(directory_fd, source, destination)

        with mock.patch(
            "pasteberth.storage._rename_noreplace",
            side_effect=swap_before_move,
        ):
            with self.assertRaises(StorageConflictError):
                self.dest.save(b"replacement", info, filename=target.name)
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_remplacement_inacheve_recupere_ancien_fichier_apres_redemarrage(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "crash.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_move = self.dest._move_expected

        def crash_after_backup(*args):
            result = original_move(*args)
            if args[2].startswith(".pbbackup-"):
                raise SystemExit("crash simulé")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=crash_after_backup):
            with mock.patch.object(self.dest, "_rollback_transaction", return_value=False):
                with self.assertRaises(SystemExit):
                    self.dest.save(b"new", info, filename=target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"old")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))
        self.assertFalse(list(self.dir.glob(".pbbackup-*")))

    def test_nettoyage_apres_commit_ne_rollback_pas_la_nouvelle_version(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed.bin"
        self.dest.save(b"old", info, filename=target.name)

        def fail_after_deleting_one_backup(directory_fd, transaction, marker_name, commit_name):
            os.unlink(transaction["data_backup"], dir_fd=directory_fd)
            raise OSError("nettoyage interrompu")

        with mock.patch.object(
            self.dest,
            "_cleanup_committed_transaction",
            side_effect=fail_after_deleting_one_backup,
        ):
            self.dest.save(b"new", info, filename=target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")

    def test_commit_conserve_les_backups_si_la_cible_publique_change(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_cleanup = self.dest._cleanup_committed_transaction

        def replace_public_target(directory_fd, transaction, marker_name, commit_name):
            target.unlink()
            target.write_bytes(b"bad")
            return original_cleanup(directory_fd, transaction, marker_name, commit_name)

        with mock.patch.object(
            self.dest,
            "_cleanup_committed_transaction",
            side_effect=replace_public_target,
        ):
            self.dest.save(b"new", info, filename=target.name)

        self.assertTrue(list(self.dir.glob(".pbbackup-*")))
        recovered = LocalDestination(self.dir)
        self.assertTrue(list(self.dir.glob(".pbbackup-*")))
        self.assertEqual(recovered.list(), [])

    def test_sidecar_orphelin_refuse_en_remplacement(self):
        stored = self.save()[0]
        (self.dir / stored.filename).unlink()
        with self.assertRaisesRegex(DestinationError, "orphelin"):
            self.dest.save(b"new", INFO(), filename=stored.filename)


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


class TestDotfiles(Base):
    def test_fichier_point_visible_dans_l_historique(self):
        info = ImageInfo(fmt=None, width=None, height=None, kind="binary",
                         mime="application/octet-stream", ext=".txt")
        stored = self.dest.save(b"secret", info, filename=".notes.txt")
        self.assertEqual([i.filename for i in self.dest.list()], [".notes.txt"])
        self.assertEqual(self.dest.read(".notes.txt"), b"secret")
        self.assertTrue((self.dir / (stored.filename + ".json")).is_file())

    def test_fichier_point_compte_en_retention(self):
        info = ImageInfo(fmt=None, width=None, height=None, kind="binary",
                         mime="application/octet-stream", ext=".txt")
        self.dest.save(b"old", info, filename=".a.txt")
        self.dest.save(b"new", info, filename=".b.txt")
        deleted = self.dest.apply_retention(1)
        self.assertEqual(deleted, [".a.txt"])
        self.assertEqual([i.filename for i in self.dest.list()], [".b.txt"])

    def test_backup_interne_crash_reste_invisible(self):
        stored = self.save()[0]
        meta = json.loads((self.dir / (stored.filename + ".json")).read_text())
        backup = self.dir / ".pbbackup-cafebabe1234567890abcdef.json"
        backup.write_text(json.dumps(meta))
        self.assertEqual([i.filename for i in self.dest.list()], [stored.filename])
        self.assertTrue(backup.exists())

    def test_verrou_et_temporaires_non_json_ignores(self):
        (self.dir / ".pasteberth.lock").write_bytes(b"x")
        (self.dir / ".pbmeta-cafebabe1234567890abcdef.tmp").write_text("{}")
        (self.dir / ".pbdata-cafebabe1234567890abcdef.tmp").write_bytes(b"x")
        self.assertEqual(self.dest.list(), [])


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

    def test_delete_n_efface_pas_une_cible_changee(self):
        stored = self.save()[0]
        target = self.dir / stored.filename
        original_identity = LocalDestination._entry_identity
        swapped = False

        def swap_before_target_check(directory_fd, name):
            nonlocal swapped
            if name == stored.filename + ".json" and not swapped:
                target.unlink()
                target.write_bytes(b"foreign")
                swapped = True
            return original_identity(directory_fd, name)

        with mock.patch.object(
            self.dest,
            "_entry_identity",
            side_effect=swap_before_target_check,
        ):
            with self.assertRaises(StorageConflictError):
                self.dest.delete(stored.filename)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_lecture_conserve_le_descripteur_ouvert_si_la_cible_change(self):
        stored = self.save()[0]
        target = self.dir / stored.filename
        original = target.read_bytes()
        foreign = b"foreign" * (len(original) // len(b"foreign"))
        foreign += b"x" * (len(original) - len(foreign))
        original_read_meta = self.dest._read_meta

        def replace_after_target_open(directory_fd, name):
            if name == stored.filename + ".json":
                target.unlink()
                target.write_bytes(foreign)
            return original_read_meta(directory_fd, name)

        with mock.patch.object(
            self.dest,
            "_read_meta",
            side_effect=replace_after_target_open,
        ):
            self.assertEqual(self.dest.read(stored.filename), original)
        self.assertEqual(target.read_bytes(), foreign)

    def test_delete_ne_deplace_pas_un_etranger_apparu_avant_deplacement(self):
        stored = self.save()[0]
        target = self.dir / stored.filename
        original_move = storage_module._rename_noreplace

        def swap_before_move(directory_fd, source, destination):
            if source == stored.filename and destination.startswith(".pbtrash-"):
                target.unlink()
                target.write_bytes(b"foreign")
            return original_move(directory_fd, source, destination)

        with mock.patch(
            "pasteberth.storage._rename_noreplace",
            side_effect=swap_before_move,
        ):
            with self.assertRaises(StorageConflictError):
                self.dest.delete(stored.filename)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_delete_nettoyage_prive_en_echec_ne_laise_pas_de_paire(self):
        stored = self.save()[0]
        original_unlink = self.dest._unlink_expected
        calls = 0

        def fail_second_cleanup(directory_fd, name, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("nettoyage interrompu")
            return original_unlink(directory_fd, name, expected)

        with mock.patch.object(
            self.dest,
            "_unlink_expected",
            side_effect=fail_second_cleanup,
        ):
            self.dest.delete(stored.filename)
        self.assertFalse((self.dir / stored.filename).exists())
        self.assertFalse((self.dir / (stored.filename + ".json")).exists())
        self.assertEqual(self.dest.list(), [])

    def test_delete_interrompu_est_repris_par_reconciliation(self):
        stored = self.save()[0]
        target = self.dir / stored.filename
        meta_name = stored.filename + ".json"
        token = "a" * 24
        marker_name = f".pbdel-{token}.json"
        transaction = {
            "version": 1,
            "target": stored.filename,
            "data_trash": f".pbtrash-{token}.data",
            "meta_trash": f".pbtrash-{token}.json",
            "target_identity": None,
            "meta_identity": None,
        }
        # Build the marker and move only the data file, matching a crash after
        # the first half of the delete operation.
        with self.dest._directory_fd() as directory_fd:
            target_identity = self.dest._entry_identity(directory_fd, stored.filename)
            meta_identity = self.dest._entry_identity(directory_fd, meta_name)
            transaction["target_identity"] = list(target_identity)
            transaction["meta_identity"] = list(meta_identity)
            self.dest._write_transaction_file(directory_fd, marker_name, transaction)
            self.dest._move_expected(
                directory_fd,
                stored.filename,
                transaction["data_trash"],
                target_identity,
            )

        fresh = LocalDestination(self.dir)
        self.assertFalse(target.exists())
        self.assertFalse((self.dir / meta_name).exists())
        self.assertFalse((self.dir / marker_name).exists())
        self.assertEqual(fresh.list(), [])

    def test_traversal_refuse(self):
        for bad in ["../evil.png", "sub/dir.png", "..\\evil.png", ".hidden.json"]:
            with self.assertRaises(DestinationError):
                self.dest.delete(bad)
            with self.assertRaises(DestinationError):
                self.dest.read(bad)

    def test_sidecar_orphelin_conserve(self):
        stored = self.save()[0]
        (self.dir / stored.filename).unlink()
        self.assertEqual(self.dest.list(), [])
        self.assertTrue((self.dir / (stored.filename + ".json")).exists())

    def test_sidecar_corrompu_conservé_et_ignore(self):
        self.save()
        bad = self.dir / "1999-01-01_00-00-00_dead00.png.json"
        bad.write_text("{corrompu")
        self.assertEqual(len(self.dest.list()), 1)
        self.assertTrue(bad.exists())

    def test_fichier_json_etranger_n_est_pas_supprime(self):
        stored = self.save()[0]
        raw = json.loads((self.dir / (stored.filename + ".json")).read_text())
        raw["filename"] = "notes"
        foreign_json = self.dir / "notes.json"
        foreign_json.write_text(json.dumps(raw))

        self.assertEqual([item.filename for item in self.dest.list()], [stored.filename])
        self.assertTrue(foreign_json.exists())

    def test_sidecar_trop_profond_est_ignore(self):
        stored = self.save()[0]
        sidecar = self.dir / (stored.filename + ".json")
        sidecar.write_text("[" * 2000 + "]" * 2000)

        self.assertEqual(self.dest.list(), [])

    def test_sidecar_texte_dimensions_incoherentes_ignore(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="text",
            mime="text/plain",
            ext=".txt",
        )
        stored = self.dest.save(b"hello", info, filename="notes.txt")
        sidecar = self.dir / (stored.filename + ".json")
        raw = json.loads(sidecar.read_text())
        raw["width"] = 1
        sidecar.write_text(json.dumps(raw))

        self.assertEqual(self.dest.list(), [])

    def test_sidecar_types_invalides_ignores(self):
        stored = self.save()[0]
        sidecar = self.dir / (stored.filename + ".json")
        raw = json.loads(sidecar.read_text())
        raw["filename"] = 123
        sidecar.write_text(json.dumps(raw))
        self.assertEqual(self.dest.list(), [])

    def test_sidecar_invalide_ne_permet_pas_de_supprimer_ou_remplacer(self):
        stored = self.save()[0]
        sidecar = self.dir / (stored.filename + ".json")
        raw = json.loads(sidecar.read_text())
        raw["size"] = "not-an-integer"
        sidecar.write_text(json.dumps(raw))

        with self.assertRaises(DestinationError):
            self.dest.delete(stored.filename)
        with self.assertRaises(DestinationError):
            self.dest.save(b"replacement", INFO(), filename=stored.filename)
        self.assertEqual((self.dir / stored.filename).read_bytes(), make_png(2, 2))

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
        self.assertTrue(valid_filename("rapport final.txt"))
        self.assertTrue(valid_filename("résumé.pdf"))
        self.assertFalse(valid_filename("../../etc/passwd"))
        self.assertTrue(valid_filename("random.png"))
        self.assertTrue(valid_filename("2026-08-25_01-22-31_a81c42.exe.bat"))
        self.assertFalse(valid_filename(".pasteberth.lock"))
        self.assertFalse(valid_filename("bad\nname.txt"))
        self.assertFalse(valid_filename("report\u202e gnp.exe"))
        self.assertFalse(valid_filename("zero\u200bwidth.txt"))
        self.assertFalse(valid_filename("escape\x1b.txt"))

    def test_espace_libre_sous_seuil(self):
        dest = LocalDestination(self.tmp / "space")
        usage = type("Usage", (), {"f_blocks": 1000, "f_bavail": 10, "f_frsize": 1024})()
        with mock.patch("pasteberth.storage.os.fstatvfs", return_value=usage):
            with self.assertRaises(StorageLowError):
                dest.ensure_space(1024, 2.0)

    def test_reconciliation_conserve_un_nom_genere_sans_preuve(self):
        target = self.tmp / "reconcile"
        LocalDestination(target)
        orphan = target / "2026-01-01_00-00-00_abcdef.png"
        orphan.write_bytes(b"orphan")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        LocalDestination(target)
        self.assertTrue(orphan.exists())

    def test_reconciliation_conserve_un_orphelin_etranger(self):
        target = self.tmp / "reconcile-foreign"
        LocalDestination(target)
        orphan = target / "2026-01-01_00-00-00_abcdef.png"
        orphan.write_bytes(b"foreign")
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        real_stat = os.stat

        def foreign_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == orphan.name and kwargs.get("dir_fd") is not None:
                return type("ForeignStat", (), {
                    "st_mode": result.st_mode,
                    "st_uid": result.st_uid + 1,
                    "st_mtime": result.st_mtime,
                })()
            return result

        with mock.patch("pasteberth.storage.os.stat", side_effect=foreign_stat):
            LocalDestination(target)
        self.assertTrue(orphan.exists())

    def test_retention_signale_les_echecs(self):
        dest = LocalDestination(self.tmp / "retention")
        dest.save(make_png(), INFO())
        with mock.patch.object(dest, "delete", side_effect=DestinationError("no")):
            with self.assertRaises(RetentionError):
                dest.apply_retention(0)


if __name__ == "__main__":
    unittest.main()
