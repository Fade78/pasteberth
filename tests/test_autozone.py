import tempfile
import unittest
from pathlib import Path
import shutil
from unittest import mock

from PasteBerth.runtime import autozone as autozone_module
from PasteBerth.runtime.autozone import discover_autozones, merge_autozone_groups
from PasteBerth.runtime.config import (
    AutoZoneConfig,
    GroupConfig,
    ZoneConfig,
    load_config,
    resolve_group_zone_ids,
)
from PasteBerth.runtime.service import PasteService


def rule(base: Path, pattern: str = r"^[^/]+/work/exchange$") -> AutoZoneConfig:
    return AutoZoneConfig(
        base_directory=base,
        pattern=pattern,
        max_depth=4,
        group="Repositories",
        retain=2,
    )


class TestAutozoneDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_discovers_only_candidates_without_user_subdirectories(self):
        accepted = self.tmp / "accepted" / "work" / "exchange"
        accepted.mkdir(parents=True)
        rejected = self.tmp / "rejected" / "work" / "exchange"
        (rejected / "nested-user-directory").mkdir(parents=True)

        candidates, diagnostics = discover_autozones((rule(self.tmp),))

        self.assertEqual([candidate.zone.id for candidate in candidates], [
            "accepted-work-exchange",
        ])
        self.assertTrue(any("user subdirectory" in message for message in diagnostics))

    def test_directory_illisible_est_ignoire_avec_un_diagnostic(self):
        candidate = self.tmp / "unreadable" / "work" / "exchange"
        candidate.mkdir(parents=True)
        original_scandir = autozone_module.os.scandir

        def scandir(path):
            if Path(path) == candidate:
                raise PermissionError("permission denied")
            return original_scandir(path)

        with mock.patch.object(autozone_module.os, "scandir", side_effect=scandir):
            candidates, diagnostics = discover_autozones((rule(self.tmp),))

        self.assertEqual(candidates, [])
        self.assertTrue(any("cannot inspect subtree" in message for message in diagnostics))

    def test_static_id_and_directory_take_precedence(self):
        candidate_path = self.tmp / "repo" / "work" / "exchange"
        candidate_path.mkdir(parents=True)
        static = ZoneConfig(
            id="repo-work-exchange",
            label="Static",
            directory=candidate_path,
            retain=3,
        )

        candidates, diagnostics = discover_autozones((rule(self.tmp),), {static.id: static})

        self.assertEqual(candidates, [])
        self.assertTrue(any("static zone has precedence" in message for message in diagnostics))

    def test_aliases_are_deduplicated_deterministically(self):
        target = self.tmp / "target" / "work" / "exchange"
        target.mkdir(parents=True)
        alias = self.tmp / "alias"
        try:
            alias.symlink_to(self.tmp / "target", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")

        candidates, diagnostics = discover_autozones(
            (rule(self.tmp, r"^(?:alias|target)/work/exchange$"),)
        )

        self.assertEqual(len(candidates), 1)
        self.assertTrue(any("directory alias" in message for message in diagnostics))

    def test_generated_group_keeps_empty_rule_and_explicit_membership(self):
        configured = (GroupConfig(name="Other", selection="other"),)
        groups, diagnostics = merge_autozone_groups(
            configured,
            (rule(self.tmp),),
            {},
            (),
        )

        self.assertEqual(diagnostics, [])
        self.assertEqual([group.name for group in groups], ["Other", "Repositories"])
        self.assertEqual(groups[-1].selection, "autozone")
        self.assertEqual(resolve_group_zone_ids(groups, ("static",)), {
            "Other": ("static",),
            "Repositories": (),
        })


class TestAutozoneConfiguration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _load(self, body: str):
        path = self.tmp / "config.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path)

    def test_autozone_peut_etre_la_seule_source_de_zones(self):
        cfg = self._load(
            f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[autozone]]
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
group = \"Repositories\"
max_items = 4
"""
        )

        self.assertEqual(cfg.zones, {})
        self.assertEqual(len(cfg.autozones), 1)

    def test_autozone_est_sidecar_et_convertit_ancienne_limite(self):
        common = f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true
base_directory = {str(self.tmp)!r}
"""
        cfg = self._load(
            common
            + "\n[[autozone]]\n"
            + "base_directory = " + repr(str(self.tmp)) + "\n"
            + "pattern = \"^[^/]+$\"\n"
            + "group = \"Repositories\"\n"
            + "storage_mode = \"directory\"\n"
            + "max_items = 2\n"
        )
        self.assertEqual(cfg.autozones[0].storage_mode, "sidecar")
        self.assertEqual(cfg.autozones[0].retain, 2)
        self.assertTrue(any("using sidecar storage" in warning for warning in cfg.warnings))


class TestDynamicService(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.candidate = self.tmp / "repo" / "work" / "exchange"
        self.candidate.mkdir(parents=True)
        config = self.tmp / "config.toml"
        config.write_text(
            f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[autozone]]
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+/work/exchange$\"
group = \"Repositories\"
retain = 2
""",
            encoding="utf-8",
        )
        self.service = PasteService(load_config(config))

    def test_dynamic_lifecycle_ignores_direct_files_and_retains_uploads(self):
        zone_id = "repo-work-exchange"
        self.assertTrue(self.service.has_zone(zone_id))
        self.assertEqual(self.service.group_overview()[0]["selection"], "autozone")

        (self.candidate / "external.txt").write_text("external", encoding="utf-8")
        history = self.service.history(zone_id)
        self.assertEqual(history, [])

        self.service.upload(zone_id, b"second", "text/plain")
        overview = self.service.overview()
        dynamic = overview["zones"][0]
        self.assertEqual(dynamic["retain"], 2)
        self.assertEqual(dynamic["storage_mode"], "sidecar")
        self.service.upload(zone_id, b"third", "text/plain")
        self.assertEqual(len(self.service.history(zone_id)), 2)

        shutil.rmtree(self.candidate)
        self.assertFalse(self.service.has_zone(zone_id))
