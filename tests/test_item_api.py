"""Generic HTTP vocabulary, legacy compatibility, and upload ambiguity."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote
import zipfile

from PasteBerth.runtime.storage import (
    StoredImage, StoredItem, UnknownImageError, UnknownItemError,
)
from PasteBerth.runtime.service import ServiceError
from PasteBerth.runtime.webapp import _item_api_payload
from tests.helpers import LiveServer, build_multipart, make_png, request, write_config


class ItemAPIFixture(unittest.TestCase):
    config_options = {}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.server = LiveServer(write_config(self.tmp, **self.config_options))
        self.addCleanup(self.server.stop)
        self.service = self.server.service
        self.destination = self.service._destinations["default"]

    def call(self, method, path, **kwargs):
        return request(self.server.port, method, self.server.cfg.url_prefix + path, **kwargs)

    def json_call(self, method, path, payload):
        return self.call(
            method, path, body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def upload(self, filename="one.txt", data=b"original content", *, legacy=False,
               field="file", mime="text/plain", replace_existing=False):
        body, ctype = build_multipart(
            field=field, filename=filename, data=data, content_type=mime,
            extra_fields={"preserve_name": "1", "replace": "1" if replace_existing else "0"},
        )
        route = "images" if legacy else "items"
        status, _, body = self.call(
            "POST", f"/api/zones/default/{route}", body=body,
            headers={"Content-Type": ctype},
        )
        self.assertIn(status, (200, 201), body)
        return json.loads(body)


class TestItemAPI(ItemAPIFixture):
    def test_canonical_types_have_concrete_legacy_aliases(self):
        self.assertIs(StoredImage, StoredItem)
        self.assertIs(UnknownImageError, UnknownItemError)
        self.upload()
        item = self.destination.list()[0]
        self.assertIs(type(item), StoredItem)
        self.assertEqual(item.etag, '"sha256-' + item.sha256 + '"')
        self.assertIsNone(replace(item, sha256=None).etag)

    def test_pdf_text_and_image_list_download_and_legacy_metadata(self):
        examples = (
            ("document.pdf", b"%PDF-1.7\n\x00binary PDF content", "application/pdf", "binary"),
            ("note.txt", b"a text document\n", "text/plain", "text"),
            ("picture.png", make_png(), "image/png", "image"),
        )
        for filename, data, mime, kind in examples:
            with self.subTest(filename=filename):
                item = self.upload(filename, data, mime=mime)
                self.assertEqual(item["kind"], kind)
                self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(item["etag"], '"sha256-' + item["sha256"] + '"')
                self.assertIsNone(item["changed_at"])
                self.assertNotIn("preview_url", item)
                status, headers, body = self.call("GET", item["content_url"], headers={"If-Match": item["etag"]})
                self.assertEqual((status, body), (200, data))
                self.assertEqual(headers["etag"], item["etag"])
                self.assertEqual(int(headers["content-length"]), len(data))
                status, headers, body = self.call("HEAD", item["content_url"], headers={"If-Match": item["etag"]})
                self.assertEqual((status, body), (200, b""))
                self.assertEqual(headers["etag"], item["etag"])
                self.assertEqual(int(headers["content-length"]), len(data))
        with mock.patch("PasteBerth.runtime.storage._sha256_file", side_effect=AssertionError("hash on list")):
            status, _, body = self.call("GET", "/api/zones/default/items")
            self.assertEqual(status, 200)
            generic = json.loads(body)
            self.assertEqual(set(generic), {"zone", "items"})
            self.assertEqual(len(generic["items"]), 3)
            status, _, body = self.call("GET", "/api/zones/default/images")
            legacy = json.loads(body)
            self.assertEqual(set(legacy), {"zone", "images"})
            for item in legacy["images"]:
                self.assertIn("preview_url", item)
                self.assertIn("content_url", item)
                self.assertIn("sha256", item)
                self.assertIn("etag", item)
            self.assertEqual(generic["items"], _item_api_payload(legacy)["items"])

    def test_aggregate_schema_is_explicit_without_duplicate_histories(self):
        self.upload()
        for query, key in (("", "images"), ("?schema=images", "images"), ("?schema=items", "items")):
            status, _, body = self.call("GET", "/api/zones" + query)
            self.assertEqual(status, 200, body)
            self.assertIn("max_image_pixels", json.loads(body))
            for zone in json.loads(body)["zones"]:
                self.assertIn(key, zone)
                self.assertNotIn("items" if key == "images" else "images", zone)
                for item in zone[key]:
                    self.assertEqual("preview_url" in item, key == "images")
        for query in ("schema=", "schema=invalid", "schema=ITEMS", "schema=items&schema=images",
                      "schema=items&schema=items", "schema=images&%73chema=images"):
            status, _, body = self.call("GET", "/api/zones?" + query)
            self.assertEqual(status, 400, body)
        self.assertIn("images", self.service.overview()["zones"][0])

    def test_generic_listing_uses_published_registry_without_refresh_or_hash(self):
        self.upload()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.service, "_refresh_zone_collections", side_effect=AssertionError("discovery")))
            stack.enter_context(mock.patch("PasteBerth.runtime.storage._sha256_file", side_effect=AssertionError("hash")))
            history = stack.enter_context(mock.patch.object(self.service, "history", wraps=self.service.history))
            for zone, status in (("default", 200), ("missing", 404)):
                response = self.call("GET", f"/api/zones/{zone}/items")
                self.assertEqual(response[0], status, response)
                history.assert_called_with(zone, blocking=False, _refresh=False)
        with mock.patch.object(self.service, "history", wraps=self.service.history) as history:
            self.assertEqual(self.call("GET", "/api/zones/default/images")[0], 200)
            history.assert_called_once_with("default")

    def test_schema_selection_does_not_leak_between_keepalive_requests(self):
        self.upload()
        conn = HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            for path, key in (
                ("/api/zones?schema=items", "items"), ("/api/zones", "images"),
                ("/api/zones/default/items", "items"), ("/api/zones/default/images", "images"),
            ):
                conn.request("GET", path)
                response = conn.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200, payload)
                zone = payload["zones"][0] if "zones" in payload else payload
                self.assertIn(key, zone)
                self.assertNotIn("images" if key == "items" else "items", zone)
        finally:
            conn.close()

    def test_busy_listing_is_423_not_an_empty_history(self):
        with self.service.zone_operation("default", kind="upload", exclusive=True):
            status, headers, body = self.call("GET", "/api/zones/default/items")
            self.assertEqual(status, 423, body)
            self.assertEqual(headers["retry-after"], "1")
            self.assertEqual(json.loads(body)["error"]["code"], "zone_busy")
            status, _, body = self.call("GET", "/api/zones?schema=items")
            zone = next(z for z in json.loads(body)["zones"] if z["id"] == "default")
            self.assertTrue(zone["busy"])
            self.assertIsNone(zone["count"])
            self.assertEqual(zone["items"], [])
            self.assertNotIn("images", zone)
        self.assertEqual(json.loads(self.call("GET", "/api/zones/default/items")[2])["items"], [])

    def test_child_routes_share_comment_transfer_archive_regularize_delete_logic(self):
        original = self.upload()
        comment = 'images unknown_image preview_url {"images":["unknown_image"]}'
        status, _, body = self.json_call("PATCH", "/api/zones/default/items/one.txt/comment", {"comment": comment})
        self.assertEqual(status, 200, body)
        updated = json.loads(body)
        self.assertEqual(updated["comment"], comment)
        self.assertEqual(updated["etag"], original["etag"])
        self.assertNotIn("preview_url", updated)
        status, _, body = self.json_call("POST", "/api/transfers?schema=items", {
            "mode": "copy", "source_zone": "default", "target_zone": "secondary", "filenames": ["one.txt"],
        })
        self.assertEqual(status, 200, body)
        copied = json.loads(body)["items"][0]
        self.assertEqual(copied["comment"], comment)
        self.assertEqual(copied["etag"], original["etag"])
        self.assertNotIn("preview_url", copied)
        status, _, body = self.json_call("POST", "/api/zones/default/items/archive", {"filenames": ["one.txt"]})
        self.assertEqual(status, 200, body)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(archive.read("one.txt"), b"original content")
        stage = self.destination.stage_direct_drop(b"staged content")
        status, _, body = self.json_call("POST", "/api/zones/default/items/regularize", {
            "stage": stage, "filename": "staged.txt", "mime": "text/plain",
        })
        self.assertEqual(status, 201, body)
        self.assertIn("content_url", json.loads(body))
        self.assertNotIn("preview_url", json.loads(body))
        self.assertFalse((self.destination.directory / stage).exists())
        self.assertEqual(self.call("DELETE", "/api/zones/default/items/staged.txt")[0], 200)
        status, _, body = self.json_call("POST", "/api/zones/default/items/batch-delete", {
            "filenames": ["one.txt", "unknown_image.txt"],
        })
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["deleted"], ["one.txt"])
        self.assertEqual(result["failed"][0]["code"], "unknown_item")
        self.assertEqual(result["failed"][0]["filename"], "unknown_image.txt")

    def test_unknown_errors_are_generic_only_on_generic_http_contract(self):
        for route, code in (("items", "unknown_item"), ("images", "unknown_image")):
            for method, child, payload in (
                ("DELETE", "/missing.txt", None),
                ("PATCH", "/missing.txt/comment", {"comment": "note"}),
                ("POST", "/archive", {"filenames": ["missing.txt"]}),
            ):
                with self.subTest(route=route, method=method):
                    path = f"/api/zones/default/{route}" + child
                    response = self.call(method, path) if payload is None else self.json_call(method, path, payload)
                    self.assertEqual(response[0], 404, response)
                    self.assertEqual(json.loads(response[2])["error"]["code"], code)
            status, _, body = self.json_call("POST", f"/api/zones/default/{route}/batch-delete", {"filenames": ["missing.txt"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["failed"][0]["code"], code)
        status, _, body = self.json_call("POST", "/api/transfers?schema=items", {
            "mode": "copy", "source_zone": "default", "target_zone": "secondary", "filenames": ["missing.txt"],
        })
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["error"]["code"], "unknown_item")

    def test_serialization_does_not_mutate_service_objects_or_user_strings(self):
        item = self.upload(legacy=True)
        item["comment"] = 'unknown_image images preview_url'
        payload = {"zones": [{"images": [item]}]}
        snapshot = json.loads(json.dumps(payload))
        converted = _item_api_payload(payload)
        self.assertEqual(payload, snapshot)
        self.assertEqual(converted["zones"][0]["items"][0]["comment"], item["comment"])
        self.assertNotIn("preview_url", converted["zones"][0]["items"][0])

    def test_both_upload_fields_and_raw_bodies_remain_supported(self):
        for legacy in (False, True):
            for field in ("image", "file"):
                item = self.upload(f"{legacy}-{field}.txt", field=field, legacy=legacy)
                self.assertEqual("preview_url" in item, legacy)
            route = "images" if legacy else "items"
            response = self.call("POST", f"/api/zones/default/{route}", body=b"raw text", headers={"Content-Type": "text/plain"})
            self.assertIn(response[0], (200, 201), response)

    def test_ambiguous_multipart_rejected_before_upload(self):
        def part(name, filename=None):
            disposition = f'Content-Disposition: form-data; name="{name}"'
            if filename is not None:
                disposition += f'; filename="{filename}"'
            return f"--B\r\n{disposition}\r\nContent-Type: text/plain\r\n\r\nsame bytes\r\n".encode()

        cases = (
            part("image", "one.txt") + part("file", "one.txt"),
            part("file", "one.txt") + part("file", "two.txt"),
            part("image", "one.txt") + part("image", "one.txt"),
            part("file", "one.txt") + part("unknown", "two.txt"),
            part("file", "one.txt") + part("unknown"),
            part("file", "one.txt") + part("replace") + part("replace"),
            part("file", "one.txt") + part("preserve_name", "extra.txt"),
            part("file", "one.txt") + part("creation_method", "extra.txt"),
            part("preserve_name"),
            part("other", "one.txt") + part("replace"),
            part("file", "one.txt") + b'--B\r\nContent-Disposition: form-data; filename="extra.txt"\r\n\r\nx\r\n',
        )
        with mock.patch.object(self.service, "upload", side_effect=AssertionError("ambiguous upload reached service")):
            for route in ("items", "images"):
                for index, body in enumerate(cases):
                    with self.subTest(route=route, case=index):
                        response = self.call("POST", f"/api/zones/default/{route}", body=body + b"--B--\r\n", headers={"Content-Type": "multipart/form-data; boundary=B"})
                        self.assertEqual(response[0], 400, response)

    def test_sole_unknown_upload_field_is_legacy_only(self):
        body, ctype = build_multipart(field="other", filename="note.txt", data=b"notes", content_type="text/plain")
        for route, status in (("items", 400), ("images", 201)):
            response = self.call("POST", f"/api/zones/default/{route}", body=body, headers={"Content-Type": ctype})
            self.assertEqual(response[0], status, response)

    def test_image_classification_matches_legacy_binary_fallback(self):
        body, ctype = build_multipart(field="file", data=b"\x89PNG\r\n\x1a\ninvalid")
        for route in ("items", "images"):
            response = self.call("POST", f"/api/zones/default/{route}", body=body, headers={"Content-Type": ctype})
            self.assertIn(response[0], (200, 201), response)
            self.assertEqual(json.loads(response[2])["kind"], "binary")
            self.assertEqual(json.loads(response[2])["mime"], "application/octet-stream")


class TestTransferSchema(ItemAPIFixture):
    transfer_request = {
        "mode": "copy", "source_zone": "default", "target_zone": "secondary", "filenames": ["one.txt"],
    }
    schemas = (("", False), ("?schema=images", False), ("?schema=items", True))

    def test_transfer_success_defaults_to_legacy_and_generic_requires_opt_in(self):
        original = self.upload()
        comment = 'unknown_image images preview_url {"images": []}'
        self.service.update_comment("default", "one.txt", comment)
        for query, generic in self.schemas:
            with self.subTest(query=query):
                status, _, body = self.json_call("POST", "/api/transfers" + query, self.transfer_request)
                self.assertEqual(status, 200, body)
                result = json.loads(body)
                self.assertEqual(result["transferred"], ["one.txt"])
                self.assertEqual(result["failed"], [])
                item = result["items"][0]
                self.assertEqual(item["comment"], comment)
                self.assertEqual(item["etag"], original["etag"])
                self.assertEqual("preview_url" in item, not generic)
                self.assertEqual(item["content_url"], self.server.cfg.url_prefix + "/api/zones/secondary/items/one.txt/content")
                if not generic:
                    self.assertEqual(item["preview_url"], self.server.cfg.url_prefix + "/previews/secondary/one.txt")
                self.service.delete("secondary", "one.txt")

    def test_transfer_service_errors_follow_selected_schema(self):
        for query, generic in self.schemas:
            status, _, body = self.json_call("POST", "/api/transfers" + query, self.transfer_request)
            self.assertEqual(status, 404, body)
            self.assertEqual(json.loads(body)["error"]["code"], "unknown_item" if generic else "unknown_image")
            with mock.patch.object(self.service, "transfer", side_effect=ServiceError("invalid_image", "image validation")):
                status, _, body = self.json_call("POST", "/api/transfers" + query, self.transfer_request)
            self.assertEqual(status, 400, body)
            self.assertEqual(json.loads(body)["error"]["code"], "invalid_image")

    def test_transfer_failed_entries_follow_schema_without_changing_messages(self):
        self.upload()
        message = "unknown_image images preview_url"
        with mock.patch.object(self.destination, "read", side_effect=UnknownItemError(message)):
            for query, generic in self.schemas:
                status, _, body = self.json_call("POST", "/api/transfers" + query, self.transfer_request)
                self.assertEqual(status, 200, body)
                result = json.loads(body)
                self.assertEqual(result["items"], [])
                self.assertEqual(result["transferred"], [])
                self.assertEqual(result["failed"], [{
                    "filename": "one.txt", "code": "unknown_item" if generic else "unknown_image",
                    "message": message, "target_published": False,
                }])

    def test_transfer_schema_rejects_invalid_or_duplicate_values_before_service(self):
        with mock.patch.object(self.service, "transfer") as transfer:
            for query in ("schema=", "schema", "schema=files", "schema=ITEMS", "schema=items&schema=images",
                          "schema=images&schema=images", "schema=items&%73chema=items"):
                status, _, body = self.json_call("POST", "/api/transfers?" + query, self.transfer_request)
                self.assertEqual(status, 400, body)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
            transfer.assert_not_called()


class TestMountedTransferSchema(TestTransferSchema):
    config_options = {"url_prefix": "/paste"}


class TestMountedItemAPI(ItemAPIFixture):
    config_options = {"url_prefix": "/paste"}

    def test_unicode_and_literal_percent_paths_are_quoted_once(self):
        for name in ("caf\u00e9 \u6587.txt", "literal%2F.txt"):
            item = self.upload(name)
            self.assertEqual(item["content_url"], "/paste/api/zones/default/items/" + quote(name, safe="") + "/content")
            status, headers, body = request(self.server.port, "GET", item["content_url"], headers={"If-Match": item["etag"]})
            self.assertEqual((status, body), (200, b"original content"))
            self.assertIn("filename*=UTF-8''" + quote(name, safe=""), headers["content-disposition"])
        for name, status in (("a%2Fb.txt", 404), ("..%2Fone.txt", 404), ("a%5Cb.txt", 400), ("a%00.txt", 400), ("%FF.txt", 400)):
            response = self.call("GET", "/api/zones/default/items/" + name + "/content")
            self.assertEqual(response[0], status, response)
        self.assertEqual(request(self.server.port, "GET", "/api/zones/default/items")[0], 404)

    def test_host_and_origin_checks_cover_new_routes(self):
        self.assertEqual(self.call("GET", "/api/zones/default/items", headers={"Host": "evil.invalid"})[0], 403)
        response = self.call("POST", "/api/zones/default/items", body=b"text", headers={"Content-Type": "text/plain", "Origin": "https://evil.invalid"})
        self.assertEqual(response[0], 403)
        self.assertEqual(json.loads(response[2])["error"]["code"], "forbidden_origin")
        self.assertEqual(self.service.history("default"), [])
