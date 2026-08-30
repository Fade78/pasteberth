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
    ReplacementRequiredError,
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
        saved = []
        generated_names = iter([
            "2026-01-01_00-00-00_cafe42.png",
            "2026-01-01_00-00-00_cafe42.png",
            "2026-01-01_00-00-00_abcd42.png",
        ])
        with mock.patch.object(self.dest, "_generate_name", side_effect=generated_names):
            saved.append(self.dest.save(make_png(), INFO()).filename)
            # même seconde + premier suffixe déjà pris -> régénération
            saved.append(self.dest.save(make_png(), INFO()).filename)
        self.assertNotEqual(saved[0], saved[1])
        self.assertEqual(len(list(self.dir.glob("*.png"))), 2)

    def test_nom_explicit_exige_confirmation_puis_ecrase_atomiquement(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".zip",
        )
        first = self.dest.save(b"old", info, filename="archive final.zip")
        with self.assertRaises(ReplacementRequiredError):
            self.dest.save(b"new", info, filename="archive final.zip")
        self.assertEqual(self.dest.read(first.filename), b"old")
        second = self.dest.save(
            b"new",
            info,
            filename="archive final.zip",
            allow_replace=True,
        )

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

    def test_sauvegarde_generee_interrompue_est_reconciliee(self):
        original_move = self.dest._move_expected

        def crash_after_data_install(directory_fd, source, target, expected):
            result = original_move(directory_fd, source, target, expected)
            if source.startswith(".pbdata-") and target.endswith(".png"):
                raise SystemExit("coupure simulée")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=crash_after_data_install):
            with mock.patch.object(self.dest, "_rollback_transaction", return_value=False):
                with self.assertRaises(SystemExit):
                    self.dest.save(make_png(2, 2), INFO(2, 2))

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.list(), [])
        self.assertFalse(list(self.dir.glob("*.png")))
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
                self.dest.save(
                    b"replacement",
                    info,
                    filename=target.name,
                    allow_replace=True,
                )
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
                self.dest.save(
                    b"replacement",
                    info,
                    filename=target.name,
                    allow_replace=True,
                )
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertTrue(list(self.dir.glob(".pbtxn-*.json")))

    def test_remplacement_ne_deplace_pas_un_sidecar_etranger_apparu_apres_lecture(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dest.save(b"old", info, filename="sidecar-race.bin")
        sidecar = self.dir / (target.filename + ".json")
        original_read = self.dest._read_meta

        def replace_sidecar_after_read(directory_fd, name):
            raw = original_read(directory_fd, name)
            if name == sidecar.name:
                sidecar.unlink()
                sidecar.write_bytes(b"foreign sidecar")
            return raw

        with mock.patch.object(self.dest, "_read_meta", side_effect=replace_sidecar_after_read):
            with self.assertRaises((DestinationError, StorageConflictError)):
                self.dest.save(b"new", info, filename=target.filename, allow_replace=True)

        self.assertEqual(sidecar.read_bytes(), b"foreign sidecar")

    def test_renommage_ne_deplace_pas_un_sidecar_etranger_apparu_apres_lecture(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"old", info, filename="sidecar-rename.bin")
        sidecar = self.dir / (source.filename + ".json")
        original_read = self.dest._read_meta

        def replace_sidecar_after_read(directory_fd, name):
            raw = original_read(directory_fd, name)
            if name == sidecar.name:
                sidecar.unlink()
                sidecar.write_bytes(b"foreign sidecar")
            return raw

        with mock.patch.object(self.dest, "_read_meta", side_effect=replace_sidecar_after_read):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, "sidecar-rename-target.bin")

        self.assertEqual((self.dir / source.filename).read_bytes(), b"old")
        self.assertEqual(sidecar.read_bytes(), b"foreign sidecar")

    def test_suppression_ne_deplace_pas_un_sidecar_etranger_apparu_apres_lecture(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="sidecar-delete.bin")
        sidecar = self.dir / (stored.filename + ".json")
        original_read = self.dest._read_meta

        def replace_sidecar_after_read(directory_fd, name):
            raw = original_read(directory_fd, name)
            if name == sidecar.name:
                sidecar.unlink()
                sidecar.write_bytes(b"foreign sidecar")
            return raw

        with mock.patch.object(self.dest, "_read_meta", side_effect=replace_sidecar_after_read):
            with self.assertRaises(DestinationError):
                self.dest.delete(stored.filename)

        self.assertEqual((self.dir / stored.filename).read_bytes(), b"old")
        self.assertEqual(sidecar.read_bytes(), b"foreign sidecar")

    def test_rollback_conserve_le_journal_si_backup_absent_et_cible_etrangere(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "rollback-foreign.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_move = self.dest._move_expected
        original_rollback = self.dest._rollback_transaction

        def fail_after_data_backup(directory_fd, source, destination, expected):
            result = original_move(directory_fd, source, destination, expected)
            if source == target.name and destination.startswith(".pbbackup-"):
                raise OSError("coupure simulée")
            return result

        def remove_backup_and_add_foreign(directory_fd, transaction, marker_name):
            (self.dir / transaction["data_backup"]).unlink()
            target.write_bytes(b"bad")
            return original_rollback(directory_fd, transaction, marker_name)

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_backup):
            with mock.patch.object(
                self.dest,
                "_rollback_transaction",
                side_effect=remove_backup_and_add_foreign,
            ):
                with self.assertRaises(OSError):
                    self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertTrue(list(self.dir.glob(".pbtxn-*.json")))
        self.assertEqual(target.read_bytes(), b"bad")

    def test_rollback_conserve_le_journal_si_cible_change_apres_suppression_du_marqueur(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "rollback-after-marker.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_backup(directory_fd, source, destination, expected):
            result = original_move(directory_fd, source, destination, expected)
            if source == target.name and destination.startswith(".pbbackup-"):
                raise OSError("coupure simulée")
            return result

        def replace_after_marker(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._TXN_MARKER_RE.fullmatch(name):
                target.unlink()
                target.write_bytes(b"foreign")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_backup):
            with mock.patch.object(self.dest, "_remove_expected", side_effect=replace_after_marker):
                with self.assertRaises(OSError):
                    self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertTrue(list(self.dir.glob(".pbtxn-*.json")))
        self.assertEqual(target.read_bytes(), b"foreign")

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
                    self.dest.save(
                        b"new",
                        info,
                        filename=target.name,
                        allow_replace=True,
                    )

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"old")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))
        self.assertFalse(list(self.dir.glob(".pbbackup-*")))

    def test_commit_deja_publie_n_est_pas_annule_si_sa_sync_echoue(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "commit-publish.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_write = self.dest._write_transaction_file

        def publish_then_fail(directory_fd, name, transaction):
            result = original_write(directory_fd, name, transaction)
            if storage_module._TXN_COMMIT_RE.fullmatch(name):
                raise OSError("sync finale simulée")
            return result

        with mock.patch.object(
            self.dest,
            "_write_transaction_file",
            side_effect=publish_then_fail,
        ):
            with self.assertRaises(OSError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_nettoyage_recree_le_commit_si_la_derniere_suppression_echoue(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "cleanup-commit-failure.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def fail_after_commit(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._TXN_COMMIT_RE.fullmatch(name):
                raise OSError("suppression finale simulée")
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=fail_after_commit):
            self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertEqual(self.dest.read(target.name), b"new")
        self.assertTrue(list(self.dir.glob(".pbtxn-*.commit")))
        self.assertFalse(list(self.dir.glob(".pbtxn-guard-*.data")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_ecriture_refuse_un_nom_couvert_par_un_commit_differe(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "deferred-reuse.bin"
        self.dest.save(b"old", info, filename=target.name)

        with mock.patch.object(self.dest, "_cleanup_committed_transaction", return_value=False):
            self.dest.save(b"new", info, filename=target.name, allow_replace=True)
            with self.assertRaises(StorageConflictError):
                self.dest.save(b"latest", info, filename=target.name, allow_replace=True)

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
            (directory_fd.path / transaction["data_backup"]).unlink()
            raise OSError("nettoyage interrompu")

        with mock.patch.object(
            self.dest,
            "_cleanup_committed_transaction",
            side_effect=fail_after_deleting_one_backup,
        ):
            self.dest.save(b"new", info, filename=target.name, allow_replace=True)

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
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertTrue(list(self.dir.glob(".pbbackup-*")))
        self.assertEqual(
            [temp.read_bytes() for temp in self.dir.glob(".pbdata-*.tmp")],
            [b"new"],
        )
        recovered = LocalDestination(self.dir)
        self.assertTrue(list(self.dir.glob(".pbbackup-*")))
        self.assertEqual(recovered.list(), [])

    def test_commit_signale_une_cible_supprimee_pendant_le_nettoyage(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-delete-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if name.startswith(".pbbackup-") and name.endswith(".data"):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertFalse(target.exists())
        self.assertEqual(
            [temp.read_bytes() for temp in self.dir.glob(".pbdata-*.tmp")],
            [b"new"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_conserve_la_copie_si_la_cible_disparait_pendant_les_marqueurs(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-marker-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._txn_token(name) is not None:
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertFalse(target.exists())
        self.assertEqual(
            [temp.read_bytes() for temp in self.dir.glob(".pbdata-*.tmp")],
            [b"new"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_recree_le_journal_si_la_cible_disparait_pendant_le_commit(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-commit-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._TXN_COMMIT_RE.fullmatch(name):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_recree_le_journal_si_la_cible_disparait_pendant_le_temporaire(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-temp-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._DATA_TEMP_RE.fullmatch(name):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_recree_le_journal_si_la_cible_disparait_pendant_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-guard-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._TXN_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_recree_le_journal_si_le_sidecar_disparait_pendant_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-meta-guard-race.bin"
        target_meta = self.dir / (target.name + ".json")
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_public_meta(directory_fd, name, expected):
            if storage_module._TXN_META_GUARD_RE.fullmatch(name):
                target_meta.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_meta):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertEqual(recovered.list()[0].filename, target.name)
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_conserve_la_copie_si_la_cible_disparait_apres_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-after-guard-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_after_guard(directory_fd, name, expected):
            existed = (self.dir / name).exists()
            result = original_remove(directory_fd, name, expected)
            if existed and storage_module._TXN_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_after_guard):
            self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_conserve_la_copie_si_un_etranger_remplace_la_cible_apres_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-foreign-after-guard.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def replace_after_guard(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._TXN_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
                target.write_bytes(b"foreign")
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=replace_after_guard):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertEqual(target.read_bytes(), b"foreign")
        data_copies = list(self.dir.glob(".pbdata-*.tmp"))
        data_copies += list(self.dir.glob(".pbtxn-guard-*.data"))
        self.assertTrue(data_copies)
        self.assertTrue(all(copy.read_bytes() == b"new" for copy in data_copies))
        target.unlink()
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_commit_ne_termine_pas_si_la_cible_disparait_avant_fermeture_des_handles(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dest.save(b"old", info, filename="committed-close-race.bin")
        original_close = self.dest._close_recovery_handles
        deleted = False

        def delete_before_close(directory_fd, handles, *, keep=(), pair_check=None):
            nonlocal deleted
            if not deleted:
                (self.dir / target.filename).unlink()
                deleted = True
            return original_close(
                directory_fd,
                handles,
                keep=keep,
                pair_check=pair_check,
            )

        with mock.patch.object(
            self.dest,
            "_close_recovery_handles",
            side_effect=delete_before_close,
        ):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.filename, allow_replace=True)

        self.assertTrue(list(self.dir.glob(".pbtxn-*.commit")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.filename), b"new")
        self.assertFalse(list(self.dir.glob(".pbtrash-*")))

    def test_commit_recupere_si_la_cible_disparait_pendant_le_dernier_handle(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dest.save(b"old", info, filename="committed-last-handle.bin")
        original_close = self.dest._close_recovery_handles
        original_unlink = self.dest._unlink_expected
        deleted = False

        def close_with_race(directory_fd, handles, *, keep=(), pair_check=None):
            nonlocal deleted
            last = handles[-1][2] if handles else None

            def unlink_with_race(fd, name, expected):
                nonlocal deleted
                if name == last and not deleted:
                    (self.dir / target.filename).unlink()
                    deleted = True
                return original_unlink(fd, name, expected)

            with mock.patch.object(
                self.dest,
                "_unlink_expected",
                side_effect=unlink_with_race,
            ):
                return original_close(
                    directory_fd,
                    handles,
                    keep=keep,
                    pair_check=pair_check,
                )

        with mock.patch.object(
            self.dest,
            "_close_recovery_handles",
            side_effect=close_with_race,
        ):
            with self.assertRaises(DestinationError):
                self.dest.save(b"new", info, filename=target.filename, allow_replace=True)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.filename), b"new")
        self.assertFalse(list(self.dir.glob(".pbtrash-*")))

    def test_commit_ne_supprime_pas_un_marqueur_etranger_apparu_avant_suppression(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dest.save(b"old", info, filename="foreign-marker-race.bin")
        original_remove = self.dest._remove_expected
        swapped = False
        marker_path = None

        def replace_marker_before_remove(directory_fd, name, expected):
            nonlocal swapped, marker_path
            if not swapped and storage_module._TXN_MARKER_RE.fullmatch(name):
                marker_path = self.dir / name
                marker_path.unlink()
                marker_path.write_bytes(b"foreign marker")
                swapped = True
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(
            self.dest,
            "_remove_expected",
            side_effect=replace_marker_before_remove,
        ):
            self.dest.save(b"new", info, filename=target.filename, allow_replace=True)

        self.assertIsNotNone(marker_path)
        self.assertEqual(marker_path.read_bytes(), b"foreign marker")

    def test_commit_conserve_la_copie_si_la_cible_disparait_apres_le_temporaire(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        target = self.dir / "committed-after-temp-race.bin"
        self.dest.save(b"old", info, filename=target.name)
        original_remove = self.dest._remove_expected

        def delete_after_temp(directory_fd, name, expected):
            existed = (self.dir / name).exists()
            result = original_remove(directory_fd, name, expected)
            if existed and storage_module._DATA_TEMP_RE.fullmatch(name):
                target.unlink()
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_after_temp):
            self.dest.save(b"new", info, filename=target.name, allow_replace=True)

        self.assertEqual(self.dest.read(target.name), b"new")
        self.assertFalse(list(self.dir.glob(".pbtxn-*")))

    def test_sidecar_orphelin_refuse_en_remplacement(self):
        stored = self.save()[0]
        (self.dir / stored.filename).unlink()
        with self.assertRaisesRegex(DestinationError, "orphelin"):
            self.dest.save(b"new", INFO(), filename=stored.filename)

    def test_renommage_deplace_le_fichier_et_le_sidecar(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"payload", info, filename="old name.bin")

        renamed = self.dest.rename(stored.filename, "new name.bin")

        self.assertEqual(renamed.filename, "new name.bin")
        self.assertEqual(self.dest.read(renamed.filename), b"payload")
        self.assertFalse((self.dir / stored.filename).exists())
        self.assertFalse((self.dir / (stored.filename + ".json")).exists())
        self.assertTrue((self.dir / renamed.filename).exists())
        metadata = json.loads((self.dir / (renamed.filename + ".json")).read_text())
        self.assertEqual(metadata["filename"], renamed.filename)
        self.assertEqual([item.filename for item in self.dest.list()], [renamed.filename])
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_ne_remplace_pas_la_cible(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"source", info, filename="source.bin")
        target = self.dir / "target.bin"
        target.write_bytes(b"foreign")

        with self.assertRaises(StorageConflictError):
            self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(source.filename), b"source")
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_renommage_nettoie_le_marqueur_si_la_cible_apparait_en_course(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"source", info, filename="racing-source.bin")
        target = self.dir / "racing-target.bin"
        original_move = storage_module._rename_noreplace

        def appear_before_move(directory_fd, old_name, new_name):
            if old_name == source.filename and new_name == target.name:
                target.write_bytes(b"foreign")
            return original_move(directory_fd, old_name, new_name)

        with mock.patch(
            "pasteberth.storage._rename_noreplace",
            side_effect=appear_before_move,
        ):
            with self.assertRaises(StorageConflictError):
                self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(source.filename), b"source")
        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))
        self.assertEqual([item.filename for item in self.dest.list()], [source.filename])

    def test_renommage_refuse_une_cible_symbolique_comme_conflit(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"source", info, filename="symlink-source.bin")
        outside = self.dir.parent / "outside.bin"
        outside.write_bytes(b"foreign")
        target = self.dir / "symlink-target.bin"
        target.symlink_to(outside)

        with self.assertRaises(StorageConflictError):
            self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(source.filename), b"source")
        self.assertTrue(target.is_symlink())

    def test_renommage_interrompu_est_repris_par_reconciliation(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"source", info, filename="before.bin")
        original_move = self.dest._move_expected

        def crash_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == "after.bin":
                raise SystemExit("coupure simulée")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=crash_after_data_move):
            with mock.patch.object(self.dest, "_rollback_rename_transaction", return_value=False):
                with self.assertRaises(SystemExit):
                    self.dest.rename(source.filename, "after.bin")

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"source")
        self.assertFalse((self.dir / "after.bin").exists())
        self.assertFalse((self.dir / ("after.bin.json")).exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_repare_une_cible_disparue_avant_nettoyage(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="repair-source.bin")
        target = self.dir / "repair-target.bin"
        original_cleanup = self.dest._cleanup_committed_rename

        def delete_before_cleanup(directory_fd, transaction, marker_name, commit_name):
            target.unlink()
            return original_cleanup(directory_fd, transaction, marker_name, commit_name)

        with mock.patch.object(
            self.dest,
            "_cleanup_committed_rename",
            side_effect=delete_before_cleanup,
        ):
            renamed = self.dest.rename(source.filename, target.name)

        self.assertEqual(renamed.filename, target.name)
        self.assertEqual(self.dest.read(target.name), b"payload")
        self.assertFalse((self.dir / source.filename).exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_signale_une_cible_etrangere_apres_commit(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="foreign-source.bin")
        target = self.dir / "foreign-target.bin"
        original_cleanup = self.dest._cleanup_committed_rename

        def replace_before_cleanup(directory_fd, transaction, marker_name, commit_name):
            target.unlink()
            target.write_bytes(b"foreign")
            return original_cleanup(directory_fd, transaction, marker_name, commit_name)

        with mock.patch.object(
            self.dest,
            "_cleanup_committed_rename",
            side_effect=replace_before_cleanup,
        ):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        self.assertEqual(target.read_bytes(), b"foreign")
        self.assertFalse((self.dir / source.filename).exists())
        self.assertEqual(
            [backup.read_bytes() for backup in self.dir.glob(".pbrename-backup-*.data")],
            [b"payload"],
        )

    def test_renommage_conserve_la_copie_si_la_cible_disparait_pendant_le_nettoyage(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="race-source.bin")
        target = self.dir / "race-target.bin"
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if name.startswith(".pbrename-backup-") and name.endswith(".data"):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        self.assertFalse((self.dir / source.filename).exists())
        self.assertFalse(target.exists())
        backup_paths = list(self.dir.glob(".pbrename-backup-*.data"))
        backup_paths += list(self.dir.glob(".pbrename-guard-*.data"))
        self.assertEqual(
            [backup.read_bytes() for backup in backup_paths],
            [b"payload"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_conserve_la_copie_si_la_cible_disparait_pendant_les_marqueurs(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="marker-source.bin")
        target = self.dir / "marker-target.bin"
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._rename_token(name) is not None:
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        self.assertFalse(target.exists())
        self.assertEqual(
            [guard.read_bytes() for guard in self.dir.glob(".pbrename-guard-*.data")],
            [b"payload"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_recree_le_journal_si_la_cible_disparait_pendant_le_commit(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="commit-source.bin")
        target = self.dir / "commit-target.bin"
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._RENAME_COMMIT_RE.fullmatch(name):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_nettoyage_renommage_recree_le_commit_si_la_derniere_suppression_echoue(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="cleanup-source.bin")
        target = self.dir / "cleanup-target.bin"
        original_remove = self.dest._remove_expected

        def fail_after_commit(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._RENAME_COMMIT_RE.fullmatch(name):
                raise OSError("suppression finale simulée")
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=fail_after_commit):
            self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(target.name), b"payload")
        self.assertTrue(list(self.dir.glob(".pbrename-*.commit")))
        self.assertFalse(list(self.dir.glob(".pbrename-guard-*.data")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_deja_publie_n_est_pas_annule_si_sa_sync_echoue(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="published-source.bin")
        target = self.dir / "published-target.bin"
        original_write = self.dest._write_transaction_file

        def publish_then_fail(directory_fd, name, transaction):
            result = original_write(directory_fd, name, transaction)
            if storage_module._RENAME_COMMIT_RE.fullmatch(name):
                raise OSError("sync finale simulée")
            return result

        with mock.patch.object(
            self.dest,
            "_write_transaction_file",
            side_effect=publish_then_fail,
        ):
            with self.assertRaises(OSError):
                self.dest.rename(source.filename, target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse((self.dir / source.filename).exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_list_bloque_une_source_reapparue_avec_un_commit_de_renommage(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="list-source.bin")
        target = self.dir / "list-target.bin"

        with mock.patch.object(self.dest, "_cleanup_committed_rename", return_value=False):
            self.dest.rename(source.filename, target.name)

        os.link(target, self.dir / source.filename)
        source_meta = json.loads((self.dir / (target.name + ".json")).read_text())
        source_meta["filename"] = source.filename
        (self.dir / (source.filename + ".json")).write_text(json.dumps(source_meta))

        self.assertEqual(self.dest.list(), [])

    def test_renommage_recree_le_journal_si_la_cible_disparait_pendant_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="guard-commit-source.bin")
        target = self.dir / "guard-commit-target.bin"
        original_remove = self.dest._remove_expected

        def delete_public_target(directory_fd, name, expected):
            if storage_module._RENAME_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_target):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_recree_le_journal_si_le_sidecar_disparait_pendant_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="meta-guard-source.bin")
        target = self.dir / "meta-guard-target.bin"
        target_meta = self.dir / (target.name + ".json")
        original_remove = self.dest._remove_expected

        def delete_public_meta(directory_fd, name, expected):
            if storage_module._RENAME_META_GUARD_RE.fullmatch(name):
                target_meta.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_public_meta):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertEqual(recovered.list()[0].filename, target.name)
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_conserve_la_copie_si_la_cible_disparait_apres_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="after-guard-source.bin")
        target = self.dir / "after-guard-target.bin"
        original_remove = self.dest._remove_expected

        def delete_after_guard(directory_fd, name, expected):
            existed = (self.dir / name).exists()
            result = original_remove(directory_fd, name, expected)
            if existed and storage_module._RENAME_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=delete_after_guard):
            self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_conserve_la_copie_si_un_etranger_remplace_la_cible_apres_la_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="foreign-after-source.bin")
        target = self.dir / "foreign-after-target.bin"
        original_remove = self.dest._remove_expected

        def replace_after_guard(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._RENAME_DATA_GUARD_RE.fullmatch(name):
                target.unlink()
                target.write_bytes(b"foreign")
            return result

        with mock.patch.object(self.dest, "_remove_expected", side_effect=replace_after_guard):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        self.assertEqual(target.read_bytes(), b"foreign")
        data_copies = list(self.dir.glob(".pbrename-backup-*.data"))
        data_copies += list(self.dir.glob(".pbrename-guard-*.data"))
        self.assertEqual([copy.read_bytes() for copy in data_copies], [b"payload"])
        target.unlink()
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_renommage_recupere_si_la_cible_disparait_pendant_le_dernier_handle(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="close-source.bin")
        target = self.dir / "close-target.bin"
        original_close = self.dest._close_recovery_handles
        original_unlink = self.dest._unlink_expected
        deleted = False

        def close_with_race(directory_fd, handles, *, keep=(), pair_check=None):
            nonlocal deleted
            last = handles[-1][2] if handles else None

            def unlink_with_race(fd, name, expected):
                nonlocal deleted
                if name == last and not deleted:
                    target.unlink()
                    deleted = True
                return original_unlink(fd, name, expected)

            with mock.patch.object(
                self.dest,
                "_unlink_expected",
                side_effect=unlink_with_race,
            ):
                return original_close(
                    directory_fd,
                    handles,
                    keep=keep,
                    pair_check=pair_check,
                )

        with mock.patch.object(
            self.dest,
            "_close_recovery_handles",
            side_effect=close_with_race,
        ):
            with self.assertRaises(DestinationError):
                self.dest.rename(source.filename, target.name)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(target.name), b"payload")
        self.assertFalse(list(self.dir.glob(".pbtrash-*")))

    def test_rollback_conserve_la_copie_si_la_cible_disparait(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="rollback-source.bin")
        target = self.dir / "rollback-target.bin"
        original_move = self.dest._move_expected

        def delete_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                target.unlink()
                raise OSError("coupure simulée")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=delete_after_data_move):
            with mock.patch.object(self.dest, "_rollback_rename_transaction", return_value=False):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        backup_paths = list(self.dir.glob(".pbrename-backup-*.data"))
        backup_paths += list(self.dir.glob(".pbrename-guard-*.data"))
        self.assertEqual(
            [backup.read_bytes() for backup in backup_paths],
            [b"payload", b"payload"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_rollback_conserve_la_copie_si_la_source_disparait_pendant_le_garde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="guard-source.bin")
        source_path = self.dir / source.filename
        target = self.dir / "guard-target.bin"
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                raise OSError("coupure simulée")
            return result

        def delete_source_during_guard(directory_fd, name, expected):
            if name.startswith(".pbrename-guard-"):
                source_path.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_move):
            with mock.patch.object(
                self.dest,
                "_remove_expected",
                side_effect=delete_source_during_guard,
            ):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        self.assertFalse(source_path.exists())
        self.assertFalse(target.exists())
        self.assertEqual(
            [backup.read_bytes() for backup in self.dir.glob(".pbrename-backup-*.data")],
            [b"payload"],
        )
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"payload")
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_rollback_recree_le_journal_si_la_source_disparait_pendant_le_marqueur(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="marker-rollback-source.bin")
        source_path = self.dir / source.filename
        target = self.dir / "marker-rollback-target.bin"
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                raise OSError("coupure simulée")
            return result

        def delete_source_during_marker(directory_fd, name, expected):
            if storage_module._RENAME_MARKER_RE.fullmatch(name):
                source_path.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_move):
            with mock.patch.object(
                self.dest,
                "_remove_expected",
                side_effect=delete_source_during_marker,
            ):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        self.assertTrue(list(self.dir.glob(".pbrename-*.json")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_rollback_recree_le_journal_si_la_source_disparait_pendant_la_sauvegarde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="backup-rollback-source.bin")
        source_path = self.dir / source.filename
        target = self.dir / "backup-rollback-target.bin"
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                raise OSError("coupure simulée")
            return result

        def delete_source_during_backup(directory_fd, name, expected):
            if storage_module._RENAME_DATA_BACKUP_RE.fullmatch(name):
                source_path.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_move):
            with mock.patch.object(
                self.dest,
                "_remove_expected",
                side_effect=delete_source_during_backup,
            ):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        self.assertTrue(list(self.dir.glob(".pbrename-*.json")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_rollback_recree_le_journal_si_le_sidecar_source_disparait_pendant_la_sauvegarde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="meta-backup-source.bin")
        source_meta_path = self.dir / (source.filename + ".json")
        target = self.dir / "meta-backup-target.bin"
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                raise OSError("coupure simulée")
            return result

        def delete_source_meta_during_backup(directory_fd, name, expected):
            if storage_module._RENAME_META_BACKUP_GUARD_RE.fullmatch(name):
                source_meta_path.unlink()
            return original_remove(directory_fd, name, expected)

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_move):
            with mock.patch.object(
                self.dest,
                "_remove_expected",
                side_effect=delete_source_meta_during_backup,
            ):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        self.assertTrue(list(self.dir.glob(".pbrename-*.json")))
        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.read(source.filename), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))

    def test_rollback_conserve_la_copie_si_la_source_disparait_apres_la_sauvegarde(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        source = self.dest.save(b"payload", info, filename="after-backup-source.bin")
        source_path = self.dir / source.filename
        target = self.dir / "after-backup-target.bin"
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_move(directory_fd, old_name, new_name, expected):
            result = original_move(directory_fd, old_name, new_name, expected)
            if old_name == source.filename and new_name == target.name:
                raise OSError("coupure simulée")
            return result

        def delete_source_after_backup(directory_fd, name, expected):
            existed = (self.dir / name).exists()
            result = original_remove(directory_fd, name, expected)
            if existed and storage_module._RENAME_DATA_BACKUP_RE.fullmatch(name):
                source_path.unlink()
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_move):
            with mock.patch.object(
                self.dest,
                "_remove_expected",
                side_effect=delete_source_after_backup,
            ):
                with self.assertRaises(OSError):
                    self.dest.rename(source.filename, target.name)

        self.assertEqual(self.dest.read(source.filename), b"payload")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.dir.glob(".pbrename-*")))


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

    def test_suppression_interne_ne_detruit_pas_un_fichier_remplace_apres_verification(self):
        stored = self.save()[0]
        target = self.dir / stored.filename
        directory_fd = os.open(self.dir, os.O_RDONLY)
        expected = LocalDestination._entry_identity(directory_fd, stored.filename)

        original_move = self.dest._move_expected

        def replace_before_move(fd, source, destination, identity):
            if source == stored.filename:
                target.unlink()
                target.write_bytes(b"foreign")
            return original_move(fd, source, destination, identity)

        try:
            with mock.patch.object(
                self.dest,
                "_move_expected",
                side_effect=replace_before_move,
            ):
                self.assertFalse(self.dest._unlink_expected(directory_fd, stored.filename, expected))
        finally:
            os.close(directory_fd)
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

    def test_delete_conserve_le_journal_si_trash_absent_et_cible_etrangere(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="delete-foreign.bin")
        target = self.dir / stored.filename
        original_move = self.dest._move_expected

        def fail_after_data_trash(directory_fd, source, destination, expected):
            result = original_move(directory_fd, source, destination, expected)
            if source == target.name and destination.startswith(".pbtrash-"):
                (self.dir / destination).unlink()
                target.write_bytes(b"bad")
                raise OSError("coupure simulée")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_trash):
            with self.assertRaises(OSError):
                self.dest.delete(stored.filename)

        self.assertTrue(list(self.dir.glob(".pbdel-*.json")))
        self.assertEqual(target.read_bytes(), b"bad")

    def test_delete_conserve_le_journal_si_cible_change_apres_suppression_du_marqueur(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="delete-after-marker.bin")
        target = self.dir / stored.filename
        original_move = self.dest._move_expected
        original_remove = self.dest._remove_expected

        def fail_after_data_trash(directory_fd, source, destination, expected):
            result = original_move(directory_fd, source, destination, expected)
            if source == target.name and destination.startswith(".pbtrash-"):
                raise OSError("coupure simulée")
            return result

        def replace_after_marker(directory_fd, name, expected):
            result = original_remove(directory_fd, name, expected)
            if storage_module._DELETE_MARKER_RE.fullmatch(name):
                target.unlink()
                target.write_bytes(b"foreign")
            return result

        with mock.patch.object(self.dest, "_move_expected", side_effect=fail_after_data_trash):
            with mock.patch.object(self.dest, "_remove_expected", side_effect=replace_after_marker):
                with self.assertRaises(OSError):
                    self.dest.delete(stored.filename)

        self.assertTrue(list(self.dir.glob(".pbdel-*.json")))
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_delete_reconcile_conserve_la_paire_si_cible_etrangere(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="delete-reconcile-foreign.bin")
        target = self.dir / stored.filename
        token = "b" * 24
        marker_name = f".pbdel-{token}.json"
        with self.dest._directory_fd() as directory_fd:
            target_identity = self.dest._entry_identity(directory_fd, stored.filename)
            meta_identity = self.dest._entry_identity(
                directory_fd,
                stored.filename + ".json",
            )
            self.dest._write_transaction_file(
                directory_fd,
                marker_name,
                {
                    "version": 1,
                    "target": stored.filename,
                    "data_trash": f".pbtrash-{token}.data",
                    "meta_trash": f".pbtrash-{token}.json",
                    "target_identity": list(target_identity),
                    "meta_identity": list(meta_identity),
                },
            )
        target.unlink()
        target.write_bytes(b"bad")

        LocalDestination(self.dir)

        self.assertTrue((self.dir / marker_name).exists())
        self.assertEqual(target.read_bytes(), b"bad")
        self.assertTrue((self.dir / (stored.filename + ".json")).exists())

    def test_ecriture_refuse_un_nom_couvert_par_un_tombstone(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="delete-deferred.bin")

        with mock.patch.object(self.dest, "_finish_delete_transaction", return_value=False):
            self.dest.delete(stored.filename)

        with self.assertRaises(StorageConflictError):
            self.dest.save(b"new", info, filename=stored.filename)

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

    def test_delete_reconcile_une_erreur_apres_suppression_effective(self):
        stored = self.save()[0]
        original_unlink = self.dest._unlink_expected

        def unlink_then_fail(directory_fd, name, expected):
            result = original_unlink(directory_fd, name, expected)
            if name.startswith(".pbtrash-") and name.endswith(".json"):
                raise OSError("nettoyage interrompu après suppression")
            return result

        with mock.patch.object(
            self.dest,
            "_unlink_expected",
            side_effect=unlink_then_fail,
        ):
            self.dest.delete(stored.filename)

        recovered = LocalDestination(self.dir)
        self.assertEqual(recovered.list(), [])
        self.assertFalse(list(self.dir.glob(".pbdel-*")))
        self.assertFalse(list(self.dir.glob(".pbtrash-*")))

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
            self.dest.save(
                b"replacement",
                INFO(),
                filename=stored.filename,
                allow_replace=True,
            )
        self.assertEqual((self.dir / stored.filename).read_bytes(), make_png(2, 2))

    def test_delete_force_supprime_une_paire_dont_la_taille_est_stale(self):
        info = ImageInfo(
            fmt=None,
            width=None,
            height=None,
            kind="binary",
            mime="application/octet-stream",
            ext=".bin",
        )
        stored = self.dest.save(b"old", info, filename="stale.bin")
        (self.dir / stored.filename).write_bytes(b"changed content")

        with self.assertRaises(DestinationError):
            self.dest.delete(stored.filename)
        self.dest.delete(stored.filename, allow_stale_sidecar=True)

        self.assertFalse((self.dir / stored.filename).exists())
        self.assertFalse((self.dir / (stored.filename + ".json")).exists())

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

    def test_reconciliation_conserve_les_prefixes_internes_inconnus(self):
        target = self.tmp / "reconcile-internal"
        LocalDestination(target)
        names = [
            ".pbmeta-foreign.txt",
            ".pbdata-foreign.bin",
            ".pbtrash-foreign.data",
            ".pbrename-foreign.txt",
        ]
        old = time.time() - 7200
        for name in names:
            path = target / name
            path.write_bytes(b"foreign")
            os.utime(path, (old, old))

        LocalDestination(target)

        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            sorted(names + [".pasteberth.lock"]),
        )

    def test_reconciliation_conserve_un_nom_interne_forme_sans_journal(self):
        target = self.tmp / "reconcile-internal-formed"
        LocalDestination(target)
        names = [
            ".pbdata-0123456789abcdef01234567.tmp",
            ".pbtrash-0123456789abcdef01234567.data",
        ]
        old = time.time() - 7200
        for name in names:
            path = target / name
            path.write_bytes(b"foreign")
            os.utime(path, (old, old))

        LocalDestination(target)

        self.assertEqual(
            sorted(path.name for path in target.iterdir()),
            sorted(names + [".pasteberth.lock"]),
        )

    def test_fallback_rename_ne_supprime_pas_une_cible_remplacee(self):
        target_dir = self.tmp / "rename-fallback"
        dest = LocalDestination(target_dir)
        source = target_dir / "source.bin"
        target = target_dir / "target.bin"
        source.write_bytes(b"source")
        real_unlink = os.unlink

        def replace_target_before_source_unlink(name, *args, **kwargs):
            if name == source.name:
                real_unlink(target)
                target.write_bytes(b"foreign")
                raise OSError("unlink source simulé")
            return real_unlink(name, *args, **kwargs)

        with dest._directory_fd() as directory_fd:
            with mock.patch.object(storage_module.platform_fs(), "_renameat2", None):
                with mock.patch.object(
                    os,
                    "unlink",
                    side_effect=replace_target_before_source_unlink,
                ):
                    with self.assertRaises(OSError):
                        storage_module._rename_noreplace(
                            directory_fd,
                            source.name,
                            target.name,
                        )

        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_retention_signale_les_echecs(self):
        dest = LocalDestination(self.tmp / "retention")
        dest.save(make_png(), INFO())
        with mock.patch.object(dest, "delete", side_effect=DestinationError("no")):
            with self.assertRaises(RetentionError):
                dest.apply_retention(0)


if __name__ == "__main__":
    unittest.main()
