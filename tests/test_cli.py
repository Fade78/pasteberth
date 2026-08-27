"""Tests CLI (sous-processus) : --version, erreurs de config, politique de
démarrage, commande passwd."""
import os
import stat
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pasteberth import __version__
from pasteberth.auth import load_password_hash, verify_password
from pasteberth.cli import _network_warning
from pasteberth.config import load_config

from tests.helpers import REPO_ROOT, write_config

ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def run_cli(args, *, cwd=None, input_text=None, env=None):
    process_env = dict(ENV)
    for key, value in (env or {}).items():
        if value is None:
            process_env.pop(key, None)
        else:
            process_env[key] = value
    return subprocess.run(
        [sys.executable, "-m", "pasteberth", *args],
        capture_output=True,
        text=True,
        input=input_text,
        env=process_env,
        timeout=60,
        cwd=cwd or str(REPO_ROOT),
    )


class TestVersion(unittest.TestCase):
    def test_version(self):
        proc = run_cli(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f"pasteberth {__version__}", proc.stdout)


class TestErreursDemarrage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_config_inexistante(self):
        proc = run_cli(["serve", "--config", str(self.tmp / "absent.toml")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("introuvable", proc.stderr)

    def test_config_invalide(self):
        cfg = write_config(self.tmp, extra="mauvaise_ligne_sans_guillemet\n")
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("erreur de configuration", proc.stderr.lower())

    def test_non_loopback_sans_auth_refuse(self):
        cfg = write_config(self.tmp, listen_address="0.0.0.0",
                           allow_unauthenticated_remote=False)
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refus de démarrer", proc.stderr)

    def test_directory_relative_refusee(self):
        zones = [{"id": "x", "directory": "relatif"}]
        cfg = write_config(self.tmp, zones=zones)
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("absolu", proc.stderr)

    def test_auth_activee_sans_hash_refusee(self):
        cfg = write_config(self.tmp, auth_enabled=True)
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("hash scrypt valide", proc.stderr)

    def test_fichier_passwd_non_utf8_refuse_sans_traceback(self):
        password_file = self.tmp / "passwd"
        password_file.write_bytes(b"\xff\n")
        password_file.chmod(0o600)
        cfg = write_config(
            self.tmp,
            auth_enabled=True,
            password_file=str(password_file),
        )
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("erreur de configuration", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_destination_inaccessible_retourne_une_erreur_propre(self):
        zone = self.tmp / "default-images"
        zone.mkdir()
        (zone / ".pasteberth.lock").symlink_to(self.tmp / "outside")
        cfg = write_config(
            self.tmp,
            zones=[{"id": "default", "directory": str(zone)}],
        )
        for command in ("serve", "audit"):
            with self.subTest(command=command):
                proc = run_cli([command, "--config", str(cfg)])
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr + proc.stdout)

    def test_parent_destination_inaccessible_retourne_une_erreur_propre(self):
        parent = self.tmp / "private"
        parent.mkdir()
        parent.chmod(0)
        self.addCleanup(lambda: parent.chmod(0o700))
        cfg = write_config(
            self.tmp,
            zones=[{"id": "default", "directory": str(parent / "images")}],
        )
        for command in ("serve", "audit"):
            with self.subTest(command=command):
                proc = run_cli([command, "--config", str(cfg)])
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stderr + proc.stdout)


class TestPasswd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _passwd_cmd(self, cfg=None):
        args = ["passwd"]
        if cfg:
            args += ["--config", str(cfg)]
        return run_cli(args, input_text=self.stdin)

    def test_mot_de_passe_court_refuse(self):
        self.stdin = "court\ncourt\n"
        proc = self._passwd_cmd(write_config(self.tmp))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("8 caractères", proc.stderr)

    def test_confirmation_differe(self):
        self.stdin = "un-long-mot-de-passe-1\nautre-long-mot-de-passe\n"
        proc = self._passwd_cmd(write_config(self.tmp))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("diffèrent", proc.stderr)

    def test_ecriture_hash_et_changement(self):
        cfg_path = write_config(self.tmp)
        # création
        self.stdin = "premier-mot-de-passe\npremier-mot-de-passe\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        passwd_file = cfg_path.parent / "passwd"
        self.assertTrue(passwd_file.is_file())
        mode = stat.S_IMODE(passwd_file.stat().st_mode)
        self.assertEqual(mode, 0o600)
        stored = load_password_hash(passwd_file)
        self.assertTrue(verify_password("premier-mot-de-passe", stored))
        self.assertFalse(verify_password("ancien-mot-de-passe", stored))
        # changement à chaud
        self.stdin = "second-mot-de-passe\nsecond-mot-de-passe\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored2 = load_password_hash(passwd_file)
        self.assertTrue(verify_password("second-mot-de-passe", stored2))
        self.assertFalse(verify_password("premier-mot-de-passe", stored2))

    def test_ecriture_hash_dans_un_chemin_externe_configure(self):
        password_file = self.tmp / "secrets" / "passwd"
        cfg_path = write_config(
            self.tmp,
            auth_enabled=True,
            password_file=str(password_file),
        )
        self.stdin = "mot-de-passe-externe\nmot-de-passe-externe\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(password_file.is_file())
        self.assertFalse((cfg_path.parent / "passwd").exists())
        self.assertTrue(verify_password("mot-de-passe-externe", load_password_hash(password_file)))

    def test_secret_jamais_en_clair_dans_le_fichier(self):
        cfg_path = write_config(self.tmp)
        secret = "mot-en-clair-impossible-a-trouver"
        self.stdin = f"{secret}\n{secret}\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0)
        content = (cfg_path.parent / "passwd").read_text()
        self.assertNotIn(secret, content)
        self.assertTrue(content.startswith("scrypt$"))


class TestConfigurationDepot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_generation_configuration_locale(self):
        target = self.tmp / "config.toml"
        proc = run_cli(["--generate-config", "--config", str(target)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        content = target.read_text(encoding="utf-8")
        self.assertIn('id = "default"', content)
        self.assertIn("allowed_hosts = []", content)
        self.assertIn("storage/default", content)
        self.assertIn("configuration générée", proc.stdout)

    def test_generation_necrase_pas_par_defaut(self):
        target = self.tmp / "config.toml"
        target.write_text("sentinelle\n", encoding="utf-8")
        proc = run_cli(["--generate-config", "--config", str(target)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinelle\n")

    def test_audit_mode_depot_sans_configuration(self):
        port_busy = False
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 8765))
            except OSError:
                port_busy = True
        proc = run_cli(
            ["audit"],
            cwd=self.tmp,
            env={
                "PASTEBERTH_REPO_ROOT": str(self.tmp),
                "PASTEBERTH_CONFIG": None,
                "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            },
        )
        self.assertEqual(proc.returncode, 2 if port_busy else 1)
        self.assertIn("stockage par défaut", proc.stdout)

    def test_audit_allowed_hosts_vide_avertit_wildcard(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(self.tmp, port=port, auth_enabled=True, password="un-hash-valide")
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertIn("wildcard", proc.stdout)
        self.assertIn("AVERTISSEMENT", proc.stdout)

    def test_audit_accept_tous_faux_avertit(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            accept_bin=False,
            accept_img=False,
            accept_doc=False,
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertIn("refusera tout contenu", proc.stdout)
        self.assertIn("AVERTISSEMENT", proc.stdout)

    def test_audit_permissions_zone_avertit_sans_echouer(self):
        target = self.tmp / "open"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            zones=[{"id": "open", "directory": str(target)}],
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("permissions non privées", proc.stdout)
        self.assertIn("Audit prêt avec", proc.stdout)

    def test_audit_permissions_group_writable_avertit_aussi(self):
        # Feature: un mode group-writable (0o775) avertit mais n'échoue pas,
        # sinon l'opérateur contourne la protection (chmod 777, stockage hors zone).
        target = self.tmp / "shared"
        target.mkdir(mode=0o775)
        os.chmod(target, 0o775)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            zones=[{"id": "shared", "directory": str(target)}],
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("permissions non privées", proc.stdout)
        self.assertIn("Audit prêt avec", proc.stdout)

    def test_audit_auth_sans_hash_echoue(self):
        cfg = write_config(self.tmp, auth_enabled=True)
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("hash scrypt absent ou invalide", proc.stdout)

    def test_audit_zones_partageant_un_repertoire_echoue(self):
        shared = self.tmp / "shared"
        cfg = write_config(
            self.tmp,
            zones=[
                {"id": "a", "directory": str(shared)},
                {"id": "b", "directory": str(shared)},
            ],
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("même répertoire", proc.stdout)

    def test_audit_verifie_le_bind(self):
        import socket

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            cfg = write_config(self.tmp, port=port)
            proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bind impossible", proc.stdout)

    def test_audit_verifie_le_certificat_tls(self):
        cfg = write_config(
            self.tmp,
            tls_enabled=True,
            tls_certificate=str(self.tmp / "cert.pem"),
            tls_private_key=str(self.tmp / "key.pem"),
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("configuration TLS invalide", proc.stdout)

    def test_audit_tls_ne_demande_pas_de_reverse_proxy(self):
        cfg = load_config(
            write_config(
                self.tmp,
                listen_address="0.0.0.0",
                auth_enabled=True,
                password="mot-de-passe-test-123",
                tls_enabled=True,
                tls_certificate="/tmp/cert.pem",
                tls_private_key="/tmp/key.pem",
            )
        )
        self.assertIsNone(_network_warning(cfg))

if __name__ == "__main__":
    unittest.main()
