"""Socket-level tests for the standalone consumer; all outputs live in work/tmp."""

from contextlib import redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote

from contrib import fetch_pasteberth_item as consumer


ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "work" / "tmp"
SCRIPT = ROOT / "contrib" / "fetch_pasteberth_item.py"
PASSWORD = "secret & +=\u00e9"
FILENAME = "r\u00e9sum\u00e9 #1 %.txt"
PREFIX = "/mounted/paste"
DATA = b"original bytes\n" * 10000
DIGEST = hashlib.sha256(DATA).hexdigest()
ETAG = '"sha256-' + DIGEST + '"'
CONTENT_URL = PREFIX + "/api/zones/default/items/" + quote(FILENAME, safe="") + "/content"


class ConsumerFixture(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="item-consumer-", dir=TMP)
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.outdir = self.tmp / "output"
        self.outdir.mkdir()
        self.output = self.outdir / "result.bin"
        self.output.write_bytes(b"previous output")

    def run_consumer(self, server, filename=FILENAME, password=PASSWORD,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE):
        env = {**os.environ, "TMPDIR": str(TMP), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PASTEBERTH_PASSWORD", None)
        if password is not None:
            env["PASTEBERTH_PASSWORD"] = password
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--server", server, "--zone", "default",
             "--filename", filename, str(self.output)],
            stdout=stdout, stderr=stderr, text=True, env=env, cwd=self.tmp, timeout=15,
        )

    def assert_preserved(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(PASSWORD, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.output.read_bytes(), b"previous output")
        self.assertEqual(list(self.outdir.iterdir()), [self.output])


class TestItemConsumer(ConsumerFixture):
    def setUp(self):
        super().setUp()
        self.item = {"filename": FILENAME, "size": len(DATA), "sha256": DIGEST,
                     "etag": ETAG, "content_url": CONTENT_URL, "kind": "doc",
                     "reference": "/private/filesystem/not-an-http-route"}
        self.listing = {"zone": "default", "items": [self.item]}
        self.listing_raw = None
        self.listing_status = 200
        self.login_status = 303
        self.cookie = "pb_session=test-session; HttpOnly; Path=" + PREFIX
        self.download_status = 200
        self.download_headers = {"Content-Length": str(len(DATA)), "ETag": ETAG}
        self.download_body = DATA
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.requests.append(("POST", self.path, dict(self.headers), body))
                self.send_response(fixture.login_status)
                if fixture.cookie is not None:
                    self.send_header("Set-Cookie", fixture.cookie)
                    self.send_header("Set-Cookie", "irrelevant=ignored; Path=/")
                self.send_header("Location", fixture.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                fixture.requests.append(("GET", self.path, dict(self.headers), b""))
                if self.path == PREFIX + "/api/zones/default/items":
                    body = (fixture.listing_raw if fixture.listing_raw is not None
                            else json.dumps(fixture.listing).encode())
                    status = fixture.listing_status
                    headers = {"Content-Length": str(len(body))}
                elif self.path == CONTENT_URL:
                    body, status = fixture.download_body, fixture.download_status
                    headers = fixture.download_headers
                else:
                    body, status, headers = b"", 404, {"Content-Length": "0"}
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Location", fixture.redirect)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        self.server = self.origin + PREFIX
        self.redirect = self.origin + "/must-not-follow"

    def test_login_origin_cookie_unicode_prefix_and_verified_download(self):
        result = self.run_consumer(self.server + "/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {
            "sha256": DIGEST, "size": len(DATA), "listing_identity_verified": True})
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(list(self.outdir.iterdir()), [self.output])
        self.assertEqual([(r[0], r[1]) for r in self.requests], [
            ("POST", PREFIX + "/login"),
            ("GET", PREFIX + "/api/zones/default/items"), ("GET", CONTENT_URL)])
        self.assertEqual(self.requests[0][2]["Origin"], self.origin)
        self.assertEqual(parse_qs(self.requests[0][3].decode()), {"password": [PASSWORD]})
        self.assertNotIn("Cookie", self.requests[0][2])
        self.assertEqual(self.requests[1][2]["Cookie"], "pb_session=test-session")
        self.assertEqual(self.requests[2][2]["If-Match"], ETAG)

    def test_legacy_null_identity_warns_and_only_reports_local_digest(self):
        self.item.update(sha256=None, etag=None)
        self.download_headers.pop("ETag")
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unable to assert listing identity", result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "sha256": DIGEST, "size": len(DATA), "listing_identity_verified": False})
        self.assertNotIn("If-Match", self.requests[-1][2])
        self.assertEqual(self.output.read_bytes(), DATA)

    def test_empty_file_still_requires_and_checks_identity(self):
        digest = hashlib.sha256(b"").hexdigest()
        etag = '"sha256-' + digest + '"'
        self.item.update(size=0, sha256=digest, etag=etag)
        self.download_body = b""
        self.download_headers = {"Content-Length": "0", "ETag": etag}
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), b"")
        self.assertEqual(json.loads(result.stdout), {
            "sha256": digest, "size": 0, "listing_identity_verified": True})

    def test_unrelated_item_metadata_does_not_block_selected_item(self):
        self.listing["items"].insert(0, {
            "filename": "other.txt", "size": -1, "sha256": "invalid",
            "etag": "invalid", "content_url": "https://evil.invalid/file"})
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(self.requests[-1][1], CONTENT_URL)

    def test_closed_stdout_exits_3_after_publication_without_final_flush_override(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, "wb") as closed_pipe:
            result = self.run_consumer(self.server, stdout=closed_pipe)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("published but report failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(list(self.outdir.iterdir()), [self.output])

    def test_closed_combined_stdout_stderr_exits_3_after_publication(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, "wb") as closed_pipe:
            result = self.run_consumer(
                self.server, stdout=closed_pipe, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(list(self.outdir.iterdir()), [self.output])

    def test_closed_stderr_preserves_prepublication_exit_1(self):
        self.download_status = 412
        for password in (None, PASSWORD):
            with self.subTest(password_present=password is not None):
                read_fd, write_fd = os.pipe()
                os.close(read_fd)
                with os.fdopen(write_fd, "wb") as closed_pipe:
                    result = self.run_consumer(self.server, password=password, stderr=closed_pipe)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.output.read_bytes(), b"previous output")
                self.assertEqual(list(self.outdir.iterdir()), [self.output])

    def test_other_stdout_write_and_flush_errors_exit_3_after_publication(self):
        for operation in ("write", "flush"):
            for error in (OSError(PASSWORD), ValueError(PASSWORD)):
                with self.subTest(operation=operation, error=type(error).__name__):
                    self.output.write_bytes(b"previous output")
                    stdout = mock.Mock()
                    getattr(stdout, operation).side_effect = error
                    stdout.fileno.side_effect = OSError("no file descriptor")
                    stderr = io.StringIO()
                    with mock.patch.dict(os.environ, {"PASTEBERTH_PASSWORD": PASSWORD}), \
                            mock.patch.object(sys, "stdout", stdout), redirect_stderr(stderr):
                        code = consumer.main([
                            "--server", self.server, "--zone", "default",
                            "--filename", FILENAME, str(self.output)])
                    self.assertEqual(code, 3)
                    self.assertIn("published but report failed", stderr.getvalue())
                    self.assertNotIn(PASSWORD, stderr.getvalue())
                    self.assertEqual(self.output.read_bytes(), DATA)
                    self.assertEqual(list(self.outdir.iterdir()), [self.output])

    def test_same_length_mutation_preserves_output_and_cleans_staging(self):
        self.download_body = b"x" * len(DATA)
        result = self.run_consumer(self.server)
        self.assert_preserved(result)
        self.assertIn("SHA-256 does not match", result.stderr)

    def test_truncation_preserves_output_and_cleans_staging(self):
        self.download_body = DATA[:70000]
        result = self.run_consumer(self.server)
        self.assert_preserved(result)
        self.assertIn("truncated", result.stderr)

    def test_wrong_missing_or_duplicate_etag_preserves_output(self):
        for etag in ('"wrong"', None, ETAG + ", " + ETAG):
            with self.subTest(etag=etag):
                if etag is None:
                    self.download_headers.pop("ETag", None)
                else:
                    self.download_headers["ETag"] = etag
                self.assert_preserved(self.run_consumer(self.server))

    def test_download_length_and_encoding_required_even_for_legacy(self):
        for legacy in (False, True):
            if legacy:
                self.item.update(sha256=None, etag=None)
            for headers in ({}, {"Content-Length": str(len(DATA) + 1)},
                            {"Content-Length": "1, 1"}, {"Content-Length": "-1"},
                            {"Content-Length": str(len(DATA)), "Transfer-Encoding": "chunked"},
                            {"Content-Length": str(len(DATA)), "Content-Encoding": "gzip"}):
                with self.subTest(legacy=legacy, headers=headers):
                    self.download_headers = {"ETag": ETAG, **headers}
                    self.assert_preserved(self.run_consumer(self.server))

    def test_412_busy_not_found_and_redirects_are_not_success(self):
        for status in (412, 503, 404, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.requests.clear()
                self.download_status = status
                result = self.run_consumer(self.server)
                self.assert_preserved(result)
                self.assertIn(f"HTTP {status}", result.stderr)
                self.assertEqual(len(self.requests), 3)

    def test_listing_failure_is_not_an_empty_listing_or_download(self):
        for status in (401, 404, 409, 503, 302, 307):
            with self.subTest(status=status):
                self.requests.clear()
                self.listing_status = status
                result = self.run_consumer(self.server)
                self.assert_preserved(result)
                self.assertIn(f"Listing failed (HTTP {status})", result.stderr)
                self.assertEqual(len(self.requests), 2)

    def test_bad_listing_shapes_missing_and_duplicate_items(self):
        for listing in (None, [], {}, {"zone": "wrong", "items": [self.item]},
                        {"zone": "default", "items": None},
                        {"zone": "default", "items": []},
                        {"zone": "default", "items": [self.item, self.item]},
                        {"zone": "default", "items": [None]},
                        {"zone": "default", "items": [self.item], "error": "busy"}):
            with self.subTest(listing=listing):
                self.listing = listing
                self.requests.clear()
                self.assert_preserved(self.run_consumer(self.server))
                self.assertEqual(len(self.requests), 2)

    def test_bad_json_and_bounded_listing(self):
        for raw in (b"not json", b"\xff", b" " * (consumer.MAX_LISTING_BYTES + 1)):
            with self.subTest(length=len(raw)):
                self.listing_raw = raw
                self.assert_preserved(self.run_consumer(self.server))

    def test_invalid_metadata_is_rejected_before_download(self):
        cases = (("size", -1), ("size", True), ("size", "5"),
                 ("sha256", "a" * 63), ("sha256", DIGEST.upper()), ("sha256", []),
                 ("etag", 'W/' + ETAG), ("etag", '"sha256-' + "0" * 64 + '"'),
                 ("filename", None), ("content_url", None))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                original = self.item[field]
                self.item[field] = value
                self.requests.clear()
                self.assert_preserved(self.run_consumer(self.server))
                self.assertEqual(len(self.requests), 2)
                self.item[field] = original
        for field in ("size", "sha256", "etag", "content_url"):
            with self.subTest(missing=field):
                value = self.item.pop(field)
                self.assert_preserved(self.run_consumer(self.server))
                self.item[field] = value

    def test_content_url_cannot_escape_origin_or_mount(self):
        for path in (self.origin + CONTENT_URL, "https://evil.invalid/file", "//evil.invalid/a",
                     "/outside/file", PREFIX + "-other/file", PREFIX + "/../secret",
                     PREFIX + "/%2e%2e/secret", PREFIX + "/%252e%252e/secret",
                     PREFIX + "/a%2fb", PREFIX + "/a%5cb", PREFIX + "/a\\b",
                     PREFIX + "/./file", PREFIX + "//file", PREFIX + "/x?y",
                     PREFIX + "/x#y", PREFIX + "/x%0d%0aCookie:evil", PREFIX + "/x%zz",
                     PREFIX + "/api/zones/other/items/" + quote(FILENAME, safe="") + "/content",
                     PREFIX + "/api/zones/default/items/other.txt/content",
                     PREFIX + "/api/zones/default/items/%252e%252e/content"):
            with self.subTest(path=path):
                self.requests.clear()
                self.item["content_url"] = path
                self.assert_preserved(self.run_consumer(self.server))
                self.assertEqual(len(self.requests), 2)

    def test_login_redirects_never_forward_password_or_cookie(self):
        self.redirect = "http://localhost:1/credential-trap"
        for status in (301, 302, 307, 308, 401):
            with self.subTest(status=status):
                self.login_status = status
                self.requests.clear()
                self.assert_preserved(self.run_consumer(self.server))
                self.assertEqual(len(self.requests), 1)
        self.login_status = 303
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_login_requires_nonempty_session_and_honors_secure_cookie(self):
        for cookie in (None, "unrelated=x", "pb_session=", "pb_session=x; Secure",
                       "bad[key]=value"):
            with self.subTest(cookie=cookie):
                self.cookie = cookie
                self.requests.clear()
                self.assert_preserved(self.run_consumer(self.server))
                self.assertEqual(len(self.requests), 1)

    def test_password_required_and_credentials_in_url_rejected_without_leak(self):
        self.assert_preserved(self.run_consumer(self.server, password=None))
        self.assert_preserved(self.run_consumer("http://username:secret@127.0.0.1:1"))
        self.assertEqual(self.requests, [])

    def test_network_and_bad_hostname_failures_are_sanitized(self):
        for url in ("http://[invalid", "http://host:bad", "http://invalid host/",
                    "http://host:99999", "ftp://host/paste"):
            with self.subTest(url=url):
                self.assert_preserved(self.run_consumer(url))
        with mock.patch.object(socket, "getaddrinfo", side_effect=OSError(PASSWORD)):
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"PASTEBERTH_PASSWORD": PASSWORD}), redirect_stderr(stderr):
                code = consumer.main(["--server", "http://invalid.test", "--zone", "default",
                                      "--filename", FILENAME, str(self.output)])
            self.assertEqual(code, 1)
            self.assertNotIn(PASSWORD, stderr.getvalue())

    def test_staging_private_same_directory_and_cleaned_on_replace_failure(self):
        client = consumer.Consumer(self.server)
        client.login(PASSWORD)
        item = client.item("default", FILENAME)

        def fail_replace(source, target):
            self.assertEqual(source.parent, self.outdir)
            self.assertEqual(source.read_bytes(), DATA)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
            self.assertEqual(self.output.read_bytes(), b"previous output")
            raise OSError("replacement failed")

        with mock.patch.object(consumer.os, "replace", side_effect=fail_replace) as replace:
            with self.assertRaises(OSError):
                client.download(item, self.output)
            replace.assert_called_once()
        self.assertEqual(list(self.outdir.iterdir()), [self.output])
        self.assertEqual(self.output.read_bytes(), b"previous output")

    def test_destination_symlink_is_replaced_not_followed(self):
        referent = self.tmp / "referent"
        referent.write_bytes(b"do not touch")
        self.output.unlink()
        try:
            self.output.symlink_to(referent)
        except OSError:
            self.skipTest("symlinks unavailable")
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.output.is_symlink())
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(referent.read_bytes(), b"do not touch")

    def test_failure_without_previous_destination_leaves_no_files(self):
        self.output.unlink()
        self.download_body = b"truncated"
        result = self.run_consumer(self.server)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(list(self.outdir.iterdir()), [])

    def test_ipv6_origin_preserves_brackets_and_port_and_tls_is_verified(self):
        client = consumer.Consumer("https://[::1]:9443/paste")
        self.assertEqual(client.origin, "https://[::1]:9443")
        self.assertEqual((client.host, client.port), ("::1", 9443))
        context = ssl.create_default_context()
        with mock.patch.object(consumer.ssl, "create_default_context", return_value=context), \
                mock.patch.object(consumer.http.client, "HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 303
            response.headers.get_all.return_value = ["pb_session=x; Secure"]
            client.login(PASSWORD)
            self.assertEqual(connection.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(connection.call_args.kwargs["context"].check_hostname)
            self.assertEqual(connection.return_value.request.call_args.kwargs["headers"]["Origin"],
                             "https://[::1]:9443")
            connection.return_value.close.assert_called_once()
            response.close.assert_called_once()


class TestItemConsumerLiveServer(ConsumerFixture):
    def test_literal_percent_names_and_unicode_filename_in_mounted_backend(self):
        from tests.helpers import LiveServer, write_config

        server = LiveServer(write_config(
            self.tmp, auth_enabled=True, password=PASSWORD, url_prefix=PREFIX,
            allow_unauthenticated_local=False, allowed_hosts='["127.0.0.1"]'))
        self.addCleanup(server.stop)
        base = f"http://127.0.0.1:{server.port}" + PREFIX
        filenames = (FILENAME, "100%effort.txt", "literal%2F.txt")
        for filename in filenames:
            server.service.upload("default", DATA, "text/plain", filename, True)
        for filename in filenames:
            with self.subTest(filename=filename):
                self.output.write_bytes(b"previous output")
                result = self.run_consumer(base, filename=filename)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.output.read_bytes(), DATA)
                self.assertEqual(json.loads(result.stdout), {
                    "sha256": DIGEST, "size": len(DATA), "listing_identity_verified": True})
                self.assertEqual(list(self.outdir.iterdir()), [self.output])

    def test_authenticated_mounted_backend_unicode_filename_and_replacement_race(self):
        from tests.helpers import LiveServer, write_config

        server = LiveServer(write_config(
            self.tmp, auth_enabled=True, password=PASSWORD, url_prefix=PREFIX,
            allow_unauthenticated_local=False, allowed_hosts='["127.0.0.1"]'))
        self.addCleanup(server.stop)
        base = f"http://127.0.0.1:{server.port}" + PREFIX
        client = consumer.Consumer(base)
        client.login(PASSWORD)
        with client.request("GET", PREFIX + "/api/zones/default/items") as response:
            self.assertEqual(response.status, 200)
        server.service.upload("default", DATA, "text/plain", FILENAME, True)
        result = self.run_consumer(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(json.loads(result.stdout), {
            "sha256": DIGEST, "size": len(DATA), "listing_identity_verified": True})
        self.assertEqual(list(self.outdir.iterdir()), [self.output])
        item = client.item("default", FILENAME)
        server.service.upload("default", b"x" * len(DATA), "text/plain", FILENAME,
                              True, allow_replace=True)
        with self.assertRaisesRegex(consumer.ConsumerError, "HTTP 412"):
            client.download(item, self.output)
        self.assertEqual(self.output.read_bytes(), DATA)
        self.assertEqual(list(self.outdir.iterdir()), [self.output])


if __name__ == "__main__":
    unittest.main()
