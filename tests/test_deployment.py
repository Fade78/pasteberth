"""Tests for deployment release identity checks."""
import unittest

from PasteBerth.support.deploy.write_build_info import validate_release_identity


class TestDeploymentReleaseIdentity(unittest.TestCase):
    def test_accepts_matching_version_and_tag(self):
        validate_release_identity("2.1.14", "2.1.14", "v2.1.14")

    def test_rejects_mismatched_project_version(self):
        with self.assertRaisesRegex(SystemExit, "does not match"):
            validate_release_identity("2.1.14", "2.1.13", "v2.1.14")

    def test_rejects_missing_or_wrong_tag(self):
        with self.assertRaisesRegex(SystemExit, "expected exact release tag"):
            validate_release_identity("2.1.14", "2.1.14", None)


if __name__ == "__main__":
    unittest.main()
