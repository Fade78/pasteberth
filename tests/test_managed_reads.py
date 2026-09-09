"""Targeted managed reads retain owned handles beyond the operation lock."""
import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PasteBerth.runtime.images import ImageInfo
from PasteBerth.runtime.platformfs import FileHandle, UnsupportedFilesystemError
from PasteBerth.runtime.storage import (
    DestinationError,
    LocalDestination,
    UnknownImageError,
)


INFO = ImageInfo(fmt=None, width=None, height=None, kind="binary",
                 mime="application/octet-stream", ext=".bin")


class ManagedReads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "zone"
        self.dest = LocalDestination(self.directory)

    def save(self, name="file.bin", data=b"original"):
        return self.dest.save(data, INFO, filename=name)

    def acquire(self, names, *, exclusive=False):
        with self.dest.operation_lock(exclusive=exclusive):
            result = self.dest.acquire_reads(names)
        for _, handle in result:
            self.addCleanup(handle.close)
        return result

    def identity(self, name):
        with self.dest._directory_fd() as directory:
            return list(self.dest._entry_identity(directory, name))

    def marker(self, kind, state="prepared", *, token="a" * 24,
               target="file.bin", source="source.bin"):
        suffix = "commit" if state == "committed" else "json"
        if kind == "delete":
            name = f".pbdel-{token}.json"
            raw = dict(version=1, target=target,
                       data_trash=f".pbtrash-{token}.data",
                       meta_trash=f".pbtrash-{token}.json",
                       target_identity=self.identity(target),
                       meta_identity=self.identity(target + ".json"))
        elif kind == "rename":
            name = f".pbrename-{token}.{suffix}"
            # The original CLI format predates data backups and guards.
            raw = dict(version=1, state=state, source=source, target=target,
                       meta_backup=f".pbrename-backup-{token}.json",
                       meta_temp=f".pbmeta-{token}.tmp",
                       source_identity=self.identity(target),
                       source_meta_identity=self.identity(target + ".json"),
                       new_meta_identity=self.identity(target + ".json"))
        else:
            name = f".pbtxn-{token}.{suffix}"
            raw = dict(version=1, state=state, target=target,
                       data_backup=f".pbbackup-{token}.data",
                       meta_backup=f".pbbackup-{token}.json",
                       data_temp=f".pbdata-{token}.tmp",
                       meta_temp=f".pbmeta-{token}.tmp",
                       target_identity=None, meta_identity=None,
                       new_data_identity=self.identity(target),
                       new_meta_identity=self.identity(target + ".json"))
        path = self.directory / name
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path, raw

    def assert_visibility(self, names, expected):
        with self.dest.operation_lock(exclusive=False):
            listed = {item.filename: item for item in self.dest.list()}
            self.assertEqual(set(listed), set(expected))
            for name in names:
                if name not in listed:
                    with self.assertRaises(UnknownImageError):
                        self.dest.acquire_reads([name])
                else:
                    result = self.dest.acquire_reads([name])
                    try:
                        self.assertEqual(result[0][0], listed[name])
                        self.assertIsInstance(result[0][1], FileHandle)
                    finally:
                        result[0][1].close()

    def test_requires_caller_operation_lock(self):
        self.save()
        with self.assertRaises(DestinationError):
            self.dest.acquire_reads(["file.bin"])
        with self.dest._directory_fd():
            with self.assertRaises(DestinationError):
                self.dest.acquire_reads(["file.bin"])

    def test_order_duplicates_empty_and_both_lock_modes(self):
        first = self.save("first.bin")
        second = self.save("second.bin")
        for exclusive in (False, True):
            self.assertEqual(self.acquire([], exclusive=exclusive), [])
            result = self.acquire([second.filename, first.filename, second.filename],
                                  exclusive=exclusive)
            self.assertEqual([item for item, _ in result], [second, first, second])
            self.assertEqual(len({id(handle) for _, handle in result}), 3)
            for _, handle in result:
                self.assertEqual(handle.tell(), 0)
                self.assertEqual(handle.read(), b"original")

    def test_no_unrelated_metadata_or_payload_access(self):
        self.save()
        self.save("other.bin")
        self.marker("save", "committed", target="other.bin")
        self.marker("delete", token="b" * 24, target="other.bin")
        original_open = self.dest._fs.open_existing
        original_info = self.dest._fs.entry_info
        opened = []

        def open_existing(directory, name, **kwargs):
            self.assertNotIn(name, {"other.bin", "other.bin.json"})
            handle = original_open(directory, name, **kwargs)
            opened.append((name, handle))
            return handle

        def entry_info(directory, name):
            self.assertNotIn(name, {"other.bin", "other.bin.json"})
            return original_info(directory, name)

        with self.dest.operation_lock(exclusive=False):
            with mock.patch.object(self.dest._fs, "entry_names",
                                   wraps=self.dest._fs.entry_names) as names, \
                 mock.patch.object(self.dest._fs, "entries", side_effect=AssertionError), \
                 mock.patch.object(self.dest._fs, "entry_info", side_effect=entry_info), \
                 mock.patch.object(self.dest._fs, "open_existing", side_effect=open_existing):
                result = self.dest.acquire_reads(["file.bin"])
            names.assert_called_once()
        self.addCleanup(result[0][1].close)
        self.assertEqual([name for name, _ in opened].count("file.bin"), 1)
        self.assertEqual([name for name, _ in opened].count("file.bin.json"), 1)
        self.assertTrue(all(handle.closed for name, handle in opened if name != "file.bin"))

    def test_handles_survive_replace_delete_and_directory_close(self):
        self.save()
        result = self.acquire(["file.bin"])
        with self.dest.operation_lock(exclusive=True):
            self.dest.save(b"replacement", INFO, filename="file.bin", allow_replace=True)
        self.assertEqual(result[0][1].read(), b"original")
        replacement = self.acquire(["file.bin"])
        with self.dest.operation_lock(exclusive=True):
            self.dest.delete("file.bin")
        self.assertEqual(replacement[0][1].read(), b"replacement")
        self.assertFalse(result[0][1].closed)

    def test_invalid_missing_and_foreign_entries_are_unknown(self):
        self.save()
        (self.directory / "foreign.bin").write_bytes(b"foreign")
        for name in ("missing.bin", "foreign.bin", "../file.bin", ".pasteberth.lock"):
            with self.subTest(name=name), self.assertRaises(UnknownImageError):
                self.acquire([name])
        sidecar = self.directory / "file.bin.json"
        valid = sidecar.read_bytes()
        invalid = [b"{", b"[]", b"\xff", b"{}", b"x" * (self.dest.limits.max_metadata_bytes + 1)]
        for change in ({"size": 99}, {"filename": "wrong.bin"}, {"sha256": "invalid"}):
            raw = json.loads(valid)
            raw.update(change)
            invalid.append(json.dumps(raw).encode())
        for content in invalid:
            with self.subTest(content=content[:30]):
                sidecar.write_bytes(content)
                with self.assertRaises(UnknownImageError):
                    self.acquire(["file.bin"])

    def test_symlink_and_nonregular_payload_and_sidecar_are_unknown(self):
        self.save()
        outside = self.directory.parent / "outside"
        outside.write_bytes(b"original")
        for name in ("file.bin", "file.bin.json"):
            path = self.directory / name
            original = path.read_bytes()
            path.unlink()
            path.mkdir()
            with self.assertRaises(UnknownImageError):
                self.acquire(["file.bin"])
            path.rmdir()
            try:
                path.symlink_to(outside)
            except OSError:
                path.write_bytes(original)
                self.skipTest("symlinks unavailable")
            with self.assertRaises(UnknownImageError):
                self.acquire(["file.bin"])
            path.unlink()
            path.write_bytes(original)

    def test_hash_is_informational(self):
        stored = self.save()
        (self.directory / "file.bin").write_bytes(b"modified")
        result = self.acquire(["file.bin"])
        self.assertEqual(result[0][0].sha256, stored.sha256)
        self.assertEqual(result[0][1].read(), b"modified")

    def test_all_handles_close_on_failure_including_current_payload(self):
        self.save("first.bin")
        self.save("second.bin")
        (self.directory / "second.bin.json").write_bytes(b"{}")
        opened = []
        original_open = self.dest._fs.open_existing

        def track(directory, name, **kwargs):
            handle = original_open(directory, name, **kwargs)
            opened.append(handle)
            return handle

        with self.dest.operation_lock(exclusive=False):
            with mock.patch.object(self.dest._fs, "open_existing", side_effect=track):
                with self.assertRaises(UnknownImageError):
                    self.dest.acquire_reads(["first.bin", "second.bin"])
        self.assertGreaterEqual(len(opened), 4)
        self.assertTrue(all(handle.closed for handle in opened))

    def test_systemic_failures_are_not_unknown(self):
        self.save()
        marker, _ = self.marker("save", "committed")
        original_open = self.dest._fs.open_existing
        for failure in (OSError(errno.EIO, "I/O error"), PermissionError("denied"),
                        UnsupportedFilesystemError("unsupported")):
            for failed_name in ("file.bin", "file.bin.json", marker.name):
                def fail(directory, name, **kwargs):
                    if name == failed_name:
                        raise failure
                    return original_open(directory, name, **kwargs)

                with self.subTest(failure=failure, name=failed_name):
                    with self.dest.operation_lock(exclusive=False):
                        with mock.patch.object(self.dest._fs, "open_existing", side_effect=fail):
                            with self.assertRaises(DestinationError) as raised:
                                self.dest.acquire_reads(["file.bin"])
                    self.assertNotIsInstance(raised.exception, UnknownImageError)

    def test_sidecar_identity_change_is_unknown(self):
        self.save()
        original_read = self.dest._read_meta

        def replace(directory, name):
            raw = original_read(directory, name)
            temporary = self.directory / "replacement.json"
            temporary.write_text(json.dumps(raw), encoding="utf-8")
            os.replace(temporary, self.directory / name)
            return raw

        with mock.patch.object(self.dest, "_read_meta", side_effect=replace):
            with self.assertRaises(UnknownImageError):
                self.acquire(["file.bin"])

    def test_interrupt_closes_handles_and_cleanup_continues_after_close_error(self):
        self.save("first.bin")
        self.save("second.bin")
        opened = []
        original_open = self.dest._fs.open_existing
        original_validate = self.dest._validated_item

        def track(directory, name, **kwargs):
            handle = original_open(directory, name, **kwargs)
            opened.append(handle)
            if name == "first.bin":
                original_close = handle.close

                def close():
                    original_close()
                    raise OSError("close failed")

                handle.close = close
            return handle

        def interrupt(raw, name, *args):
            if name == "second.bin":
                raise KeyboardInterrupt
            return original_validate(raw, name, *args)

        with self.dest.operation_lock(exclusive=False):
            with mock.patch.object(self.dest._fs, "open_existing", side_effect=track), \
                 mock.patch.object(self.dest, "_validated_item", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.dest.acquire_reads(["first.bin", "second.bin"])
        self.assertEqual(len(opened), 4)
        self.assertTrue(all(handle.closed for handle in opened))

    def test_enumeration_and_identity_io_errors_are_not_unknown(self):
        self.save()
        self.marker("save", "committed")
        for method in ("entry_names", "identity"):
            with self.subTest(method=method):
                with self.dest.operation_lock(exclusive=False):
                    with mock.patch.object(self.dest._fs, method,
                                           side_effect=OSError(errno.EIO, "I/O error")):
                        with self.assertRaises(DestinationError) as raised:
                            self.dest.acquire_reads(["file.bin"])
                self.assertNotIsInstance(raised.exception, UnknownImageError)

    def test_historical_marker_classification_does_not_mask_io_failure(self):
        self.save()
        marker, _ = self.marker("delete")
        companion = marker.name + ".json"
        (self.directory / companion).write_bytes(b"{}")
        original_info = self.dest._fs.entry_info

        def fail(directory, name):
            if name == companion:
                raise OSError(errno.EIO, "I/O error")
            return original_info(directory, name)

        with self.dest.operation_lock(exclusive=False):
            with mock.patch.object(self.dest._fs, "entry_info", side_effect=fail):
                with self.assertRaises(DestinationError) as raised:
                    for _, handle in self.dest.acquire_reads(["file.bin"]):
                        handle.close()
                self.assertNotIsInstance(raised.exception, UnknownImageError)

    def test_list_and_targeted_transaction_visibility(self):
        self.save()
        for kind in ("save", "rename", "delete"):
            for state in ("prepared", "committed"):
                for mutation in (None, "data", "meta", "source", "source_meta"):
                    if kind == "delete" and (state != "prepared" or mutation is not None):
                        continue
                    if kind == "save" and mutation in ("source", "source_meta"):
                        continue
                    with self.subTest(kind=kind, state=state, mutation=mutation):
                        marker, raw = self.marker(kind, state)
                        extra = None
                        if mutation in ("data", "meta"):
                            key = "new_meta_identity" if mutation == "meta" else (
                                "source_identity" if kind == "rename" else "new_data_identity")
                            raw[key][1] += 1
                            marker.write_text(json.dumps(raw), encoding="utf-8")
                        elif mutation in ("source", "source_meta"):
                            extra = self.directory / ("source.bin" + (".json" if mutation == "source_meta" else ""))
                            extra.write_bytes(b"foreign")
                        visible = state == "committed" and mutation is None
                        try:
                            self.assert_visibility(["file.bin", "source.bin"],
                                                   ["file.bin"] if visible else [])
                        finally:
                            marker.unlink()
                            if extra is not None:
                                extra.unlink()

    def test_committed_marker_overrides_prepared_and_delete_blocks(self):
        self.save()
        self.marker("save", "prepared")
        self.marker("save", "committed")
        self.marker("delete", token="b" * 24)
        self.marker("rename", token="c" * 24)
        self.assert_visibility(["file.bin", "source.bin"], ["file.bin"])

    def test_all_journal_schema_generations_preserve_visibility(self):
        self.save()
        token = "a" * 24
        for kind in ("save", "rename"):
            for state in ("prepared", "committed"):
                marker, raw = self.marker(kind, state)
                additions = (
                    [("data_guard", f".pbtxn-guard-{token}.data"),
                     ("meta_guard", f".pbtxn-guard-{token}.json")]
                    if kind == "save" else
                    [("data_backup", f".pbrename-backup-{token}.data"),
                     ("data_guard", f".pbrename-guard-{token}.data"),
                     ("meta_guard", f".pbrename-guard-{token}.json"),
                     ("meta_backup_guard", f".pbrename-backup-{token}.guard.json")]
                )
                try:
                    for key, value in additions:
                        raw[key] = value
                        # Both metadata guards arrived in the same format.
                        if key == "meta_guard" and kind == "rename":
                            continue
                        with self.subTest(kind=kind, state=state, last_key=key):
                            marker.write_text(json.dumps(raw), encoding="utf-8")
                            self.assert_visibility(["file.bin", "source.bin"],
                                                   ["file.bin"] if state == "committed" else [])
                finally:
                    marker.unlink()

    def test_valid_rename_source_reappearance_is_hidden(self):
        self.save()
        self.save("source.bin")
        for state in ("prepared", "committed"):
            marker, _ = self.marker("rename", state)
            try:
                self.assert_visibility(["file.bin", "source.bin"], [])
            finally:
                marker.unlink()

    def test_malformed_journals_are_ignored_like_list(self):
        self.save()
        for prefix in ("pbtxn", "pbrename", "pbdel"):
            (self.directory / f".{prefix}-{'d' * 24}.json").write_bytes(b"{")
        self.assert_visibility(["file.bin"], ["file.bin"])

    def test_historical_pbdel_upload_is_not_executed_as_marker(self):
        self.save()
        marker, _ = self.marker("delete")
        # A client payload with a regular companion is not a deletion journal,
        # even if its bytes happen to describe a valid deletion.
        (self.directory / (marker.name + ".json")).write_bytes(b"{}")
        self.assert_visibility(["file.bin", marker.name], ["file.bin"])

    def test_historical_pbdel_mixed_case_companion_preserves_visibility(self):
        self.save()
        marker, _ = self.marker("delete")
        (self.directory / (marker.name + ".JSON")).write_bytes(b"{}")
        if not (self.directory / (marker.name + ".json")).is_file():
            self.skipTest("requires a case-insensitive filesystem")
        with self.dest.operation_lock(exclusive=False):
            directory = self.dest._operation_directory.get()
            self.assertTrue(self.dest._historical_pbdel_pair(directory, marker.name))
            self.assertNotIn(marker.name + ".json", self.dest._fs.entry_names(directory))
        self.assert_visibility(["file.bin", marker.name], ["file.bin"])
