"""Tests de configuration : parsing, validation, politique de sécurité."""
import tempfile
import unittest
from pathlib import Path

from pasteberth.config import (
    ConfigError,
    check_startup_policy,
    default_config_path,
    is_loopback_address,
    load_config,
    parse_size,
    resolve_config_path,
)
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
        self.assertEqual(cfg.zones["z0"].reference_prefix, "@")
        self.assertTrue(cfg.zones["z0"].create_directory)

    def test_zones_chargees(self):
        zones = [
            {"id": "pulse", "label": "Pulse", "retain": 7, "color": "#304237",
             "directory": str(self.tmp / "p")},
            {"id": "lwp", "directory": str(self.tmp / "l")},
        ]
        cfg = make_cfg(self.tmp, zones=zones)
        self.assertEqual(set(cfg.zones), {"pulse", "lwp"})
        self.assertEqual(cfg.zones["pulse"].retain, 7)
        self.assertEqual(cfg.zones["pulse"].color, "#304237")
        self.assertEqual(cfg.zones["lwp"].label, "lwp")

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

    def test_accepte_remote_avec_auth(self):
        cfg = make_cfg(self.tmp, listen_address="0.0.0.0", auth_enabled=True)
        check_startup_policy(cfg)

    def test_override_explicite(self):
        cfg = make_cfg(
            self.tmp,
            listen_address="0.0.0.0",
            auth_enabled=False,
            allow_unauthenticated_remote=True,
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


if __name__ == "__main__":
    unittest.main()
