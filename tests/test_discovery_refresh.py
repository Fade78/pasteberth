"""Service refresh scheduling, request freshness, and scan coalescing."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from PasteBerth.runtime import service as service_module
from PasteBerth.runtime.config import ZoneCollectionConfig, load_config
from PasteBerth.runtime.service import PasteService, ServiceError
from PasteBerth.runtime.storage import DestinationError
from tests.helpers import write_config


class TestDiscoveryRefresh(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.candidate = self.tmp / "projects" / "repo" / "work" / "exchange"
        self.candidate.mkdir(parents=True)
        config = load_config(write_config(self.tmp, zones=[
            {"id": zid, "directory": str(self.tmp / zid), "retain": 10}
            for zid in ("source", "target")
        ]))
        self.cfg = replace(config, zone_collections=(ZoneCollectionConfig(
            id="@repositories",
            base_directory=self.tmp / "projects",
            pattern=r"^[^/]+/work/exchange$",
            max_depth=4,
            retain=10,
        ),))
        self.now = 100.0
        clock = mock.patch.object(service_module, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        discovery = mock.patch.object(
            service_module, "discover_zone_collections",
            wraps=service_module.discover_zone_collections,
        )
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)
        self.service = PasteService(self.cfg)
        self.addCleanup(self.service.close)

    def wait_for_refresh(self):
        with self.service._zone_collection_refresh_condition:
            self.assertTrue(self.service._zone_collection_refresh_condition.wait_for(
                lambda: not self.service._zone_collection_refresh_in_progress,
                timeout=3,
            ))

    def archive(self, service, zid, filenames):
        with service.archive_files(zid, filenames) as (_destination, items):
            return items

    def test_startup_result_throttles_both_poll_endpoints(self):
        self.discovery.assert_called_once()
        for now in (100.0, 105.0, 109.999):
            self.now = now
            self.service.overview()
            self.service.group_overview()
        self.discovery.assert_called_once()
        self.now = 110.0
        self.service.group_overview()
        self.wait_for_refresh()
        self.assertEqual(self.discovery.call_count, 2)
        self.service.overview()
        self.assertEqual(self.discovery.call_count, 2)

    def test_slow_startup_also_schedules_adaptive_cooldown(self):
        def slow_scan(*args):
            self.now += 13.0
            return [], []

        self.discovery.side_effect = slow_scan
        service = PasteService(self.cfg)
        self.addCleanup(service.close)
        self.assertEqual(self.now, 113.0)
        self.assertEqual(service._zone_collection_refresh_after, 126.0)
        self.discovery.reset_mock()
        self.now = 125.999
        service.group_overview()
        self.discovery.assert_not_called()

    def test_cooldown_includes_install_and_adapts_to_latest_duration(self):
        install = self.service._install_registry

        def slow_scan(*args):
            self.now += 13.0
            return [], []

        def slow_install(*args):
            self.now += 4.0
            return install(*args)

        self.now = 110.0
        self.discovery.side_effect = slow_scan
        with mock.patch.object(self.service, "_install_registry", side_effect=slow_install):
            with self.assertLogs("pasteberth.service", level="DEBUG") as logs:
                self.service.group_overview()
                self.wait_for_refresh()
        self.assertIn("scan=13.000s install=4.000s", logs.output[0])
        self.assertEqual(self.now, 127.0)
        self.assertEqual(self.service._zone_collection_refresh_after, 144.0)
        self.now = 143.999
        self.service.group_overview()
        self.assertEqual(self.discovery.call_count, 2)
        self.discovery.side_effect = None
        self.now = 144.0
        self.service.group_overview()
        self.wait_for_refresh()
        self.assertEqual(self.discovery.call_count, 3)
        self.assertEqual(self.service._zone_collection_refresh_after, 154.0)

    def test_scan_and_install_failures_cool_down_without_rapid_retries(self):
        for method in ("discover_zone_collections", "_install_registry"):
            with self.subTest(method=method):
                self.now = self.service._zone_collection_refresh_after

                def fail(*args):
                    self.now += 13.0
                    raise OSError("network unavailable")

                owner = service_module if method == "discover_zone_collections" else self.service
                with mock.patch.object(owner, method, side_effect=fail) as failure:
                    with self.assertLogs("pasteberth.service", level="ERROR"):
                        self.service.group_overview()
                        self.wait_for_refresh()
                    completed = self.now
                    self.assertEqual(self.service._zone_collection_refresh_after, completed + 13.0)
                    self.now = completed + 12.999
                    for _ in range(10):
                        self.service.group_overview()
                    failure.assert_called_once()
                    self.now = completed + 13.0
                    with self.assertLogs("pasteberth.service", level="ERROR"):
                        self.service.group_overview()
                        self.wait_for_refresh()
                    self.assertEqual(failure.call_count, 2)

    def test_foreground_failure_propagates_but_still_schedules_cooldown(self):
        def fail(*args):
            self.now += 13.0
            raise OSError("network unavailable")

        self.discovery.side_effect = fail
        with self.assertRaisesRegex(ServiceError, "network unavailable") as raised:
            self.service.history("source")
        self.assertEqual(raised.exception.code, "destination_error")
        self.assertFalse(self.service._zone_collection_refresh_in_progress)
        self.assertEqual(self.service._zone_collection_refresh_after, 126.0)
        self.service.group_overview()
        self.assertEqual(self.discovery.call_count, 2)

    def test_explicit_requests_ignore_cooldown_and_discover_changes(self):
        candidate = self.tmp / "projects" / "new" / "work" / "exchange"
        candidate.mkdir(parents=True)
        self.assertTrue(self.service.has_zone("new-work-exchange"))
        shutil.rmtree(candidate)
        self.assertFalse(self.service.has_zone("new-work-exchange"))
        self.assertEqual(self.service.history("source"), [])
        self.assertEqual(self.service.zone_id_for_directory(self.candidate), "repo-work-exchange")
        self.assertEqual(self.discovery.call_count, 5)
        self.assertEqual(self.now, 100.0)

    def test_foreground_completion_postpones_background_refresh(self):
        self.now = 109.0
        self.service.history("source")
        self.assertEqual(self.service._zone_collection_refresh_after, 119.0)
        self.now = 110.0
        self.service.group_overview()
        self.assertEqual(self.discovery.call_count, 2)

    def test_polls_share_one_worker_and_explicit_requests_wait_without_rescanning(self):
        started = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        wait_count = 0
        scan = self.discovery._mock_wraps
        wait = self.service._zone_collection_refresh_condition.wait

        def slow_scan(*args):
            started.set()
            if not release.wait(3):
                raise AssertionError("scan was not released")
            return scan(*args)

        def observed_wait(*args, **kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count == 2:
                waiting.set()
            return wait(*args, **kwargs)

        self.now = 110.0
        self.discovery.reset_mock()
        self.discovery.side_effect = slow_scan
        with mock.patch.object(
            self.service, "_background_zone_collection_refresh",
            wraps=self.service._background_zone_collection_refresh,
        ) as worker:
            with ThreadPoolExecutor(max_workers=8) as pool:
                try:
                    polls = [pool.submit(self.service.group_overview) for _ in range(32)]
                    for poll in polls:
                        poll.result(timeout=3)
                    self.assertTrue(started.wait(3))
                    self.service.overview()
                    with mock.patch.object(
                        self.service._zone_collection_refresh_condition,
                        "wait", side_effect=observed_wait,
                    ):
                        action = pool.submit(self.service.upload, "source", b"content", "text/plain")
                        reader = pool.submit(self.service.history, "target")
                        self.assertTrue(waiting.wait(3))
                        self.assertFalse(action.done())
                        self.assertFalse(reader.done())
                        release.set()
                        self.assertIn("filename", action.result(timeout=3))
                        self.assertEqual(reader.result(timeout=3), [])
                finally:
                    release.set()
                    self.wait_for_refresh()
            worker.assert_called_once()
        self.discovery.assert_called_once()
        self.service.group_overview()
        self.discovery.assert_called_once()

    def test_registry_reads_do_not_wait_for_destination_install_io(self):
        started = threading.Event()
        release = threading.Event()
        destination = self.service._destinations["source"]
        ensure = destination._ensure_dir

        def slow_ensure():
            started.set()
            if not release.wait(3):
                raise AssertionError("install was not released")
            return ensure()

        self.now = 110.0
        with mock.patch.object(destination, "_ensure_dir", side_effect=slow_ensure):
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    self.service.group_overview()
                    self.assertTrue(started.wait(3))
                    self.assertEqual(pool.submit(self.service.active_zone_count).result(timeout=1), 3)
                    pool.submit(self.service.group_overview).result(timeout=1)
                finally:
                    release.set()
                    self.wait_for_refresh()

    def test_device_space_locks_are_created_once_per_device(self):
        with mock.patch.object(
            service_module, "_DeviceSpaceLock", wraps=service_module._DeviceSpaceLock,
        ) as space_lock:
            service = PasteService(self.cfg)
            self.addCleanup(service.close)
            expected = len(service._space_locks)
            self.assertGreater(expected, 0)
            self.assertEqual(space_lock.call_count, expected)
            service.has_zone("source")
            service.has_zone("repo-work-exchange")
            self.assertEqual(space_lock.call_count, expected)

    def test_each_operation_refreshes_only_once(self):
        for zid in ("source", "repo-work-exchange"):
            stage_name = ".pbdrop-" + "a" * 24 + ".tmp"
            stage = self.service._zone_cfg[zid].directory / stage_name
            stage.write_bytes(b"staged")
            operations = [
                lambda: self.service.upload(zid, b"content", "text/plain", "one.txt", True),
                lambda: self.service.preview(zid, "one.txt"),
                lambda: self.service.history(zid),
                lambda: self.service.rename(zid, "one.txt", "two.txt"),
                lambda: self.service.update_comment(zid, "two.txt", "note"),
                lambda: self.archive(self.service, zid, ["two.txt"]),
                lambda: self.service.transfer(zid, "target", ["two.txt"], mode="copy"),
                lambda: self.service.delete_many("target", ["two.txt"]),
                lambda: self.service.delete(zid, "two.txt"),
                lambda: self.service.regularize_staged_upload(zid, stage_name, "stage.txt", "text/plain"),
            ]
            for index, operation in enumerate(operations):
                with self.subTest(zid=zid, operation=index):
                    self.discovery.reset_mock()
                    operation()
                    self.discovery.assert_called_once()

    def test_zone_validation_still_precedes_payload_validation(self):
        static = PasteService(replace(self.cfg, zone_collections=()))
        self.addCleanup(static.close)
        for service, zones in ((static, ("source",)), (self.service, ("source", "repo-work-exchange"))):
            operations = [
                (lambda zid: service.upload(zid, b"", "bad", creation_method="bad"), "invalid_request"),
                (lambda zid: service.upload(zid, b"", "bad"), "empty_upload"),
                (lambda zid: service.delete(zid, "../bad"), "unknown_image"),
                (lambda zid: service.delete_many(zid, []), "invalid_request"),
                (lambda zid: service.rename(zid, "../bad", "../bad"), "invalid_filename"),
                (lambda zid: service.update_comment(zid, "../bad", object()), "unknown_image"),
                (lambda zid: service.preview(zid, "../bad"), "unknown_image"),
                (lambda zid: self.archive(service, zid, []), "invalid_request"),
                (lambda zid: service.regularize_staged_upload(zid, "bad", "bad", "bad"), "destination_error"),
            ]
            for zid in (*zones, "missing"):
                for index, (operation, code) in enumerate(operations):
                    with self.subTest(dynamic=bool(service.cfg.zone_collections), zid=zid, operation=index):
                        self.discovery.reset_mock()
                        with self.assertRaises(ServiceError) as raised:
                            operation(zid)
                        self.assertEqual(raised.exception.code, "unknown_zone" if zid == "missing" else code)
                        self.assertEqual(self.discovery.call_count, int(bool(service.cfg.zone_collections)))

    def test_transfer_validation_precedes_discovery(self):
        for args, kwargs, code in (
            (("missing", "target", ["one.txt"]), {"mode": "bad"}, "invalid_request"),
            (("missing", "target", []), {}, "invalid_request"),
            (("missing", "target", ["../bad"]), {}, "invalid_filename"),
            (("missing", "missing", ["one.txt"]), {}, "invalid_request"),
        ):
            with self.subTest(args=args, kwargs=kwargs):
                self.discovery.reset_mock()
                with self.assertRaises(ServiceError) as raised:
                    self.service.transfer(*args, **kwargs)
                self.assertEqual(raised.exception.code, code)
                self.discovery.assert_not_called()

    def test_unknown_zone_never_reaches_upload_preparation_or_storage(self):
        with mock.patch.object(self.service, "_prepare_upload") as prepare:
            with mock.patch.object(self.service, "zone_operation") as operation:
                with self.assertRaises(ServiceError) as raised:
                    self.service.upload("missing", b"", "bad")
        self.assertEqual(raised.exception.code, "unknown_zone")
        prepare.assert_not_called()
        operation.assert_not_called()

    def test_directory_replacement_after_refresh_is_rejected_by_operation_lock(self):
        prepare = self.service._prepare_upload
        for zid in ("source", "repo-work-exchange"):
            with self.subTest(zid=zid):
                directory = self.service._zone_cfg[zid].directory

                def replace_directory(*args):
                    result = prepare(*args)
                    directory.rename(directory.with_name(directory.name + "-old"))
                    directory.mkdir()
                    return result

                self.discovery.reset_mock()
                with mock.patch.object(self.service, "_prepare_upload", side_effect=replace_directory):
                    with self.assertRaisesRegex((DestinationError, ServiceError), "replaced"):
                        self.service.upload(zid, b"content", "text/plain")
                self.discovery.assert_called_once()
                self.assertEqual(list(directory.iterdir()), [])
                directory.rmdir()
                directory.with_name(directory.name + "-old").rename(directory)
