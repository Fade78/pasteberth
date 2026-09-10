"""Version-aware downloads evaluated against real retained acquisitions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection, IncompleteRead
import json
import socket
import threading
from unittest import mock

from PasteBerth.runtime.service import ServiceError
from tests.helpers import request
from tests.test_item_api import ItemAPIFixture


class TestConditionalDownloads(ItemAPIFixture):
    paths = ("/api/zones/default/items/one.txt/content", "/previews/default/one.txt")

    def test_same_size_managed_replacement_and_return_to_original_identity(self):
        a = self.upload(data=b"AAAA")
        b = self.upload(data=b"BBBB", replace_existing=True)
        self.assertEqual(a["size"], b["size"])
        self.assertNotEqual(a["sha256"], b["sha256"])
        for path in self.paths:
            status, headers, body = self.call("GET", path, headers={"If-Match": a["etag"]})
            self.assertEqual(status, 412, body)
            self.assertEqual(json.loads(body)["error"]["code"], "precondition_failed")
            self.assertNotIn(b"BBBB", body)
            self.assertNotIn("etag", headers)
            status, headers, body = self.call("GET", path, headers={"If-Match": b["etag"]})
            self.assertEqual((status, body), (200, b"BBBB"))
            self.assertEqual(headers["etag"], b["etag"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), b["sha256"])
        again = self.upload(data=b"AAAA", replace_existing=True)
        self.assertEqual(again["etag"], a["etag"])
        self.assertEqual(again["sha256"], a["sha256"])
        for path in self.paths:
            self.assertEqual(self.call("GET", path, headers={"If-Match": a["etag"]})[2], b"AAAA")

    def test_comment_only_change_does_not_change_payload_validator(self):
        original = self.upload()
        for route in ("items", "images"):
            status, _, body = self.json_call("PATCH", f"/api/zones/default/{route}/one.txt/comment", {"comment": route + " note"})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["etag"], original["etag"])
        for path in self.paths:
            status, headers, body = self.call("GET", path, headers={"If-Match": original["etag"]})
            self.assertEqual((status, body), (200, b"original content"))
            self.assertEqual(headers["etag"], original["etag"])

    def test_strong_lists_whitespace_weak_tags_and_malformed_grammar(self):
        etag = self.upload()["etag"]
        conditions = (
            (None, 200), (etag, 200), ("*", 200), (" \t*\t ", 200),
            ('"other", ' + etag, 200), (etag + ', "other"', 200),
            ('W/"other",\t' + etag, 200), ('"comma,inside", ' + etag, 200),
            ('"W/notweak", ' + etag, 200), (etag + ", " + etag, 200),
            ("W/" + etag, 412), ('"other", W/' + etag, 412), ('""', 412),
            ('"other"', 412), ("", 400), (" \t", 400), ("sha256-unquoted", 400),
            ('"unterminated', 400), ('w/' + etag, 400), ('W/ ' + etag, 400),
            (etag + ",", 400), ("," + etag, 400), (etag + ",," + etag, 400),
            ("*, " + etag, 400), ("*, *", 400), ('"space inside"', 400),
            ('"tab\tinside"', 400), (etag + " garbage", 400),
        )
        for path in self.paths:
            for method in ("GET", "HEAD"):
                for condition, expected in conditions:
                    with self.subTest(path=path, method=method, condition=condition):
                        headers = {} if condition is None else {"If-Match": condition}
                        status, received, body = self.call(method, path, headers=headers)
                        self.assertEqual(status, expected, body)
                        if method == "HEAD":
                            self.assertEqual(body, b"")
                        if expected == 200:
                            self.assertEqual(received["etag"], etag)
                            self.assertEqual(received["content-length"], "16")
                        elif method == "GET":
                            self.assertEqual(json.loads(body)["error"]["code"], "invalid_request" if expected == 400 else "precondition_failed")

    def test_repeated_header_lines_combine_as_one_list(self):
        etag = self.upload()["etag"]
        for path in self.paths:
            for values, expected in (
                (['"other"', etag], 200), ([etag, '"other"'], 200),
                (["W/" + etag, '"other"'], 412),
                (["*", etag], 400), ([etag, "malformed"], 400), ([etag, ""], 400),
            ):
                with self.subTest(path=path, values=values):
                    conn = HTTPConnection("127.0.0.1", self.server.port, timeout=3)
                    try:
                        conn.putrequest("GET", path)
                        for value in values:
                            conn.putheader("If-Match", value)
                        conn.endheaders()
                        response = conn.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, expected, body)
                    finally:
                        conn.close()

    def test_legacy_digest_unknown_without_hashing_on_get_head_or_listing(self):
        self.upload()
        meta_path = self.destination.directory / "one.txt.json"
        meta = json.loads(meta_path.read_text())
        del meta["sha256"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with mock.patch("PasteBerth.runtime.storage._sha256_file", side_effect=AssertionError("read-time hash")), mock.patch("PasteBerth.runtime.storage.hashlib.sha256", side_effect=AssertionError("read-time hash")):
            for route, key in (("items", "items"), ("images", "images")):
                response = self.call("GET", f"/api/zones/default/{route}")
                item = json.loads(response[2])[key][0]
                self.assertIsNone(item["sha256"])
                self.assertIsNone(item["etag"])
            for path in self.paths:
                for method in ("GET", "HEAD"):
                    for condition, expected in ((None, 200), ("*", 200), ('"specific"', 412), ('W/"specific", "other"', 412)):
                        with self.subTest(path=path, method=method, condition=condition):
                            status, headers, body = self.call(method, path, headers={} if condition is None else {"If-Match": condition})
                            self.assertEqual(status, expected, body)
                            self.assertNotIn("etag", headers)
                            if method == "HEAD":
                                self.assertEqual(body, b"")
                            elif expected == 200:
                                self.assertEqual(body, b"original content")
        self.assertNotIn("sha256", json.loads(meta_path.read_text()))

    def test_head_and_failed_conditions_close_real_handles_without_reading_payload(self):
        etag = self.upload()["etag"]
        acquire = self.destination.acquire_reads
        captured = []

        def capture(names):
            selected = acquire(names)
            for item, handle in selected:
                captured.append(handle)
                handle.read = mock.Mock(side_effect=AssertionError("file bytes read"))
            return selected

        with mock.patch.object(self.destination, "acquire_reads", side_effect=capture):
            for path in self.paths:
                for method, condition, expected in (
                    ("HEAD", etag, 200), ("GET", '"other"', 412),
                    ("HEAD", '"other"', 412), ("GET", "malformed", 400),
                ):
                    for _ in range(3):
                        status, _, body = self.call(method, path, headers={"If-Match": condition})
                        self.assertEqual(status, expected, body)
                        self.assertNotIn(b"original content", body)
                        if method == "HEAD":
                            self.assertEqual(body, b"")
        self.assertTrue(captured)
        self.assertTrue(all(handle.closed for handle in captured))
        self.assertEqual(self.service._operation_state, {})
        with self.destination.operation_lock(exclusive=True, blocking=False):
            pass
        self.assertEqual(self.call("GET", self.paths[0], headers={"If-Match": etag})[0], 200)

    def test_managed_replacement_after_acquisition_returns_original_bytes_and_etag(self):
        original_open = self.service.open_preview
        for path in self.paths:
            for method in ("GET", "HEAD"):
                a = self.upload(data=b"AAAA", replace_existing=True)
                captured = []
                closed = threading.Event()

                @contextmanager
                def replace_after_acquisition(*args, **kwargs):
                    try:
                        with original_open(*args, **kwargs) as selected:
                            captured.append(selected[1])
                            self.service.upload("default", b"BBBB", "text/plain", "one.txt", True, allow_replace=True)
                            self.service.delete("default", "one.txt")
                            yield selected
                    finally:
                        closed.set()

                with mock.patch.object(self.service, "open_preview", replace_after_acquisition):
                    status, headers, body = self.call(method, path, headers={"If-Match": a["etag"]})
                    self.assertTrue(closed.wait(2))
                self.assertEqual(status, 200, body)
                self.assertEqual(headers["etag"], a["etag"])
                self.assertEqual(headers["content-length"], "4")
                self.assertEqual(body, b"AAAA" if method == "GET" else b"")
                self.assertTrue(captured[0].closed)

    def test_external_truncation_after_acquisition_is_incomplete_not_successful(self):
        original_open = self.service.open_preview
        for path in self.paths:
            a = self.upload(data=b"AAAA", replace_existing=True)
            captured = []
            closed = threading.Event()

            @contextmanager
            def truncate_after_acquisition(*args, **kwargs):
                try:
                    with original_open(*args, **kwargs) as selected:
                        captured.append(selected[1])
                        with (self.destination.directory / "one.txt").open("r+b") as writer:
                            writer.truncate(2)
                        yield selected
                finally:
                    closed.set()

            with mock.patch.object(self.service, "open_preview", truncate_after_acquisition), mock.patch("PasteBerth.runtime.webapp.log.exception"):
                conn = HTTPConnection("127.0.0.1", self.server.port, timeout=3)
                try:
                    conn.request("GET", path, headers={"If-Match": a["etag"]})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("ETag"), a["etag"])
                    self.assertEqual(response.getheader("Content-Length"), "4")
                    with self.assertRaises(IncompleteRead) as raised:
                        response.read()
                    self.assertEqual(raised.exception.partial, b"AA")
                    self.assertTrue(closed.wait(2))
                    self.assertTrue(captured[0].closed)
                finally:
                    conn.close()
            # Restore the external damage only in this temporary test zone.
            with (self.destination.directory / "one.txt").open("wb") as writer:
                writer.write(b"AAAA")

    def test_busy_and_admission_signals_precede_conditions(self):
        self.upload()
        for method in ("GET", "HEAD"):
            with self.destination.operation_lock(exclusive=True):
                status, headers, body = self.call(method, self.paths[0], headers={"If-Match": '"wrong"'})
            self.assertEqual(status, 423, body)
            self.assertEqual(headers["retry-after"], "1")
            if method == "HEAD":
                self.assertEqual(body, b"")
            for path in self.paths:
                with mock.patch.object(self.service, "open_preview", side_effect=ServiceError("server_busy", "capacity exhausted")):
                    status, headers, body = self.call(method, path, headers={"If-Match": '"wrong"'})
                self.assertEqual(status, 503, body)
                self.assertEqual(headers["retry-after"], "1")
                if method == "HEAD":
                    self.assertEqual(body, b"")

    def test_download_routes_select_legacy_blocking_and_generic_nonblocking(self):
        item = self.upload()
        for method in ("GET", "HEAD"):
            for path, blocking in zip(self.paths, (False, True)):
                with self.subTest(method=method, path=path), mock.patch.object(
                    self.service, "open_preview", wraps=self.service.open_preview,
                ) as acquire:
                    status, _, body = self.call(method, path, headers={"If-Match": item["etag"]})
                    self.assertEqual(status, 200, body)
                    acquire.assert_called_once_with("default", "one.txt", blocking=blocking)

    def test_legacy_preview_waits_for_writer_then_serves_acquired_version(self):
        item = self.upload()
        operation_lock = self.destination.operation_lock
        for method in ("GET", "HEAD"):
            entered, acquired = threading.Event(), threading.Event()

            @contextmanager
            def observe_lock(*args, **kwargs):
                entered.set()
                with operation_lock(*args, **kwargs):
                    acquired.set()
                    yield

            with ThreadPoolExecutor(max_workers=1) as pool:
                with operation_lock(exclusive=True):
                    with mock.patch.object(self.destination, "operation_lock", observe_lock):
                        download = pool.submit(self.call, method, self.paths[1], headers={"If-Match": item["etag"]})
                        self.assertTrue(entered.wait(2))
                        self.assertFalse(acquired.wait(0.05))
                        self.assertFalse(download.done())
                status, headers, body = download.result(timeout=3)
            self.assertTrue(acquired.is_set())
            self.assertEqual(status, 200, body)
            self.assertEqual(headers["etag"], item["etag"])
            self.assertEqual(body, b"original content" if method == "GET" else b"")

    def test_missing_items_keep_legacy_and_generic_error_codes(self):
        for path, code in zip(self.paths, ("unknown_item", "unknown_image")):
            for condition in ('"specific"', "*"):
                status, _, body = self.call("GET", path, headers={"If-Match": condition})
                self.assertEqual(status, 404, body)
                self.assertEqual(json.loads(body)["error"]["code"], code)

    def test_raw_head_has_no_bytes_for_success_and_all_error_responses(self):
        item = self.upload()
        for path in self.paths:
            for condition, expected in ((item["etag"], 200), ('"other"', 412), ("bad", 400)):
                with socket.create_connection(("127.0.0.1", self.server.port), timeout=3) as client:
                    client.sendall(f"HEAD {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.server.port}\r\nIf-Match: {condition}\r\nConnection: close\r\n\r\n".encode())
                    response = bytearray()
                    while chunk := client.recv(65536):
                        response.extend(chunk)
                headers, separator, body = response.partition(b"\r\n\r\n")
                self.assertTrue(separator)
                self.assertEqual(int(headers.split(b" ", 2)[1]), expected)
                self.assertEqual(body, b"")


class TestAuthenticatedConditionalDownloads(ItemAPIFixture):
    config_options = {"url_prefix": "/paste", "auth_enabled": True, "password": "conditional-test-password"}

    def test_auth_precedes_conditions_and_cookie_login_works_under_mount(self):
        item = self.service.upload("default", b"secret", "text/plain", "caf\u00e9.txt", True)
        path = item["content_url"]
        for method in ("GET", "HEAD"):
            for target in (path, "/paste/api/zones/default/items/missing.txt/content", item["preview_url"]):
                with mock.patch.object(self.service, "open_preview", side_effect=AssertionError("anonymous acquisition")):
                    status, headers, body = request(self.server.port, method, target, headers={"If-Match": "malformed"})
                self.assertEqual(status, 401, body)
                self.assertNotIn("etag", headers)
                if method == "HEAD":
                    self.assertEqual(body, b"")
        for path_suffix in ("/api/zones/default/items", "/api/zones?schema=invalid"):
            self.assertEqual(self.call("GET", path_suffix)[0], 401)
        for query in ("", "?schema=items", "?schema=invalid", "?schema=items&schema=images"):
            with mock.patch.object(self.service, "transfer", side_effect=AssertionError("anonymous transfer")):
                self.assertEqual(self.json_call("POST", "/api/transfers" + query, {})[0], 401)
        status, headers, _ = self.call("POST", "/login", body=b"password=conditional-test-password", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 303)
        self.assertIn("Path=/paste;", headers["set-cookie"])
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = self.call("GET", "/api/zones/default/items", cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["items"][0]["etag"], item["etag"])
        for method in ("GET", "HEAD"):
            status, headers, body = request(self.server.port, method, path, cookie=cookie, headers={"If-Match": item["etag"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(headers["etag"], item["etag"])
            self.assertEqual(body, b"secret" if method == "GET" else b"")
