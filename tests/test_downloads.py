"""Retained download handles, bounded streaming, and lock/permit lifetimes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import io
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from PasteBerth.runtime.config import load_config
from PasteBerth.runtime.service import PasteService, ServiceError
from PasteBerth.runtime.tokens import PERMISSION_ALL
from PasteBerth.runtime.webapp import ClientAbort, make_handler
from tests.helpers import LiveServer, build_multipart, request
from tests.helpers import write_config


class DownloadFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.config_path = write_config(self.tmp)
        self.cfg = load_config(self.config_path)
        self.service = PasteService(self.cfg)
        self.addCleanup(self.service.close)
        self.destination = self.service._destinations["default"]
        self.data = b"original content\n" * 9000
        self.service.upload("default", self.data, "text/plain", "one.txt", True)

    def archive_bytes(self, zid="default"):
        with self.service.archive_files(zid, ["one.txt"], blocking=False) as selected:
            return selected[0][1].read()

    def capture_reads(self, captured, transform=None):
        acquire = self.destination.acquire_reads

        def capture(names):
            selected = acquire(names)
            captured.extend(handle for _item, handle in selected)
            if transform:
                transform(selected)
            return selected

        return mock.patch.object(self.destination, "acquire_reads", side_effect=capture)


class TestDownloadService(DownloadFixture):
    def test_overview_exposes_effective_archive_file_limit(self):
        self.assertEqual(self.service.overview()["max_archive_files"], 64)
        for limit in (2, None):
            cfg = replace(self.cfg, limits=replace(self.cfg.limits, max_archive_files=limit))
            with mock.patch.object(self.service, "cfg", cfg):
                self.assertEqual(self.service.overview()["max_archive_files"], limit)

    def test_acquisition_is_shared_once_without_listing_refresh_or_zone_lock(self):
        for archive in (False, True):
            with self.subTest(archive=archive), ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.service, "_refresh_zone_collections", side_effect=AssertionError("scan")))
                stack.enter_context(mock.patch.object(self.destination, "list", side_effect=AssertionError("list")))
                stack.enter_context(mock.patch.object(self.service, "zone_operation", side_effect=AssertionError("zone lock")))
                lock = stack.enter_context(mock.patch.object(self.destination, "operation_lock", wraps=self.destination.operation_lock))
                context = (
                    self.service.archive_files("default", ["one.txt"], blocking=False)
                    if archive else self.service.open_preview("default", "one.txt", blocking=False)
                )
                with context as result:
                    item, handle = result[0] if archive else result
                    lock.assert_called_once_with(exclusive=False, blocking=False)
                    self.assertEqual(item.size, len(self.data))
                    self.assertFalse(handle.closed)
                    self.assertEqual(self.service._operation_state, {})
                    # An exclusive operation in another thread proves the FS lock is gone.
                    def writer():
                        with self.destination.operation_lock(exclusive=True, blocking=False):
                            pass

                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(writer).result(timeout=2)
                    self.assertEqual(handle.read(), self.data)
                self.assertTrue(handle.closed)

    def test_long_history_does_not_block_download_acquisition(self):
        started, release = threading.Event(), threading.Event()
        listing = self.destination.list

        def blocked_list():
            started.set()
            if not release.wait(5):
                raise AssertionError("history was not released")
            return listing()

        with mock.patch.object(self.destination, "list", side_effect=blocked_list):
            with ThreadPoolExecutor(max_workers=2) as pool:
                history = pool.submit(self.service.history, "default")
                try:
                    self.assertTrue(started.wait(2))
                    self.assertEqual(self.service._operation_state, {"default": "history"})
                    self.assertEqual(pool.submit(self.service.preview, "default", "one.txt", blocking=False).result(timeout=1)[0], self.data)
                    self.assertEqual(pool.submit(self.archive_bytes).result(timeout=1), self.data)
                    self.assertEqual(self.service._operation_state, {"default": "history"})
                finally:
                    release.set()
                history.result(timeout=2)

    def test_existing_exclusive_writer_still_blocks_acquisition_without_permit_leak(self):
        started, release = threading.Event(), threading.Event()

        def writer():
            with self.service.zone_operation("default", kind="delete_batch", exclusive=True):
                started.set()
                if not release.wait(5):
                    raise AssertionError("writer was not released")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            try:
                self.assertTrue(started.wait(2))
                for _ in range(6):
                    for operation in (
                        lambda: self.service.preview("default", "one.txt", blocking=False),
                        self.archive_bytes,
                    ):
                        with self.assertRaises(ServiceError) as raised:
                            operation()
                        self.assertEqual(raised.exception.code, "zone_busy")
            finally:
                release.set()
            future.result(timeout=2)
        self.assertEqual(self.archive_bytes(), self.data)

    def test_archive_retains_original_bytes_after_replace_and_delete(self):
        with self.service.archive_files("default", ["one.txt"]) as selected:
            handle = selected[0][1]
            self.service.upload("default", b"replacement", "text/plain", "one.txt", True, allow_replace=True)
            self.assertEqual(self.service.preview("default", "one.txt")[0], b"replacement")
            self.service.delete("default", "one.txt")
            self.assertEqual(handle.read(), self.data)
        self.assertTrue(handle.closed)

    def test_replaced_zone_directory_is_not_followed_by_snapshot_reads(self):
        directory = self.destination.directory
        directory.rename(directory.with_name(directory.name + "-old"))
        directory.mkdir()
        for download in (
            lambda: self.service.preview("default", "one.txt"),
            self.archive_bytes,
        ):
            with self.assertRaisesRegex(ServiceError, "replaced"):
                download()
        self.assertEqual(list(directory.iterdir()), [])

    def test_archive_global_quota_precedes_acquisition_and_releases_on_exit(self):
        self.service.upload("secondary", b"other", "text/plain", "one.txt", True)
        with ExitStack() as stack:
            for zid in ("default", "secondary", "default", "secondary"):
                stack.enter_context(self.service.archive_files(zid, ["one.txt"]))
            with mock.patch.object(self.destination, "acquire_reads") as acquire:
                with self.assertRaises(ServiceError) as raised:
                    self.archive_bytes()
                self.assertEqual(raised.exception.code, "server_busy")
                self.assertEqual(raised.exception.status, 503)
                acquire.assert_not_called()
            self.assertEqual(self.service.preview("default", "one.txt")[0], self.data)
        self.assertEqual(self.archive_bytes(), self.data)

    def test_file_count_limit_is_413_before_any_open(self):
        self.assertEqual(self.cfg.limits.max_archive_files, 64)
        with mock.patch.object(self.destination, "acquire_reads") as acquire:
            with self.assertRaises(ServiceError) as raised:
                with self.service.archive_files("default", [f"file-{i}.txt" for i in range(65)]):
                    self.fail("accepted oversized selection")
            self.assertEqual(raised.exception.status, 413)
            acquire.assert_not_called()

    def test_unlimited_archive_budgets(self):
        cfg = replace(self.cfg, limits=replace(self.cfg.limits, max_archive_files=None, max_active_archives=None))
        service = PasteService(cfg)
        self.addCleanup(service.close)
        with ExitStack() as stack:
            for _ in range(6):
                stack.enter_context(service.archive_files("default", ["one.txt"]))
        self.assertIsNone(service._archive_slots)

    def test_size_limits_and_consumer_failure_close_handles_and_release_quota(self):
        for archive in (False, True):
            with self.subTest(archive=archive):
                cfg = (
                    replace(self.cfg, limits=replace(self.cfg.limits, max_archive_bytes=1))
                    if archive else replace(self.cfg, max_upload_bytes=1)
                )
                captured = []
                with mock.patch.object(self.service, "cfg", cfg), self.capture_reads(captured):
                    with self.assertRaises(ServiceError) as raised:
                        if archive:
                            self.archive_bytes()
                        else:
                            self.service.preview("default", "one.txt")
                    self.assertEqual(raised.exception.status, 413)
                self.assertTrue(captured)
                self.assertTrue(all(handle.closed for handle in captured))
        for _ in range(6):
            with self.assertRaisesRegex(OSError, "consumer"):
                with self.service.archive_files("default", ["one.txt"]) as selected:
                    raise OSError("consumer")
            self.assertTrue(selected[0][1].closed)
        self.assertEqual(self.archive_bytes(), self.data)

    def test_missing_and_invalid_selection_have_distinct_errors_without_quota_leaks(self):
        for name, status, code in (
            ("missing.txt", 404, "unknown_image"),
            ("../one.txt", 400, "invalid_filename"),
        ):
            for _ in range(6):
                with self.assertRaises(ServiceError) as raised:
                    with self.service.archive_files("default", ["one.txt", name]):
                        self.fail("accepted unknown file")
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(raised.exception.code, code)
                with self.assertRaises(ServiceError) as raised:
                    self.service.preview("default", name)
                self.assertEqual(raised.exception.status, 404)
        self.assertEqual(self.archive_bytes(), self.data)

    def test_preview_wrapper_translates_read_and_close_errors(self):
        for operation in ("read", "close"):
            with self.subTest(operation=operation):
                failure = OSError(f"{operation} failed")
                captured = []

                def fault(selected):
                    handle = selected[0][1]
                    if operation == "read":
                        handle.read = mock.Mock(side_effect=failure)
                    else:
                        close = handle.close

                        def failing_close():
                            close()
                            raise failure

                        handle.close = failing_close

                with self.capture_reads(captured, fault):
                    with self.assertRaises(ServiceError) as raised:
                        self.service.preview("default", "one.txt")
                self.assertEqual(raised.exception.code, "destination_error")
                self.assertIs(raised.exception.__cause__, failure)
                self.assertTrue(captured[0].closed)

    def test_open_preview_does_not_translate_consumer_exceptions(self):
        failure = OSError("consumer failure")
        with self.assertRaises(OSError) as raised:
            with self.service.open_preview("default", "one.txt") as (_item, handle):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertTrue(handle.closed)

    def test_close_failure_still_closes_other_handles_and_releases_permit(self):
        self.service.upload("default", b"second", "text/plain", "two.txt", True)
        with self.assertRaisesRegex(OSError, "close failed"):
            with self.service.archive_files("default", ["one.txt", "two.txt"]) as selected:
                handle = selected[-1][1]
                close = handle.close

                def failing_close():
                    close()
                    raise OSError("close failed")

                handle.close = failing_close
        self.assertTrue(all(handle.closed for _item, handle in selected))
        with ExitStack() as stack:
            for _ in range(4):
                stack.enter_context(self.service.archive_files("default", ["one.txt"]))


class TestDownloadHandler(DownloadFixture):
    def handler(self, archive=False):
        cls = make_handler(self.cfg, self.service, mock.Mock(), mock.Mock())
        handler = object.__new__(cls)
        handler.command = "POST" if archive else "GET"
        handler.path = "/api/zones/default/images/archive" if archive else "/previews/default/one.txt"
        handler.close_connection = False
        handler._response_started = False
        handler._streaming_response = False
        handler._request_expired = False
        handler._request_timer_lock = threading.Lock()
        handler._request_token = object()
        handler._request_timer = None
        handler.connection = mock.Mock()
        handler.connection.fileno.return_value = 10
        handler._zone_permissions = lambda _zid, _required: (PERMISSION_ALL, True)
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._security_headers = lambda: []
        handler._require_auth_api = lambda: True
        handler._read_filename_request = lambda: ["one.txt"]
        handler._error = mock.Mock()
        handler._dispatch = (
            lambda: handler._h_zone_archive("default")
            if archive else handler._h_preview("default", "one.txt")
        )
        return handler

    def test_stale_reschedule_cannot_replace_timer_or_expire_next_request(self):
        handler = self.handler()
        handler.rfile = mock.Mock()
        first_entered, first_done = threading.Event(), threading.Event()
        second_entered, second_done = threading.Event(), threading.Event()
        callback_paused, callback_resume = threading.Event(), threading.Event()
        timers = []
        callback_thread = None

        def timer_factory(interval, function, args=(), kwargs=None):
            timer = mock.Mock()
            timer.fire = lambda: function(*args, **(kwargs or {}))
            timers.append(timer)
            if threading.get_ident() == callback_thread:
                callback_paused.set()
                if not callback_resume.wait(3):
                    raise AssertionError("callback was not resumed")
            return timer

        def serve_request():
            if not first_entered.is_set():
                handler._streaming_response = True
                handler._stream_last_activity = time.monotonic()
                first_entered.set()
                self.assertTrue(first_done.wait(3))
            else:
                second_entered.set()
                self.assertTrue(second_done.wait(3))

        def serve_connection():
            handler.handle_one_request()
            handler.handle_one_request()

        def callback():
            nonlocal callback_thread
            callback_thread = threading.get_ident()
            timers[0].fire()

        with mock.patch("PasteBerth.runtime.webapp.threading.Timer", side_effect=timer_factory), mock.patch.object(BaseHTTPRequestHandler, "handle_one_request", side_effect=serve_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                connection = pool.submit(serve_connection)
                try:
                    self.assertTrue(first_entered.wait(2))
                    firing = pool.submit(callback)
                    self.assertTrue(callback_paused.wait(2))
                    first_done.set()
                    self.assertTrue(second_entered.wait(2))
                    second_timer = handler._request_timer
                    timers[0].cancel.assert_called_once()
                    callback_resume.set()
                    firing.result(timeout=2)
                    # Even a cancelled callback already queued by Timer must be harmless.
                    timers[1].fire()
                    handler.connection.shutdown.assert_not_called()
                    self.assertIs(handler._request_timer, second_timer)
                    timers[1].start.assert_not_called()
                finally:
                    first_done.set()
                    second_done.set()
                    callback_resume.set()
                connection.result(timeout=2)
        second_timer.cancel.assert_called_once()

    def test_timer_shutdown_and_request_cleanup_are_serialized(self):
        handler = self.handler()
        handler.rfile = mock.Mock()
        entered, finish = threading.Event(), threading.Event()
        shutdown_entered, shutdown_resume = threading.Event(), threading.Event()
        cleanup_waiting, next_entered = threading.Event(), threading.Event()
        request_thread = None
        timers = []
        lock = threading.Lock()

        class ObservedLock:
            def __enter__(self):
                if threading.get_ident() == request_thread and shutdown_entered.is_set():
                    cleanup_waiting.set()
                lock.acquire()

            def __exit__(self, *args):
                lock.release()

        handler._request_timer_lock = ObservedLock()

        def timer_factory(interval, function, args=(), kwargs=None):
            timer = mock.Mock()
            timer.fire = lambda: function(*args, **(kwargs or {}))
            timers.append(timer)
            return timer

        def shutdown(_how):
            shutdown_entered.set()
            self.assertTrue(shutdown_resume.wait(3))

        def serve_request():
            if not entered.is_set():
                entered.set()
                self.assertTrue(finish.wait(3))
            else:
                next_entered.set()

        def serve_connection():
            nonlocal request_thread
            request_thread = threading.get_ident()
            handler.handle_one_request()
            handler.handle_one_request()

        handler.connection.shutdown.side_effect = shutdown
        with mock.patch("PasteBerth.runtime.webapp.threading.Timer", side_effect=timer_factory), mock.patch.object(BaseHTTPRequestHandler, "handle_one_request", side_effect=serve_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                connection = pool.submit(serve_connection)
                try:
                    self.assertTrue(entered.wait(2))
                    firing = pool.submit(timers[0].fire)
                    self.assertTrue(shutdown_entered.wait(2))
                    finish.set()
                    self.assertTrue(cleanup_waiting.wait(1))
                    self.assertFalse(next_entered.is_set())
                finally:
                    finish.set()
                    shutdown_resume.set()
                firing.result(timeout=2)
                connection.result(timeout=2)
        self.assertTrue(next_entered.is_set())

    def test_idle_timer_can_fire_as_streaming_starts(self):
        handler = self.handler()
        handler.rfile = mock.Mock()
        timers = []

        def timer_factory(interval, function, args=(), kwargs=None):
            timer = mock.Mock()
            timer.fire = lambda: function(*args, **(kwargs or {}))
            timers.append(timer)
            return timer

        def serve_request():
            handler._streaming_response = True
            # Callback may run before the first streaming activity update.
            timers[0].fire()

        with mock.patch("PasteBerth.runtime.webapp.threading.Timer", side_effect=timer_factory), mock.patch.object(BaseHTTPRequestHandler, "handle_one_request", side_effect=serve_request):
            handler.handle_one_request()
        self.assertEqual(len(timers), 2)
        timers[1].cancel.assert_called_once()
        handler.connection.shutdown.assert_not_called()

    def test_download_start_is_atomic_with_near_deadline_callback(self):
        for archive in (False, True):
            with self.subTest(archive=archive):
                handler = self.handler(archive)
                handler.timeout = 60.0
                handler._request_deadline = 60.0
                handler._stream_last_activity = 0.0
                now = 59.999
                starting, resume = threading.Event(), threading.Event()
                callback_waiting, callback_done = threading.Event(), threading.Event()
                lock = threading.Lock()
                callback_thread = None
                start_holds_lock = []
                captured = []

                class ObservedLock:
                    def __enter__(self):
                        if threading.get_ident() == callback_thread:
                            callback_waiting.set()
                        lock.acquire()

                    def __exit__(self, *args):
                        lock.release()

                handler._request_timer_lock = ObservedLock()

                def observe_transition(instance, name, value):
                    object.__setattr__(instance, name, value)
                    if instance is handler and name == "_streaming_response" and value:
                        start_holds_lock.append(lock.locked())
                        starting.set()
                        self.assertTrue(resume.wait(3))

                def expire():
                    nonlocal callback_thread
                    callback_thread = threading.get_ident()
                    try:
                        handler._expire_request(handler._request_token)
                    finally:
                        callback_done.set()

                def headers(_status):
                    self.assertTrue(callback_done.wait(3))
                    self.assertFalse(lock.locked(), "timer lock held over headers")

                handler.send_response.side_effect = headers
                with self.capture_reads(captured), mock.patch.object(type(handler), "__setattr__", observe_transition), mock.patch("PasteBerth.runtime.webapp.time.monotonic", side_effect=lambda: now), mock.patch("PasteBerth.runtime.webapp.threading.Timer"):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        download = pool.submit(handler.do_GET)
                        try:
                            self.assertTrue(starting.wait(2))
                            now = 60.0
                            callback = pool.submit(expire)
                            self.assertTrue(callback_waiting.wait(2))
                            if not start_holds_lock[0]:
                                # Reproduce the old interleaving before allowing its timestamp update.
                                self.assertTrue(callback_done.wait(2))
                            resume.set()
                            callback.result(timeout=2)
                            download.result(timeout=2)
                        finally:
                            resume.set()
                self.assertFalse(handler._request_expired)
                self.assertEqual(start_holds_lock, [True])
                handler.connection.shutdown.assert_not_called()
                handler.send_response.assert_called_once_with(200)
                handler._error.assert_not_called()
                self.assertTrue(captured and all(handle.closed for handle in captured))

    def test_expiry_winning_download_start_prevents_headers(self):
        for archive in (False, True):
            with self.subTest(archive=archive):
                handler = self.handler(archive)
                handler.timeout = 60.0
                handler._request_deadline = 60.0
                handler._stream_last_activity = 0.0
                shutdown_started, shutdown_resume = threading.Event(), threading.Event()
                acquired = threading.Event()
                captured = []

                def shutdown(_how):
                    shutdown_started.set()
                    self.assertTrue(shutdown_resume.wait(3))

                handler.connection.shutdown.side_effect = shutdown
                with self.capture_reads(captured, lambda selected: acquired.set()), mock.patch("PasteBerth.runtime.webapp.time.monotonic", return_value=60.0):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        callback = pool.submit(handler._expire_request, handler._request_token)
                        try:
                            self.assertTrue(shutdown_started.wait(2))
                            download = pool.submit(handler.do_GET)
                            self.assertTrue(acquired.wait(2))
                            shutdown_resume.set()
                            callback.result(timeout=2)
                            download.result(timeout=2)
                        finally:
                            shutdown_resume.set()
                self.assertTrue(handler._request_expired)
                self.assertFalse(handler._response_started)
                handler.send_response.assert_not_called()
                handler._error.assert_not_called()
                self.assertTrue(captured and all(handle.closed for handle in captured))

    def test_no_network_writes_or_flushes_after_source_fault_or_cancellation(self):
        for archive in (False, True):
            for failure in ("eof", "read_error", "cancelled_read", "closed_read", "archive_deadline"):
                if failure == "archive_deadline" and not archive:
                    continue
                with self.subTest(archive=archive, failure=failure):
                    handler = self.handler(archive)
                    handler.wfile = mock.Mock(wraps=io.BytesIO())
                    captured, counts_at_fault = [], []

                    def fault(selected):
                        def read(_size):
                            counts_at_fault.append((handler.wfile.write.call_count, handler.wfile.flush.call_count))
                            if failure == "eof":
                                return b""
                            if failure == "read_error":
                                raise OSError("source failed")
                            if failure == "cancelled_read":
                                handler._request_expired = True
                            elif failure == "closed_read":
                                handler.connection.fileno.return_value = -1
                            else:
                                handler._archive_deadline = 0
                            return b"late data"

                        selected[0][1].read = read

                    with self.capture_reads(captured, fault), mock.patch("PasteBerth.runtime.webapp.threading.Timer"), mock.patch("PasteBerth.runtime.webapp.log.exception"), mock.patch.object(zipfile._ZipWriteFile, "write", side_effect=AssertionError("compressed after source fault")) as compress:
                        handler.do_GET()
                    compress.assert_not_called()
                    self.assertEqual(len(counts_at_fault), 1)
                    self.assertEqual(
                        (handler.wfile.write.call_count, handler.wfile.flush.call_count),
                        counts_at_fault[0],
                    )
                    self.assertTrue(handler.close_connection)
                    self.assertTrue(captured[0].closed)
                    handler._error.assert_not_called()

    def test_streams_selected_size_in_bounded_reads_even_if_source_grows(self):
        for archive in (False, True):
            handler = self.handler(archive)
            captured, reads = [], []

            def growing_source(selected):
                stream = io.BytesIO(self.data + b"must not escape selected size")

                def read(size):
                    reads.append(size)
                    return stream.read(size)

                selected[0][1].read = read

            with self.subTest(archive=archive), self.capture_reads(captured, growing_source):
                handler.do_GET()
            handler._error.assert_not_called()
            self.assertTrue(all(0 < size <= 64 * 1024 for size in reads))
            self.assertGreater(len(reads), 1)
            self.assertEqual(sum(reads), len(self.data))
            output = handler.wfile.getvalue()
            if archive:
                chunks = io.BytesIO(output)
                body = bytearray()
                while size := int(chunks.readline(), 16):
                    body.extend(chunks.read(size))
                    self.assertEqual(chunks.read(2), b"\r\n")
                with zipfile.ZipFile(io.BytesIO(body)) as zipped:
                    self.assertEqual(zipped.read("one.txt"), self.data)
            else:
                self.assertEqual(output, self.data)
            self.assertTrue(all(handle.closed for handle in captured))

    def test_head_acquires_and_closes_without_reading_body(self):
        handler = self.handler()
        handler.command = "HEAD"
        captured = []

        def forbid_read(selected):
            selected[0][1].read = mock.Mock(side_effect=AssertionError("HEAD read body"))

        with self.capture_reads(captured, forbid_read):
            handler.do_GET()
        handler.send_header.assert_any_call("Content-Length", str(len(self.data)))
        handler._error.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b"")
        self.assertTrue(captured[0].closed)

    def test_failures_after_response_start_close_handles_without_second_json(self):
        for archive in (False, True):
            for failure in ("headers", "disconnect", "eof", "read_error", "service_error", "client_abort", "request_timeout"):
                with self.subTest(archive=archive, failure=failure):
                    handler = self.handler(archive)
                    captured = []

                    def fault(selected):
                        if failure == "eof":
                            selected[0][1].read = mock.Mock(return_value=b"")
                        elif failure == "read_error":
                            selected[0][1].read = mock.Mock(side_effect=OSError("read failed"))
                        elif failure == "service_error":
                            selected[0][1].read = mock.Mock(side_effect=ServiceError("destination_error", "read failed"))
                        elif failure == "client_abort":
                            selected[0][1].read = mock.Mock(side_effect=ClientAbort())
                        elif failure == "request_timeout":
                            read = selected[0][1].read

                            def expire(size):
                                handler._stream_last_activity = 0
                                handler._expire_request(handler._request_token)
                                return read(size)

                            selected[0][1].read = expire

                    if failure == "headers":
                        handler.end_headers.side_effect = OSError("headers failed")
                    elif failure == "disconnect":
                        handler.wfile = mock.Mock()
                        handler.wfile.write.side_effect = BrokenPipeError("client gone")
                    with self.capture_reads(captured, fault), mock.patch("PasteBerth.runtime.webapp.threading.Timer") as timer, mock.patch("PasteBerth.runtime.webapp.log.exception"):
                        handler.do_GET()
                    handler.send_response.assert_called_once_with(200)
                    handler._error.assert_not_called()
                    self.assertTrue(handler.close_connection)
                    self.assertFalse(handler._streaming_response)
                    self.assertTrue(captured and all(handle.closed for handle in captured))
                    if archive:
                        timer.return_value.cancel.assert_called_once()
                        self.assertIsNone(handler._archive_deadline)
                        with ExitStack() as stack:
                            for _ in range(4):
                                stack.enter_context(self.service.archive_files("default", ["one.txt"]))

    def test_archive_deadline_failure_cancels_timer_and_releases_resources(self):
        handler = self.handler(True)
        captured = []

        def timeout_on_read(selected):
            read = selected[0][1].read

            def expire(size):
                handler._expire_archive(handler._request_token)
                return read(size)

            selected[0][1].read = expire

        with self.capture_reads(captured, timeout_on_read), mock.patch("PasteBerth.runtime.webapp.threading.Timer") as timer, mock.patch("PasteBerth.runtime.webapp.log.exception"):
            handler.do_GET()
        self.assertTrue(handler.close_connection)
        handler._error.assert_not_called()
        timer.return_value.cancel.assert_called_once()
        self.assertIsNone(handler._archive_deadline)
        self.assertTrue(captured[0].closed)
        self.assertEqual(self.archive_bytes(), self.data)

    def test_late_acquisition_never_starts_headers_on_expired_or_closed_request(self):
        for archive in (False, True):
            for reason in ("timer", "deadline", "closed"):
                handler = self.handler(archive)
                captured = []

                def expire(_selected):
                    if reason == "timer":
                        handler._expire_request(handler._request_token)
                    elif reason == "deadline":
                        handler._request_deadline = time.monotonic() - 1
                    else:
                        handler.connection.fileno.return_value = -1

                with self.subTest(archive=archive, reason=reason), self.capture_reads(captured, expire):
                    handler.do_GET()
                handler.send_response.assert_not_called()
                handler._error.assert_not_called()
                self.assertTrue(handler.close_connection)
                self.assertTrue(captured[0].closed)
                if archive:
                    self.assertEqual(self.archive_bytes(), self.data)

    def test_late_acquisition_error_does_not_send_headers(self):
        for archive in (False, True):
            handler = self.handler(archive)

            def fail(_names):
                handler._request_deadline = time.monotonic() - 1
                raise ServiceError("unknown_image", "gone")

            with mock.patch.object(self.destination, "acquire_reads", side_effect=fail):
                handler.do_GET()
            handler.send_response.assert_not_called()
            handler._error.assert_not_called()
            self.assertTrue(handler.close_connection)

    def test_archive_capacity_http_error_is_503_with_retry_after(self):
        handler = self.handler(True)
        with ExitStack() as stack:
            for _ in range(4):
                stack.enter_context(self.service.archive_files("default", ["one.txt"]))
            handler.do_GET()
        self.assertEqual(handler._error.call_args.args[:2], (503, "server_busy"))
        self.assertEqual(handler._error.call_args.kwargs["extra_headers"], [("Retry-After", "1")])


class TestDownloadHTTP(unittest.TestCase):
    def test_raw_head_has_no_body_for_success_auth_failure_or_missing_resource(self):
        for auth in (False, True):
            with self.subTest(auth=auth), tempfile.TemporaryDirectory() as temporary:
                server = LiveServer(write_config(Path(temporary), auth_enabled=auth, password="test-head-password" if auth else None))
                try:
                    server.service.upload("default", b"hello", "text/plain", "one.txt", True)
                    for path, status in (
                        ("/previews/default/one.txt", 401 if auth else 200),
                        ("/previews/default/missing.txt", 401 if auth else 404),
                        ("/missing-route", 404),
                    ):
                        with self.subTest(path=path), socket.create_connection(("127.0.0.1", server.port), timeout=3) as client:
                            client.sendall(f"HEAD {path} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nConnection: close\r\n\r\n".encode("ascii"))
                            response = bytearray()
                            while chunk := client.recv(64 * 1024):
                                response.extend(chunk)
                        headers, separator, body = bytes(response).partition(b"\r\n\r\n")
                        self.assertEqual(separator, b"\r\n\r\n")
                        self.assertEqual(int(headers.split(b" ", 2)[1]), status)
                        self.assertEqual(body, b"")
                finally:
                    server.stop()

    def test_slow_send_allows_same_zone_mutations_and_second_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = LiveServer(write_config(Path(temporary)))
            try:
                cls = server.httpd.RequestHandlerClass
                end_headers = cls.end_headers
                for archive in (False, True):
                    with self.subTest(archive=archive):
                        original = b"original bytes\n" * 9000
                        server.service.upload("default", original, "text/plain", "one.txt", True)
                        started, release = threading.Event(), threading.Event()

                        class SlowWriter:
                            def __init__(self, output):
                                self.output = output

                            def write(self, data):
                                started.set()
                                if not release.wait(8):
                                    raise AssertionError("download was not released")
                                return self.output.write(data)

                            def __getattr__(self, name):
                                return getattr(self.output, name)

                        def slow_headers(handler):
                            end_headers(handler)
                            if handler.headers.get("X-Test-Slow"):
                                handler.wfile = SlowWriter(handler.wfile)

                        def download():
                            if archive:
                                return request(server.port, "POST", "/api/zones/default/images/archive", body=b'{"filenames":["one.txt"]}', headers={"Content-Type": "application/json", "X-Test-Slow": "1"})
                            return request(server.port, "GET", "/previews/default/one.txt", headers={"X-Test-Slow": "1"})

                        with mock.patch.object(cls, "end_headers", slow_headers), ThreadPoolExecutor(max_workers=2) as pool:
                            downloading = pool.submit(download)
                            try:
                                self.assertTrue(started.wait(3))
                                self.assertEqual(server.service._operation_state, {})
                                body, ctype = build_multipart(filename="one.txt", data=b"replacement", content_type="text/plain", extra_fields={"preserve_name": "1", "replace": "1"})
                                response = pool.submit(request, server.port, "POST", "/api/zones/default/images", body=body, headers={"Content-Type": ctype}).result(timeout=2)
                                self.assertEqual(response[0], 201, response)
                                response = pool.submit(request, server.port, "PATCH", "/api/zones/default/images/one.txt/comment", body=b'{"comment":"note"}', headers={"Content-Type": "application/json"}).result(timeout=2)
                                self.assertEqual(response[0], 200, response)
                                response = pool.submit(request, server.port, "GET", "/previews/default/one.txt").result(timeout=2)
                                self.assertEqual(response[2], b"replacement")
                                response = pool.submit(request, server.port, "POST", "/api/zones/default/images/archive", body=b'{"filenames":["one.txt"]}', headers={"Content-Type": "application/json"}).result(timeout=2)
                                self.assertEqual(response[0], 200, response)
                                with zipfile.ZipFile(io.BytesIO(response[2])) as zipped:
                                    self.assertEqual(zipped.read("one.txt"), b"replacement")
                                response = pool.submit(request, server.port, "DELETE", "/api/zones/default/images/one.txt").result(timeout=2)
                                self.assertEqual(response[0], 200, response)
                            finally:
                                release.set()
                            status, headers, content = downloading.result(timeout=3)
                        self.assertEqual(status, 200)
                        self.assertEqual(headers["cache-control"], "no-store")
                        if archive:
                            with zipfile.ZipFile(io.BytesIO(content)) as zipped:
                                self.assertEqual(zipped.read("one.txt"), original)
                        else:
                            self.assertEqual(content, original)
            finally:
                server.stop()

    def test_head_preview_over_real_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = LiveServer(write_config(Path(temporary)))
            try:
                server.service.upload("default", b"hello", "text/plain", "one.txt", True)
                status, headers, body = request(server.port, "HEAD", "/previews/default/one.txt")
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-length"], "5")
                self.assertIn("attachment", headers["content-disposition"])
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(body, b"")
            finally:
                server.stop()
