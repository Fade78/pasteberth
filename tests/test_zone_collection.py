from dataclasses import replace
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PasteBerth.runtime import service as service_module
from PasteBerth.runtime import zone_collection as zone_collection_module
from PasteBerth.runtime.zone_collection import (
    _zone_collection_color,
    discover_zone_collections,
    resolve_collection_members,
)
from PasteBerth.runtime.config import (
    ConfigError,
    GroupConfig,
    ZoneCollectionConfig,
    ZoneConfig,
    load_config,
    resolve_group_zone_ids,
)
from PasteBerth.runtime.service import PasteService


def rule(base: Path, pattern: str = r"^[^/]+/work/exchange$") -> ZoneCollectionConfig:
    return ZoneCollectionConfig(
        id="@repositories",
        base_directory=base,
        pattern=pattern,
        max_depth=4,
        retain=2,
    )


class TestZoneCollectionDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_discovers_only_candidates_without_user_subdirectories(self):
        accepted = self.tmp / "accepted" / "work" / "exchange"
        accepted.mkdir(parents=True)
        rejected = self.tmp / "rejected" / "work" / "exchange"
        (rejected / "nested-user-directory").mkdir(parents=True)

        candidates, diagnostics = discover_zone_collections((rule(self.tmp),))

        self.assertEqual([candidate.zone.id for candidate in candidates], [
            "accepted-work-exchange",
        ])
        self.assertTrue(any("user subdirectory" in message for message in diagnostics))

    def test_directory_illisible_est_ignoire_avec_un_diagnostic(self):
        candidate = self.tmp / "unreadable" / "work" / "exchange"
        candidate.mkdir(parents=True)
        original_scandir = zone_collection_module.os.scandir

        def scandir(path):
            if Path(path) == candidate:
                raise PermissionError("permission denied")
            return original_scandir(path)

        with mock.patch.object(zone_collection_module.os, "scandir", side_effect=scandir):
            candidates, diagnostics = discover_zone_collections((rule(self.tmp),))

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

        candidates, diagnostics = discover_zone_collections((rule(self.tmp),), {static.id: static})

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

        candidates, diagnostics = discover_zone_collections(
            (rule(self.tmp, r"^(?:alias|target)/work/exchange$"),)
        )

        self.assertEqual(len(candidates), 1)
        self.assertTrue(any("directory alias" in message for message in diagnostics))

    def test_omitted_color_is_deterministic_for_path_and_collection(self):
        first = self.tmp / "first" / "work" / "exchange"
        second = self.tmp / "second" / "work" / "exchange"
        first.mkdir(parents=True)
        second.mkdir(parents=True)

        first_rule = rule(self.tmp)
        first_candidates, _ = discover_zone_collections((first_rule,))
        first_color = next(
            candidate.zone.color
            for candidate in first_candidates
            if candidate.zone.directory == first
        )
        second_candidates, _ = discover_zone_collections((first_rule,))
        second_color = next(
            candidate.zone.color
            for candidate in second_candidates
            if candidate.zone.directory == second
        )

        self.assertEqual(first_color, next(
            candidate.zone.color
            for candidate in discover_zone_collections((first_rule,))[0]
            if candidate.zone.directory == first
        ))
        self.assertEqual(second_color, next(
            candidate.zone.color
            for candidate in discover_zone_collections((first_rule,))[0]
            if candidate.zone.directory == second
        ))
        self.assertNotEqual(first_color, second_color)
        self.assertNotEqual(
            _zone_collection_color(Path("/var/lib/pasteberth/repo-a"), "@repositories"),
            _zone_collection_color(Path("/var/lib/pasteberth/repo-b"), "@repositories"),
        )
        self.assertNotEqual(
            _zone_collection_color(Path("/var/lib/pasteberth/repo-a"), "@repositories"),
            _zone_collection_color(Path("/var/lib/pasteberth/repo-a"), "@other"),
        )

    def test_omitted_colors_are_distinct_within_a_collection(self):
        for index in range(20):
            (self.tmp / f"repo-{index}" / "work" / "exchange").mkdir(parents=True)

        candidates, _ = discover_zone_collections((rule(self.tmp),))
        colors = [candidate.zone.color for candidate in candidates]

        self.assertEqual(len(colors), 20)
        self.assertEqual(len(colors), len(set(colors)))

    def test_explicit_collection_color_is_preserved(self):
        candidate_path = self.tmp / "repo" / "work" / "exchange"
        candidate_path.mkdir(parents=True)
        explicit = rule(self.tmp)
        explicit = replace(explicit, color="#243447")

        candidates, _ = discover_zone_collections((explicit,))

        self.assertEqual(candidates[0].zone.color, "#243447")

    def test_first_directory_label_uses_the_root_below_base(self):
        candidate_path = self.tmp / "repo" / "nested" / "work" / "exchange"
        candidate_path.mkdir(parents=True)
        first_directory = replace(
            rule(self.tmp, r"^[^/]+/nested/work/exchange$"),
            label_mode="first-directory",
        )

        candidates, _ = discover_zone_collections((first_directory,))

        self.assertEqual(candidates[0].zone.label, "repo")

    def test_candidate_can_belong_to_several_collections(self):
        candidate_path = self.tmp / "repo" / "work" / "exchange"
        candidate_path.mkdir(parents=True)

        candidates, _ = discover_zone_collections(
            (rule(self.tmp), replace(rule(self.tmp), id="@workspaces"))
        )

        self.assertEqual(candidates[0].collection_ids, (
            "@repositories",
            "@workspaces",
        ))
        self.assertEqual(
            resolve_collection_members(candidates, {candidates[0].zone.id: candidates[0].zone}),
            {candidates[0].collection_ids[0]: (candidates[0].zone.id,),
             candidates[0].collection_ids[1]: (candidates[0].zone.id,)},
        )

    def test_conflicting_collection_settings_reject_the_candidate(self):
        candidate_path = self.tmp / "repo" / "work" / "exchange"
        candidate_path.mkdir(parents=True)

        candidates, diagnostics = discover_zone_collections(
            (rule(self.tmp), replace(rule(self.tmp), id="@workspaces", retain=3))
        )

        self.assertEqual(candidates, [])
        self.assertTrue(any("conflicting zone settings" in message for message in diagnostics))

    def test_generated_zone_id_collision_between_collections_is_rejected(self):
        first_base = self.tmp / "first"
        second_base = self.tmp / "second"
        (first_base / "repo" / "work" / "exchange").mkdir(parents=True)
        (second_base / "repo" / "work" / "exchange").mkdir(parents=True)

        candidates, diagnostics = discover_zone_collections(
            (
                replace(rule(first_base), id="@first"),
                replace(rule(second_base), id="@second"),
            )
        )

        self.assertEqual(candidates, [])
        self.assertTrue(any("another zone collection" in message for message in diagnostics))

    def test_group_pattern_expands_a_zone_collection(self):
        candidate_path = self.tmp / "repo" / "work" / "exchange"
        candidate_path.mkdir(parents=True)
        candidates, _ = discover_zone_collections((rule(self.tmp),))
        dynamic = candidates[0].zone
        zones = {
            "static": ZoneConfig(
                id="static",
                label="Static",
                directory=self.tmp / "static",
                retain=2,
            ),
            dynamic.id: dynamic,
        }
        groups = (
            GroupConfig(name="Other", selection="other"),
            GroupConfig(
                name="Repositories",
                selection="pattern",
                pattern=(r"^@repositories$",),
                pattern_defined=True,
            ),
        )

        collection_members = resolve_collection_members(candidates, zones)

        self.assertEqual(resolve_group_zone_ids(groups, zones, collection_members), {
            "Other": ("static",),
            "Repositories": (dynamic.id,),
        })


class TestZoneCollectionConfiguration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _load(self, body: str):
        path = self.tmp / "config.toml"
        path.write_text(body, encoding="utf-8")
        return load_config(path)

    def test_collection_peut_etre_la_seule_source_de_zones(self):
        cfg = self._load(
            f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[zone_collection]]
id = \"@repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
max_items = 4
"""
        )

        self.assertEqual(cfg.zones, {})
        self.assertEqual(len(cfg.zone_collections), 1)
        self.assertEqual(cfg.zone_collections[0].id, "@repositories")
        self.assertIsNone(cfg.zone_collections[0].color)

    def test_collection_explicit_color_is_loaded(self):
        cfg = self._load(
            f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[zone_collection]]
id = \"@repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
color = \"#304237\"
"""
        )

        self.assertEqual(cfg.zone_collections[0].color, "#304237")

    def test_collection_first_directory_label_mode_is_loaded(self):
        cfg = self._load(
            f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[zone_collection]]
id = \"@repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
label_mode = \"first-directory\"
"""
        )

        self.assertEqual(cfg.zone_collections[0].label_mode, "first-directory")

    def test_collection_est_sidecar_et_convertit_ancienne_limite(self):
        common = f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true
base_directory = {str(self.tmp)!r}
"""
        cfg = self._load(
            common
            + "\n[[zone_collection]]\n"
            + 'id = "@repositories"\n'
            + "base_directory = " + repr(str(self.tmp)) + "\n"
            + "pattern = \"^[^/]+$\"\n"
            + "storage_mode = \"directory\"\n"
            + "max_items = 2\n"
        )
        self.assertEqual(cfg.zone_collections[0].storage_mode, "sidecar")
        self.assertEqual(cfg.zone_collections[0].retain, 2)
        self.assertTrue(any("using sidecar storage" in warning for warning in cfg.warnings))

    def test_collection_id_requires_the_reserved_prefix(self):
        with self.assertRaisesRegex(ConfigError, "invalid collection ID"):
            self._load(
                f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[zone_collection]]
id = \"repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
"""
            )

    def test_invalid_legacy_max_items_is_rejected_even_with_retain(self):
        with self.assertRaisesRegex(ConfigError, "max_items"):
            self._load(
                f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[zone_collection]]
id = \"@repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
retain = 10
max_items = \"invalid\"
"""
            )

    def test_previous_collection_table_is_not_accepted(self):
        with self.assertRaisesRegex(ConfigError, "legacy dynamic-zone key"):
            self._load(
                f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true

[[autozone]]
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+$\"
group = \"Repositories\"
"""
            )


class TestDynamicCollectionService(unittest.TestCase):
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

[[zone_collection]]
id = \"@repositories\"
base_directory = {str(self.tmp)!r}
pattern = \"^[^/]+/work/exchange$\"
retain = 2

[[groups]]
name = \"Repositories\"
selection = \"pattern\"
pattern = [\"^@repositories$\"]
""",
            encoding="utf-8",
        )
        self.service = PasteService(load_config(config))
        self.addCleanup(self.service.close)

    def test_dynamic_lifecycle_ignores_direct_files_and_retains_uploads(self):
        zone_id = "repo-work-exchange"
        self.assertTrue(self.service.has_zone(zone_id))
        self.assertEqual(self.service.active_zone_count(), 1)
        self.assertEqual(self.service.group_overview()[0]["selection"], "pattern")

        (self.candidate / "external.txt").write_text("external", encoding="utf-8")
        history = self.service.history(zone_id)
        self.assertEqual(history, [])

        self.service.upload(zone_id, b"second", "text/plain")
        overview = self.service.overview()
        dynamic = overview["zones"][0]
        self.assertEqual(dynamic["retain"], 2)
        self.assertEqual(dynamic["storage_mode"], "sidecar")
        self.assertRegex(dynamic["color"], r"^#[0-9a-f]{6}$")
        self.service.upload(zone_id, b"third", "text/plain")
        self.assertEqual(len(self.service.history(zone_id)), 2)

        shutil.rmtree(self.candidate)
        self.assertFalse(self.service.has_zone(zone_id))

    def test_destination_reuse_rejects_changed_creation_settings(self):
        zone_id = "repo-work-exchange"
        destination = self.service._destinations[zone_id]
        zone = self.service._zone_cfg[zone_id]

        self.assertFalse(
            self.service._destination_matches(
                destination,
                replace(zone, file_group="pasteberth"),
            )
        )

    def test_new_matching_project_is_discovered_without_restart(self):
        new_candidate = self.tmp / "new-repo" / "work" / "exchange"
        new_candidate.mkdir(parents=True)

        self.assertTrue(self.service.has_zone("new-repo-work-exchange"))
        overview = self.service.overview()
        self.assertIn(
            "new-repo-work-exchange",
            {zone["id"] for zone in overview["zones"]},
        )

    def test_overview_ne_bloque_pas_pendant_une_decouverte_lente(self):
        original_discovery = service_module.discover_zone_collections
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        result = {}

        def slow_discovery(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_discovery(*args, **kwargs)

        def read_overview():
            result["overview"] = self.service.overview()
            finished.set()

        with mock.patch.object(
            service_module,
            "discover_zone_collections",
            side_effect=slow_discovery,
        ):
            worker = threading.Thread(target=read_overview)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(finished.wait(1))
            finally:
                release.set()
                worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertIn("zones", result["overview"])

    def test_overview_signale_une_zone_disparue_pendant_le_scan(self):
        original_discovery = service_module.discover_zone_collections
        started = threading.Event()
        release = threading.Event()

        def slow_discovery(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_discovery(*args, **kwargs)

        with mock.patch.object(
            service_module,
            "discover_zone_collections",
            side_effect=slow_discovery,
        ):
            self.service.overview()
            self.assertTrue(started.wait(1))
            shutil.rmtree(self.candidate)
            overview = self.service.overview()
            release.set()

        zone = next(zone for zone in overview["zones"] if zone["id"] == "repo-work-exchange")
        self.assertTrue(zone["busy"])

    def test_operation_explicite_attend_la_fin_du_scan(self):
        original_discovery = service_module.discover_zone_collections
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_discovery(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_discovery(*args, **kwargs)

        def read_history():
            self.service.history("repo-work-exchange")
            finished.set()

        with mock.patch.object(
            service_module,
            "discover_zone_collections",
            side_effect=slow_discovery,
        ):
            self.service.overview()
            self.assertTrue(started.wait(1))
            worker = threading.Thread(target=read_history)
            worker.start()
            try:
                self.assertFalse(finished.wait(0.1))
            finally:
                release.set()
                worker.join(1)

        self.assertTrue(finished.is_set())
        self.assertFalse(worker.is_alive())

    def test_close_attend_la_decouverte_en_cours(self):
        original_discovery = service_module.discover_zone_collections
        started = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        def slow_discovery(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_discovery(*args, **kwargs)

        with mock.patch.object(
            service_module,
            "discover_zone_collections",
            side_effect=slow_discovery,
        ) as discovery:
            self.service.overview()
            self.assertTrue(started.wait(1))
            closer = threading.Thread(
                target=lambda: (self.service.close(), closed.set())
            )
            closer.start()
            self.assertFalse(closed.wait(0.1))
            release.set()
            self.assertTrue(closed.wait(1))
            closer.join(1)
            self.service.overview()
            discovery.assert_called_once()

        self.assertFalse(closer.is_alive())
