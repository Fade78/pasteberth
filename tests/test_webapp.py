"""Tests d'intégration HTTP sur serveur réel (socket) :
auth, uploads, previews, CSRF/Origin, proxys, en-têtes, fuites."""
import json
import io
import logging
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

from pasteberth import __version__
from pasteberth.platformfs import VolumeSpace
from tests.helpers import (
    build_multipart,
    json_of,
    login,
    make_jpeg,
    make_png,
    make_webp_lossy,
    request,
    write_config,
    LiveServer,
)
from pasteberth.webapp import BodyMemoryBudget, _safe_log_text

PASSWORD = "mot-de-passe-de-test-123"
FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{6}\.(png|jpg|webp)$")


class Base(unittest.TestCase):
    auth = False
    password: str | None = None
    config_kwargs: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.zones_dirs = {
            "default": tmp / "default-images",
            "secondary": tmp / "secondary-images",
        }
        zones = [
            {"id": zid, "label": zid.upper(), "retain": retain,
             "color": color, "directory": str(path),
             "reference_prefix": self.config_kwargs.get("reference_prefix", "@"),
             "reference_suffix": self.config_kwargs.get("reference_suffix", ""),
             "reference_list_prefix": self.config_kwargs.get("reference_list_prefix", ""),
             "reference_list_suffix": self.config_kwargs.get("reference_list_suffix", ""),
             "reference_separator": self.config_kwargs.get("reference_separator", ","),
             "allow_zip_download": self.config_kwargs.get("allow_zip_download", True)}
            for zid, path, retain, color in [
                ("default", self.zones_dirs["default"], 3, "#304237"),
                ("secondary", self.zones_dirs["secondary"], 2, "#26394a"),
            ]
        ]
        cfg_path = write_config(
            tmp,
            zones=zones,
            auth_enabled=self.auth,
            password=self.password,
            max_upload_size=self.config_kwargs.get("max_upload_size", "20MB"),
            url_prefix=self.config_kwargs.get("url_prefix"),
            trusted_proxies=self.config_kwargs.get("trusted_proxies", '["127.0.0.1", "::1"]'),
            allowed_hosts=self.config_kwargs.get("allowed_hosts"),
            accept_bin=self.config_kwargs.get("accept_bin"),
            accept_img=self.config_kwargs.get("accept_img"),
            accept_doc=self.config_kwargs.get("accept_doc"),
            groups=self.config_kwargs.get("groups"),
        )
        self.tmp = tmp
        self.url_prefix = self.config_kwargs.get("url_prefix") or ""
        self.server = LiveServer(cfg_path)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)
        if self.auth:
            # login avec le mot de passe défini par la classe de test
            status, headers, _ = request(
                self.server.port,
                "POST",
                f"{self.url_prefix}/login" if self.url_prefix else "/login",
                body=f"password={self.password}".encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert status == 303, f"login échoué : {status}"
            self.set_cookie_header = headers["set-cookie"]
            self.cookie = self.set_cookie_header.split(";", 1)[0]
        else:
            self.set_cookie_header = None
            self.cookie = None

    def req(self, method, path, body=None, headers=None, cookie="default"):
        return request(
            self.server.port,
            method,
            path,
            body=body,
            headers=headers,
            cookie=self.cookie if cookie == "default" else cookie,
        )


class TestBudgetMemoire(unittest.TestCase):
    def test_reservation_et_liberation(self):
        budget = BodyMemoryBudget(200_000)
        first = budget.reserve(60_000)
        self.assertIsNotNone(first)
        self.assertIsNone(budget.reserve(60_000))
        budget.release(first)
        self.assertIsNotNone(budget.reserve(60_000))


class TestPublic(Base):
    def test_health_sans_auth(self):
        status, _, body = self.req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(body), {"ok": True})

    def test_sans_groupes_toutes_les_zones_restant_visibles(self):
        status, _, response = self.req("GET", "/api/groups")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(response), {"groups": []})

        status, _, response = self.req("GET", "/api/zones")
        self.assertEqual(status, 200)
        self.assertEqual(
            [zone["groups"] for zone in json_of(response)["zones"]],
            [[], []],
        )

    def test_static_assets(self):
        for path, ctype in [("/static/app.js", "text/javascript"),
                            ("/static/style.css", "text/css"),
                            ("/static/favicon.svg", "image/svg+xml")]:
            status, headers, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(ctype), path)
            if path != "/static/favicon.svg":
                self.assertEqual(headers["cache-control"], "no-store", path)


class TestConfigurationProxyParDefaut(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {"trusted_proxies": None}

    def test_health_avec_proxy_par_defaut(self):
        status, _, body = self.req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(body), {"ok": True})

    def test_host_distant_accepte_sans_allowlist_configuree(self):
        for host in ("remote-station.example", "second-station.example"):
            with self.subTest(host=host):
                status, _, body = self.req(
                    "GET",
                    "/api/health",
                    headers={"Host": host},
                )
                self.assertEqual(status, 200)
                self.assertEqual(json_of(body), {"ok": True})


class TestProxySpoofingParDefaut(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {"trusted_proxies": None}

    def test_xff_ne_change_pas_la_cle_de_limitation_sans_proxy_declare(self):
        statuses = []
        for index in range(6):
            status, _, _ = self.req(
                "POST",
                "/login",
                body=b"password=incorrect",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Forwarded-For": f"203.0.113.{index + 1}",
                },
                cookie=None,
            )
            statuses.append(status)
        self.assertEqual(statuses[:5], [401] * 5)
        self.assertEqual(statuses[5], 429)


class TestModeAnonymeLoopback(Base):
    auth = False

    def test_index_servi(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", body)
        self.assertIn(f"v{__version__}".encode(), body)
        self.assertNotIn(b"__PASTEBERTH_VERSION__", body)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_hote_non_loopback_refuse(self):
        status, _, body = self.req(
            "GET",
            "/api/health",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(body)["error"]["code"], "forbidden_host")

    def test_flux_complet_anonyme(self):
        png = make_png(10, 5)
        body, ctype = build_multipart(data=png)
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        item = json_of(resp)
        ref = item["reference"]
        self.assertTrue(ref.startswith("@"))
        self.assertEqual(ref[1:], str(self.zones_dirs["default"] / item["filename"]))
        # preview
        status, headers, data = self.req("GET", item["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(data, png)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "no-store")


class TestUrlPrefix(Base):
    config_kwargs = {"url_prefix": "/paste"}

    def test_route_et_assets_sous_prefixe(self):
        status, headers, body = self.req("GET", "/paste?next=%2Fapi%2Fhealth")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/paste/?next=%2Fapi%2Fhealth")

        status, _, body = self.req("GET", "/paste/")
        self.assertEqual(status, 200)
        self.assertIn(b'/paste/static/app.js', body)
        self.assertNotIn(b'href="/static/style.css"', body)

        status, _, body = self.req("GET", "/paste/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(body), {"ok": True})

        status, _, body = self.req("GET", "/api/health")
        self.assertEqual(status, 404)
        self.assertNotIn(b'"ok"', body)

        status, _, _ = self.req("GET", "/")
        self.assertEqual(status, 404)

        status, _, _ = self.req("GET", "/paste/static/app.js")
        self.assertEqual(status, 200)

    def test_upload_et_preview_url_sont_sous_prefixe(self):
        body, ctype = build_multipart(data=make_png(10, 5))
        status, _, response = self.req(
            "POST",
            "/paste/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 201)
        item = json_of(response)
        self.assertTrue(item["preview_url"].startswith("/paste/previews/"))

        status, _, preview = self.req("GET", item["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(preview, make_png(10, 5))


class TestUrlPrefixAuth(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {"url_prefix": "/paste"}

    def test_cookie_et_redirections_sont_scopes(self):
        self.assertIn("Path=/paste", self.set_cookie_header)
        status, _, body = self.req("GET", "/paste/login", cookie=None)
        self.assertEqual(status, 200)
        self.assertIn(b'action="/paste/login"', body)
        self.assertIn(b'/paste/static/favicon.svg', body)

        status, headers, _ = self.req("GET", "/paste/login")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/paste/")

        status, headers, _ = self.req("POST", "/paste/logout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/paste/login")
        self.assertIn("Path=/paste", headers["set-cookie"])

    def test_origin_ne_contient_pas_le_prefixe(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/paste/api/zones/default/images",
            body=body,
            headers={
                "Content-Type": ctype,
                "Origin": f"http://127.0.0.1:{self.server.port}",
            },
        )
        self.assertEqual(status, 201)

        body, ctype = build_multipart(data=make_png())
        status, _, response = self.req(
            "POST",
            "/paste/api/zones/default/images",
            body=body,
            headers={
                "Content-Type": ctype,
                "Origin": f"http://127.0.0.1:{self.server.port}/paste",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(response)["error"]["code"], "forbidden_origin")

class TestAuthentification(Base):
    auth = True
    password = PASSWORD

    # ---- accès protégés (#27, #28)
    def test_index_redirige_vers_login(self):
        status, headers, _ = self.req("GET", "/", cookie=None)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")

    def test_api_401_json(self):
        status, headers, body = self.req("GET", "/api/zones", cookie=None)
        self.assertEqual(status, 401)
        self.assertEqual(json_of(body)["error"]["code"], "unauthorized")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_groups_401_json(self):
        status, headers, body = self.req("GET", "/api/groups", cookie=None)
        self.assertEqual(status, 401)
        self.assertEqual(json_of(body)["error"]["code"], "unauthorized")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_upload_401(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req("POST", "/api/zones/default/images", body=body,
                                headers={"Content-Type": ctype}, cookie=None)
        self.assertEqual(status, 401)

    def test_preview_401(self):
        body, ctype = build_multipart(data=make_png())
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        url = json_of(resp)["preview_url"]
        status, headers, _ = self.req("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        status, _, _ = self.req("GET", url, cookie=None)
        self.assertEqual(status, 401)

    # ---- login
    def test_mauvais_mot_de_passe(self):
        start = time.monotonic()
        status, headers, body = request(
            self.server.port, "POST", "/login",
            body=b"password=totalement-faux",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elapsed = time.monotonic() - start
        self.assertEqual(status, 401)
        self.assertGreaterEqual(elapsed, 0.4)  # temporisation anti-bruteforce
        self.assertIn("Incorrect password", body.decode())
        self.assertNotIn("set-cookie", headers)

    def test_bon_mot_de_passe_cookie_drapeaux(self):
        status, headers, _ = request(
            self.server.port, "POST", "/login",
            body=f"password={PASSWORD}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        set_cookie = headers["set-cookie"]
        self.assertIn("pb_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)  # schéma effectif http ici

    def test_login_json_accepte(self):
        status, headers, _ = request(
            self.server.port, "POST", "/login",
            body=json.dumps({"password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 303)

    def test_page_login_deja_connecte_redirige(self):
        status, headers, _ = self.req("GET", "/login")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

    # ---- logout (#29)
    def test_logout_revoque_la_session(self):
        token = self.cookie.split("=", 1)[1]
        status, headers, _ = self.req("POST", "/logout")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["set-cookie"])
        status, _, _ = self.req("GET", "/api/zones")
        self.assertEqual(status, 401)

    def test_logout_get_refuse_et_ne_revoque_pas(self):
        status, _, _ = self.req("GET", "/logout")
        self.assertEqual(status, 405)
        status, _, _ = self.req("GET", "/api/zones")
        self.assertEqual(status, 200)

    def test_corps_login_trop_grand(self):
        status, _, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=b"password=" + b"x" * (16 * 1024),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 413)

    def test_corps_login_plafond_4kio(self):
        status, _, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=b"password=" + b"x" * (4 * 1024),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 413)

    def test_login_multipart_trop_de_parties_refuse(self):
        boundary = "----pb-test"
        parts = []
        for index in range(40):
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="f{index}"\r\n\r\n'
                f"value{index}\r\n"
            )
        body = ("".join(parts) + f"--{boundary}--\r\n").encode()
        status, _, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        # Le parse borné échoue avant toute vérification scrypt : le login
        # est refusé sans réserver de slot coûteux.
        self.assertEqual(status, 401)

    def test_reset_reseau_pendant_login_ne_reserve_pas_de_slot(self):
        import socket
        import struct

        sockets = []
        try:
            for index in range(4):
                sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
                sockets.append(sock)
                sock.sendall(
                    b"POST /login HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    + f"X-Forwarded-For: 203.0.113.{index + 1}\r\n".encode()
                    + b"Content-Length: 100\r\n"
                    b"Content-Type: application/x-www-form-urlencoded\r\n\r\n"
                )

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.server.limiter._lock:
                    in_flight = sum(entry[3] for entry in self.server.limiter._state.values())
                if in_flight == 4:
                    self.fail("un corps de login incomplet ne doit pas réserver de slot")
                time.sleep(0.01)

            for sock in sockets:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sock.close()
            sockets.clear()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.server.limiter._lock:
                    in_flight = sum(entry[3] for entry in self.server.limiter._state.values())
                if in_flight == 0:
                    break
                time.sleep(0.01)
            self.assertEqual(in_flight, 0)

            status, _, _ = request(
                self.server.port,
                "POST",
                "/login",
                body=f"password={PASSWORD}".encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 303)
        finally:
            for sock in sockets:
                sock.close()

    def test_cookie_forge_rejete(self):
        forged = "pb_session=AQAAANBBB_forged-token-value"
        status, _, _ = self.req("GET", "/api/zones", cookie=forged)
        self.assertEqual(status, 401)

    def test_suppression_sans_auth_401(self):
        status, _, _ = self.req("DELETE", "/api/zones/default/images/2026-01-01_00-00-00_abcdef.png",
                                cookie=None)
        self.assertEqual(status, 401)


class TestPreviewsConcurrence(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {"max_upload_size": "50MiB"}

    def test_preview_busy_est_temporaire(self):
        body, ctype = build_multipart(data=make_png())
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 201)
        preview_url = json_of(response)["preview_url"]

        active = 0
        active_lock = threading.Lock()
        both_active = threading.Event()
        release = threading.Event()
        results = []
        original_preview = self.server.service.preview

        def blocked_preview(*args):
            nonlocal active
            with active_lock:
                active += 1
                if active == 2:
                    both_active.set()
            self.assertTrue(release.wait(2))
            return original_preview(*args)

        def fetch_preview():
            results.append(self.req("GET", preview_url))

        with mock.patch.object(self.server.service, "preview", side_effect=blocked_preview):
            threads = [threading.Thread(target=fetch_preview) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(both_active.wait(2))
            status, headers, _ = self.req("GET", preview_url)
            self.assertEqual(status, 503)
            self.assertEqual(headers["retry-after"], "1")
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.assertEqual([item[0] for item in results], [200, 200])


class TestUploadsFormats(Base):
    """(#1)(#2)(#3) PNG, JPEG, WebP ; (#8) faux MIME."""

    def setUp(self):
        super().setUp()

    def _upload_raw(self, zone, data, ctype="application/octet-stream"):
        status, _, body = self.req("POST", f"/api/zones/{zone}/images", body=data,
                                   headers={"Content-Type": ctype})
        return status, (json_of(body) if status < 500 else {})

    def test_png(self):
        png = make_png(1920, 1080)
        status, item = self._upload_raw("default", png, "image/png")
        self.assertEqual(status, 201)
        self.assertEqual((item["width"], item["height"]), (1920, 1080))
        self.assertEqual(item["format"], "png")
        self.assertTrue(item["filename"].endswith(".png"))
        self.assertEqual(item["size"], len(png))
        self.assertRegex(item["filename"], FILENAME_RE)

    def test_jpeg(self):
        jpg = make_jpeg(800, 600)
        status, item = self._upload_raw("default", jpg, "image/jpeg")
        self.assertEqual(status, 201)
        self.assertEqual(item["format"], "jpeg")
        self.assertTrue(item["filename"].endswith(".jpg"))

    def test_webp(self):
        webp = make_webp_lossy(640, 480)
        status, item = self._upload_raw("default", webp, "image/webp")
        self.assertEqual(status, 201)
        self.assertEqual(item["format"], "webp")
        self.assertTrue(item["filename"].endswith(".webp"))

    def test_mime_mensonger_contenu_jpeg(self):
        # Le navigateur déclare image/png mais le contenu est JPEG : le contenu gagne.
        jpg = make_jpeg(50, 40)
        body, ctype = build_multipart(data=jpg, content_type="image/png")
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        item = json_of(resp)
        self.assertEqual(item["format"], "jpeg")
        self.assertTrue(item["filename"].endswith(".jpg"))

    def test_nom_client_ignore(self):
        body, ctype = build_multipart(filename="../../etc/passwd.png", data=make_png())
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        self.assertRegex(json_of(resp)["filename"], FILENAME_RE)

    def test_nom_pbdel_reserve_refuse_sans_ecriture(self):
        filename = ".pbdel-0123456789abcdef01234567.json"
        body, ctype = build_multipart(
            filename=filename,
            data=b"transaction-looking client data",
            content_type="application/json",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json_of(response)["error"]["code"], "invalid_filename")
        self.assertFalse((self.zones_dirs["default"] / filename).exists())
        self.assertFalse((self.zones_dirs["default"] / f"{filename}.json").exists())

    def test_html_texte_brut_accepte(self):
        status, item = self._upload_raw("default", b"<p>hello</p>", "text/html")
        self.assertEqual(status, 201)
        self.assertEqual(item["kind"], "text")
        self.assertEqual(item["mime"], "text/html")
        self.assertTrue(item["filename"].endswith(".html"))

    def test_preview_html_telechargee_en_piece_jointe(self):
        status, item = self._upload_raw("default", b"<p>hello</p>", "text/html")
        self.assertEqual(status, 201)
        status, headers, data = self.req("GET", item["preview_url"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("content-disposition", ""))

    def test_preview_javascript_telechargee_en_piece_jointe(self):
        status, item = self._upload_raw("default", b"alert(1)", "text/javascript")
        self.assertEqual(status, 201)
        status, headers, _ = self.req("GET", item["preview_url"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("content-disposition", ""))

    def test_mime_sidecar_ne_peut_pas_injecter_un_entete(self):
        status, item = self._upload_raw("default", b"hello", "text/plain")
        self.assertEqual(status, 201)
        sidecar = self.zones_dirs["default"] / (item["filename"] + ".json")
        raw = json.loads(sidecar.read_text())
        raw["mime"] = "text/plain\r\nX-Injected: yes"
        sidecar.write_text(json.dumps(raw))

        status, headers, _ = self.req("GET", item["preview_url"])
        self.assertEqual(status, 404)
        self.assertNotIn("x-injected", headers)

    def test_mime_multipart_invalide_refuse_avant_ecriture(self):
        body, ctype = build_multipart(
            filename="bad.txt",
            data=b"hello",
            content_type="text/plain,evil",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 415)
        self.assertEqual(json_of(response)["error"]["code"], "unsupported_media_type")

    def test_mime_syntax_trop_longue_refuse_avant_ecriture(self):
        before = {path.name for path in self.zones_dirs["default"].iterdir()}
        body, ctype = build_multipart(
            filename="long.txt",
            data=b"hello",
            content_type="text/" + "a" * 121,
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 415)
        self.assertEqual(json_of(response)["error"]["code"], "unsupported_media_type")
        self.assertEqual(
            {path.name for path in self.zones_dirs["default"].iterdir()},
            before,
        )

    def test_nom_du_fichier_glisse_et_ecrasement(self):
        def upload(data, replace=False):
            fields = {"preserve_name": "1"}
            if replace:
                fields["replace"] = "1"
            body, ctype = build_multipart(
                filename="rapport final.txt",
                data=data,
                content_type="text/plain",
                extra_fields=fields,
            )
            return self.req(
                "POST",
                "/api/zones/default/images",
                body=body,
                headers={"Content-Type": ctype},
            )

        status, _, first = upload(b"version 1")
        self.assertEqual(status, 201)
        item = json_of(first)
        self.assertEqual(item["filename"], "rapport final.txt")
        self.assertEqual(item["kind"], "text")

        status, _, second = upload(b"version 2")
        self.assertEqual(status, 428)
        self.assertEqual(json_of(second)["error"]["code"], "replacement_required")

        status, _, second = upload(b"version 2", replace=True)
        self.assertEqual(status, 201)
        replacement = json_of(second)
        self.assertEqual(replacement["filename"], item["filename"])
        self.assertEqual(replacement["size"], len(b"version 2"))
        self.assertEqual(
            replacement["preview_url"],
            "/previews/default/rapport%20final.txt",
        )

        status, _, listed = self.req("GET", "/api/zones/default/images")
        self.assertEqual(status, 200)
        self.assertEqual([i["filename"] for i in json_of(listed)["images"]], [item["filename"]])
        status, _, data = self.req(
            "GET",
            replacement["preview_url"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, b"version 2")

    def test_nom_glisse_sur_fichier_etranger_conflit_409(self):
        foreign = self.zones_dirs["default"] / "notes.txt"
        foreign.write_bytes(b"foreign")
        body, ctype = build_multipart(
            filename="notes.txt",
            data=b"new content",
            content_type="text/plain",
            extra_fields={"preserve_name": "1", "replace": "1"},
        )
        status, _, resp = self.req("POST", "/api/zones/default/images",
                                   body=body, headers={"Content-Type": ctype})
        self.assertEqual(status, 409)
        self.assertEqual(json_of(resp)["error"]["code"], "storage_conflict")
        self.assertEqual(foreign.read_bytes(), b"foreign")


class TestRejetsUploads(Base):
    """(#4)(#5)(#6)(#7)(#8)."""

    config_kwargs = {"max_upload_size": "4KB"}

    def test_texte_brut_accepte(self):
        status_code, _, body = self.req("POST", "/api/zones/default/images",
                                        body=b"juste du texte",
                                        headers={"Content-Type": "text/plain"})
        self.assertEqual(status_code, 201)
        self.assertEqual(json_of(body)["kind"], "text")

    def test_texte_accepte_et_preview(self):
        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=b"hello world",
                                        headers={"Content-Type": "text/plain"})
        self.assertEqual(status_code, 201)
        item = json_of(resp)
        self.assertEqual(item["kind"], "text")
        self.assertTrue(item["filename"].endswith(".txt"))
        self.assertEqual(item["mime"], "text/plain")
        status_code, headers, data = self.req("GET", item["preview_url"])
        self.assertEqual(status_code, 200)
        self.assertEqual(data, b"hello world")
        self.assertEqual(headers["content-type"], "text/plain")

    def test_markdown_selon_type_declare(self):
        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=b"# Titre",
                                        headers={"Content-Type": "text/markdown"})
        self.assertEqual(status_code, 201)
        self.assertTrue(json_of(resp)["filename"].endswith(".md"))

    def test_binaire_avec_extension_dorigine(self):
        body, ctype = build_multipart(filename="archive.zip", data=b"\x00\x01\x02\x03")
        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=body, headers={"Content-Type": ctype})
        self.assertEqual(status_code, 201)
        item = json_of(resp)
        self.assertEqual(item["kind"], "binary")
        self.assertTrue(item["filename"].endswith(".zip"))
        status_code, headers, data = self.req("GET", item["preview_url"])
        self.assertEqual(status_code, 200)
        self.assertEqual(data, b"\x00\x01\x02\x03")
        self.assertIn("attachment", headers.get("content-disposition", ""))

    def test_corps_vide(self):
        status_code, _, body = self.req("POST", "/api/zones/default/images",
                                        body=b"", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status_code, 400)
        self.assertEqual(json_of(body)["error"]["code"], "empty_upload")

    def test_multipart_champ_image_vide(self):
        body, ctype = build_multipart(data=b"")
        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=body, headers={"Content-Type": ctype})
        self.assertEqual(status_code, 400)
        self.assertEqual(json_of(resp)["error"]["code"], "empty_upload")

    def test_trop_gros(self):
        big = make_png(3, 3) + b"\x00" * (5 * 1024)  # > 4KB configuré
        status_code, _, body = self.req("POST", "/api/zones/default/images",
                                        body=big, headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 413)
        self.assertEqual(json_of(body)["error"]["code"], "too_large")

    def test_content_length_menteur_excessif(self):
        port = self.server.port
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /api/zones/default/images HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Content-Length: {100 * 1024 * 1024}\r\n".encode()
                + b"Content-Type: application/octet-stream\r\n\r\npartial"
            )
            sock.shutdown(socket.SHUT_WR)
            response = sock.recv(4096).decode("latin-1")
        self.assertTrue(response.startswith("HTTP/1.1 413"), response[:60])

    def test_total_en_tetes_trop_grand(self):
        import socket

        port = self.server.port
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"GET /api/health HTTP/1.1\r\n"
                + b"Host: localhost\r\n"
                + b"X-First: " + b"a" * 33_000 + b"\r\n"
                + b"X-Second: " + b"b" * 33_000 + b"\r\n\r\n"
            )
            response = sock.recv(4096).decode("latin-1")
        self.assertTrue(response.startswith("HTTP/1.1 431"), response[:60])

    def test_gif_refuse(self):
        status_code, _, body = self.req("POST", "/api/zones/default/images",
                                        body=b"GIF89a" + b"\x00" * 30,
                                        headers={"Content-Type": "image/gif"})
        self.assertEqual(status_code, 415)

    def test_png_tronque_est_conserve_comme_binaire(self):
        truncated = make_png(8, 8)[:14]
        status_code, _, body = self.req("POST", "/api/zones/default/images",
                                        body=truncated, headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 201)
        item = json_of(body)
        self.assertEqual(item["kind"], "binary")
        self.assertEqual(item["mime"], "application/octet-stream")

    def test_espace_disque_insuffisant(self):
        destination = self.server.service._destinations["default"]
        with mock.patch.object(
            destination._fs,
            "volume_space",
            return_value=VolumeSpace(1000 * 1024, 1 * 1024),
        ):
            status_code, _, body = self.req(
                "POST",
                "/api/zones/default/images",
                body=make_png(),
                headers={"Content-Type": "image/png"},
            )
        self.assertEqual(status_code, 507)
        self.assertEqual(json_of(body)["error"]["code"], "storage_low")

    def test_verrou_destination_inaccessible_reste_une_erreur_json(self):
        lock = self.zones_dirs["default"] / ".pasteberth.lock"
        lock.unlink()
        lock.symlink_to(self.tmp / "outside-lock")
        self.addCleanup(lock.unlink)
        status, headers, body = self.req("GET", "/api/zones/default/images")
        self.assertEqual(status, 500)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json_of(body)["error"]["code"], "destination_error")
        status, headers, body = self.req("GET", "/api/zones")
        self.assertEqual(status, 500)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json_of(body)["error"]["code"], "destination_error")

    def test_verrou_destination_non_regulier_reste_une_erreur_json(self):
        import os

        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO indisponible sous Windows")
        lock = self.zones_dirs["default"] / ".pasteberth.lock"
        lock.unlink()
        os.mkfifo(lock)
        self.addCleanup(lock.unlink)
        status, headers, body = self.req("GET", "/api/zones/default/images")
        self.assertEqual(status, 500)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json_of(body)["error"]["code"], "destination_error")


class TestAcceptFlags(Base):
    """accept_bin / accept_img / accept_doc : refus par kind."""

    config_kwargs = {"accept_bin": "false"}

    def test_binaire_refuse_texte_et_image_acceptes(self):
        body, ctype = build_multipart(filename="archive.zip", data=b"\x00\x01\x02\x03")
        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=body, headers={"Content-Type": ctype})
        self.assertEqual(status_code, 415)
        self.assertEqual(json_of(resp)["error"]["code"], "unsupported_media_type")

        status_code, _, resp = self.req("POST", "/api/zones/default/images",
                                        body=b"hello", headers={"Content-Type": "text/plain"})
        self.assertEqual(status_code, 201)

        body, ctype = build_multipart(data=make_png())
        status_code, _, _ = self.req("POST", "/api/zones/default/images",
                                     body=body, headers={"Content-Type": ctype})
        self.assertEqual(status_code, 201)


class TestZonesEtPreviews(Base):
    """(#9)(#10) zones inconnues, traversées de chemin."""

    def setUp(self):
        super().setUp()
        body, ctype = build_multipart(data=make_png())
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.item = json_of(resp)

    def test_zone_inexistante(self):
        status_code, _, body = self.req("GET", "/api/zones/inconnue/images")
        self.assertEqual(status_code, 404)
        status_code, _, body = self.req("POST", "/api/zones/inconnue/images",
                                        body=make_png(), headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 404)

    def test_erreur_api_avec_chemin_encode_reste_json(self):
        status_code, headers, body = self.req("GET", "/%61pi/zones/inconnue/images")
        self.assertEqual(status_code, 404)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json_of(body)["error"]["code"], "unknown_zone")

    def test_zone_traversal(self):
        for bad in ["..%2Fetc", "..", "default%2f..%2fsecondary", "DEFAULT"]:
            status_code, _, _ = self.req("GET", f"/api/zones/{bad}/images")
            self.assertEqual(status_code, 404, bad)

    def test_preview_inexistante(self):
        status_code, _, _ = self.req("GET", "/previews/default/2026-01-01_00-00-00_000000.png")
        self.assertEqual(status_code, 404)

    def test_preview_traversal(self):
        attempts = [
            "/previews/default/../../etc/passwd",
            "/previews/default/%2e%2e/%2e%2e/etc/passwd",
            "/previews/default/..%2f..%2fconfig.toml",
            f"/previews/default/{self.item['filename']}%00.png",
            "/previews/../pasteberth/__init__.py",
        ]
        for path in attempts:
            status_code, _, _ = self.req("GET", path)
            # 400 (encodage invalide / NUL) ou 404 : jamais 200/303.
            self.assertIn(status_code, (400, 404), path)

    def test_preview_autre_zone_interdite_logique(self):
        # le fichier existe dans default, pas dans secondary
        status_code, _, _ = self.req("GET", f"/previews/secondary/{self.item['filename']}")
        self.assertEqual(status_code, 404)


class TestHistoriqueOrdre(Base):
    """(#11) ordre déterministe ; (#21) référence exacte."""

    def test_ordre_nouveau_premier(self):
        refs = []
        for i in range(3):
            body, ctype = build_multipart(data=make_png(i + 2, 4))
            status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                       headers={"Content-Type": ctype})
            self.assertEqual(status, 201)
            refs.append(json_of(resp)["reference"])
        status, _, resp = self.req("GET", "/api/zones/default/images")
        listed = json_of(resp)["images"]
        self.assertEqual([i["reference"] for i in listed], list(reversed(refs)))

    def test_reference_opencode_exacte(self):
        body, ctype = build_multipart(data=make_png())
        _, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                              headers={"Content-Type": ctype})
        item = json_of(resp)
        path = Path(item["reference"][1:])
        # la référence = préfixe + chemin absolu exact du fichier écrit sur disque
        self.assertEqual(item["reference"], "@" + str(path))
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(), self.zones_dirs["default"].resolve())


class TestReferenceFormatee(Base):
    config_kwargs = {"reference_prefix": "`", "reference_suffix": "`"}

    def test_reference_entouree_de_backquotes(self):
        body, ctype = build_multipart(data=make_png())
        _, _, resp = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        item = json_of(resp)
        self.assertTrue(
            item["reference"].startswith("`" + str(self.zones_dirs["default"]))
        )
        self.assertTrue(item["reference"].endswith(".png`"))


class TestOperationsDeZone(Base):
    def _upload_named(self, filename: str, data: bytes) -> dict:
        body, ctype = build_multipart(
            filename=filename,
            data=data,
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 201)
        return json_of(response)

    def _filename_form(self, *filenames: str) -> tuple[bytes, str]:
        body = urllib.parse.urlencode(
            [("filename", filename) for filename in filenames]
        ).encode("utf-8")
        return body, "application/x-www-form-urlencoded"

    def test_archive_streame_sans_fichier_temporaire(self):
        first = self._upload_named("first.txt", b"first")
        second = self._upload_named("second.txt", b"second")
        body, ctype = self._filename_form(first["filename"], second["filename"])

        status, headers, archive_body = self.req(
            "POST",
            "/api/zones/default/images/archive",
            body=body,
            headers={"Content-Type": ctype},
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["transfer-encoding"], "chunked")
        self.assertEqual(headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(archive_body)) as archive:
            self.assertEqual(archive.namelist(), [first["filename"], second["filename"]])
            self.assertEqual(archive.read(first["filename"]), b"first")
            self.assertEqual(archive.read(second["filename"]), b"second")

    def test_suppression_multiple_rapporte_les_echecs(self):
        first = self._upload_named("first.txt", b"first")
        second = self._upload_named("second.txt", b"second")
        body = json.dumps(
            {"filenames": [first["filename"], second["filename"], "missing.txt"]}
        ).encode("utf-8")

        status, _, response = self.req(
            "POST",
            "/api/zones/default/images/batch-delete",
            body=body,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        result = json_of(response)
        self.assertEqual(result["deleted"], [first["filename"], second["filename"]])
        self.assertEqual(result["failed"][0]["filename"], "missing.txt")

    def test_zone_busy_est_visible_et_non_bloquant(self):
        with self.server.service.zone_operation(
            "default", kind="archive", exclusive=True
        ):
            status, _, response = self.req("GET", "/api/zones")
            self.assertEqual(status, 200)
            zone = next(item for item in json_of(response)["zones"] if item["id"] == "default")
            self.assertTrue(zone["busy"])

            status, headers, response = self.req(
                "GET", "/api/zones/default/images"
            )
            self.assertEqual(status, 423)
            self.assertEqual(headers["retry-after"], "1")
            self.assertEqual(json_of(response)["error"]["code"], "zone_busy")


class TestZipDesactive(Base):
    config_kwargs = {"allow_zip_download": False}

    def test_zip_desactive(self):
        self.assertFalse(self.server.cfg.zones["default"].allow_zip_download)
        body, ctype = build_multipart(
            filename="blocked.txt",
            data=b"blocked",
            content_type="text/plain",
            extra_fields={"preserve_name": "1"},
        )
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype},
        )
        self.assertEqual(status, 201)
        item = json_of(response)
        body = urllib.parse.urlencode([("filename", item["filename"])]).encode("utf-8")
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images/archive",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(response)["error"]["code"], "zip_disabled")


class TestRetentionAPI(Base):
    """(#12)(#13)(#19)(#20) rétention par API et indépendance des zones."""

    def test_suppression_image_par_api(self):
        body, ctype = build_multipart(data=make_png())
        status, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        item = json_of(resp)
        filename = item["filename"]
        self.assertTrue((self.zones_dirs["default"] / filename).exists())

        status, _, resp = self.req("DELETE", f"/api/zones/default/images/{filename}")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(resp)["deleted"], filename)
        self.assertFalse((self.zones_dirs["default"] / filename).exists())
        self.assertFalse((self.zones_dirs["default"] / (filename + ".json")).exists())

        status, _, resp = self.req("GET", "/api/zones/default/images")
        self.assertNotIn(filename, [i["filename"] for i in json_of(resp)["images"]])

    def test_suppression_image_inconnue_404(self):
        status, _, resp = self.req("DELETE", "/api/zones/default/images/2026-01-01_00-00-00_abcdef.png")
        self.assertEqual(status, 404)
        self.assertEqual(json_of(resp)["error"]["code"], "unknown_image")

    def test_depassement_retain_default3(self):
        saved = []
        for i in range(5):
            body, ctype = build_multipart(data=make_png(i + 2, 2))
            _, _, resp = self.req("POST", "/api/zones/default/images", body=body,
                                  headers={"Content-Type": ctype})
            saved.append(json_of(resp))
        status, _, resp = self.req("GET", "/api/zones/default/images")
        images = json_of(resp)["images"]
        self.assertEqual(len(images), 3)
        survivors = {i["filename"] for i in images}
        expected = {i["filename"] for i in saved[-3:]}
        self.assertEqual(survivors, expected)
        for name in {i["filename"] for i in saved[:2]}:
            self.assertFalse((self.zones_dirs["default"] / name).exists())

    def test_independance_zones(self):
        # secondary retain=2, default retain=3 : les flux ne se mélangent jamais.
        for i in range(5):
            zone = ["default", "secondary"][i % 2]
            body, ctype = build_multipart(data=make_png(3, 3))
            self.req("POST", f"/api/zones/{zone}/images", body=body,
                     headers={"Content-Type": ctype})
        counts = {}
        for zone in ("default", "secondary"):
            _, _, resp = self.req("GET", f"/api/zones/{zone}/images")
            counts[zone] = len(json_of(resp)["images"])
        self.assertEqual(counts, {"default": 3, "secondary": 2})

    def test_compteur_zones_endpoint(self):
        body, ctype = build_multipart(data=make_png())
        self.req("POST", "/api/zones/default/images", body=body, headers={"Content-Type": ctype})
        _, _, resp = self.req("GET", "/api/zones")
        overview = json_of(resp)
        self.assertFalse(overview["auth_enabled"]) if not self.auth else None
        by_id = {z["id"]: z for z in overview["zones"]}
        self.assertEqual(by_id["default"]["count"], 1)
        self.assertEqual(by_id["secondary"]["count"], 0)
        self.assertEqual(by_id["default"]["color"], "#304237")
        self.assertNotEqual(by_id["default"]["color"], by_id["secondary"]["color"])


class TestGroupsAPI(Base):
    config_kwargs = {
        "groups": [
            {"name": "All", "selection": "all", "pattern": ["^ignored-.*$"], "show_count": True},
            {"name": "Operational", "selection": "pattern", "pattern": ["^default$", "^missing-.*$"], "show_count": False, "layout": "tab"},
            {"name": "Other", "selection": "other"},
        ]
    }

    def test_groups_et_memberships_zones(self):
        status, _, response = self.req("GET", "/api/groups")
        self.assertEqual(status, 200)
        groups = json_of(response)["groups"]
        self.assertEqual([group["name"] for group in groups], ["All", "Operational", "Other"])
        self.assertEqual(groups[0]["selection"], "all")
        self.assertEqual(groups[1]["selection"], "pattern")
        self.assertEqual(groups[1]["layout"], "tab")
        self.assertEqual(groups[0]["zone_ids"], ["default", "secondary"])
        self.assertEqual(groups[1]["zone_ids"], ["default"])
        self.assertEqual(groups[1]["zone_count"], 1)
        self.assertFalse(groups[1]["show_count"])
        self.assertEqual(groups[2]["zone_ids"], ["secondary"])

        status, _, response = self.req("GET", "/api/zones")
        self.assertEqual(status, 200)
        by_id = {zone["id"]: zone for zone in json_of(response)["zones"]}
        self.assertEqual(by_id["default"]["groups"], ["All", "Operational"])
        self.assertEqual(by_id["secondary"]["groups"], ["All", "Other"])

    def test_groups_ne_lit_pas_les_historiques(self):
        with mock.patch.object(self.server.service, "history", side_effect=AssertionError):
            status, _, response = self.req("GET", "/api/groups")
        self.assertEqual(status, 200)
        self.assertEqual(len(json_of(response)["groups"]), 3)

    def test_overview_ne_lit_qu_un_historique_par_zone(self):
        original = self.server.service.history
        with mock.patch.object(self.server.service, "history", wraps=original) as history:
            status, _, _ = self.req("GET", "/api/zones")
        self.assertEqual(status, 200)
        self.assertEqual(history.call_count, 2)


class TestPersistenceRestart(Base):
    """(#16)(#35) historiques reconstruits après redémarrage."""

    def test_redemarrage_conserve_historique(self):
        refs = []
        for zone in ("default", "secondary"):
            body, ctype = build_multipart(data=make_png(4, 4))
            _, _, resp = self.req("POST", f"/api/zones/{zone}/images", body=body,
                                  headers={"Content-Type": ctype})
            refs.append(((zone, json_of(resp)["reference"])))
        self.server.restart()
        for zone, reference in refs:
            status, _, resp = self.req("GET", f"/api/zones/{zone}/images")
            images = json_of(resp)["images"]
            self.assertEqual(images[0]["reference"], reference)
            # la preview reste servie après restart
            status, headers, data = self.req("GET", images[0]["preview_url"])
            self.assertEqual(status, 200)


class TestOriginCSRF(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {"allowed_hosts": "[]"}

    def _post(self, origin_header=None):
        body, ctype = build_multipart(data=make_png())
        headers = {"Content-Type": ctype}
        if origin_header:
            headers["Origin"] = origin_header
        return self.req("POST", "/api/zones/default/images", body=body, headers=headers)

    def test_origin_etrangere_refusee(self):
        status, _, body = self._post("https://evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(json_of(body)["error"]["code"], "forbidden_origin")

    def test_origin_correcte_acceptee(self):
        status, _, _ = self._post(f"http://127.0.0.1:{self.server.port}")
        self.assertEqual(status, 201)

    def test_hote_ipv6_loopback_normalise(self):
        handler = self.server.httpd.RequestHandlerClass
        self.assertEqual(handler._host_name("::1"), "::1")
        self.assertEqual(handler._host_name("[2001:0db8::1]:443"), "2001:db8::1")
        self.assertEqual(
            handler._normalize_netloc("[2001:0db8::1]:80", "http"),
            "[2001:db8::1]",
        )
        self.assertEqual(
            handler._normalize_netloc("hote:80", "https"),
            "hote:80",
        )
        self.assertEqual(
            handler._normalize_netloc("hote:443", "http"),
            "hote:443",
        )
        self.assertEqual(
            handler._normalize_netloc("hote:443", "https"),
            "hote",
        )
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Host": f"[::1]:{self.server.port}",
                "Origin": f"http://[::1]:{self.server.port}",
                "Content-Type": ctype,
            },
        )
        self.assertEqual(status, 201)

    def test_sans_origin_client_scripte_acceptee(self):
        status, _, _ = self._post(None)
        self.assertEqual(status, 201)

    def test_origin_null_avec_fetch_same_origin_acceptee(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Content-Type": ctype,
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(status, 201)

    def test_login_origin_null_avec_fetch_same_origin_acceptee(self):
        status, _, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=f"password={PASSWORD}".encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(status, 303)

    def test_origin_null_sans_contexte_refusee(self):
        body, ctype = build_multipart(data=make_png())
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={"Content-Type": ctype, "Origin": "null"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(response)["error"]["code"], "forbidden_origin")

    def test_origin_malformee_ou_avec_chemin_refusee_sans_erreur_interne(self):
        for origin in ("http://[invalid", f"http://127.0.0.1:{self.server.port}/path"):
            with self.subTest(origin=origin):
                status, _, body = self._post(origin)
                self.assertEqual(status, 403)
                self.assertEqual(json_of(body)["error"]["code"], "forbidden_origin")

    def test_referer_malforme_refuse_proprement(self):
        body, ctype = build_multipart(data=make_png())
        status, _, response = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Content-Type": ctype,
                "Referer": "http://[invalid",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(response)["error"]["code"], "forbidden_origin")

    def test_referer_etranger_refuse(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req("POST", "/api/zones/default/images", body=body,
                                headers={"Content-Type": ctype,
                                         "Referer": "https://evil.example/page"})
        self.assertEqual(status, 403)

    def test_hote_inconnu_accepte_en_wildcard_si_origine_correspond(self):
        # Feature: allowed_hosts vide = wildcard ; un hôte quelconque est
        # accepté tant que l'Origin matche le Host de la requête.
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Host": "attacker.example",
                "Origin": "http://attacker.example",
                "Content-Type": ctype,
            },
        )
        self.assertEqual(status, 201)


class TestAllowedHosts(Base):
    auth = True
    password = PASSWORD
    config_kwargs = {
        "allowed_hosts": '["pasteberth.example", "127.0.0.1", "2001:0db8::1"]',
    }

    def test_hote_public_configure_accepte_origin_correspondante(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Host": "pasteberth.example",
                "Origin": "http://pasteberth.example",
                "Content-Type": ctype,
            },
        )
        self.assertEqual(status, 201)

    def test_hote_ipv6_noncanonique_est_compare_normalise(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Host": f"[2001:db8::1]:{self.server.port}",
                "Origin": f"http://[2001:db8::1]:{self.server.port}",
                "Content-Type": ctype,
            },
        )
        self.assertEqual(status, 201)

    def test_hote_hors_liste_refuse_meme_avec_origine_correspondante(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req(
            "POST",
            "/api/zones/default/images",
            body=body,
            headers={
                "Host": "attacker.example",
                "Origin": "http://attacker.example",
                "Content-Type": ctype,
            },
        )
        self.assertEqual(status, 403)

    def test_hote_hors_liste_refuse_aussi_les_get(self):
        status, _, body = self.req(
            "GET",
            "/api/health",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json_of(body)["error"]["code"], "forbidden_host")

    def test_hote_hors_liste_refuse_les_methodes_inconnues(self):
        for method in ("HEAD", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                status, _, body = self.req(
                    method,
                    "/api/health",
                    headers={"Host": "attacker.example"},
                )
                self.assertEqual(status, 403)
                if body:
                    self.assertIn(b"forbidden_host", body)

    def test_suffixe_de_controle_dans_la_route_est_refuse(self):
        status, _, _ = self.req(
            "GET",
            "/api/health%0a",
            headers={"Host": "pasteberth.example"},
        )
        self.assertEqual(status, 400)


class TestRequestFraming(Base):
    auth = True
    password = PASSWORD

    @staticmethod
    def _read_all(sock):
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def test_corps_refuse_non_lu_sur_401_et_connexion_fermee(self):
        import socket

        pipelined = b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        request_bytes = (
            b"POST /api/zones/default/images HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + f"Content-Length: {len(pipelined)}\r\n".encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + pipelined
        )
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1").lower()
        self.assertEqual(response.count("http/1.1 401"), 1)
        self.assertNotIn("http/1.1 200", response)
        self.assertIn("connection: close", response)

    def test_content_length_duplique_refuse(self):
        import socket

        request_bytes = (
            b"POST /api/zones/default/images HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1")
        self.assertTrue(response.startswith("HTTP/1.1 400"), response[:60])

    def test_host_duplique_refuse(self):
        import socket

        request_bytes = (
            b"GET /api/health HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Host: attacker.example\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1")
        self.assertTrue(response.startswith("HTTP/1.1 400"), response[:60])

    def test_cible_invalide_refuse_le_corps_et_ferme(self):
        import socket

        pipelined = b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        request_bytes = (
            b"POST /%ff HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            + f"Content-Length: {len(pipelined)}\r\n".encode()
            + b"Content-Type: application/octet-stream\r\n\r\n"
            + pipelined
        )
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1").lower()
        self.assertEqual(response.count("http/1.1 400"), 1)
        self.assertNotIn("http/1.1 200", response)
        self.assertIn("connection: close", response)

    def test_cibles_get_invalides_ferment_la_connexion(self):
        import socket

        pipelined = b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        for target in (b"/%ff", b"/%ZZ", b"/\\bad", b"/\x00"):
            with self.subTest(target=target):
                request_bytes = (
                    b"GET " + target + b" HTTP/1.1\r\n"
                    b"Host: localhost\r\n\r\n"
                    + pipelined
                )
                with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
                    sock.sendall(request_bytes)
                    sock.shutdown(socket.SHUT_WR)
                    response = self._read_all(sock).decode("latin-1").lower()
                self.assertNotIn("http/1.1 200", response)
                self.assertIn("connection: close", response)

    def test_connection_close_dans_une_liste_de_tokens_ferme(self):
        import socket

        request_bytes = (
            b"GET /api/health HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: keep-alive, close\r\n\r\n"
            b"GET /api/health HTTP/1.1\r\n"
            b"Host: localhost\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1").lower()
        self.assertEqual(response.count("http/1.1 200"), 1)
        self.assertIn("connection: close", response)

    def test_transfer_encoding_est_refuse(self):
        import socket

        for headers in (
            b"Transfer-Encoding: chunked\r\n",
            b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
        ):
            with self.subTest(headers=headers):
                request_bytes = (
                    b"POST /api/zones/default/images HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    + headers
                    + b"\r\n"
                )
                with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
                    sock.sendall(request_bytes)
                    sock.shutdown(socket.SHUT_WR)
                    response = self._read_all(sock).decode("latin-1")
                self.assertTrue(response.startswith("HTTP/1.1 400"), response[:60])

    def test_http_10_et_connection_close_restent_fermes(self):
        import socket

        for request_line, extra in (
            (b"GET /api/health HTTP/1.0\r\n", b""),
            (b"GET /api/health HTTP/1.1\r\n", b"Connection: close\r\n"),
        ):
            with self.subTest(request_line=request_line):
                with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
                    sock.sendall(request_line + b"Host: localhost\r\n" + extra + b"\r\n")
                    sock.shutdown(socket.SHUT_WR)
                    response = self._read_all(sock).decode("latin-1").lower()
                self.assertTrue(response.startswith("http/1.1 200"), response[:60])
                self.assertIn("connection: close", response)

    def test_http_09_ne_provoque_pas_erreur_interne(self):
        import socket

        with socket.create_connection(("127.0.0.1", self.server.port), timeout=5) as sock:
            sock.sendall(b"GET /api/health\r\n")
            sock.shutdown(socket.SHUT_WR)
            response = self._read_all(sock).decode("latin-1")
        self.assertEqual(response, '{"ok": true}')


class TestProxysConfiance(Base):
    """(#39)(#40 simulation) X-Forwarded-* seulement depuis un pair de confiance."""

    auth = True
    password = PASSWORD
    config_kwargs = {"trusted_proxies": '["127.0.0.1", "::1"]'}

    def _login(self, extra_headers):
        return request(
            self.server.port, "POST", "/login",
            body=f"password={PASSWORD}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **extra_headers},
        )

    def test_proxy_confie_https_donnes_cookie_secure(self):
        status, headers, _ = self._login({"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 303)
        self.assertIn("Secure", headers["set-cookie"])

    def test_proxy_confie_hsts_present(self):
        request(
            self.server.port, "POST", "/login",
            body=f"password={PASSWORD}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Forwarded-Proto": "https"},
        )
        cookie = login(self.server.port, PASSWORD)
        status, headers, _ = request(
            self.server.port, "GET", "/api/zones",
            headers={"X-Forwarded-Proto": "https"}, cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertIn("max-age=31536000", headers.get("strict-transport-security", ""))

    def test_proxy_non_confie_proto_ignoree(self):
        # trusted_proxies vide -> X-Forwarded-Proto ignoré même s'il est présent.
        self.server.stop()
        cfg_path = write_config(
            self.tmp,
            zones=[{"id": "default", "directory": str(self.zones_dirs["default"])},
                   {"id": "secondary", "directory": str(self.zones_dirs["secondary"])}],
            auth_enabled=True, password=PASSWORD,
            trusted_proxies="[]",
        )
        self.server = LiveServer(cfg_path)
        self.addCleanup(self.server.stop)
        status, headers, _ = self._login({"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 303)
        self.assertNotIn("Secure", headers["set-cookie"])

    def test_xff_confie_pour_rate_limiting(self):
        from pasteberth.auth import LoginRateLimiter

        for i in range(LoginRateLimiter.THRESHOLD):
            status, _, _ = request(
                self.server.port, "POST", "/login",
                body=b"password=faux",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "X-Forwarded-For": "203.0.113.7"},
            )
            self.assertEqual(status, 401)
        status, headers, _ = request(
            self.server.port, "POST", "/login",
            body=b"password=faux",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Forwarded-For": "203.0.113.7"},
        )
        self.assertEqual(status, 429)
        self.assertIn("retry-after", headers)
        # une autre IP cliente n'est pas impactée
        status, _, _ = request(
            self.server.port, "POST", "/login",
            body=b"password=faux",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-Forwarded-For": "203.0.113.8"},
        )
        self.assertEqual(status, 401)

    def test_xff_prend_le_saut_le_plus_proche(self):
        prefix = "198.51.100."
        for _ in range(5):
            status, _, _ = request(
                self.server.port,
                "POST",
                "/login",
                body=b"password=faux",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Forwarded-For": prefix + "99," + prefix + "7",
                },
            )
            self.assertEqual(status, 401)
        status, _, _ = request(
            self.server.port,
            "POST",
            "/login",
            body=b"password=faux",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-For": prefix + "7",
            },
        )
        self.assertEqual(status, 429)

    def test_hsts_sur_redirect_https(self):
        status, headers, _ = request(
            self.server.port,
            "GET",
            "/",
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(status, 303)
        self.assertIn("max-age=31536000", headers["strict-transport-security"])


class TestEnTetesSecurite(Base):
    def test_en_tetes_systematiques(self):
        for path in ("/api/health", "/", "/static/app.js"):
            status, headers, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)
            csp = headers["content-security-policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertEqual(headers["x-frame-options"], "DENY")
            self.assertEqual(headers["referrer-policy"], "no-referrer")


class TestLogsHttp(Base):
    def test_controles_http_echappes_dans_les_logs(self):
        self.assertEqual(
            _safe_log_text("ok\x00\r\n\x1b[2J\x7f\x80"),
            "ok\\x00\\x0d\\x0a\\x1b[2J\\x7f\\x80",
        )
        captured = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        logger = logging.getLogger("pasteberth.http")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            status, _, _ = self.req(
                "POST",
                "/api/zones/default/images",
                headers={"Origin": "http://attacker.example/\x1b[2J"},
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.assertEqual(status, 403)
        joined = "\n".join(captured)
        self.assertNotIn("\x1b", joined)
        self.assertIn("\\x1b[2J", joined)


class TestFuiteSecret(Base):
    """(#30) le mot de passe n'apparaît nulle part : logs, réponses, disque Git."""

    auth = True
    password = "LE-SECRET-ultra-prive-9876"

    def test_secret_absent_des_logs_et_reponses(self):
        captured = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        root = logging.getLogger("pasteberth")
        root.addHandler(handler)
        try:
            request(
                self.server.port, "POST", "/login",
                body=f"password={self.password}".encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            # connexion réussie puis échec volontaire
            request(
                self.server.port, "POST", "/login",
                body=b"password=autre-mauvais",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        finally:
            root.removeHandler(handler)
        joined = "\n".join(captured)
        self.assertNotIn(self.password, joined)
        # ni dans aucune réponse API
        status, _, body = self.req("GET", "/api/zones")
        self.assertNotIn(self.password.encode(), body)
        # hash sur disque uniquement, hors du dépôt
        passwd_file = self.server.cfg.password_file()
        self.assertTrue(passwd_file.is_file())
        content = passwd_file.read_text()
        self.assertNotIn(self.password, content)
        self.assertTrue(content.startswith("scrypt$"))


class TestMethodesInterdites(Base):
    def test_put_delete_options(self):
        for method in ("PUT", "DELETE", "OPTIONS"):
            status, headers, _ = request(self.server.port, method, "/api/zones")
            self.assertIn(status, (404, 405), method)

    def test_racine_inconnue_404(self):
        status, _, _ = self.req("GET", "/nimporte/quoi")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
