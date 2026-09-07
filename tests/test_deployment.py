"""Tests for deployment release identity checks."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PasteBerth.support.deploy.write_build_info import (
    file_digests,
    main as write_build_info,
    validate_release_identity,
)


def _release_tree(root: Path) -> tuple[Path, Path]:
    source = root / "PasteBerth"
    (source / "runtime").mkdir(parents=True)
    (source / "runtime" / "__init__.py").write_text(
        '__version__ = "2.1.16"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "2.1.16"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Pasteberth tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "PasteBerth", "pyproject.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "release fixture"], cwd=root, check=True)
    subprocess.run(["git", "tag", "v2.1.16"], cwd=root, check=True)
    destination = root.parent / f"{root.name}-deployed"
    shutil.copytree(source, destination)
    return source, destination


class TestDeploymentReleaseIdentity(unittest.TestCase):
    def test_accepts_matching_version_and_tag(self):
        validate_release_identity("2.1.16", "2.1.16", "v2.1.16")

    def test_rejects_mismatched_project_version(self):
        with self.assertRaisesRegex(SystemExit, "does not match"):
            validate_release_identity("2.1.16", "2.1.15", "v2.1.16")

    def test_rejects_missing_or_wrong_tag(self):
        with self.assertRaisesRegex(SystemExit, "expected exact release tag"):
            validate_release_identity("2.1.16", "2.1.16", None)

    def test_main_writes_a_manifest_for_the_tagged_bundle(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            with mock.patch.object(
                sys,
                "argv",
                [
                    "write_build_info.py",
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                ],
            ):
                self.assertEqual(write_build_info(), 0)
            info = json.loads((destination / "BUILD_INFO.json").read_text(encoding="utf-8"))
            self.assertEqual(info["source_tag"], "v2.1.16")
            self.assertFalse(info["source_dirty"])
            self.assertEqual(info["bundle_files"], file_digests(source))

    def test_main_reports_untracked_files_in_the_source_checkout(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            (source.parent / "release-note.txt").write_text("local note\n", encoding="utf-8")
            with mock.patch.object(
                sys,
                "argv",
                [
                    "write_build_info.py",
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                ],
            ):
                self.assertEqual(write_build_info(), 0)
            info = json.loads((destination / "BUILD_INFO.json").read_text(encoding="utf-8"))
            self.assertTrue(info["source_dirty"])

    def test_main_rejects_a_dirty_bundle(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            (source / "runtime" / "__init__.py").write_text(
                '__version__ = "2.1.16"\n# changed\n', encoding="utf-8"
            )
            with mock.patch.object(
                sys,
                "argv",
                [
                    "write_build_info.py",
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                ],
            ):
                with self.assertRaisesRegex(SystemExit, "tracked changes"):
                    write_build_info()


if __name__ == "__main__":
    unittest.main()
