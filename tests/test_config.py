"""Tests de configuration : parsing, validation, politique de sécurité."""
import tempfile
import unittest
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

from pasteberth.config import (
    ConfigError,
    build_default_config,
    check_startup_policy,
    default_config_path,
    default_storage_path,
    is_loopback_address,
    load_config,
    prepare_directories,
    parse_size,
    public_path,
    resolve_config_path,
    resolve_group_zone_ids,
)
from pasteberth.platformfs import platform_fs
from pasteberth.server import address_family_for
from tests.helpers import write_config


def make_cfg(tmp: Path, **kwargs):
    return load_config(write_config(tmp, **kwargs))


class TestTailles(unittest.TestCase):
    def test_variantes(self):
        self.assertEqual(parse_size(4096), 4096)
        self.assertEqual(parse_size("20MB"), 20 * 1024 * 1024)
        self.assertEqual(parse_size("512KiB"), 512 * 1024)
        self.assertEqual(parse_size("1GB"), 1024**3)

    def test_invalides(self):
        for bad in [
            "", "abc", "12ZB", "-5MB", "0", "1.2.3MB", True, None,
            float("nan"), float("inf"),
        ]:
            with self.assertRaises(ConfigError):
                parse_size(bad)


class TestLoopback(unittest.TestCase):
    def test_adresses(self):
        self.assertTrue(is_loopback_address("127.0.0.1"))
        self.assertTrue(is_loopback_address("127.8.8.8"))
        self.assertTrue(is_loopback_address("::1"))
        self.assertTrue(is_loopback_address("localhost"))
        self.assertFalse(is_loopback_address("0.0.0.0"))
        self.assertFalse(is_loopback_address("::"))
        self.assertFalse(is_loopback_address("192.168.1.10"))

    def test_famille_ipv6_du_serveur(self):
        import socket

        self.assertEqual(address_family_for("::1"), socket.AF_INET6)
        self.assertEqual(address_family_for("127.0.0.1"), socket.AF_INET)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _zones(self, tmp=None, count=2):
        base = Path(tmp or self.tmp)
        return [
            {"id": f"z{i}", "directory": str(base / f"dir{i}")} for i in range(count)
        ]

    def test_valeurs_defaut(self):
        cfg = make_cfg(self.tmp)
        self.assertEqual(cfg.listen_address, "127.0.0.1")
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.max_upload_bytes, 20 * 1024 * 1024)
        self.assertFalse(cfg.auth.enabled)
        self.assertEqual(cfg.auth.max_sessions, 4096)
        self.assertEqual(cfg.allowed_hosts, ("localhost", "127.0.0.1", "::1"))
        self.assertEqual(cfg.url_prefix, "")
        self.assertEqual(set(cfg.zones), {"default", "secondary"})
        self.assertEqual(cfg.groups, ())
        self.assertEqual(cfg.zones["default"].reference_prefix, "@")
        self.assertEqual(cfg.zones["default"].reference_suffix, "")
        self.assertEqual(cfg.zones["default"].reference_list_prefix, "")
        self.assertEqual(cfg.zones["default"].reference_list_suffix, "")
        self.assertEqual(cfg.zones["default"].reference_separator, ",")
        self.assertTrue(cfg.zones["default"].allow_zip_download)
        self.assertTrue(cfg.zones["default"].create_directory)
        self.assertEqual(cfg.zones["default"].min_free_percent, 2.0)

    def test_format_reference_configurable(self):
        cfg = load_config(
            write_config(
                self.tmp,
                zones=[{
                    "id": "quoted",
                    "directory": str(self.tmp / "quoted"),
                    "reference_prefix": "`",
                    "reference_suffix": "`",
                }],
            )
        )
        zone = cfg.zones["quoted"]
        self.assertEqual(zone.reference_prefix, "`")
        self.assertEqual(zone.reference_suffix, "`")

    def test_format_reference_prefix_vide(self):
        cfg = load_config(
            write_config(
                self.tmp,
                zones=[{
                    "id": "plain",
                    "directory": str(self.tmp / "plain"),
                    "reference_prefix": "",
                }],
            )
        )
        self.assertEqual(cfg.zones["plain"].reference_prefix, "")

    def test_format_liste_et_zip_configurables(self):
        cfg = load_config(
            write_config(
                self.tmp,
                zones=[{
                    "id": "custom",
                    "directory": str(self.tmp / "custom"),
                    "reference_list_prefix": "[",
                    "reference_list_suffix": "]",
                    "reference_separator": "; ",
                    "allow_zip_download": False,
                }],
            )
        )
        zone = cfg.zones["custom"]
        self.assertEqual(zone.reference_list_prefix, "[")
        self.assertEqual(zone.reference_list_suffix, "]")
        self.assertEqual(zone.reference_separator, "; ")
        self.assertFalse(zone.allow_zip_download)

    def test_allowed_hosts(self):
        cfg = make_cfg(
            self.tmp,
            allowed_hosts='["pasteberth.example", "[::1]", "2001:0db8::1"]',
        )
        self.assertEqual(cfg.allowed_hosts, ("pasteberth.example", "::1", "2001:db8::1"))

        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, allowed_hosts='["https://pasteberth.example"]')

    def test_allowed_hosts_vide_active_le_wildcard(self):
        cfg = make_cfg(self.tmp, auth_enabled=True, allowed_hosts="[]")
        self.assertEqual(cfg.allowed_hosts, ())

    def test_allowed_hosts_vide_refuse_sans_auth(self):
        with self.assertRaisesRegex(ConfigError, "allowed_hosts"):
            make_cfg(
                self.tmp,
                auth_enabled=False,
                allowed_hosts="[]",
                allow_unauthenticated_local=True,
            )

    def test_url_prefix_valide_et_invalide(self):
        for value in ("/paste", "/tools/paste"):
            with self.subTest(value=value):
                cfg = make_cfg(self.tmp, url_prefix=value)
                self.assertEqual(cfg.url_prefix, value)
        for value in ("/", "/paste/", "paste", "/paste//x", "/paste/../x",
                      "/paste?x=1", "/paste#fragment", "/paste%2Fx"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    make_cfg(self.tmp, url_prefix=value)

    def test_public_path_ne_double_pas_le_prefixe(self):
        self.assertEqual(public_path("", "/api/health"), "/api/health")
        self.assertEqual(public_path("/paste", "/"), "/paste/")
        self.assertEqual(public_path("/paste", "/api/health"), "/paste/api/health")
        self.assertEqual(public_path("/paste", "/paste/api/health"), "/paste/api/health")
        with self.assertRaises(ValueError):
            public_path("/paste", "api/health")

    def test_defauts_reels_auth_et_proxy(self):
        path = self.tmp / "minimal.toml"
        path.write_text(
            f'listen_address = "127.0.0.1"\n'
            f'[[zones]]\n'
            f'id = "one"\n'
            f'directory = {json.dumps(str(self.tmp / "one"))}\n',
            encoding="utf-8",
        )
        cfg = load_config(path)
        self.assertTrue(cfg.auth.enabled)
        self.assertEqual(cfg.trusted_proxies, ())

    def test_fichier_passwd_configurable(self):
        password_file = self.tmp / "secrets" / "passwd"
        cfg = make_cfg(
            self.tmp,
            auth_enabled=True,
            password_file=str(password_file),
        )
        self.assertEqual(cfg.auth.password_file, password_file)
        self.assertEqual(cfg.password_file(), password_file)

    def test_max_sessions_configurable_et_borne(self):
        cfg = make_cfg(self.tmp, max_sessions=12)
        self.assertEqual(cfg.auth.max_sessions, 12)
        for value in (0, -1, 1_000_001, True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    make_cfg(self.tmp, max_sessions=value)

    def test_fichier_passwd_doit_etre_absolu(self):
        with self.assertRaisesRegex(ConfigError, "password_file"):
            make_cfg(self.tmp, password_file="secrets/passwd")

    def test_fichier_passwd_nul_refuse(self):
        with self.assertRaisesRegex(ConfigError, "NUL"):
            make_cfg(self.tmp, password_file=str(self.tmp / "passwd\x00bad"))

    def test_chemin_config_nul_retourne_une_erreur(self):
        with self.assertRaises(ConfigError):
            load_config(Path(str(self.tmp / "config.toml") + "\x00bad"))

    def test_zones_chargees(self):
        zones = [
            {"id": "default", "label": "Default", "retain": 7, "color": "#304237",
             "directory": str(self.tmp / "p")},
            {"id": "secondary", "directory": str(self.tmp / "l")},
        ]
        cfg = make_cfg(self.tmp, zones=zones)
        self.assertEqual(set(cfg.zones), {"default", "secondary"})
        self.assertEqual(cfg.zones["default"].retain, 7)
        self.assertEqual(cfg.zones["default"].color, "#304237")
        self.assertEqual(cfg.zones["secondary"].label, "secondary")

    def test_groupes_charges_et_valides(self):
        cfg = make_cfg(
            self.tmp,
            groups=[
                {"name": "All", "pattern": [".*"]},
                {"name": "Ops", "pattern": ["^default$", "^api-.*"]},
            ],
        )
        self.assertEqual([group.name for group in cfg.groups], ["All", "Ops"])
        self.assertEqual(cfg.groups[0].selection, "pattern")
        self.assertEqual(cfg.groups[1].pattern, ("^default$", "^api-.*"))
        self.assertEqual(cfg.groups[1].layout, "area")
        self.assertFalse(cfg.groups[0].hide_empty)
        self.assertTrue(cfg.groups[0].show_count)

    def test_selections_all_pattern_other_et_layout(self):
        zones = [
            {"id": "lightwebpres-api", "directory": str(self.tmp / "lwp")},
            {"id": "api", "directory": str(self.tmp / "api")},
            {"id": "misc", "directory": str(self.tmp / "misc")},
        ]
        cfg = make_cfg(
            self.tmp,
            zones=zones,
            groups=[
                {"name": "All", "selection": "all", "pattern": ["^ignored-.*$"], "layout": "tab"},
                {"name": "LWP", "selection": "pattern", "pattern": ["^lightwebpres.*$"]},
                {"name": "Other", "selection": "other"},
            ],
        )
        self.assertEqual(cfg.groups[0].layout, "tab")
        self.assertTrue(cfg.groups[0].pattern_defined)
        memberships = resolve_group_zone_ids(cfg.groups, cfg.zones)
        self.assertEqual(memberships["All"], tuple(cfg.zones))
        self.assertEqual(memberships["LWP"], ("lightwebpres-api",))
        self.assertEqual(memberships["Other"], ("api", "misc"))

    def test_selection_et_layout_invalides(self):
        with self.assertRaisesRegex(ConfigError, "selection"):
            make_cfg(self.tmp, groups=[{"name": "Bad", "selection": "never"}])
        with self.assertRaisesRegex(ConfigError, "layout"):
            make_cfg(self.tmp, groups=[{"name": "Bad", "pattern": [".*"], "layout": "stack"}])
        with self.assertRaisesRegex(ConfigError, "pattern"):
            make_cfg(self.tmp, groups=[{"name": "Bad", "selection": "pattern"}])
        with self.assertRaisesRegex(ConfigError, "expression régulière"):
            make_cfg(self.tmp, groups=[{"name": "Bad", "pattern": ["["]}])

    def test_groupes_refusent_les_noms_dupliques_et_patterns_vides(self):
        with self.assertRaisesRegex(ConfigError, "dupliqué"):
            make_cfg(
                self.tmp,
                groups=[
                    {"name": "Ops", "pattern": ["default"]},
                    {"name": "Ops", "pattern": ["secondary"]},
                ],
            )
        with self.assertRaisesRegex(ConfigError, "ne peut pas être vide"):
            make_cfg(self.tmp, groups=[{"name": "Empty", "pattern": []}])

    def test_cle_inconnue_avertissement(self):
        cfg = load_config(
            write_config(self.tmp, extra='mauvaise_cle = "valeur"\n')
        )
        self.assertTrue(any("mauvaise_cle" in w for w in cfg.warnings))

    def test_faute_groupes_suggere_la_cle_anglaise(self):
        path = write_config(self.tmp)
        path.write_text(
            path.read_text().replace("\n[auth]\n", "\ngroupes = []\n\n[auth]\n", 1),
            encoding="utf-8",
        )
        cfg = load_config(path)
        self.assertTrue(any("groupes" in w and "groups" in w for w in cfg.warnings))

    def test_auth_hash_dans_config_averti_et_ignore(self):
        path = write_config(self.tmp, auth_enabled=True)
        text = path.read_text()
        # Injecte password_hash dans la section [auth] (juste avant [[zones]]).
        path.write_text(
            text.replace(
                "\n[[zones]]",
                '\npassword_hash = "scrypt$16384$8$1$c2FsdA==$aGFzaA=="\n\n[[zones]]',
                1,
            )
        )
        cfg = load_config(path)
        self.assertTrue(any("password_hash" in w for w in cfg.warnings))

    def test_erreurs_zone(self):
        cases = [
            {"id": "Mauvais", "directory": "/tmp/x"},          # id invalide
            {"id": "ok", "directory": "relatif/path"},         # chemin relatif
            {"id": "ok2", "directory": "/tmp/x", "color": "red"},
            {"id": "ok5", "directory": "/tmp/x", "color": "#777777"},
            {"id": "ok3", "directory": "/tmp/x", "retain": 0},
            {"id": "ok4", "directory": "/tmp/x", "type": "ssh"},
        ]
        for zone in cases:
            with self.subTest(zone=zone):
                with self.assertRaises(ConfigError):
                    make_cfg(self.tmp, zones=[zone, self._zones()[0]])

    def test_id_duplique(self):
        z = {"id": "dup", "directory": str(self.tmp / "a")}
        with self.assertRaises(ConfigError) as ctx:
            make_cfg(self.tmp, zones=[z, dict(z)])
        self.assertIn("dupliqué", str(ctx.exception))

    def test_port_invalide(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, port=99999)

    def test_upload_trop_grand(self):
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, max_upload_size="51MiB")

    def test_budget_pixels_borne(self):
        cfg = make_cfg(self.tmp, max_image_pixels=50_000_000)
        self.assertEqual(cfg.max_image_pixels, 50_000_000)
        with self.assertRaises(ConfigError):
            make_cfg(self.tmp, max_image_pixels=50_000_001)

    def test_seuil_espace_invalide(self):
        with self.assertRaises(ConfigError):
            make_cfg(
                self.tmp,
                zones=[{"id": "x", "directory": str(self.tmp / "x"),
                        "min_free_percent": 100}],
            )


class TestPolitiqueSecurite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_refus_non_loopback_sans_auth(self):
        with self.assertRaises(ConfigError) as ctx:
            make_cfg(self.tmp, listen_address="0.0.0.0", auth_enabled=False)
        self.assertIn("refus de démarrer", str(ctx.exception))

    def test_accepte_loopback_sans_auth(self):
        cfg = make_cfg(self.tmp, listen_address="127.0.0.1", auth_enabled=False)
        check_startup_policy(cfg)

    def test_refuse_loopback_sans_optin(self):
        with self.assertRaises(ConfigError):
            make_cfg(
                self.tmp,
                auth_enabled=False,
                allow_unauthenticated_local=None,
            )

    def test_accepte_remote_avec_auth(self):
        cfg = make_cfg(
            self.tmp,
            listen_address="0.0.0.0",
            auth_enabled=True,
            allow_insecure_http_remote=True,
        )
        check_startup_policy(cfg)

    def test_refuse_remote_http_sans_optin(self):
        with self.assertRaisesRegex(ConfigError, "HTTP"):
            make_cfg(self.tmp, listen_address="0.0.0.0", auth_enabled=True)

    def test_accepte_remote_avec_tls(self):
        cfg = make_cfg(
            self.tmp,
            listen_address="0.0.0.0",
            auth_enabled=True,
            tls_enabled=True,
            tls_certificate=str(self.tmp / "pasteberth-cert.pem"),
            tls_private_key=str(self.tmp / "pasteberth-key.pem"),
        )
        self.assertTrue(cfg.tls.enabled)
        check_startup_policy(cfg)

    def test_tls_active_exige_certificat_et_cle(self):
        with self.assertRaisesRegex(ConfigError, "certificate"):
            make_cfg(self.tmp, tls_enabled=True)

    def test_override_explicite(self):
        cfg = make_cfg(
            self.tmp,
            listen_address="0.0.0.0",
            auth_enabled=False,
            allow_unauthenticated_remote=True,
            allow_insecure_http_remote=True,
        )
        check_startup_policy(cfg)


class TestChemins(unittest.TestCase):
    def test_xdg_par_defaut(self):
        self.assertIn("pasteberth", str(default_config_path()))
        self.assertIn(".config", str(default_config_path()))

    def test_resolution_priorite_env(self):
        import os

        old = os.environ.get("PASTEBERTH_CONFIG")
        try:
            configured = str(Path.cwd() / "autre.toml")
            explicit = str(Path.cwd() / "explicit.toml")
            os.environ["PASTEBERTH_CONFIG"] = configured
            self.assertEqual(str(resolve_config_path()), configured)
            self.assertEqual(str(resolve_config_path(explicit)), explicit)
        finally:
            if old is None:
                del os.environ["PASTEBERTH_CONFIG"]
            else:
                os.environ["PASTEBERTH_CONFIG"] = old

    def test_configuration_integree_du_depot(self):
        cfg = build_default_config()
        self.assertTrue(cfg.using_default_config)
        self.assertFalse(cfg.auth.enabled)
        self.assertTrue(cfg.allow_unauthenticated_local)
        self.assertEqual(cfg.allowed_hosts, ("localhost", "127.0.0.1", "::1"))
        self.assertEqual(cfg.url_prefix, "")
        self.assertEqual(set(cfg.zones), {"default"})
        self.assertEqual(cfg.zones["default"].directory, default_storage_path())


class TestRepertoires(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_zones_meme_repertoire_refusees(self):
        shared = self.tmp / "shared"
        cfg = load_config(
            write_config(
                self.tmp,
                zones=[
                    {"id": "a", "directory": str(shared)},
                    {"id": "b", "directory": str(shared)},
                ],
            )
        )
        with self.assertRaises(ConfigError):
            prepare_directories(cfg)

    def test_repertoire_permissions_non_privees_avertissent(self):
        target = self.tmp / "open"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        before_audit = (
            platform_fs().audit_permissions(target, directory=True)
            if platform_fs().backend_name == "windows"
            else None
        )
        cfg = load_config(
            write_config(self.tmp, zones=[{"id": "x", "directory": str(target)}])
        )
        prepare_directories(cfg)
        if platform_fs().backend_name == "windows":
            self.assertEqual(before_audit, platform_fs().audit_permissions(target, directory=True))
        else:
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_repertoire_permissions_ecriture_non_privees_avertissent(self):
        target = self.tmp / "group-writable"
        target.mkdir(mode=0o775)
        os.chmod(target, 0o775)
        before_audit = (
            platform_fs().audit_permissions(target, directory=True)
            if platform_fs().backend_name == "windows"
            else None
        )
        cfg = load_config(
            write_config(self.tmp, zones=[{"id": "x", "directory": str(target)}])
        )
        prepare_directories(cfg)
        if platform_fs().backend_name == "windows":
            self.assertEqual(before_audit, platform_fs().audit_permissions(target, directory=True))
        else:
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o775)

    def test_lien_symbolique_parent_refuse(self):
        target = self.tmp / "outside"
        target.mkdir(mode=0o700)
        link = self.tmp / "link"
        link.symlink_to(target, target_is_directory=True)
        cfg = load_config(
            write_config(self.tmp, zones=[{"id": "x", "directory": str(link / "images")}])
        )
        with self.assertRaisesRegex(ConfigError, "lien symbolique"):
            prepare_directories(cfg)

    def test_chemin_nul_retourne_une_erreur_de_configuration(self):
        cfg = make_cfg(self.tmp)
        zone = replace(cfg.zones["default"], directory=Path(str(self.tmp / "bad") + "\x00zone"))
        cfg = replace(cfg, zones={"default": zone})
        with self.assertRaises(ConfigError):
            prepare_directories(cfg)


if __name__ == "__main__":
    unittest.main()
