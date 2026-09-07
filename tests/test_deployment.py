"""Tests for deployment release identity checks."""
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PasteBerth.support.deploy.write_build_info import (
    file_digests,
    git,
    main as write_build_info,
    source_checkout_dirty,
    validate_release_identity,
)


def _release_tree(root: Path) -> tuple[Path, Path]:
    source = root / "PasteBerth"
    (source / "runtime").mkdir(parents=True)
    (source / "runtime" / "__init__.py").write_text(
        '__version__ = "2.1.17"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nversion = "2.1.17"\n', encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Pasteberth tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "PasteBerth", "pyproject.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "release fixture"], cwd=root, check=True)
    subprocess.run(["git", "tag", "v2.1.17"], cwd=root, check=True)
    destination = root.parent / f"{root.name}-deployed"
    shutil.copytree(source, destination)
    return source, destination


def _write_manifest(source: Path, destination: Path) -> int:
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
        return write_build_info()


class TestDeploymentReleaseIdentity(unittest.TestCase):
    def test_accepts_matching_version_and_tag(self):
        validate_release_identity("2.1.17", "2.1.17", "v2.1.17")

    def test_rejects_mismatched_project_version(self):
        with self.assertRaisesRegex(SystemExit, "does not match"):
            validate_release_identity("2.1.17", "2.1.16", "v2.1.17")

    def test_rejects_missing_or_wrong_tag(self):
        with self.assertRaisesRegex(SystemExit, "expected exact release tag"):
            validate_release_identity("2.1.17", "2.1.17", None)

    def test_main_writes_a_manifest_for_the_tagged_bundle(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            self.assertEqual(_write_manifest(source, destination), 0)
            info = json.loads((destination / "BUILD_INFO.json").read_text(encoding="utf-8"))
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source.parent,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(info["version"], "2.1.17")
            self.assertEqual(info["source_commit"], expected_commit)
            self.assertEqual(info["source_tag"], "v2.1.17")
            self.assertFalse(info["source_dirty"])
            self.assertEqual(info["bundle_files"], file_digests(source))

    def test_main_reports_untracked_files_in_the_source_checkout(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            (source.parent / "release-note.txt").write_text("local note\n", encoding="utf-8")
            self.assertEqual(_write_manifest(source, destination), 0)
            info = json.loads((destination / "BUILD_INFO.json").read_text(encoding="utf-8"))
            self.assertTrue(info["source_dirty"])

    def test_main_rejects_a_dirty_bundle(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            (source / "runtime" / "__init__.py").write_text(
                '__version__ = "2.1.17"\n# changed\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(SystemExit, "tracked changes"):
                _write_manifest(source, destination)

    def test_main_rejects_a_source_mode_mismatch_even_when_git_ignores_modes(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            target = source / "runtime" / "__init__.py"
            target.chmod(stat.S_IMODE(target.stat().st_mode) | stat.S_IXUSR)
            subprocess.run(
                ["git", "config", "core.filemode", "false"],
                cwd=source.parent,
                check=True,
            )
            with self.assertRaisesRegex(SystemExit, "modes differ from tagged Git tree"):
                _write_manifest(source, destination)

    def test_main_rejects_destination_file_mismatches(self):
        for kind in ("missing", "changed", "extra"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw_root:
                source, destination = _release_tree(Path(raw_root))
                target = destination / "runtime" / "__init__.py"
                if kind == "missing":
                    target.unlink()
                elif kind == "changed":
                    target.write_text('__version__ = "2.1.17"\n# changed\n', encoding="utf-8")
                else:
                    (destination / "extra.txt").write_text("extra\n", encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "deployment bundle differs from source"):
                    _write_manifest(source, destination)

    def test_main_rejects_destination_mode_mismatch(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            target = destination / "runtime" / "__init__.py"
            target.chmod(stat.S_IMODE(target.stat().st_mode) | stat.S_IXUSR)
            with self.assertRaisesRegex(SystemExit, "file modes differ"):
                _write_manifest(source, destination)

    def test_main_rejects_destination_symlinks(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            target = destination / "runtime" / "__init__.py"
            target.unlink()
            target.symlink_to(source / "runtime" / "__init__.py")
            with self.assertRaisesRegex(SystemExit, "contains a symlink"):
                _write_manifest(source, destination)

    def test_main_rejects_a_missing_source_commit(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source, destination = _release_tree(Path(raw_root))
            real_git = git

            def missing_commit(root, *args):
                if args == ("rev-parse", "HEAD"):
                    return None
                return real_git(root, *args)

            with mock.patch(
                "PasteBerth.support.deploy.write_build_info.git",
                side_effect=missing_commit,
            ):
                with self.assertRaisesRegex(SystemExit, "could not determine full source commit"):
                    _write_manifest(source, destination)
            self.assertFalse((destination / "BUILD_INFO.json").exists())

    def test_source_dirty_check_fails_closed(self):
        with mock.patch(
            "PasteBerth.support.deploy.write_build_info.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["git", "status"]),
        ):
            with self.assertRaisesRegex(SystemExit, "could not determine source checkout status"):
                source_checkout_dirty(Path("."))


if __name__ == "__main__":
    unittest.main()
