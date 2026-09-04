import tempfile
import unittest
from pathlib import Path
import shutil
from dataclasses import replace
from unittest import mock

from PasteBerth.runtime.autozone import discover_autozones, merge_autozone_groups
from PasteBerth.runtime.config import (
    AutoZoneConfig,
    ConfigError,
    GroupConfig,
    ZoneConfig,
    load_config,
    resolve_group_zone_ids,
)
from PasteBerth.runtime.content import ContentInfo
from PasteBerth.runtime.service import PasteService, ServiceError
from PasteBerth.runtime.storage import (
    DirectoryDestination,
    StorageLimitError,
)


def rule(base: Path, pattern: str = r"^[^/]+/work/exchange$") -> AutoZoneConfig:
    return AutoZoneConfig(
        base_directory=base,
        pattern=pattern,
        max_depth=4,
        group="Repositories",
        max_items=2,
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

    def test_autozone_exige_max_items_et_refuse_sidecar(self):
        common = f"""listen_address = \"127.0.0.1\"
allowed_hosts = [\"localhost\"]
allow_unauthenticated_local = true
base_directory = {str(self.tmp)!r}
"""
        for extra in (
            "storage_mode = \"directory\"\n",
            "storage_mode = \"sidecar\"\nmax_items = 2\n",
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ConfigError):
                    self._load(
                        common
                        + "\n[[autozone]]\n"
                        + "base_directory = " + repr(str(self.tmp)) + "\n"
                        + "pattern = \"^[^/]+$\"\n"
                        + "group = \"Repositories\"\n"
                        + extra
                    )


class TestDirectoryDestination(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.destination = DirectoryDestination(self.tmp, max_items=2)
        self.info = ContentInfo(kind="text", ext=".txt", mime="text/plain")

    def test_root_files_are_visible_without_sidecars(self):
        source = self.tmp.with_name(f"{self.tmp.name}-source.txt")
        source.write_text("hello", encoding="utf-8")
        self.addCleanup(source.unlink)
        shutil.copyfile(source, self.tmp / "external.txt")

        items = self.destination.list()

        self.assertEqual([item.filename for item in items], ["external.txt"])
        self.assertEqual(items[0].comment, "")
        self.assertIsNotNone(items[0].changed_at)
        self.assertFalse((self.tmp / "external.txt.json").exists())

    def test_comment_is_optional_and_stale_annotations_do_not_hide_data(self):
        (self.tmp / "external.txt").write_text("hello", encoding="utf-8")
        self.destination.update_comment("external.txt", "memo")
        self.assertEqual(self.destination.list()[0].comment, "memo")

        (self.tmp / "external.txt").write_text("changed", encoding="utf-8")

        items = self.destination.list()
        self.assertEqual(items[0].filename, "external.txt")
        self.assertEqual(items[0].comment, "")

    def test_limit_blocks_new_file_but_delete_remains_available(self):
        (self.tmp / "one.txt").write_text("one", encoding="utf-8")
        (self.tmp / "two.txt").write_text("two", encoding="utf-8")
        with self.assertRaises(StorageLimitError):
            self.destination.save(b"three", self.info, filename="three.txt")

        self.destination.delete("one.txt")
        stored = self.destination.save(b"three", self.info, filename="three.txt")
        self.assertEqual(stored.filename, "three.txt")

    def test_rename_and_delete_preserve_external_file_semantics(self):
        (self.tmp / "source.txt").write_text("source", encoding="utf-8")

        renamed = self.destination.rename("source.txt", "target.txt")

        self.assertEqual(renamed.filename, "target.txt")
        self.assertTrue((self.tmp / "target.txt").is_file())
        self.destination.delete("target.txt")
        self.assertFalse((self.tmp / "target.txt").exists())

    def test_completed_incoming_file_is_published_on_list(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        (incoming / "published.txt").write_text("published", encoding="utf-8")
        (incoming / "pbinc_partial.txt").write_text("partial", encoding="utf-8")

        items = self.destination.list()

        self.assertEqual([item.filename for item in items], ["published.txt"])
        self.assertEqual((self.tmp / "published.txt").read_text(encoding="utf-8"), "published")
        self.assertFalse((incoming / "published.txt").exists())
        self.assertTrue((incoming / "pbinc_partial.txt").exists())

    def test_incoming_collision_is_not_allowed_to_replace_root_file(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        (self.tmp / "same.txt").write_text("root", encoding="utf-8")
        (incoming / "same.txt").write_text("incoming", encoding="utf-8")

        self.destination.list()

        self.assertEqual((self.tmp / "same.txt").read_text(encoding="utf-8"), "root")
        self.assertEqual((incoming / "same.txt").read_text(encoding="utf-8"), "incoming")

    def test_incoming_file_counts_before_a_new_save(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        (incoming / "published.txt").write_text("published", encoding="utf-8")
        (self.tmp / "existing.txt").write_text("existing", encoding="utf-8")

        with self.assertRaises(StorageLimitError):
            self.destination.save(b"third", self.info, filename="third.txt")

        self.assertTrue((self.tmp / "published.txt").exists())
        self.assertFalse((self.tmp / "third.txt").exists())

    def test_pbinc_masque_une_copie_progressive_puis_publie_apres_renommage(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        partial = incoming / "pbinc_report.txt"
        published = incoming / "report.txt"
        data = b"first line\nsecond line\n"

        with partial.open("wb") as stream:
            stream.write(data[:6])
            stream.flush()
            self.assertEqual(self.destination.list(), [])
        self.assertTrue(partial.exists())

        with partial.open("ab") as stream:
            stream.write(data[6:])
            stream.flush()
        partial.rename(published)

        items = self.destination.list()
        self.assertEqual([item.filename for item in items], ["report.txt"])
        self.assertEqual((self.tmp / "report.txt").read_bytes(), data)
        self.assertFalse(published.exists())

    def test_publication_incoming_ne_depasse_pas_max_items(self):
        incoming = self.tmp / "incoming"
        incoming.mkdir()
        (self.tmp / "one.txt").write_text("one", encoding="utf-8")
        (self.tmp / "two.txt").write_text("two", encoding="utf-8")
        pending = incoming / "three.txt"
        pending.write_text("three", encoding="utf-8")

        items = self.destination.list()

        self.assertEqual(
            {item.filename for item in items},
            {"one.txt", "two.txt"},
        )
        self.assertTrue(pending.exists())
        self.assertFalse((self.tmp / "three.txt").exists())

        self.destination.delete("one.txt")
        items = self.destination.list()
        self.assertEqual([item.filename for item in items], ["three.txt", "two.txt"])
        self.assertFalse(pending.exists())

    def test_classification_ignore_fichier_modifie_pendant_inspection(self):
        target = self.tmp / "copied.txt"
        target.write_text("copied", encoding="utf-8")

        with self.destination._directory_fd() as directory_fd:
            entry = next(
                item
                for item in self.destination._regular_root_entries(directory_fd)
                if item.name == target.name
            )
            original_entry_info = self.destination._fs.entry_info

            def changed_entry_info(directory, name):
                current = original_entry_info(directory, name)
                if name == target.name and current is not None:
                    return replace(
                        current,
                        modified_ns=(current.modified_ns or 0) + 1,
                    )
                return current

            with mock.patch.object(
                self.destination._fs,
                "entry_info",
                side_effect=changed_entry_info,
            ):
                self.assertIsNone(
                    self.destination._classify_entry(directory_fd, entry)
                )

        self.assertEqual(
            [item.filename for item in self.destination.list()],
            [target.name],
        )


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
max_items = 2
""",
            encoding="utf-8",
        )
        self.service = PasteService(load_config(config))

    def test_dynamic_lifecycle_and_limit_error(self):
        zone_id = "repo-work-exchange"
        self.assertTrue(self.service.has_zone(zone_id))
        self.assertEqual(self.service.group_overview()[0]["selection"], "autozone")

        (self.candidate / "external.txt").write_text("external", encoding="utf-8")
        history = self.service.history(zone_id)
        self.assertEqual(history[0]["filename"], "external.txt")

        self.service.upload(zone_id, b"second", "text/plain")
        overview = self.service.overview()
        dynamic = overview["zones"][0]
        self.assertTrue(dynamic["blocked"])
        self.assertEqual(dynamic["max_items"], 2)
        with self.assertRaises(ServiceError) as context:
            self.service.upload(zone_id, b"third", "text/plain")
        self.assertEqual(context.exception.code, "storage_limit")

        shutil.rmtree(self.candidate)
        self.assertFalse(self.service.has_zone(zone_id))
