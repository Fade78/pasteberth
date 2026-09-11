"""Tests for persistent bearer tokens and their HTTP authorization policy."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

from PasteBerth.runtime.tokens import (
    PERMISSION_LIST,
    PERMISSION_READ,
    PERMISSION_WRITE,
    MAX_TOKEN_DURATION_SECONDS,
    SCOPE_GROUP,
    SCOPE_GLOBAL,
    SCOPE_PATH,
    SCOPE_ZONE,
    TokenGrant,
    TokenStore,
    TokenStoreError,
    normalize_scope_path,
    parse_permissions,
)
from PasteBerth.runtime.service import ServiceError
from tests.helpers import build_multipart, json_of, request, write_config, LiveServer


PASSWORD = "token-test-password-123"


class TestTokenStore(unittest.TestCase):
    def test_persists_rotates_and_revokes(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "tokens.sqlite3"
            store = TokenStore(path, clock=lambda: now[0])
            record, credential = store.create(
                "deployment",
                [TokenGrant(SCOPE_ZONE, "default", PERMISSION_LIST | PERMISSION_READ)],
                duration_seconds=60,
            )
            self.assertEqual(store.authenticate(credential).token_id, record.token_id)

            restarted = TokenStore(path, clock=lambda: now[0])
            self.assertEqual(restarted.authenticate(credential).label, "deployment")
            extended = restarted.extend(record.token_id, duration_seconds=120)
            self.assertEqual(extended.expires_at, 1120.0)

            rotated, replacement = restarted.rotate(record.token_id, duration_seconds=30)
            self.assertEqual(rotated.label, "deployment")
            self.assertIsNone(restarted.authenticate(credential))
            self.assertEqual(restarted.authenticate(replacement).token_id, record.token_id)
            restarted.revoke(record.token_id)
            self.assertIsNone(restarted.authenticate(replacement))

    def test_permanent_expiry_and_suspension_survive_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "tokens.sqlite3"
            store = TokenStore(path)
            record, credential = store.create(
                "probe",
                [TokenGrant(SCOPE_GROUP, "builds", 0)],
                duration_seconds=None,
            )
            self.assertTrue(store.authenticate(credential).is_active())
            store.set_suspension(SCOPE_GROUP, "builds", True)
            restarted = TokenStore(path)
            self.assertIn((SCOPE_GROUP, "builds"), restarted.suspensions())
            self.assertEqual(restarted.authenticate(credential).token_id, record.token_id)

    def test_permission_parser_accepts_no_rights_and_rejects_duplicates(self):
        self.assertEqual(parse_permissions([]), 0)
        self.assertEqual(parse_permissions(["L", "R", "W"]), 7)
        with self.assertRaises(ValueError):
            parse_permissions(["L", "L"])
        with self.assertRaises(ValueError):
            parse_permissions([["L"]])

    def test_path_grants_are_normalized_at_the_model_boundary(self):
        root = Path(tempfile.gettempdir())
        grant = TokenGrant(SCOPE_PATH, str(root / "pasteberth" / ".." / "zone"), PERMISSION_LIST)
        self.assertEqual(grant.scope_value, normalize_scope_path(str(root / "zone")))
        with self.assertRaises(ValueError):
            TokenGrant(SCOPE_PATH, "relative/zone", PERMISSION_LIST)

    def test_maximum_finite_duration_is_storable(self):
        with tempfile.TemporaryDirectory() as raw:
            store = TokenStore(Path(raw) / "tokens.sqlite3")
            record, credential = store.create(
                "long-lived",
                [TokenGrant(SCOPE_ZONE, "default", PERMISSION_LIST)],
                duration_seconds=MAX_TOKEN_DURATION_SECONDS,
            )
            self.assertIsNotNone(record.expires_at)
            self.assertIsNotNone(store.authenticate(credential))

    def test_registry_rejects_a_world_writable_ancestor(self):
        if os.name == "nt":
            self.skipTest("POSIX directory permissions are not representative on Windows")
        with tempfile.TemporaryDirectory() as raw:
            outer = Path(raw) / "outer"
            inner = outer / "inner"
            inner.mkdir(parents=True, mode=0o700)
            outer.chmod(0o777)
            with self.assertRaisesRegex(TokenStoreError, "permissions"):
                TokenStore(inner / "tokens.sqlite3")


class TokenHTTPTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.default_dir = root / "default-images"
        self.secondary_dir = root / "secondary-images"
        self.cfg = write_config(
            root,
            zones=[
                {"id": "default", "label": "Default", "retain": 3, "directory": str(self.default_dir)},
                {"id": "secondary", "label": "Secondary", "retain": 3, "directory": str(self.secondary_dir)},
            ],
            groups=[{"name": "builds", "selection": "pattern", "pattern": ["default", "secondary"]}],
            auth_enabled=True,
            password=PASSWORD,
        )
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)
        status, headers, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=f"password={PASSWORD}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.cookie = headers["set-cookie"].split(";", 1)[0]

    def req(self, method, path, *, body=None, headers=None, cookie=None):
        return request(
            self.server.port,
            method,
            path,
            body=body,
            headers=headers,
            cookie=self.cookie if cookie is None else cookie,
        )

    def create_token(self, grants, duration=3600):
        body = json.dumps(
            {"label": "test token", "duration_seconds": duration, "grants": grants},
            separators=(",", ":"),
        ).encode()
        status, _, response = self.req(
            "POST",
            "/api/tokens",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201, response)
        return json_of(response)

    @staticmethod
    def grant(scope_type, scope_value, permissions, allow_replace=False):
        return {
            "scope_type": scope_type,
            "scope_value": scope_value,
            "permissions": permissions,
            "allow_replace": allow_replace,
        }

    def test_zone_token_is_scoped_and_survives_restart(self):
        created = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["L", "R", "W"], True),
        ])
        token = created["token"]
        status, _, response = self.req(
            "GET",
            "/api/zones/default/access",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(json_of(response)["permissions"], ["L", "R", "W"])
        status, _, response = self.req(
            "GET",
            "/api/zones/secondary/access",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 404, response)
        status, _, response = self.req(
            "GET",
            "/api/zones/secondary/items",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 404, response)
        status, _, response = self.req(
            "GET",
            "/api/tokens",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 403, response)
        status, _, response = self.req(
            "GET",
            "/api/zones?schema=items",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        overview = json_of(response)
        self.assertEqual({zone["id"] for zone in overview["zones"]}, {"default"})
        for group in overview["groups"]:
            self.assertNotIn("pattern", group)
            self.assertEqual(set(group["zone_ids"]), {"default"})
        status, _, response = self.req(
            "GET",
            "/api/groups",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        groups = json_of(response)["groups"]
        for group in groups:
            self.assertNotIn("pattern", group)
            self.assertEqual(set(group["zone_ids"]), {"default"})

        self.server.restart()
        status, _, response = self.req(
            "GET",
            "/api/zones/default/access",
            headers={"Authorization": f"Bearer {token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)

    def test_independent_permissions_and_blind_write_response(self):
        status, _, response = self.req(
            "POST",
            "/api/zones/default/items",
            body=build_multipart(
                filename="known.txt",
                data=b"old",
                content_type="text/plain",
                extra_fields={"preserve_name": "1"},
            )[0],
            headers={
                "Content-Type": build_multipart(
                    filename="known.txt",
                    data=b"old",
                    content_type="text/plain",
                    extra_fields={"preserve_name": "1"},
                )[1],
            },
        )
        self.assertEqual(status, 201, response)

        list_token = self.create_token([self.grant(SCOPE_ZONE, "default", ["L"])])["token"]
        read_token = self.create_token([self.grant(SCOPE_ZONE, "default", ["R"])])["token"]
        write_token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["W"], True),
        ])["token"]

        status, _, response = self.req(
            "GET",
            "/api/zones/default/items",
            headers={"Authorization": f"Bearer {list_token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        status, _, response = self.req(
            "GET",
            "/api/zones/default/items/known.txt/content",
            headers={"Authorization": f"Bearer {list_token}"},
            cookie="",
        )
        self.assertEqual(status, 403, response)
        status, _, response = self.req(
            "GET",
            "/api/zones/default/items/known.txt/content",
            headers={"Authorization": f"Bearer {read_token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        status, _, response = self.req(
            "GET",
            "/api/zones/default/items",
            headers={"Authorization": f"Bearer {read_token}"},
            cookie="",
        )
        self.assertEqual(status, 403, response)

        comment = json.dumps({"comment": "write-only"}, separators=(",", ":")).encode()
        status, _, response = self.req(
            "PATCH",
            "/api/zones/default/items/known.txt/comment",
            body=comment,
            headers={
                "Authorization": f"Bearer {write_token}",
                "Content-Type": "application/json",
            },
            cookie="",
        )
        self.assertEqual(status, 200, response)
        self.assertEqual(json_of(response), {"updated": True})

        body, content_type = build_multipart(
            filename="known.txt",
            data=b"new",
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/items",
            body=body,
            headers={
                "Authorization": f"Bearer {write_token}",
                "Content-Type": content_type,
            },
            cookie="",
        )
        self.assertEqual(status, 428, response)
        self.assertEqual(
            json_of(response)["error"],
            {"code": "upload_failed", "message": "upload failed"},
        )
        body, content_type = build_multipart(
            filename="known.txt",
            data=b"new",
            content_type="text/plain",
            extra_fields={"preserve_name": "1", "replace": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/items",
            body=body,
            headers={
                "Authorization": f"Bearer {write_token}",
                "Content-Type": content_type,
            },
            cookie="",
        )
        self.assertEqual(status, 201, response)
        accepted = json_of(response)
        self.assertEqual(accepted, {"accepted": True})

    def test_multi_grant_transfer_and_group_suspension(self):
        body, content_type = build_multipart(
            filename="move.txt",
            data=b"move me",
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/items",
            body=body,
            headers={"Content-Type": content_type},
        )
        self.assertEqual(status, 201, response)
        transfer_token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["L", "R"]),
            self.grant(SCOPE_ZONE, "secondary", ["W"]),
        ])["token"]
        transfer_body = json.dumps(
            {
                "mode": "copy",
                "source_zone": "default",
                "target_zone": "secondary",
                "filenames": ["move.txt"],
            },
            separators=(",", ":"),
        ).encode()
        status, _, response = self.req(
            "POST",
            "/api/transfers?schema=items",
            body=transfer_body,
            headers={
                "Authorization": f"Bearer {transfer_token}",
                "Content-Type": "application/json",
            },
            cookie="",
        )
        self.assertEqual(status, 200, response)
        transfer_result = json_of(response)
        self.assertEqual(transfer_result["items"], [])
        self.assertEqual(transfer_result["retention_deleted"], [])

        group_token = self.create_token([
            self.grant(SCOPE_GROUP, "builds", ["L"]),
        ])["token"]
        status, _, response = self.req(
            "GET",
            "/api/zones/default/access",
            headers={"Authorization": f"Bearer {group_token}"},
            cookie="",
        )
        self.assertEqual(status, 200, response)
        suspension = json.dumps(
            {"scope_type": SCOPE_GROUP, "scope_value": "builds", "suspended": True},
            separators=(",", ":"),
        ).encode()
        status, _, response = self.req(
            "POST",
            "/api/token-suspensions",
            body=suspension,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, response)
        status, _, response = self.req(
            "GET",
            "/api/zones/default/access",
            headers={"Authorization": f"Bearer {group_token}"},
            cookie="",
        )
        self.assertEqual(status, 404, response)

    def test_write_only_upload_errors_hide_service_details(self):
        write_token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["W"]),
        ])['token']
        body, content_type = build_multipart(
            filename="upload.txt",
            data=b"upload",
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        failures = (
            ("replacement_required", 428, "explicit replacement required for 'private.txt'"),
            ("storage_conflict", 409, "target already exists: private.txt"),
            ("destination_error", 500, "cannot open /srv/private/private.txt"),
        )
        for code, status_code, message in failures:
            with self.subTest(code=code), mock.patch.object(
                self.server.service,
                "upload",
                side_effect=ServiceError(code, message),
            ):
                status, _, response = self.req(
                    "POST",
                    "/api/zones/default/items",
                    body=body,
                    headers={
                        "Authorization": f"Bearer {write_token}",
                        "Content-Type": content_type,
                    },
                    cookie="",
                )
            self.assertEqual(status, status_code, response)
            self.assertEqual(
                json_of(response)["error"],
                {"code": "upload_failed", "message": "upload failed"},
            )
            self.assertNotIn("private", response.decode("utf-8"))

    def test_write_only_transfer_errors_hide_target_details(self):
        transfer_token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["L", "R"]),
            self.grant(SCOPE_ZONE, "secondary", ["W"]),
        ])['token']
        transfer_body = json.dumps(
            {
                "mode": "copy",
                "source_zone": "default",
                "target_zone": "secondary",
                "filenames": ["known.txt"],
            },
            separators=(",", ":"),
        ).encode()
        service_result = {
            "source_zone": "default",
            "target_zone": "secondary",
            "mode": "copy",
            "transferred": [],
            "failed": [{
                "filename": "known.txt",
                "code": "retention_error",
                "message": "private-target.txt",
                "target_published": True,
            }],
            "items": [{"filename": "private-target.txt"}],
            "retention_deleted": ["private-target.txt"],
        }
        with mock.patch.object(self.server.service, "transfer", return_value=service_result):
            status, _, response = self.req(
                "POST",
                "/api/transfers?schema=items",
                body=transfer_body,
                headers={
                    "Authorization": f"Bearer {transfer_token}",
                    "Content-Type": "application/json",
                },
                cookie="",
            )
        self.assertEqual(status, 200, response)
        result = json_of(response)
        self.assertNotIn("private-target.txt", response.decode("utf-8"))
        self.assertEqual(result["failed"], [{
            "filename": "known.txt",
            "code": "retention_error",
            "message": "transfer failed",
        }])

        with mock.patch.object(
            self.server.service,
            "transfer",
            side_effect=ServiceError("storage_conflict", "target already exists: private-target.txt"),
        ):
            status, _, response = self.req(
                "POST",
                "/api/transfers?schema=items",
                body=transfer_body,
                headers={
                    "Authorization": f"Bearer {transfer_token}",
                    "Content-Type": "application/json",
                },
                cookie="",
            )
        self.assertEqual(status, 409, response)
        self.assertEqual(
            json_of(response)["error"],
            {"code": "transfer_failed", "message": "transfer failed"},
        )

    def test_write_only_mutation_errors_hide_storage_details(self):
        write_token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["W"]),
        ])['token']
        auth = {"Authorization": f"Bearer {write_token}"}
        comment_body = json.dumps({"comment": "note"}, separators=(",", ":")).encode()
        with mock.patch.object(
            self.server.service,
            "update_comment",
            side_effect=ServiceError("destination_error", "cannot open /srv/secret/comment.txt"),
        ):
            status, _, response = self.req(
                "PATCH",
                "/api/zones/default/items/submitted.txt/comment",
                body=comment_body,
                headers={**auth, "Content-Type": "application/json"},
                cookie="",
            )
        self.assertEqual(status, 500, response)
        self.assertEqual(
            json_of(response)["error"],
            {"code": "comment_failed", "message": "comment failed"},
        )
        self.assertNotIn("/srv/secret", response.decode("utf-8"))

        with mock.patch.object(
            self.server.service,
            "delete",
            side_effect=ServiceError("destination_error", "cannot open /srv/secret/delete.txt"),
        ):
            status, _, response = self.req(
                "DELETE",
                "/api/zones/default/items/submitted.txt",
                headers=auth,
                cookie="",
            )
        self.assertEqual(status, 500, response)
        self.assertEqual(
            json_of(response)["error"],
            {"code": "delete_failed", "message": "delete failed"},
        )
        self.assertNotIn("/srv/secret", response.decode("utf-8"))

        batch_body = json.dumps(
            {"filenames": ["submitted.txt"]}, separators=(",", ":")
        ).encode()
        with mock.patch.object(
            self.server.service,
            "delete_many",
            return_value={
                "deleted": [],
                "failed": [{
                    "filename": "submitted.txt",
                    "code": "destination_error",
                    "message": "cannot open /srv/secret/batch.txt",
                }],
            },
        ):
            status, _, response = self.req(
                "POST",
                "/api/zones/default/items/batch-delete",
                body=batch_body,
                headers={**auth, "Content-Type": "application/json"},
                cookie="",
            )
        self.assertEqual(status, 200, response)
        self.assertEqual(
            json_of(response)["failed"],
            [{
                "filename": "submitted.txt",
                "code": "destination_error",
                "message": "delete failed",
            }],
        )
        self.assertNotIn("/srv/secret", response.decode("utf-8"))

    def test_invalid_duration_is_rejected_without_server_error(self):
        body = json.dumps(
            {
                "label": "too large",
                "duration_seconds": 10**400,
                "grants": [self.grant(SCOPE_ZONE, "default", ["L"])],
            },
            separators=(",", ":"),
        ).encode()
        status, headers, response = self.req(
            "POST",
            "/api/tokens",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(headers.get("cache-control"), "no-store")

    def test_unauthenticated_api_response_advertises_bearer(self):
        status, headers, response = request(
            self.server.port,
            "GET",
            "/api/zones?schema=items",
            headers={"Host": "127.0.0.1"},
        )
        self.assertEqual(status, 401, response)
        self.assertIn("Bearer", headers.get("www-authenticate", ""))

    def test_logout_rejects_ambiguous_session_and_bearer_credentials(self):
        token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["L"]),
        ])['token']
        status, _, response = self.req(
            "POST",
            "/logout",
            headers={"Authorization": f"Bearer {token}"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 401, response)

        status, _, response = self.req("GET", "/api/zones?schema=items")
        self.assertEqual(status, 200, response)

    def test_authentication_cache_is_reset_between_persistent_requests(self):
        token = self.create_token([
            self.grant(SCOPE_ZONE, "default", ["L"]),
        ])['token']
        connection = HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            connection.request(
                "GET",
                "/api/zones?schema=items",
                headers={"Authorization": f"Bearer {token}"},
            )
            first = connection.getresponse()
            first_body = first.read()
            self.assertEqual(first.status, 200, first_body)

            connection.request("GET", "/api/zones?schema=items")
            second = connection.getresponse()
            second_body = second.read()
            self.assertEqual(second.status, 401, second_body)
        finally:
            connection.close()


    def test_suspension_cache_is_reset_between_persistent_requests(self):
        token = self.create_token([
            self.grant(SCOPE_GROUP, "builds", ["L"]),
        ])['token']
        connection = HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            headers = {"Authorization": f"Bearer {token}"}
            connection.request("GET", "/api/zones/default/access", headers=headers)
            first = connection.getresponse()
            first_body = first.read()
            self.assertEqual(first.status, 200, first_body)

            suspension = json.dumps(
                {"scope_type": SCOPE_GROUP, "scope_value": "builds", "suspended": True},
                separators=(",", ":"),
            ).encode()
            status, _, response = self.req(
                "POST",
                "/api/token-suspensions",
                body=suspension,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, response)

            connection.request("GET", "/api/zones/default/access", headers=headers)
            second = connection.getresponse()
            second_body = second.read()
            self.assertEqual(second.status, 404, second_body)
        finally:
            connection.close()


class TestPathGrantUsesActiveDestination(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.old_directory = root / "old-zone"
        self.new_directory = root / "new-zone"
        self.alias = root / "zone"
        self.old_directory.mkdir()
        self.new_directory.mkdir()
        try:
            self.alias.symlink_to(self.old_directory, target_is_directory=True)
        except (OSError, NotImplementedError):
            self._tmp.cleanup()
            self.skipTest("symlinks unavailable")
        self.cfg = write_config(
            root,
            zones=[{
                "id": "default",
                "label": "Default",
                "retain": 3,
                "directory": str(self.alias),
            }],
            auth_enabled=True,
            password=PASSWORD,
        )
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_path_scope_follows_the_destination_used_by_service(self):
        _record, token = self.server.tokens.create(
            "path-grant",
            [TokenGrant(SCOPE_PATH, str(self.new_directory), PERMISSION_WRITE)],
            duration_seconds=3600,
        )
        self.alias.unlink()
        self.alias.symlink_to(self.new_directory, target_is_directory=True)
        body, content_type = build_multipart(
            filename="must-not-land.txt",
            data=b"path test",
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = request(
            self.server.port,
            "POST",
            "/api/zones/default/items",
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )
        self.assertEqual(status, 404, response)
        self.assertFalse((self.old_directory / "must-not-land.txt").exists())
        self.assertFalse((self.new_directory / "must-not-land.txt").exists())


if __name__ == "__main__":
    unittest.main()
