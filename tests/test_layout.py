"""Tests for the directly copyable deployment layout."""
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "PasteBerth"


class TestDeploymentLayout(unittest.TestCase):
    def test_bundle_exposes_one_executable_and_private_runtime(self):
        executable = BUNDLE / "pasteberth"

        self.assertTrue(executable.is_file())
        self.assertTrue(os.access(executable, os.X_OK))
        self.assertTrue((BUNDLE / "runtime" / "__main__.py").is_file())
        self.assertTrue((BUNDLE / "runtime" / "static").is_dir())
        self.assertTrue((BUNDLE / "runtime" / "templates").is_dir())
        self.assertFalse((BUNDLE / "bin").exists())
        self.assertFalse((BUNDLE / "runtime" / "pasteberth").exists())

    def test_bundle_does_not_contain_mutable_deployment_state(self):
        for name in ("config.toml", "passwd", "storage", "captures"):
            with self.subTest(name=name):
                self.assertFalse((BUNDLE / name).exists())


if __name__ == "__main__":
    unittest.main()
