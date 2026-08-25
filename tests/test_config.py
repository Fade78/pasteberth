"""Tests de configuration : parsing, validation, politique de sécurité."""
import tempfile
import unittest
import os
import stat
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
    resolve_config_path,
)
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
        for bad in ["", "abc", "12ZB", "-5MB", "0", "1.2.3MB", True, None]:
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
        self.assertEqual(set(cfg.zones), {"default", "secondary"})
        self.assertEqual(cfg.zones["default"].reference_prefix, "@")
        self.assertEqual(cfg.zones["default"].reference_suffix, "")
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

    def test_defauts_reels_auth_et_proxy(self):
        path = self.tmp / "minimal.toml"
        path.write_text(
            f'listen_address = "127.0.0.1"\n'
            f'[[zones]]\n'
            f'id = "one"\n'
            f'directory = "{self.tmp / "one"}"\n',
            encoding="utf-8",
        )
        cfg = load_config(path)
        self.assertTrue(cfg.auth.enabled)
        self.assertEqual(str(cfg.trusted_proxies[0]), "127.0.0.1/32")
        self.assertEqual(str(cfg.trusted_proxies[1]), "::1/128")

    def test_fichier_passwd_configurable(self):
        password_file = self.tmp / "secrets" / "passwd"
        cfg = make_cfg(
            self.tmp,
            auth_enabled=True,
            password_file=str(password_file),
        )
        self.assertEqual(cfg.auth.password_file, password_file)
        self.assertEqual(cfg.password_file(), password_file)

    def test_fichier_passwd_doit_etre_absolu(self):
        with self.assertRaisesRegex(ConfigError, "password_file"):
            make_cfg(self.tmp, password_file="secrets/passwd")

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

    def test_cle_inconnue_avertissement(self):
        cfg = load_config(
            write_config(self.tmp, extra='mauvaise_cle = "valeur"\n')
        )
        self.assertTrue(any("mauvaise_cle" in w for w in cfg.warnings))

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
        z = {"id": "dup", "directory": "/tmp/a"}
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
            tls_certificate="/tmp/pasteberth-cert.pem",
            tls_private_key="/tmp/pasteberth-key.pem",
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
            os.environ["PASTEBERTH_CONFIG"] = "/tmp/autre.toml"
            self.assertEqual(str(resolve_config_path()), "/tmp/autre.toml")
            self.assertEqual(str(resolve_config_path("/tmp/explicit.toml")), "/tmp/explicit.toml")
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
        cfg = load_config(
            write_config(self.tmp, zones=[{"id": "x", "directory": str(target)}])
        )
        prepare_directories(cfg)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

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


if __name__ == "__main__":
    unittest.main()
