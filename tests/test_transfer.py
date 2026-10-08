"""Tests for managed copy/move operations between Pasteberth zones."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PasteBerth.runtime.config import load_config
from PasteBerth.runtime.service import PasteService, ServiceError
from PasteBerth.runtime.storage import DestinationError
from tests.helpers import write_config


class TransferServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config_path = write_config(
            self.tmp,
            zones=[
                {
                    "id": "source",
                    "label": "Source",
                    "retain": 10,
                    "directory": str(self.tmp / "source"),
                },
                {
                    "id": "target",
                    "label": "Target",
                    "retain": 1,
                    "directory": str(self.tmp / "target"),
                },
            ],
        )
        self.service = PasteService(load_config(self.config_path))
        self.addCleanup(self._tmp.cleanup)

    def upload(self, zone_id: str, filename: str, data: bytes = b"content") -> dict:
        return self.service.upload(
            zone_id,
            data,
            "text/plain",
            filename,
            preserve_filename=True,
            creation_method="web_paste",
        )

    def test_copy_preserves_source_pair_and_metadata(self):
        self.upload("source", "report.txt", b"old report")
        source = self.service.upload(
            "source",
            b"report",
            "text/plain",
            "report.txt",
            preserve_filename=True,
            allow_replace=True,
            creation_method="web_paste",
        )
        self.service.update_comment("source", "report.txt", "keep this note")
        source["comment"] = "keep this note"

        result = self.service.transfer(
            "source",
            "target",
            ["report.txt"],
            mode="copy",
        )

        self.assertEqual(result["mode"], "copy")
        self.assertEqual(result["transferred"], ["report.txt"])
        self.assertEqual(self.service._destinations["source"].read("report.txt"), b"report")
        self.assertEqual(self.service._destinations["target"].read("report.txt"), b"report")
        copied = self.service.history("target")[0]
        self.assertEqual(copied["filename"], source["filename"])
        self.assertEqual(copied["comment"], "keep this note")
        self.assertTrue(source["replaced"])
        self.assertTrue(copied["replaced"])
        source_meta = json.loads((self.tmp / "source" / "report.txt.json").read_text())
        target_meta = json.loads((self.tmp / "target" / "report.txt.json").read_text())
        self.assertEqual(target_meta["sha256"], source_meta["sha256"])
        self.assertEqual(target_meta["comment"], "keep this note")
        self.assertTrue(target_meta["replaced"])

    def test_move_removes_source_only_after_target_is_complete(self):
        self.upload("source", "report.txt", b"report")

        result = self.service.transfer(
            "source",
            "target",
            ["report.txt"],
            mode="move",
        )

        self.assertEqual(result["mode"], "move")
        self.assertEqual(result["transferred"], ["report.txt"])
        self.assertEqual(self.service.history("source"), [])
        self.assertEqual(self.service._destinations["target"].read("report.txt"), b"report")

    def test_batch_conflict_is_prevalidated_without_partial_copy(self):
        self.upload("source", "first.txt", b"first")
        self.upload("source", "second.txt", b"second")
        self.upload("target", "second.txt", b"existing")

        with self.assertRaisesRegex(ServiceError, "explicit replacement required") as raised:
            self.service.transfer(
                "source",
                "target",
                ["first.txt", "second.txt"],
                mode="copy",
            )

        self.assertEqual(raised.exception.code, "replacement_required")
        self.assertEqual(self.service.history("target")[0]["filename"], "second.txt")
        self.assertEqual(self.service.history("source")[0]["filename"], "second.txt")
        self.assertEqual(self.service.history("source")[1]["filename"], "first.txt")
        self.assertFalse((self.tmp / "target" / "first.txt").exists())

    def test_copy_replaces_only_an_existing_managed_target_when_authorized(self):
        self.upload("source", "report.txt", b"new report")
        self.upload("target", "report.txt", b"old report")

        result = self.service.transfer(
            "source",
            "target",
            ["report.txt"],
            mode="copy",
            replace_filenames=["report.txt"],
        )

        self.assertEqual(result["transferred"], ["report.txt"])
        self.assertEqual(self.service._destinations["source"].read("report.txt"), b"new report")
        self.assertEqual(self.service._destinations["target"].read("report.txt"), b"new report")
        target_item = self.service.history("target")[0]
        self.assertTrue(target_item["replaced"])

    def test_move_replaces_existing_managed_target_before_removing_source(self):
        self.upload("source", "report.txt", b"new report")
        self.upload("target", "report.txt", b"old report")

        result = self.service.transfer(
            "source",
            "target",
            ["report.txt"],
            mode="move",
            replace_filenames=["report.txt"],
        )

        self.assertEqual(result["transferred"], ["report.txt"])
        self.assertEqual(self.service.history("source"), [])
        self.assertEqual(self.service._destinations["target"].read("report.txt"), b"new report")
        self.assertTrue(self.service.history("target")[0]["replaced"])

    def test_replace_flag_does_not_overwrite_a_foreign_target(self):
        self.upload("source", "report.txt", b"managed source")
        target = self.tmp / "target" / "report.txt"
        target.write_bytes(b"foreign target")

        with self.assertRaises(ServiceError) as raised:
            self.service.transfer(
                "source",
                "target",
                ["report.txt"],
                mode="copy",
                replace_filenames=["report.txt"],
            )

        self.assertEqual(raised.exception.code, "storage_conflict")
        self.assertEqual(target.read_bytes(), b"foreign target")
        self.assertFalse(target.with_name("report.txt.json").exists())
        self.assertEqual(self.service._destinations["source"].read("report.txt"), b"managed source")

    def test_replace_names_must_be_selected_for_transfer(self):
        self.upload("source", "report.txt", b"report")

        with self.assertRaises(ServiceError) as raised:
            self.service.transfer(
                "source",
                "target",
                ["report.txt"],
                mode="copy",
                replace_filenames=["other.txt"],
            )

        self.assertEqual(raised.exception.code, "invalid_request")

    def test_transfer_rejects_same_zone_and_invalid_requests(self):
        self.upload("source", "report.txt")

        for mode in ("clone", "", None):
            with self.subTest(mode=mode), self.assertRaises(ServiceError):
                self.service.transfer("source", "target", ["report.txt"], mode=mode)
        with self.assertRaises(ServiceError):
            self.service.transfer("source", "target", [], mode="copy")
        with self.assertRaises(ServiceError):
            self.service.transfer("source", "source", ["report.txt"], mode="move")

    def test_changed_source_is_not_published(self):
        self.upload("source", "report.txt", b"report")
        source_destination = self.service._destinations["source"]
        with patch.object(source_destination, "read", return_value=b"tampered"):
            result = self.service.transfer(
                "source",
                "target",
                ["report.txt"],
                mode="copy",
            )

        self.assertEqual(result["transferred"], [])
        self.assertEqual(result["failed"][0]["code"], "storage_conflict")
        self.assertFalse(result["failed"][0]["target_published"])
        self.assertFalse((self.tmp / "target" / "report.txt").exists())
        self.assertEqual(source_destination.read("report.txt"), b"report")

    def test_move_reports_when_source_removal_fails_after_publish(self):
        self.upload("source", "report.txt", b"report")
        source_destination = self.service._destinations["source"]
        with patch.object(
            source_destination,
            "delete",
            side_effect=DestinationError("source unavailable"),
        ):
            result = self.service.transfer(
                "source",
                "target",
                ["report.txt"],
                mode="move",
            )

        self.assertEqual(result["transferred"], [])
        self.assertEqual(result["failed"][0]["code"], "destination_error")
        self.assertTrue(result["failed"][0]["target_published"])
        self.assertEqual(self.service._destinations["target"].read("report.txt"), b"report")
        self.assertEqual(source_destination.read("report.txt"), b"report")

    def test_move_does_not_delete_source_when_retention_evicts_target(self):
        self.upload("source", "first.txt", b"first")
        self.upload("source", "second.txt", b"second")

        result = self.service.transfer(
            "source",
            "target",
            ["first.txt", "second.txt"],
            mode="move",
        )

        self.assertEqual(len(result["transferred"]), 1)
        self.assertEqual(result["failed"][0]["code"], "retention_error")
        self.assertTrue(result["failed"][0]["target_published"])
        self.assertEqual(len(self.service.history("target")), 1)
        self.assertEqual(len(self.service.history("source")), 1)

    def test_copy_reports_target_evicted_by_retention(self):
        self.upload("source", "first.txt", b"first")
        self.upload("source", "second.txt", b"second")

        result = self.service.transfer(
            "source",
            "target",
            ["first.txt", "second.txt"],
            mode="copy",
        )

        self.assertEqual(len(result["transferred"]), 1)
        self.assertEqual(result["failed"][0]["code"], "retention_error")
        self.assertTrue(result["failed"][0]["target_published"])
        self.assertEqual(len(self.service.history("source")), 2)
        self.assertEqual(len(self.service.history("target")), 1)


if __name__ == "__main__":
    unittest.main()
