"""Bundled HTTP client uses the canonical item API without legacy fallback."""
import hashlib
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from PasteBerth.runtime.client import ClientError, ClientResponse, PasteberthClient, api_error
from tests.helpers import LiveServer, make_png, request, write_config


class TestItemTransport(unittest.TestCase):
    def test_bearer_transport_sets_header_and_rejects_cookie(self):
        client = PasteberthClient("https://example.test/paste", bearer_token="pb_secret")
        with mock.patch("PasteBerth.runtime.client.http.client.HTTPSConnection") as connect:
            connection = connect.return_value
            raw_response = connection.getresponse.return_value
            raw_response.status = 200
            raw_response.read.return_value = b'{"ok":true}'
            raw_response.getheaders.return_value = []
            self.assertEqual(client.request("GET", "/api/health").json(), {"ok": True})
            self.assertEqual(
                connection.request.call_args.kwargs["headers"]["Authorization"],
                "Bearer pb_secret",
            )
        with self.assertRaisesRegex(ClientError, "cannot be combined"):
            client.request("GET", "/api/health", cookie="pb_session=test")
        with self.assertRaisesRegex(ClientError, "header-safe"):
            PasteberthClient("https://example.test", bearer_token="pb_\u2603")
        with self.assertRaises(ClientError) as invalid_url:
            PasteberthClient("https://user:secret@example.test:not-a-port")
        self.assertNotIn("secret", str(invalid_url.exception))

    def test_upload_uses_file_field_and_encoded_item_route(self):
        client = PasteberthClient("https://example.test/paste")
        response = ClientResponse(404, {}, b'{"error":{"code":"unknown_item","message":"missing"}}')
        with mock.patch.object(client, "request", return_value=response) as send:
            self.assertIs(client.upload(
                "zone / one", b"document", "report.pdf", "application/pdf",
                replace=True, cookie="pb_session=test",
            ), response)
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("POST", "/api/zones/zone%20%2F%20one/items"))
        options = send.call_args.kwargs
        self.assertEqual(options["cookie"], "pb_session=test")
        self.assertIn(b'name="file"; filename="report.pdf"', options["body"])
        self.assertNotIn(b'name="image"', options["body"])
        self.assertIn(b'name="preserve_name"\r\n\r\n1', options["body"])
        self.assertIn(b'name="replace"\r\n\r\n1', options["body"])
        self.assertIn(b'name="creation_method"\r\n\r\nfilesystem_drop', options["body"])
        self.assertTrue(options["content_type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(api_error(response).code, "unknown_item")

    def test_regularize_uses_item_route_and_preserves_payload(self):
        client = PasteberthClient("http://localhost/paste")
        with mock.patch.object(client, "request") as send:
            client.regularize(
                "zone / one", ".pbdrop-stage.tmp", "notes.txt", "text/plain",
                replace=True, cookie="pb_session=test",
            )
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("POST", "/api/zones/zone%20%2F%20one/items/regularize"))
        self.assertEqual(json.loads(send.call_args.kwargs["body"]), {
            "stage": ".pbdrop-stage.tmp", "filename": "notes.txt",
            "mime": "text/plain", "replace": True,
        })
        self.assertEqual(send.call_args.kwargs["cookie"], "pb_session=test")

    def test_mounted_request_preserves_cookie_and_verified_tls_default(self):
        client = PasteberthClient("https://example.test:8443/paste/")
        with mock.patch("PasteBerth.runtime.client.http.client.HTTPSConnection") as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"items":[]}'
            response.getheaders.return_value = [("Content-Type", "application/json")]
            result = client.request("GET", "/api/zones/default/items", cookie="pb_session=test")
        connect.assert_called_once_with("example.test", 8443, timeout=60.0, context=None)
        self.assertEqual(connection.request.call_args.args, ("GET", "/paste/api/zones/default/items"))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Cookie"], "pb_session=test")
        connection.close.assert_called_once()
        self.assertEqual(result.json(), {"items": []})


class TestAuthenticatedItemClient(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pasteberth-client-items-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = write_config(
            self.root, auth_enabled=True, password="client test password",
            allowed_hosts='["127.0.0.1"]', url_prefix="/paste",
        )
        self.server = LiveServer(config)
        self.addCleanup(self.server.stop)
        self.client = PasteberthClient(f"http://127.0.0.1:{self.server.port}/paste")

    def test_login_upload_list_and_download_generic_content(self):
        self.assertEqual(self.client.request("GET", "/api/zones/default/items").status, 401)
        cookie = self.client.login("client test password")
        fixtures = [
            ("r\u00e9sum\u00e9.pdf", b"%PDF-1.7\n\x00\xff", "application/pdf", "binary"),
            ("notes.txt", b"plain text\n", "text/plain", "text"),
            ("pixel.png", make_png(), "image/png", "image"),
        ]
        for filename, data, mime, kind in fixtures:
            with self.subTest(filename=filename):
                response = self.client.upload("default", data, filename, mime, cookie=cookie)
                self.assertEqual(response.status, 201, response.body)
                item = response.json()
                self.assertEqual(item["kind"], kind)
                self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
                content_path = f"/api/zones/default/items/{urllib.parse.quote(filename, safe='')}/content"
                self.assertEqual(item["content_url"], "/paste" + content_path)
                self.assertEqual(self.client.request("GET", content_path).status, 401)
                content = self.client.request("GET", content_path, cookie=cookie)
                self.assertEqual(content.status, 200, content.body)
                self.assertEqual(content.body, data)
                self.assertEqual(content.headers["content-length"], str(len(data)))
                self.assertEqual(content.headers["etag"], item["etag"])
                head = self.client.request("HEAD", content_path, cookie=cookie)
                self.assertEqual(head.status, 200)
                self.assertEqual(head.body, b"")
                self.assertEqual(head.headers["etag"], item["etag"])
                self.assertEqual(head.headers["content-length"], str(len(data)))

        listing = self.client.request("GET", "/api/zones/default/items", cookie=cookie)
        self.assertEqual(listing.status, 200)
        self.assertNotIn("images", listing.json())
        self.assertEqual({item["filename"] for item in listing.json()["items"]},
                         {filename for filename, *_ in fixtures})
        overview = self.client.request("GET", "/api/zones?schema=items", cookie=cookie).json()
        zone = next(zone for zone in overview["zones"] if zone["id"] == "default")
        self.assertNotIn("images", zone)
        self.assertEqual(len(zone["items"]), len(fixtures))

        status, _, _ = request(
            self.server.port, "POST", "/paste/api/zones/default/items",
            headers={"Origin": "https://untrusted.example"}, cookie=cookie,
        )
        self.assertEqual(status, 403)
        status, _, _ = request(
            self.server.port, "GET", "/paste/api/zones/default/items",
            headers={"Host": "untrusted.example"}, cookie=cookie,
        )
        self.assertEqual(status, 403)
