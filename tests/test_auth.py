"""Tests auth : hash scrypt, sessions, rate limiting."""
import tempfile
import time
import unittest
import os
from pathlib import Path

from PasteBerth.runtime.auth import (
    LoginRateLimiter,
    SessionStore,
    hash_password,
    load_password_hash,
    save_password_hash,
    valid_password_hash,
    verify_password,
)
from PasteBerth.runtime.platformfs import platform_fs
from tests.helpers import running_under_wine

PASSWORD = "correct horse battery staple"


class TestHash(unittest.TestCase):
    def test_budget_scrypt_desactive_explicitement(self):
        stored = hash_password(PASSWORD, maxmem=None)
        self.assertTrue(verify_password(PASSWORD, stored, maxmem=None))

    def test_roundtrip(self):
        stored = hash_password(PASSWORD)
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(verify_password(PASSWORD, stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("", stored))

    def test_salt_unique(self):
        a, b = hash_password(PASSWORD), hash_password(PASSWORD)
        self.assertNotEqual(a, b)  # sels différents
        self.assertTrue(verify_password(PASSWORD, b))

    def test_hash_corrompu_refuse(self):
        for bad in ["", "scrypt", "md5$abc", "scrypt$x$y$z$a$b", None]:
            self.assertFalse(verify_password(PASSWORD, bad))

    def test_fichier_passwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passwd"
            save_password_hash(path, hash_password(PASSWORD))
            if platform_fs().backend_name == "windows":
                if running_under_wine():
                    self.assertTrue(path.is_file())
                else:
                    self.assertTrue(platform_fs().audit_permissions(path, directory=False).private)
            else:
                mode = oct(path.stat().st_mode & 0o777)
                self.assertEqual(mode, "0o600")
            loaded = load_password_hash(path)
            self.assertTrue(verify_password(PASSWORD, loaded))
            self.assertIsNone(load_password_hash(Path(tmp) / "absent"))

    def test_fichier_passwd_non_utf8_retourne_une_erreur_propre(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passwd"
            path.write_bytes(b"\xff\n")
            path.chmod(0o600)
            with self.assertRaises(RuntimeError):
                load_password_hash(path)

    def test_fichier_passwd_fifo_ne_bloque_pas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passwd"
            try:
                os.mkfifo(path, 0o600)
            except (AttributeError, NotImplementedError, OSError):
                self.skipTest("FIFO indisponible")
            with self.assertRaises(RuntimeError):
                load_password_hash(path)

    def test_parent_symbolique_est_suivi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            save_password_hash(link / "passwd", hash_password(PASSWORD))
            self.assertTrue((real / "passwd").is_file())

    def test_changement_corrige_un_mode_trop_ouvert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passwd"
            save_password_hash(path, hash_password(PASSWORD))
            path.chmod(0o644)
            save_password_hash(path, hash_password("nouveau mot de passe long"))
            if platform_fs().backend_name == "windows":
                if not running_under_wine():
                    self.assertTrue(platform_fs().audit_permissions(path, directory=False).private)
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ecriture_ne_chmodde_pas_le_repertoire_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "repository"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            before_audit = (
                platform_fs().audit_permissions(parent, directory=True)
                if platform_fs().backend_name == "windows"
                else None
            )
            save_password_hash(parent / "passwd", hash_password(PASSWORD))
            if platform_fs().backend_name == "windows":
                self.assertEqual(before_audit, platform_fs().audit_permissions(parent, directory=True))
            else:
                self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    def test_validation_structurelle_sans_scrypt(self):
        stored = hash_password(PASSWORD)
        self.assertTrue(valid_password_hash(stored))
        self.assertFalse(valid_password_hash("scrypt$1$1$1$bad$bad"))


class TestSessions(unittest.TestCase):
    def test_cycle_complet(self):
        store = SessionStore(ttl_seconds=60)
        token = store.create()
        self.assertTrue(store.validate(token))
        self.assertFalse(store.validate("forged-token"))
        self.assertFalse(store.validate(None))
        self.assertFalse(store.validate(token + "x"))
        store.revoke(token)
        self.assertFalse(store.validate(token))
        store.revoke(None)  # ne doit pas lever

    def test_expiration(self):
        store = SessionStore(ttl_seconds=0.05)
        token = store.create()
        self.assertTrue(store.validate(token))
        time.sleep(0.08)
        self.assertFalse(store.validate(token))

    def test_sessions_independantes(self):
        store = SessionStore(ttl_seconds=60)
        t1, t2 = store.create(), store.create()
        store.revoke(t1)
        self.assertTrue(store.validate(t2))

    def test_plafond_evince_la_session_la_plus_ancienne(self):
        store = SessionStore(ttl_seconds=60, max_sessions=2)
        first, second = store.create(), store.create()
        third = store.create()

        self.assertFalse(store.validate(first))
        self.assertTrue(store.validate(second))
        self.assertTrue(store.validate(third))
        self.assertEqual(store.active_count, 2)

    def test_creation_concurrente_respecte_le_plafond(self):
        from concurrent.futures import ThreadPoolExecutor

        store = SessionStore(ttl_seconds=60, max_sessions=4)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: store.create(), range(64)))

        self.assertEqual(store.active_count, 4)
        self.assertEqual(sum(store.validate(token) for token in tokens), 4)

    def test_changement_mot_de_passe_invalide_les_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            passwd = Path(tmp) / "passwd"
            save_password_hash(passwd, hash_password(PASSWORD))
            store = SessionStore(ttl_seconds=60, password_file=passwd)
            token = store.create()
            self.assertTrue(store.validate(token))
            save_password_hash(passwd, hash_password("nouveau mot de passe long"))
            self.assertFalse(store.validate(token))


class TestRateLimiter(unittest.TestCase):
    def test_budgets_de_login_sont_configurables(self):
        limiter = LoginRateLimiter(
            max_concurrent_checks=None,
            max_tracked_ips=1,
            max_delay=1.0,
            forget_after=None,
        )
        ip = "10.0.0.20"
        for _ in range(LoginRateLimiter.THRESHOLD):
            limiter.register_failure(ip)
        self.assertLessEqual(limiter.retry_after(ip), 1.0)
        limiter.register_failure("10.0.0.21")
        self.assertIn(ip, limiter._state)

    def test_verrouillage_progressif(self):
        limiter = LoginRateLimiter()
        ip = "10.1.2.3"
        for _ in range(LoginRateLimiter.THRESHOLD - 1):
            limiter.register_failure(ip)
        self.assertEqual(limiter.retry_after(ip), 0.0)
        limiter.register_failure(ip)  # seuil atteint
        delay = limiter.retry_after(ip)
        self.assertGreaterEqual(delay, 25.0)
        # échecs suivants : délai croissant plafonné
        limiter.register_failure(ip)
        limiter.register_failure(ip)
        self.assertLessEqual(limiter.retry_after(ip), LoginRateLimiter.MAX_DELAY)

    def test_succes_reinitialise(self):
        limiter = LoginRateLimiter()
        ip = "10.9.9.9"
        for _ in range(10):
            limiter.register_failure(ip)
        limiter.register_success(ip)
        self.assertEqual(limiter.retry_after(ip), 0.0)

    def test_ips_independantes(self):
        limiter = LoginRateLimiter()
        for _ in range(8):
            limiter.register_failure("1.1.1.1")
        self.assertEqual(limiter.retry_after("2.2.2.2"), 0.0)

    def test_une_seule_verification_couteuse_en_vol(self):
        limiter = LoginRateLimiter()
        ip = "10.2.2.2"
        self.assertEqual(limiter.acquire(ip), 0.0)
        self.assertGreater(limiter.acquire(ip), 0.0)
        limiter.release(ip)
        limiter.register_failure(ip)
        self.assertEqual(limiter.acquire(ip), 0.0)

    def test_plafond_global_des_verifications_couteuses(self):
        limiter = LoginRateLimiter(max_concurrent_checks=1)
        self.assertEqual(limiter.acquire("10.0.0.1"), 0.0)
        self.assertGreater(limiter.acquire("10.0.0.2"), 0.0)
        limiter.release("10.0.0.1")
        self.assertEqual(limiter.acquire("10.0.0.2"), 0.0)
        limiter.release("10.0.0.2")

    def test_completion_met_a_jour_le_resultat_et_libere_atomiquement(self):
        limiter = LoginRateLimiter()
        ip = "10.0.0.4"
        for _ in range(LoginRateLimiter.THRESHOLD - 1):
            limiter.register_failure(ip)
        self.assertEqual(limiter.acquire(ip), 0.0)
        limiter.complete(ip, success=False)
        self.assertGreaterEqual(limiter.retry_after(ip), 25.0)
        limiter.register_success(ip)
        self.assertEqual(limiter.acquire(ip), 0.0)
        limiter.complete(ip, success=True)

    def test_nombre_ips_suivi_borne(self):
        limiter = LoginRateLimiter(max_tracked_ips=2)
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            limiter.register_failure(ip)
        self.assertLessEqual(len(limiter._state), 2)

    def test_lockout_actif_ne_peut_pas_etre_evince_par_du_churn(self):
        limiter = LoginRateLimiter(max_tracked_ips=1)
        locked = "10.0.0.10"
        for _ in range(LoginRateLimiter.THRESHOLD):
            limiter.register_failure(locked)

        limiter.register_failure("10.0.0.11")

        self.assertIn(locked, limiter._state)
        self.assertNotIn("10.0.0.11", limiter._state)
        self.assertGreater(limiter.retry_after(locked), 0.0)

    def test_table_pleine_de_lockouts_ne_perd_pas_de_slot(self):
        limiter = LoginRateLimiter(max_concurrent_checks=1, max_tracked_ips=1)
        locked = "10.0.0.12"
        for _ in range(LoginRateLimiter.THRESHOLD):
            limiter.register_failure(locked)

        self.assertGreater(limiter.acquire("10.0.0.13"), 0.0)
        limiter.register_success(locked)
        self.assertEqual(limiter.acquire("10.0.0.13"), 0.0)
        limiter.complete("10.0.0.13", success=True)

    def test_lockout_expire_reste_evinçable(self):
        limiter = LoginRateLimiter(max_tracked_ips=1)
        expired = "10.0.0.14"
        for _ in range(LoginRateLimiter.THRESHOLD):
            limiter.register_failure(expired)
        limiter._state[expired][1] = 0.0

        limiter.register_failure("10.0.0.15")

        self.assertNotIn(expired, limiter._state)
        self.assertIn("10.0.0.15", limiter._state)


if __name__ == "__main__":
    unittest.main()
