"""Contract tests for the selected semantic filesystem backend."""
from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from pasteberth.platformfs import (
    BusyError,
    EntryChangedError,
    EntryExistsError,
    FileIdentity,
    InvalidNameError,
    platform_fs,
)
from tests.helpers import running_under_wine


def _hold_lock(path: str, ready, release) -> None:
    fs = platform_fs()
    with fs.open_directory(Path(path)) as directory:
        with fs.acquire_lock(directory, exclusive=True):
            ready.set()
            release.wait(10)


class PlatformFSContract(unittest.TestCase):
    def setUp(self):
        self.fs = platform_fs()
        self.tmp = tempfile.TemporaryDirectory()
        self.directory_path = Path(self.tmp.name) / "zone"

    def tearDown(self):
        self.tmp.cleanup()

    def test_capabilities_and_directory_identity(self):
        self.fs.capabilities.require(
            "safe_directory_open",
            "safe_file_open",
            "exclusive_create",
            "identity",
            "hard_link_guard",
            "expected_remove",
            "interprocess_locks",
            "file_flush",
            "directory_flush",
            "volume_space",
            "volume_identity",
        )
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            self.assertIsInstance(directory.identity, FileIdentity)
            self.assertEqual(self.fs.volume_identity(directory), directory.identity.volume)

    def test_exclusive_file_identity_and_enumeration(self):
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            with self.fs.create_exclusive(directory, "payload.bin") as file:
                file.write(b"payload")
                file.sync()
                identity = file.identity
            self.assertEqual(self.fs.identity(directory, "payload.bin"), identity)
            entry = self.fs.entry_info(directory, "payload.bin")
            self.assertIsNotNone(entry)
            self.assertTrue(entry.is_regular)
            self.assertEqual(entry.identity, identity)
            self.assertEqual({item.name for item in self.fs.entries(directory)}, {"payload.bin"})
            with self.assertRaises(EntryExistsError):
                self.fs.create_exclusive(directory, "payload.bin")

    def test_private_permissions_and_ownership(self):
        if running_under_wine():
            self.skipTest("Wine ne persiste pas les ACL NTFS sur ce volume")
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            directory_audit = self.fs.audit_permissions(
                self.directory_path,
                directory=True,
            )
            self.assertTrue(directory_audit.private, directory_audit.detail)
            with self.fs.create_exclusive(directory, "private.bin") as file:
                file.write(b"private")
                file.sync()
                identity = file.identity
            entry = self.fs.entry_info(directory, "private.bin")
            self.assertIsNotNone(entry)
            self.assertEqual(entry.identity, identity)
            self.assertTrue(self.fs.is_owned(entry))
            file_audit = self.fs.audit_permissions(
                self.directory_path / "private.bin",
                directory=False,
            )
            self.assertTrue(file_audit.private, file_audit.detail)

    def test_safe_component_validation(self):
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            for name in ("../escape", "a/b", "a\\b", "", ".."):
                with self.subTest(name=name), self.assertRaises(InvalidNameError):
                    self.fs.entry_info(directory, name)

    def test_no_replace_rename_and_expected_identity(self):
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            with self.fs.create_exclusive(directory, "source") as source:
                source.write(b"source")
                source.sync()
                identity = source.identity
            self.fs.rename_noreplace(directory, "source", "target", expected=identity)
            self.assertEqual(self.fs.identity(directory, "target"), identity)
            with self.fs.create_exclusive(directory, "other") as other:
                other.write(b"other")
            with self.assertRaises(EntryExistsError):
                self.fs.rename_noreplace(directory, "target", "other", expected=identity)
            with self.assertRaises(EntryChangedError):
                self.fs.rename_noreplace(
                    directory,
                    "target",
                    "third",
                    expected=FileIdentity(identity.volume, identity.file_id + 1),
                )

    def test_expected_link_and_remove_do_not_touch_foreign_entry(self):
        with self.fs.open_directory(self.directory_path, create=True) as directory:
            with self.fs.create_exclusive(directory, "source") as source:
                source.write(b"source")
                source.sync()
                identity = source.identity
            self.fs.link_expected(directory, "source", "backup", identity)
            self.assertEqual(self.fs.identity(directory, "backup"), identity)
            self.assertFalse(
                self.fs.remove_expected(
                    directory,
                    "source",
                    FileIdentity(identity.volume, identity.file_id + 1),
                )
            )
            self.assertEqual(self.fs.identity(directory, "source"), identity)
            self.assertTrue(self.fs.remove_expected(directory, "backup", identity))

    def test_nonblocking_interprocess_lock(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_lock,
            args=(str(self.directory_path), ready, release),
        )
        with self.fs.open_directory(self.directory_path, create=True):
            pass
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.fs.open_directory(self.directory_path) as directory:
                with self.assertRaises(BusyError):
                    with self.fs.acquire_lock(
                        directory,
                        exclusive=True,
                        blocking=False,
                    ):
                        pass
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)
