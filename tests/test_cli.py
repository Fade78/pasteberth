"""Tests CLI (sous-processus) : --version, erreurs de config, politique de
démarrage, commande passwd."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pasteberth.auth import load_password_hash, verify_password

from tests.helpers import REPO_ROOT, write_config

ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def run_cli(args, *, cwd=None, input_text=None):
    return subprocess.run(
        [sys.executable, "-m", "pasteberth", *args],
        capture_output=True,
        text=True,
        input=input_text,
        env=ENV,
        timeout=60,
        cwd=cwd or str(REPO_ROOT),
    )


class TestVersion(unittest.TestCase):
    def test_version(self):
        proc = run_cli(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("pasteberth", proc.stdout)


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
        proc = self._passwd_cmd()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("8 caractères", proc.stderr)

    def test_confirmation_differe(self):
        self.stdin = "un-long-mot-de-passe-1\nautre-long-mot-de-passe\n"
        proc = self._passwd_cmd()
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

    def test_secret_jamais_en_clair_dans_le_fichier(self):
        cfg_path = write_config(self.tmp)
        secret = "mot-en-clair-impossible-a-trouver"
        self.stdin = f"{secret}\n{secret}\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0)
        content = (cfg_path.parent / "passwd").read_text()
        self.assertNotIn(secret, content)
        self.assertTrue(content.startswith("scrypt$"))


if __name__ == "__main__":
    unittest.main()
