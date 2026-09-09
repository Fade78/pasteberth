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

    def _symlink(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")

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

    def test_alias_chains_share_pass_metadata_and_leaf_enumeration(self):
        from collections import Counter

        target = self.tmp / "target"
        target.mkdir()
        previous = target
        for index in range(8):
            alias = self.tmp / f"alias-{index}"
            self._symlink(alias, previous)
            previous = alias
        rules = tuple(
            replace(rule(self.tmp, r"target"), id=f"@rule-{index}", label_mode="relative")
            for index in range(3)
        )
        resolutions, stats, enumerations = Counter(), Counter(), Counter()
        original_resolve, original_stat = Path.resolve, Path.stat
        original_scandir = zone_collection_module.os.scandir

        def resolve(path, *args, **kwargs):
            resolutions[path] += 1
            return original_resolve(path, *args, **kwargs)

        def stat(path, *args, **kwargs):
            stats[path] += 1
            return original_stat(path, *args, **kwargs)

        def scandir(path):
            enumerations[Path(path)] += 1
            return original_scandir(path)

        with (
            mock.patch.object(Path, "resolve", resolve),
            mock.patch.object(Path, "stat", stat),
            mock.patch.object(zone_collection_module.os, "scandir", scandir),
            self.assertLogs(zone_collection_module.log, level="DEBUG") as logs,
        ):
            candidates, diagnostics = discover_zone_collections(rules)

        self.assertEqual([candidate.zone.id for candidate in candidates], ["target"])
        self.assertEqual(candidates[0].collection_ids, tuple(item.id for item in rules))
        self.assertEqual(candidates[0].rule_indexes, (0, 1, 2))
        self.assertEqual(sum("directory alias" in message for message in diagnostics), 24)
        self.assertEqual(enumerations, {self.tmp: 1, target: 1})
        self.assertTrue(all(count == 1 for count in resolutions.values()), resolutions)
        # Strict Path.resolve may itself stat once; identity lookup adds at most one.
        self.assertLessEqual(stats[target], 2)
        self.assertLessEqual(stats[self.tmp], 2)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("resolution=0 stat=0 enumeration=0", logs.output[-1])

    def test_negative_candidate_inspection_is_cached_but_not_across_passes(self):
        from collections import Counter

        unreadable = self.tmp / "unreadable"
        unreadable.mkdir()
        ineligible = self.tmp / "ineligible"
        nested = ineligible / "nested"
        nested.mkdir(parents=True)
        rules = tuple(
            replace(rule(self.tmp, r"unreadable|ineligible"), id=f"@rule-{index}")
            for index in range(3)
        )
        enumerations = Counter()
        original_scandir = zone_collection_module.os.scandir
        deny = True

        def scandir(path):
            enumerations[Path(path)] += 1
            if Path(path) == unreadable and deny:
                raise PermissionError("permission denied")
            return original_scandir(path)

        with mock.patch.object(zone_collection_module.os, "scandir", scandir):
            candidates, diagnostics = discover_zone_collections(rules)
            self.assertEqual(candidates, [])
            # A rejected leaf probe stops early; walking it needs a full scan.
            self.assertEqual(enumerations, {
                self.tmp: 1, unreadable: 1, ineligible: 2, nested: 1,
            })
            self.assertEqual(sum("cannot inspect subtree" in text for text in diagnostics), 3)
            self.assertEqual(sum("user subdirectory" in text for text in diagnostics), 3)
            deny = False
            nested.rmdir()
            candidates, _ = discover_zone_collections(rules)

        self.assertEqual([item.zone.id for item in candidates], ["ineligible", "unreadable"])
        self.assertEqual(enumerations[unreadable], 2)
        self.assertEqual(enumerations[ineligible], 3)

    def test_max_depth_leaf_rejection_does_not_inspect_tail_entries(self):
        candidate = self.tmp / "candidate"
        candidate.mkdir()
        self._symlink(self.tmp / "alias", candidate)
        rules = tuple(
            replace(rule(self.tmp, r"candidate"), id=f"@rule-{index}", max_depth=1)
            for index in range(3)
        )
        original_scandir = zone_collection_module.os.scandir
        for first_is_error in (False, True):
            with self.subTest(first_is_error=first_is_error):
                first, tail = mock.Mock(), mock.Mock()
                first.name, tail.name = "a-subdir", "z-network-link"
                first.is_dir.return_value = True
                if first_is_error:
                    first.is_dir.side_effect = PermissionError("entry denied")
                tail.is_dir.side_effect = AssertionError("unnecessary network type check")
                iterator = mock.MagicMock()
                iterator.__enter__.return_value = [tail, first]
                context = zone_collection_module._DiscoveryPass()

                def scandir(path):
                    return iterator if Path(path) == candidate else original_scandir(path)

                with (
                    mock.patch.object(zone_collection_module.os, "scandir", scandir),
                    mock.patch.object(zone_collection_module, "_DiscoveryPass", return_value=context),
                ):
                    candidates, diagnostics = discover_zone_collections(rules)

                self.assertEqual(candidates, [])
                self.assertEqual(sum("candidate 'candidate' ignored" in text for text in diagnostics), 6)
                first.is_dir.assert_called_once_with(follow_symlinks=True)
                tail.is_dir.assert_not_called()
                iterator.__exit__.assert_called_once()
                self.assertNotIn(candidate, context.entries)
                self.assertFalse(context.subtrees[candidate][0])

    def test_partial_leaf_probe_does_not_hide_tail_from_later_rule(self):
        candidate = self.tmp / "candidate"
        (candidate / "a-subdir").mkdir(parents=True)
        (candidate / "z-tail").mkdir()
        rules = (
            replace(rule(self.tmp, r"candidate"), max_depth=1),
            replace(rule(self.tmp, r"candidate/z-tail"), id="@deep", max_depth=2),
        )
        original_scandir = zone_collection_module.os.scandir
        with mock.patch.object(
            zone_collection_module.os, "scandir", wraps=original_scandir,
        ) as scandir:
            candidates, _ = discover_zone_collections(rules)

        self.assertEqual([item.zone.id for item in candidates], ["candidate-z-tail"])
        self.assertEqual(candidates[0].collection_ids, ("@deep",))
        self.assertEqual(scandir.call_args_list.count(mock.call(candidate)), 2)

    def test_entry_cache_does_not_retain_successful_regular_file_records(self):
        directories = [self.tmp]
        for index in range(4):
            directory = self.tmp / f"leaf-{index}"
            directory.mkdir()
            directories.append(directory)
        for directory in directories:
            for index in range(40):
                (directory / f"file-{index}").touch()
        context = zone_collection_module._DiscoveryPass()
        with mock.patch.object(zone_collection_module, "_DiscoveryPass", return_value=context):
            candidates, _ = discover_zone_collections((rule(self.tmp, r"leaf-.*"),))

        self.assertEqual(len(candidates), 4)
        self.assertEqual(len(context.entries), 5)
        self.assertEqual(sum(len(entries) for entries, _ in context.entries.values()), 4)
        for directory in directories[1:]:
            self.assertEqual(context.entries[directory], ((), None))

    def test_overlapping_rules_keep_resolved_patterns_depth_and_conflicts(self):
        from collections import Counter

        shallow = self.tmp / "shallow"
        shallow.mkdir()
        deep = self.tmp / "branch" / "deep"
        deep.mkdir(parents=True)
        self._symlink(self.tmp / "shortcut", deep)
        rules = (
            replace(rule(self.tmp, r"shallow|branch/deep"), id="@shallow", max_depth=1),
            replace(rule(self.tmp, r"branch/deep"), id="@deep", max_depth=2),
            replace(rule(self.tmp, r"branch/deep"), id="@conflict", retain=3),
            replace(rule(self.tmp, r"shortcut|deep"), id="@lexical"),
        )
        original_scandir = zone_collection_module.os.scandir
        enumerations = Counter()

        def scandir(path):
            enumerations[Path(path)] += 1
            return original_scandir(path)

        with mock.patch.object(zone_collection_module.os, "scandir", scandir):
            candidates, diagnostics = discover_zone_collections(rules)

        self.assertEqual([item.zone.id for item in candidates], ["shallow"])
        self.assertEqual(candidates[0].collection_ids, ("@shallow",))
        self.assertTrue(any("conflicting zone settings" in text for text in diagnostics))
        self.assertTrue(all(count == 1 for count in enumerations.values()), enumerations)

    def test_entry_inspection_errors_are_cached_and_retried_next_pass(self):
        candidate = self.tmp / "candidate"
        candidate.mkdir()
        entry = mock.Mock()
        entry.name = "unreadable-entry"
        entry.is_dir.side_effect = PermissionError("entry denied")
        iterator = mock.MagicMock()
        iterator.__enter__.return_value = [entry]
        original_scandir = zone_collection_module.os.scandir
        rules = tuple(
            replace(rule(self.tmp, r"candidate"), id=f"@rule-{index}")
            for index in range(3)
        )

        def scandir(path):
            return iterator if Path(path) == candidate else original_scandir(path)

        with mock.patch.object(zone_collection_module.os, "scandir", scandir):
            candidates, diagnostics = discover_zone_collections(rules)

        self.assertEqual(candidates, [])
        self.assertEqual(sum("cannot inspect directory entry" in text for text in diagnostics), 3)
        self.assertEqual(entry.is_dir.call_args_list, [
            mock.call(follow_symlinks=True), mock.call(follow_symlinks=True),
        ])
        self.assertEqual(iterator.__exit__.call_count, 2)
        candidates, _ = discover_zone_collections(rules)
        self.assertEqual([item.zone.id for item in candidates], ["candidate"])

    def test_overlapping_bases_keep_their_own_relative_paths_and_membership(self):
        candidate = self.tmp / "repo" / "work" / "exchange"
        candidate.mkdir(parents=True)
        alias = self.tmp / "alias"
        self._symlink(alias, candidate.parent)
        rules = (
            rule(self.tmp),
            replace(rule(candidate.parent, r"exchange"), id="@nested", max_depth=1),
            replace(rule(alias, r"exchange"), id="@alias", max_depth=1),
        )
        original_scandir = zone_collection_module.os.scandir
        with mock.patch.object(
            zone_collection_module.os, "scandir", wraps=original_scandir,
        ) as scandir:
            candidates, _ = discover_zone_collections(rules)

        self.assertEqual([item.zone.id for item in candidates], ["exchange"])
        self.assertEqual(candidates[0].zone.directory, candidate)
        self.assertEqual(candidates[0].collection_ids, ("@nested", "@alias", "@repositories"))
        self.assertEqual(candidates[0].rule_indexes, (1, 2, 0))
        self.assertEqual(scandir.call_count, 4)

    def test_resolution_failures_are_cached_and_retried_next_pass(self):
        candidate = self.tmp / "candidate"
        candidate.mkdir()
        original_resolve = Path.resolve
        rules = tuple(
            replace(rule(self.tmp, r"candidate"), id=f"@rule-{index}")
            for index in range(3)
        )
        for unavailable in (self.tmp, candidate):
            with self.subTest(unavailable=unavailable):
                attempts = 0
                deny = True

                def resolve(path, *args, **kwargs):
                    nonlocal attempts
                    if path == unavailable:
                        attempts += 1
                        if deny:
                            raise FileNotFoundError("disappeared")
                    return original_resolve(path, *args, **kwargs)

                with mock.patch.object(Path, "resolve", resolve):
                    candidates, diagnostics = discover_zone_collections(rules)
                    self.assertEqual(candidates, [])
                    self.assertEqual(attempts, 1)
                    self.assertEqual(sum("disappeared" in text for text in diagnostics), 3)
                    deny = False
                    candidates, _ = discover_zone_collections(rules)
                self.assertEqual(attempts, 2)
                self.assertEqual([item.zone.id for item in candidates], ["candidate"])

    def test_static_directory_alias_with_different_id_takes_precedence(self):
        base = self.tmp / "base"
        candidate = base / "candidate"
        candidate.mkdir(parents=True)
        alias = self.tmp / "static-alias"
        self._symlink(alias, candidate)
        static = ZoneConfig(id="pinned", label="Pinned", directory=alias, retain=2)

        candidates, diagnostics = discover_zone_collections((rule(base, r".*"),), (static,))

        self.assertEqual(candidates, [])
        self.assertTrue(any("static zone has precedence" in text for text in diagnostics))

    def test_outside_links_cycles_and_retargeting_are_resolved_each_pass(self):
        base = self.tmp / "base"
        leaf = base / "leaf"
        leaf.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        alias = base / "alias"
        self._symlink(alias, outside)
        self._symlink(base / "cycle", base)
        loop = base / "loop"
        self._symlink(loop, loop)
        rules = (rule(base, r".*"),)

        candidates, _ = discover_zone_collections(rules)
        self.assertEqual([item.zone.directory for item in candidates], [leaf])
        alias.unlink()
        self._symlink(alias, leaf)
        candidates, diagnostics = discover_zone_collections(rules)
        self.assertEqual([item.zone.directory for item in candidates], [leaf])
        self.assertTrue(any(f"{alias} resolves to {leaf}" in text for text in diagnostics))
        (leaf / "child").mkdir()
        candidates, _ = discover_zone_collections(rules)
        self.assertEqual([item.zone.id for item in candidates], ["leaf-child"])

    def test_same_identity_at_different_bases_does_not_share_entry_paths(self):
        first, second = self.tmp / "first", self.tmp / "second"
        (first / "only-first").mkdir(parents=True)
        (second / "only-second").mkdir(parents=True)
        original_stat = Path.stat
        shared_info = first.stat()

        def stat(path, *args, **kwargs):
            if path in (first, second):
                return shared_info
            return original_stat(path, *args, **kwargs)

        rules = (rule(first, r".*"), replace(rule(second, r".*"), id="@second"))
        with mock.patch.object(Path, "stat", stat):
            candidates, _ = discover_zone_collections(rules)

        self.assertEqual([item.zone.id for item in candidates], ["only-first", "only-second"])
        self.assertEqual(candidates[1].zone.directory, second / "only-second")
        self.assertEqual(candidates[1].collection_ids, ("@second",))

    def test_scandir_is_closed_even_when_iteration_fails(self):
        iterator = mock.MagicMock()
        iterator.__enter__.return_value = iterator
        iterator.__iter__.side_effect = PermissionError("iteration denied")
        with mock.patch.object(zone_collection_module.os, "scandir", return_value=iterator):
            candidates, diagnostics = discover_zone_collections((rule(self.tmp),))

        self.assertEqual(candidates, [])
        self.assertTrue(any("iteration denied" in text for text in diagnostics))
        iterator.__exit__.assert_called_once()

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
        self.enterContext(mock.patch.object(
            service_module, "monotonic", return_value=self.service._zone_collection_refresh_after,
        ))
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
        self.enterContext(mock.patch.object(
            service_module, "monotonic", return_value=self.service._zone_collection_refresh_after,
        ))
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
        self.enterContext(mock.patch.object(
            service_module, "monotonic", return_value=self.service._zone_collection_refresh_after,
        ))
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
        self.enterContext(mock.patch.object(
            service_module, "monotonic", return_value=self.service._zone_collection_refresh_after,
        ))
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
