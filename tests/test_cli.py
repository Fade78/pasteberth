"""Tests CLI (sous-processus) : --version, erreurs de config, politique de
démarrage, commande passwd."""
import os
import shutil
import stat
import ssl
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PasteBerth.runtime import __version__
from PasteBerth.runtime.auth import load_password_hash, verify_password
from PasteBerth.runtime.client import ClientError, PasteberthClient
from PasteBerth.runtime.cli import _audit_tls, _network_warning, _read_drop_source
from PasteBerth.runtime.config import load_config
from PasteBerth.runtime.platformfs import platform_fs

from tests.helpers import LiveServer, REPO_ROOT, running_under_wine, write_config

ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def run_cli(args, *, cwd=None, input_text=None, env=None):
    process_env = dict(ENV)
    for key, value in (env or {}).items():
        if value is None:
            process_env.pop(key, None)
        else:
            process_env[key] = value
    return subprocess.run(
        [sys.executable, "-m", "PasteBerth.runtime", *args],
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

    def test_aide_expose_la_configuration_globale(self):
        proc = run_cli(["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--config", proc.stdout)

    def test_aide_serveur_expose_le_niveau_de_log(self):
        proc = run_cli(["serve", "--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--log-level", proc.stdout)
        self.assertIn("DEBUG", proc.stdout)

    def test_aide_drop_explique_les_formes_et_le_fallback(self):
        proc = run_cli(["drop", "--help"])
        self.assertEqual(proc.returncode, 0)
        for text in (
            "With --zone ID, positional arguments are source files only.",
            "loopback server",
            "HTTP API",
            "trusted self-signed HTTPS certificate",
            "pasteberth drop --config config.toml --zone project-alpha report.pdf",
            "--password-stdin",
            "ZONE_DIRECTORY_OR_FIRST_SOURCE",
        ):
            with self.subTest(text=text):
                self.assertIn(text, proc.stdout)

    def test_client_aide_apres_une_erreur_de_verification_tls(self):
        client = PasteberthClient("https://127.0.0.1:8765")
        connection = mock.Mock()
        connection.request.side_effect = ssl.SSLCertVerificationError(
            "certificate verify failed"
        )

        with mock.patch.object(client, "_connection", return_value=connection):
            with self.assertRaisesRegex(ClientError, r"retry with --insecure"):
                client.request("POST", "/api/health")

    def test_aide_expose_les_sous_commandes_courtes(self):
        proc = run_cli(["--help"])
        self.assertEqual(proc.returncode, 0)
        for command in ("drop", "rename", "delete"):
            self.assertIn(command, proc.stdout)
        for old_command in ("filesystem-drop", "filesystem-rename", "filesystem-delete"):
            self.assertNotIn(old_command, proc.stdout)

    def test_anciens_noms_de_sous_commande_sont_rejetes(self):
        for command in ("filesystem-drop", "filesystem-rename", "filesystem-delete"):
            with self.subTest(command=command):
                proc = run_cli([command, "--help"])
                self.assertEqual(proc.returncode, 2)
                self.assertIn("invalid choice", proc.stderr)

    def test_completion_bash_emet_le_script_emballe(self):
        proc = run_cli(["completion", "--shell", "bash"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("_pasteberth_complete()", proc.stdout)
        self.assertIn("complete -o bashdefault", proc.stdout)


class TestWrappers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _fake_package(self):
        package = self.tmp / "shadow" / "pasteberth"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "raise RuntimeError('WRAPPER_SHADOW_MARKER')\n",
            encoding="utf-8",
        )
        return package.parent

    def _run_wrapper(self, wrapper, *, cwd, pythonpath):
        return self._run_executable(REPO_ROOT / wrapper, cwd=cwd, pythonpath=pythonpath)

    def _run_executable(self, executable, *, cwd, pythonpath, extra_env=None, args=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(pythonpath)
        env.pop("PASTEBERTH_HOME", None)
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            [str(executable), *(args or ["--version"])],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
            timeout=30,
        )

    def test_wrappers_resistent_au_cwd_et_au_pythonpath(self):
        fake_root = self._fake_package()
        for wrapper in ("PasteBerth/pasteberth",):
            with self.subTest(wrapper=wrapper):
                proc = self._run_wrapper(
                    wrapper,
                    cwd=fake_root,
                    pythonpath=fake_root,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"pasteberth {__version__}", proc.stdout)
                self.assertNotIn("WRAPPER_SHADOW_MARKER", proc.stdout + proc.stderr)

    def test_lien_symbolique_externe_retrouve_le_bundle(self):
        fake_root = self._fake_package()
        link_dir = self.tmp / "local-bin"
        link_dir.mkdir()
        link = link_dir / "pasteberth"
        link.symlink_to(REPO_ROOT / "PasteBerth" / "pasteberth")

        proc = self._run_executable(link, cwd=fake_root, pythonpath=fake_root)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"pasteberth {__version__}", proc.stdout)
        self.assertNotIn("WRAPPER_SHADOW_MARKER", proc.stdout + proc.stderr)

    def test_copie_complete_du_bundle_reste_autonome(self):
        fake_root = self._fake_package()
        bundle = self.tmp / "PasteBerth"
        shutil.copytree(
            REPO_ROOT / "PasteBerth",
            bundle,
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        proc = self._run_executable(
            bundle / "pasteberth",
            cwd=fake_root,
            pythonpath=fake_root,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"pasteberth {__version__}", proc.stdout)
        self.assertFalse((bundle / "runtime" / "__pycache__").exists())

    def test_copie_complete_genere_son_etat_hors_bundle(self):
        bundle = self.tmp / "PasteBerth"
        config_home = self.tmp / "config-home"
        data_home = self.tmp / "data-home"
        shutil.copytree(
            REPO_ROOT / "PasteBerth",
            bundle,
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        proc = self._run_executable(
            bundle / "pasteberth",
            cwd=self.tmp,
            pythonpath=self.tmp,
            args=["--generate-config"],
            extra_env={
                "PASTEBERTH_CONFIG": None,
                "XDG_CONFIG_HOME": str(config_home),
                "XDG_DATA_HOME": str(data_home),
            },
        )

        target = config_home / "pasteberth" / "config.toml"
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())
        self.assertIn(str(data_home / "pasteberth" / "storage" / "default"), target.read_text())
        self.assertFalse((bundle / "config.toml").exists())
        self.assertFalse((bundle / "storage").exists())
        self.assertFalse((bundle / "runtime" / "__pycache__").exists())

    def test_executable_isole_exige_pasteberth_home(self):
        bundle = self.tmp / "PasteBerth"
        shutil.copytree(
            REPO_ROOT / "PasteBerth",
            bundle,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        executable = self.tmp / "pasteberth"
        shutil.copy2(bundle / "pasteberth", executable)
        executable.chmod(0o755)

        without_home = self._run_executable(
            executable,
            cwd=self.tmp,
            pythonpath=self.tmp,
        )
        with_home = self._run_executable(
            executable,
            cwd=self.tmp,
            pythonpath=self.tmp,
            extra_env={"PASTEBERTH_HOME": str(bundle)},
        )

        self.assertEqual(without_home.returncode, 2)
        self.assertIn("invalid deployment root", without_home.stderr)
        self.assertEqual(with_home.returncode, 0, with_home.stderr)
        self.assertIn(f"pasteberth {__version__}", with_home.stdout)


class TestErreursDemarrage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_config_inexistante(self):
        proc = run_cli(["serve", "--config", str(self.tmp / "absent.toml")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("configuration file not found", proc.stderr)

    def test_config_invalide(self):
        cfg = write_config(self.tmp, extra="mauvaise_ligne_sans_guillemet\n")
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("configuration error", proc.stderr.lower())

    def test_non_loopback_sans_auth_refuse(self):
        cfg = write_config(self.tmp, listen_address="0.0.0.0",
                           allow_unauthenticated_remote=False)
        proc = run_cli(["serve", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refusing to start", proc.stderr)

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
        self.assertIn("valid scrypt hash", proc.stderr)

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
        self.assertIn("configuration error", proc.stderr)
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
        if platform_fs().backend_name == "windows":
            self.skipTest("chmod(0) ne modélise pas une ACL inaccessible sous Windows")
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

    def test_port_occupe_retourne_une_erreur_propre(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            cfg = write_config(self.tmp, port=blocker.getsockname()[1])
            proc = run_cli(["--config", str(cfg)])
            self.assertEqual(proc.returncode, 1)
            self.assertIn("cannot listen", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr + proc.stdout)
        finally:
            blocker.close()


@unittest.skipIf(
    platform_fs().backend_name == "windows",
    "getpass nécessite une console Windows réelle dans ce test subprocess",
)
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
        self.assertIn("at least 8 characters", proc.stderr)

    def test_confirmation_differe(self):
        self.stdin = "un-long-mot-de-passe-1\nautre-long-mot-de-passe\n"
        proc = self._passwd_cmd(write_config(self.tmp))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("entries differ", proc.stderr)

    def test_ecriture_hash_et_changement(self):
        cfg_path = write_config(self.tmp)
        # création
        self.stdin = "premier-mot-de-passe\npremier-mot-de-passe\n"
        proc = self._passwd_cmd(cfg_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        passwd_file = cfg_path.parent / "passwd"
        self.assertTrue(passwd_file.is_file())
        if platform_fs().backend_name == "windows":
            if running_under_wine():
                self.assertTrue(passwd_file.is_file())
            else:
                self.assertTrue(
                    platform_fs().audit_permissions(passwd_file, directory=False).private
                )
        else:
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

    def test_configuration_invalide_n_ecrit_pas_de_hash(self):
        cfg_path = write_config(self.tmp, password_file="relative/passwd")
        self.stdin = "mot-de-passe-inutile\nmot-de-passe-inutile\n"

        proc = self._passwd_cmd(cfg_path)

        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid configuration", proc.stderr)
        self.assertIn("no hash was written", proc.stderr)
        self.assertFalse((cfg_path.parent / "passwd").exists())


class TestConfigurationDepot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_generation_configuration_locale(self):
        target = self.tmp / "config.toml"
        proc = run_cli(
            ["--generate-config", "--config", str(target)],
            env={
                "PASTEBERTH_CONFIG": None,
                "XDG_CONFIG_HOME": str(self.tmp / "config-home"),
                "XDG_DATA_HOME": str(self.tmp / "data-home"),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())
        if platform_fs().backend_name == "windows":
            if running_under_wine():
                self.assertTrue(target.is_file())
            else:
                self.assertTrue(platform_fs().audit_permissions(target, directory=False).private)
        else:
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        content = target.read_text(encoding="utf-8")
        self.assertIn('id = "default"', content)
        self.assertIn('url_prefix = ""', content)
        self.assertIn("allowed_hosts = []", content)
        self.assertIn(
            str(self.tmp / "data-home" / "pasteberth" / "storage" / "default"),
            content,
        )
        self.assertIn("structural pixel budget", content)
        self.assertIn("Keep this listener on loopback", content)
        self.assertIn("show_full_path = true", content)
        self.assertIn("configuration generated", proc.stdout)

    def test_generation_necrase_pas_par_defaut(self):
        target = self.tmp / "config.toml"
        target.write_text("sentinelle\n", encoding="utf-8")
        proc = run_cli(["--generate-config", "--config", str(target)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "sentinelle\n")

    def test_audit_mode_depot_sans_configuration(self):
        proc = run_cli(
            ["audit"],
            cwd=self.tmp,
            env={
                "PASTEBERTH_CONFIG": None,
                "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                "XDG_DATA_HOME": str(self.tmp / "xdg-data"),
            },
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("default storage", proc.stdout)

    def test_generation_par_defaut_ecrit_hors_du_bundle(self):
        config_home = self.tmp / "config-home"
        data_home = self.tmp / "data-home"
        proc = run_cli(
            ["--generate-config"],
            cwd=self.tmp,
            env={
                "PASTEBERTH_CONFIG": None,
                "XDG_CONFIG_HOME": str(config_home),
                "XDG_DATA_HOME": str(data_home),
            },
        )

        target = config_home / "pasteberth" / "config.toml"
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.is_file())
        self.assertIn(str(data_home / "pasteberth" / "storage" / "default"), target.read_text())
        self.assertFalse((REPO_ROOT / "PasteBerth" / "config.toml").exists())

    def test_configuration_dans_le_bundle_refusee(self):
        target = REPO_ROOT / "PasteBerth" / "config.toml"
        target.write_text("", encoding="utf-8")
        self.addCleanup(target.unlink)

        proc = run_cli(
            ["audit", "--config", str(target)],
            env={"PASTEBERTH_CONFIG": None},
        )

        self.assertEqual(proc.returncode, 2)
        self.assertIn("outside the PasteBerth deployment", proc.stderr)

    def test_audit_allowed_hosts_vide_avertit_wildcard(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            auth_enabled=True,
            password="un-hash-valide",
            allowed_hosts="[]",
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertIn("wildcard", proc.stdout)
        self.assertIn("WARNING", proc.stdout)

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
        self.assertIn("server will reject all content", proc.stdout)
        self.assertIn("WARNING", proc.stdout)

    def test_audit_avertit_si_les_chemins_complets_sont_visibles(self):
        cfg = write_config(self.tmp, show_full_path=True)
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertIn("show_full_path", proc.stdout)
        self.assertIn("absolute paths", proc.stdout)

    def test_audit_n_avertit_pas_si_les_chemins_sont_masques(self):
        cfg = write_config(self.tmp, show_full_path=False)
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertNotIn("show_full_path", proc.stdout)

    def test_audit_selections_groupes_redondantes_avertit(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            groups=[
                {"name": "All", "selection": "all", "pattern": ["^ignored-.*$"]},
                {"name": "AllAgain", "selection": "all"},
                {"name": "AllPattern", "pattern": [".*"]},
                {"name": "Other", "selection": "other", "pattern": ["^ignored-.*$"]},
                {"name": "OtherAgain", "selection": "other"},
            ],
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pattern' ignored", proc.stdout)
        self.assertIn("redundant selection='all' groups", proc.stdout)
        self.assertIn("selection='all' and selection='other'", proc.stdout)
        self.assertIn("redundant selection='other' groups", proc.stdout)
        self.assertIn("redundant groups: All (all) and AllPattern (pattern)", proc.stdout)

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
        self.assertIn("permissions are not private", proc.stdout)
        self.assertIn("Audit ready with", proc.stdout)

    def test_audit_refuse_un_groupe_de_fichiers_inconnu(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cfg = write_config(
            self.tmp,
            port=port,
            zones=[
                {
                    "id": "grouped",
                    "directory": str(self.tmp / "grouped"),
                    "file_group": "__pasteberth_missing_group__",
                },
            ],
        )

        proc = run_cli(["audit", "--config", str(cfg)])

        self.assertEqual(proc.returncode, 2)
        self.assertIn("file_group", proc.stdout)

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
        self.assertIn("permissions are not private", proc.stdout)
        self.assertIn("Audit ready with", proc.stdout)

    def test_audit_auth_sans_hash_echoue(self):
        cfg = write_config(self.tmp, auth_enabled=True)
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("missing or invalid scrypt hash", proc.stdout)

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
        self.assertIn("same directory", proc.stdout)

    def test_audit_verifie_le_bind(self):
        import socket

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            cfg = write_config(self.tmp, port=port)
            proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("port already in use", proc.stdout)
        self.assertIn("WARNING", proc.stdout)

    def test_audit_verifie_le_certificat_tls(self):
        cfg = write_config(
            self.tmp,
            tls_enabled=True,
            tls_certificate=str(self.tmp / "cert.pem"),
            tls_private_key=str(self.tmp / "key.pem"),
        )
        proc = run_cli(["audit", "--config", str(cfg)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid TLS configuration", proc.stdout)

    def test_audit_refuse_configuration_inscriptible(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("chmod POSIX non représentatif des ACL Windows")
        cfg = write_config(self.tmp)
        cfg.chmod(0o666)

        proc = run_cli(["audit", "--config", str(cfg)])

        self.assertEqual(proc.returncode, 2)
        self.assertIn("configuration: writable by a third party", proc.stdout)

    def test_audit_accepte_configuration_symlinkee_avec_avertissement(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("symlink et ACL Windows native non couverts ici")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        zone = self.tmp / "default-images"
        zone.mkdir(mode=0o700)
        real_config = write_config(
            self.tmp,
            port=port,
            zones=[{"id": "default", "directory": str(zone)}],
        )
        linked_config = self.tmp / "config-link.toml"
        linked_config.symlink_to(real_config)

        proc = run_cli(["audit", "--config", str(linked_config)])

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("configuration: symbolic link accepted", proc.stdout)
        self.assertNotIn("configuration: symbolic link rejected", proc.stdout)

    def test_audit_refuse_proxy_global(self):
        cfg = write_config(self.tmp, trusted_proxies='["0.0.0.0/0", "::/0"]')

        proc = run_cli(["audit", "--config", str(cfg)])

        self.assertEqual(proc.returncode, 2)
        self.assertIn("global proxy is not allowed", proc.stdout)

    def _tls_config_with_files(self, *, certificate_name="cert.pem"):
        certificate = self.tmp / certificate_name
        private_key = self.tmp / "key.pem"
        certificate.write_text("certificate", encoding="ascii")
        private_key.write_text("private key", encoding="ascii")
        if platform_fs().backend_name != "windows":
            certificate.chmod(0o644)
            private_key.chmod(0o600)
        cfg = load_config(
            write_config(
                self.tmp,
                tls_enabled=True,
                tls_certificate=str(certificate),
                tls_private_key=str(private_key),
            )
        )
        return cfg, certificate, private_key

    def test_audit_accepte_certificat_public_et_verifie_dates(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("ACL Windows native non couverte par ce test POSIX")
        cfg, _, _ = self._tls_config_with_files()
        decoded = {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2099 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch("PasteBerth.runtime.server.create_tls_context"):
                self.assertEqual(_audit_tls(cfg), [])

    def test_audit_refuse_certificat_expire(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("ACL Windows native non couverte par ce test POSIX")
        cfg, _, _ = self._tls_config_with_files()
        decoded = {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2021 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch("PasteBerth.runtime.server.create_tls_context"):
                errors = _audit_tls(cfg)
        self.assertIn("TLS certificate: certificate has expired", errors)

    def test_audit_refuse_certificat_pas_encore_valide(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("ACL Windows native non couverte par ce test POSIX")
        cfg, _, _ = self._tls_config_with_files()
        decoded = {
            "notBefore": "Jan  1 00:00:00 2099 GMT",
            "notAfter": "Jan  1 00:00:00 2100 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch("PasteBerth.runtime.server.create_tls_context"):
                errors = _audit_tls(cfg)
        self.assertIn("TLS certificate: certificate is not valid yet", errors)

    def test_audit_refuse_correspondance_cle_certificat(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("ACL Windows native non couverte par ce test POSIX")
        cfg, _, _ = self._tls_config_with_files()
        decoded = {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2099 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch(
                "PasteBerth.runtime.server.create_tls_context",
                side_effect=ValueError("clé et certificat différents"),
            ):
                errors = _audit_tls(cfg)
        self.assertTrue(any("invalid TLS configuration" in error for error in errors))

    def test_audit_accepte_rotation_certificat_par_symlink_controle(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("symlink et ACL Windows native non couverts ici")
        cfg, certificate, _ = self._tls_config_with_files(certificate_name="cert-real.pem")
        link = self.tmp / "cert-current.pem"
        link.symlink_to(certificate)
        cfg = load_config(
            write_config(
                self.tmp,
                tls_enabled=True,
                tls_certificate=str(link),
                tls_private_key=str(self.tmp / "key.pem"),
            )
        )
        decoded = {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2099 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch("PasteBerth.runtime.server.create_tls_context"):
                self.assertEqual(_audit_tls(cfg), [])

    def test_audit_refuse_rotation_certificat_dans_parent_inscriptible(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("ACL Windows native non couverte par ce test POSIX")
        cfg, certificate, _ = self._tls_config_with_files(certificate_name="cert-real.pem")
        unsafe = self.tmp / "unsafe"
        unsafe.mkdir()
        unsafe.chmod(0o777)
        link = unsafe / "cert-current.pem"
        link.symlink_to(certificate)
        cfg = load_config(
            write_config(
                self.tmp,
                tls_enabled=True,
                tls_certificate=str(link),
                tls_private_key=str(self.tmp / "key.pem"),
            )
        )
        decoded = {
            "notBefore": "Jan  1 00:00:00 2020 GMT",
            "notAfter": "Jan  1 00:00:00 2099 GMT",
        }
        with mock.patch("PasteBerth.runtime.cli.ssl._ssl._test_decode_cert", return_value=decoded):
            with mock.patch("PasteBerth.runtime.server.create_tls_context"):
                errors = _audit_tls(cfg)
        self.assertTrue(any("parent writable by a third party" in error for error in errors))

    def test_audit_refuse_cle_privee_trop_lisible(self):
        if platform_fs().backend_name == "windows":
            self.skipTest("chmod POSIX non représentatif des ACL Windows")
        cfg, _, private_key = self._tls_config_with_files()
        private_key.chmod(0o644)

        errors = _audit_tls(cfg)

        self.assertTrue(any("TLS private key: permissions too open" in error for error in errors))

    def test_audit_tls_ne_demande_pas_de_reverse_proxy(self):
        cfg = load_config(
            write_config(
                self.tmp,
                listen_address="0.0.0.0",
                auth_enabled=True,
                password="mot-de-passe-test-123",
                tls_enabled=True,
                tls_certificate=str(self.tmp / "cert.pem"),
                tls_private_key=str(self.tmp / "key.pem"),
            )
        )
        self.assertIsNone(_network_warning(cfg))


class TestFilesystemDrop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = write_config(self.tmp)
        self.zone = self.tmp / "default-images"
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)

    def _run_drop(self, *sources, replace=False):
        args = [
            "drop",
            "--config",
            str(self.cfg),
            "--server",
            f"http://127.0.0.1:{self.server.port}",
            "--zone",
            "default",
        ]
        if replace:
            args.append("--replace")
        args.extend(str(source) for source in sources)
        return run_cli(args)

    def _run_rename(self, source, target):
        return run_cli(
            [
                "rename",
                "--config",
                str(self.cfg),
                str(self.zone),
                source,
                target,
            ]
        )

    def _run_delete(self, *files, force=False):
        args = ["delete", "--config", str(self.cfg)]
        if force:
            args.append("--force")
        args.extend([str(self.zone), *files])
        return run_cli(args)

    def test_copie_le_fichier_et_cree_le_sidecar(self):
        source = self.tmp / "report.txt"
        source.write_text("version 1\n", encoding="utf-8")

        proc = self._run_drop(source)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("@", proc.stdout)
        target = self.zone / source.name
        self.assertEqual(target.read_text(encoding="utf-8"), "version 1\n")
        self.assertTrue((self.zone / (source.name + ".json")).is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), "version 1\n")

    def test_drop_refuse_le_namespace_pbdel(self):
        source = self.tmp / ".pbdel-0123456789abcdef01234567.json"
        source.write_text("client data", encoding="utf-8")

        proc = self._run_drop(source)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid", proc.stderr)
        self.assertFalse((self.zone / source.name).exists())

    def test_remplacement_exige_l_option_expresse(self):
        source = self.tmp / "report.txt"
        source.write_text("version 1\n", encoding="utf-8")
        first = self._run_drop(source)
        self.assertEqual(first.returncode, 0, first.stderr)

        source.write_text("version 2\n", encoding="utf-8")
        refused = self._run_drop(source)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("explicit replacement required", refused.stderr)
        self.assertEqual((self.zone / source.name).read_text(encoding="utf-8"), "version 1\n")

        replaced = self._run_drop(source, replace=True)
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertEqual((self.zone / source.name).read_text(encoding="utf-8"), "version 2\n")

    def test_fichier_etranger_jamais_ecrase_meme_avec_replace(self):
        foreign = self.zone / "foreign.txt"
        self.zone.mkdir(parents=True, exist_ok=True)
        foreign.write_text("foreign\n", encoding="utf-8")
        source = self.tmp / "foreign-source.txt"
        source.write_text("replacement\n", encoding="utf-8")
        source = source.rename(self.tmp / "foreign.txt")

        proc = self._run_drop(source, replace=True)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("foreign file", proc.stderr)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign\n")

    def test_drop_refuse_un_fichier_deja_present_sans_sidecar(self):
        target = self.zone / "already.txt"
        self.zone.mkdir(parents=True, exist_ok=True)
        target.write_text("already here\n", encoding="utf-8")

        proc = self._run_drop(target)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("foreign file", proc.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "already here\n")
        self.assertFalse((self.zone / (target.name + ".json")).exists())

    def test_plusieurs_sources_sont_traitees_individuellement(self):
        first = self.tmp / "first.txt"
        second = self.tmp / "second.bin"
        first.write_text("first", encoding="utf-8")
        second.write_bytes(b"\x00\x01")

        proc = self._run_drop(first, second)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.zone / first.name).read_text(encoding="utf-8"), "first")
        self.assertEqual((self.zone / second.name).read_bytes(), b"\x00\x01")

    def test_source_fifo_refusee_sans_bloquer(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO indisponible sous Windows")
        fifo = self.tmp / "source.fifo"
        os.mkfifo(fifo)

        with self.assertRaisesRegex(ValueError, "regular file"):
            _read_drop_source(fifo, 1024)

    def test_rename_deplace_le_sidecar(self):
        source = self.tmp / "report.txt"
        source.write_text("report", encoding="utf-8")
        dropped = self._run_drop(source)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)

        renamed = self._run_rename("report.txt", "renamed.txt")

        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        self.assertFalse((self.zone / "report.txt").exists())
        self.assertFalse((self.zone / "report.txt.json").exists())
        self.assertEqual((self.zone / "renamed.txt").read_text(encoding="utf-8"), "report")
        metadata = (self.zone / "renamed.txt.json").read_text(encoding="utf-8")
        self.assertIn('"filename":"renamed.txt"', metadata)

    def test_delete_supprime_le_fichier_et_le_sidecar(self):
        source = self.tmp / "report.txt"
        source.write_text("report", encoding="utf-8")
        dropped = self._run_drop(source)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)

        deleted = self._run_delete("report.txt")

        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertFalse((self.zone / "report.txt").exists())
        self.assertFalse((self.zone / "report.txt.json").exists())

    def test_delete_force_supprime_un_sidecar_stale(self):
        source = self.tmp / "report.txt"
        source.write_text("report", encoding="utf-8")
        dropped = self._run_drop(source)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        (self.zone / "report.txt").write_text("changed content", encoding="utf-8")

        refused = self._run_delete("report.txt")

        self.assertEqual(refused.returncode, 1)
        self.assertTrue((self.zone / "report.txt").exists())

        forced = self._run_delete("report.txt", force=True)

        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse((self.zone / "report.txt").exists())
        self.assertFalse((self.zone / "report.txt.json").exists())

    def test_rename_ne_remplace_pas_une_cible_etrangere(self):
        source = self.tmp / "report.txt"
        source.write_text("report", encoding="utf-8")
        dropped = self._run_drop(source)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        (self.zone / "target.txt").write_text("foreign", encoding="utf-8")

        renamed = self._run_rename("report.txt", "target.txt")

        self.assertEqual(renamed.returncode, 1)
        self.assertIn("target already exists", renamed.stderr)
        self.assertTrue((self.zone / "report.txt").exists())
        self.assertEqual((self.zone / "target.txt").read_text(encoding="utf-8"), "foreign")

class TestFilesystemDropAutozone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.zone = self.tmp / "project" / "work" / "exchange"
        self.zone.mkdir(parents=True)
        self.cfg = self.tmp / "config.toml"
        self.cfg.write_text(
            f'''listen_address = "127.0.0.1"
allowed_hosts = ["localhost", "127.0.0.1"]
allow_unauthenticated_local = true

[auth]
enabled = false

[[autozone]]
base_directory = {str(self.tmp)!r}
pattern = "^[^/]+/work/exchange$"
group = "Repositories"
retain = 2
''',
            encoding="utf-8",
        )
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)

    def test_drop_resout_une_zone_dynamique(self):
        source = self.tmp / "published.txt"
        source.write_text("published", encoding="utf-8")

        proc = run_cli(
            [
                "drop",
                "--config",
                str(self.cfg),
                "--server",
                f"http://127.0.0.1:{self.server.port}",
                "--zone",
                "project-work-exchange",
                str(source),
            ]
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("@", proc.stdout)
        self.assertEqual(
            (self.zone / source.name).read_text(encoding="utf-8"),
            "published",
        )


class TestFilesystemDropWithWritableZone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = write_config(
            self.tmp,
            auth_enabled=True,
            password="drop-password",
        )
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)

    def test_drop_utilise_le_droit_fichier_sans_demander_le_mot_de_passe(self):
        source = self.tmp / "direct.txt"
        source.write_text("direct", encoding="utf-8")

        proc = run_cli(
            [
                "drop",
                "--config",
                str(self.cfg),
                "--server",
                f"http://127.0.0.1:{self.server.port}",
                "--zone",
                "default",
                str(source),
            ],
            input_text="",
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.tmp / "default-images" / source.name).read_text(), "direct")
        self.assertTrue((self.tmp / "default-images" / (source.name + ".json")).is_file())
        self.assertEqual(list((self.tmp / "default-images").glob(".pbdrop-*.tmp")), [])


class TestServerBackedDropAuth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = write_config(
            self.tmp,
            auth_enabled=True,
            password="drop-password",
        )
        self.server = LiveServer(self.cfg)
        self.addCleanup(self.server.stop)

    def test_drop_lit_le_mot_de_passe_sur_stdin_apres_une_reponse_401(self):
        source = self.tmp / "authenticated.txt"
        source.write_text("authenticated", encoding="utf-8")

        proc = run_cli(
            [
                "drop",
                "--config",
                str(self.cfg),
                "--server",
                f"http://127.0.0.1:{self.server.port}",
                "--zone",
                "default",
                "--password-stdin",
                str(source),
            ],
            input_text="drop-password\n",
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("@", proc.stdout)
        self.assertTrue((self.tmp / "default-images" / source.name).exists())


if __name__ == "__main__":
    unittest.main()
