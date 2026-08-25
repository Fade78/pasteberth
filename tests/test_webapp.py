"""Tests d'intégration HTTP sur serveur réel (socket) :
auth, uploads, previews, CSRF/Origin, proxys, en-têtes, fuites."""
import json
import logging
import re
import tempfile
import time
import unittest
from pathlib import Path

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
            "pulse": tmp / "Pulse" / ".opencode-images",
            "lwp": tmp / "LWP" / ".opencode-images",
        }
        zones = [
            {"id": zid, "label": zid.upper(), "retain": retain,
             "color": color, "directory": str(path)}
            for zid, path, retain, color in [
                ("pulse", self.zones_dirs["pulse"], 3, "#304237"),
                ("lwp", self.zones_dirs["lwp"], 2, "#26394a"),
            ]
        ]
        cfg_path = write_config(
            tmp,
            zones=zones,
            auth_enabled=self.auth,
            password=self.password,
            max_upload_size=self.config_kwargs.get("max_upload_size", "20MB"),
            trusted_proxies=self.config_kwargs.get("trusted_proxies", '["127.0.0.1", "::1"]'),
        )
        self.tmp = tmp
        self.server = LiveServer(cfg_path)
        self.addCleanup(self.server.stop)
        self.addCleanup(self._tmp.cleanup)
        if self.auth:
            # login avec le mot de passe défini par la classe de test
            status, headers, _ = request(
                self.server.port, "POST", "/login",
                body=f"password={self.password}".encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert status == 303, f"login échoué : {status}"
            self.cookie = headers["set-cookie"].split(";", 1)[0]
        else:
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


class TestPublic(Base):
    def test_health_sans_auth(self):
        status, _, body = self.req("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json_of(body), {"ok": True})

    def test_static_assets(self):
        for path, ctype in [("/static/app.js", "text/javascript"),
                            ("/static/style.css", "text/css"),
                            ("/static/favicon.svg", "image/svg+xml")]:
            status, headers, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["content-type"].startswith(ctype), path)


class TestModeAnonymeLoopback(Base):
    auth = False

    def test_index_servi(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", body)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_flux_complet_anonyme(self):
        png = make_png(10, 5)
        body, ctype = build_multipart(data=png)
        status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        item = json_of(resp)
        ref = item["reference"]
        self.assertTrue(ref.startswith("@"))
        self.assertEqual(ref[1:], str(self.zones_dirs["pulse"] / item["filename"]))
        # preview
        status, headers, data = self.req("GET", item["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(data, png)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "private, max-age=3600")


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

    def test_upload_401(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req("POST", "/api/zones/pulse/images", body=body,
                                headers={"Content-Type": ctype}, cookie=None)
        self.assertEqual(status, 401)

    def test_preview_401(self):
        body, ctype = build_multipart(data=make_png())
        status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        url = json_of(resp)["preview_url"]
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
        self.assertIn("Mot de passe incorrect", body.decode())
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

    def test_cookie_forge_rejete(self):
        forged = "pb_session=AQAAANBBB_forged-token-value"
        status, _, _ = self.req("GET", "/api/zones", cookie=forged)
        self.assertEqual(status, 401)


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
        status, item = self._upload_raw("pulse", png, "image/png")
        self.assertEqual(status, 201)
        self.assertEqual((item["width"], item["height"]), (1920, 1080))
        self.assertEqual(item["format"], "png")
        self.assertTrue(item["filename"].endswith(".png"))
        self.assertEqual(item["size"], len(png))
        self.assertRegex(item["filename"], FILENAME_RE)

    def test_jpeg(self):
        jpg = make_jpeg(800, 600)
        status, item = self._upload_raw("pulse", jpg, "image/jpeg")
        self.assertEqual(status, 201)
        self.assertEqual(item["format"], "jpeg")
        self.assertTrue(item["filename"].endswith(".jpg"))

    def test_webp(self):
        webp = make_webp_lossy(640, 480)
        status, item = self._upload_raw("pulse", webp, "image/webp")
        self.assertEqual(status, 201)
        self.assertEqual(item["format"], "webp")
        self.assertTrue(item["filename"].endswith(".webp"))

    def test_mime_mensonger_contenu_jpeg(self):
        # Le navigateur déclare image/png mais le contenu est JPEG : le contenu gagne.
        jpg = make_jpeg(50, 40)
        body, ctype = build_multipart(data=jpg, content_type="image/png")
        status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        item = json_of(resp)
        self.assertEqual(item["format"], "jpeg")
        self.assertTrue(item["filename"].endswith(".jpg"))

    def test_nom_client_ignore(self):
        body, ctype = build_multipart(filename="../../etc/passwd.png", data=make_png())
        status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                   headers={"Content-Type": ctype})
        self.assertEqual(status, 201)
        self.assertRegex(json_of(resp)["filename"], FILENAME_RE)


class TestRejetsUploads(Base):
    """(#4)(#5)(#6)(#7)(#8)."""

    config_kwargs = {"max_upload_size": "4KB"}

    def test_texte_brut_refuse(self):
        status, err = None, None
        status_code, _, body = self.req("POST", "/api/zones/pulse/images",
                                        body=b"juste du texte",
                                        headers={"Content-Type": "text/plain"})
        self.assertEqual(status_code, 415)

    def test_corps_vide(self):
        status_code, _, body = self.req("POST", "/api/zones/pulse/images",
                                        body=b"", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status_code, 400)
        self.assertEqual(json_of(body)["error"]["code"], "empty_upload")

    def test_multipart_champ_image_vide(self):
        body, ctype = build_multipart(data=b"")
        status_code, _, resp = self.req("POST", "/api/zones/pulse/images",
                                        body=body, headers={"Content-Type": ctype})
        self.assertEqual(status_code, 400)
        self.assertEqual(json_of(resp)["error"]["code"], "empty_upload")

    def test_trop_gros(self):
        big = make_png(3, 3) + b"\x00" * (5 * 1024)  # > 4KB configuré
        status_code, _, body = self.req("POST", "/api/zones/pulse/images",
                                        body=big, headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 413)
        self.assertEqual(json_of(body)["error"]["code"], "too_large")

    def test_content_length_menteur_excessif(self):
        port = self.server.port
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                b"POST /api/zones/pulse/images HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Content-Length: {100 * 1024 * 1024}\r\n".encode()
                + b"Content-Type: application/octet-stream\r\n\r\npartial"
            )
            sock.shutdown(socket.SHUT_WR)
            response = sock.recv(4096).decode("latin-1")
        self.assertTrue(response.startswith("HTTP/1.1 413"), response[:60])

    def test_gif_refuse(self):
        status_code, _, body = self.req("POST", "/api/zones/pulse/images",
                                        body=b"GIF89a" + b"\x00" * 30,
                                        headers={"Content-Type": "image/gif"})
        self.assertEqual(status_code, 415)

    def test_png_tronque_refuse(self):
        truncated = make_png(8, 8)[:14]
        status_code, _, body = self.req("POST", "/api/zones/pulse/images",
                                        body=truncated, headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 400)
        self.assertEqual(json_of(body)["error"]["code"], "invalid_image")


class TestZonesEtPreviews(Base):
    """(#9)(#10) zones inconnues, traversées de chemin."""

    def setUp(self):
        super().setUp()
        body, ctype = build_multipart(data=make_png())
        status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                   headers={"Content-Type": ctype})
        self.item = json_of(resp)

    def test_zone_inexistante(self):
        status_code, _, body = self.req("GET", "/api/zones/inconnue/images")
        self.assertEqual(status_code, 404)
        status_code, _, body = self.req("POST", "/api/zones/inconnue/images",
                                        body=make_png(), headers={"Content-Type": "image/png"})
        self.assertEqual(status_code, 404)

    def test_zone_traversal(self):
        for bad in ["..%2Fetc", "..", "pulse%2f..%2flwp", "PULSE"]:
            status_code, _, _ = self.req("GET", f"/api/zones/{bad}/images")
            self.assertEqual(status_code, 404, bad)

    def test_preview_inexistante(self):
        status_code, _, _ = self.req("GET", "/previews/pulse/2026-01-01_00-00-00_000000.png")
        self.assertEqual(status_code, 404)

    def test_preview_traversal(self):
        attempts = [
            "/previews/pulse/../../etc/passwd",
            "/previews/pulse/%2e%2e/%2e%2e/etc/passwd",
            "/previews/pulse/..%2f..%2fconfig.toml",
            f"/previews/pulse/{self.item['filename']}%00.png",
            "/previews/../pasteberth/__init__.py",
        ]
        for path in attempts:
            status_code, _, _ = self.req("GET", path)
            # 400 (encodage invalide / NUL) ou 404 : jamais 200/303.
            self.assertIn(status_code, (400, 404), path)

    def test_preview_autre_zone_interdite_logique(self):
        # le fichier existe dans pulse, pas dans lwp
        status_code, _, _ = self.req("GET", f"/previews/lwp/{self.item['filename']}")
        self.assertEqual(status_code, 404)


class TestHistoriqueOrdre(Base):
    """(#11) ordre déterministe ; (#21) référence exacte."""

    def test_ordre_nouveau_premier(self):
        refs = []
        for i in range(3):
            body, ctype = build_multipart(data=make_png(i + 2, 4))
            status, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                       headers={"Content-Type": ctype})
            self.assertEqual(status, 201)
            refs.append(json_of(resp)["reference"])
        status, _, resp = self.req("GET", "/api/zones/pulse/images")
        listed = json_of(resp)["images"]
        self.assertEqual([i["reference"] for i in listed], list(reversed(refs)))

    def test_reference_opencode_exacte(self):
        body, ctype = build_multipart(data=make_png())
        _, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                              headers={"Content-Type": ctype})
        item = json_of(resp)
        path = Path(item["reference"][1:])
        # la référence = préfixe + chemin absolu exact du fichier écrit sur disque
        self.assertEqual(item["reference"], "@" + str(path))
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.resolve(), self.zones_dirs["pulse"].resolve())


class TestRetentionAPI(Base):
    """(#12)(#13)(#19)(#20) rétention par API et indépendance des zones."""

    def test_depassement_retain_pulse3(self):
        saved = []
        for i in range(5):
            body, ctype = build_multipart(data=make_png(i + 2, 2))
            _, _, resp = self.req("POST", "/api/zones/pulse/images", body=body,
                                  headers={"Content-Type": ctype})
            saved.append(json_of(resp))
        status, _, resp = self.req("GET", "/api/zones/pulse/images")
        images = json_of(resp)["images"]
        self.assertEqual(len(images), 3)
        survivors = {i["filename"] for i in images}
        expected = {i["filename"] for i in saved[-3:]}
        self.assertEqual(survivors, expected)
        for name in {i["filename"] for i in saved[:2]}:
            self.assertFalse((self.zones_dirs["pulse"] / name).exists())

    def test_independance_zones(self):
        # lwp retain=2, pulse retain=3 : les flux ne se mélangent jamais.
        for i in range(5):
            zone = ["pulse", "lwp"][i % 2]
            body, ctype = build_multipart(data=make_png(3, 3))
            self.req("POST", f"/api/zones/{zone}/images", body=body,
                     headers={"Content-Type": ctype})
        counts = {}
        for zone in ("pulse", "lwp"):
            _, _, resp = self.req("GET", f"/api/zones/{zone}/images")
            counts[zone] = len(json_of(resp)["images"])
        self.assertEqual(counts, {"pulse": 3, "lwp": 2})

    def test_compteur_zones_endpoint(self):
        body, ctype = build_multipart(data=make_png())
        self.req("POST", "/api/zones/pulse/images", body=body, headers={"Content-Type": ctype})
        _, _, resp = self.req("GET", "/api/zones")
        overview = json_of(resp)
        self.assertFalse(overview["auth_enabled"]) if not self.auth else None
        by_id = {z["id"]: z for z in overview["zones"]}
        self.assertEqual(by_id["pulse"]["count"], 1)
        self.assertEqual(by_id["lwp"]["count"], 0)
        self.assertEqual(by_id["pulse"]["color"], "#304237")
        self.assertNotEqual(by_id["pulse"]["color"], by_id["lwp"]["color"])


class TestPersistenceRestart(Base):
    """(#16)(#35) historiques reconstruits après redémarrage."""

    def test_redemarrage_conserve_historique(self):
        refs = []
        for zone in ("pulse", "lwp"):
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

    def _post(self, origin_header=None):
        body, ctype = build_multipart(data=make_png())
        headers = {"Content-Type": ctype}
        if origin_header:
            headers["Origin"] = origin_header
        return self.req("POST", "/api/zones/pulse/images", body=body, headers=headers)

    def test_origin_etrangere_refusee(self):
        status, _, body = self._post("https://evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(json_of(body)["error"]["code"], "forbidden_origin")

    def test_origin_correcte_acceptee(self):
        status, _, _ = self._post(f"http://127.0.0.1:{self.server.port}")
        self.assertEqual(status, 201)

    def test_sans_origin_client_scripte_acceptee(self):
        status, _, _ = self._post(None)
        self.assertEqual(status, 201)

    def test_referer_etranger_refuse(self):
        body, ctype = build_multipart(data=make_png())
        status, _, _ = self.req("POST", "/api/zones/pulse/images", body=body,
                                headers={"Content-Type": ctype,
                                         "Referer": "https://evil.example/page"})
        self.assertEqual(status, 403)


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
            zones=[{"id": "pulse", "directory": str(self.zones_dirs["pulse"])},
                   {"id": "lwp", "directory": str(self.zones_dirs["lwp"])}],
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
