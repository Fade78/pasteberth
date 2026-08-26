"""Authentification : hash de mot de passe (scrypt), sessions serveur,
limitation des tentatives de connexion.

- Le mot de passe n'est JAMAIS stocké en clair : hash scrypt salé dans un
  fichier ``passwd`` (mode 0600) à côté de la configuration.
- Les sessions sont côté serveur (révocables par logout), identifiées par un
  token aléatoire de 256 bits ; le cookie n'est qu'une référence.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=_DKLEN,
        maxmem=_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Comparaison sûre ; retourne False si aucun hash configuré."""
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.strip().split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        n_value, r_value, p_value = int(n), int(r), int(p)
        if (n_value, r_value, p_value) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        if len(salt) != 16 or len(expected) != _DKLEN:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_value,
            r=r_value,
            p=p_value,
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def valid_password_hash(stored: str | None) -> bool:
    """Vérifie la structure du hash sans exécuter scrypt."""
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.strip().split("$")
        if algo != "scrypt":
            return False
        if (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        digest = base64.b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return False
    return len(salt) == 16 and len(digest) == _DKLEN


def load_password_hash(path: Path) -> str | None:
    """Lit le fichier ``passwd`` (première ligne). Rechargé à chaque essai :
    un changement via `pasteberth passwd` est effectif sans redémarrage."""
    try:
        stat_result = path.lstat()
        if not stat_result or not path.is_file() or path.is_symlink():
            raise RuntimeError(f"fichier passwd non régulier : {path}")
        if stat_result.st_mode & 0o077:
            raise RuntimeError(f"permissions trop ouvertes sur {path} (0600 requis)")
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"lecture impossible de {path} : {exc}") from exc
    return raw.splitlines()[0] if raw else None


def save_password_hash(path: Path, password_hash: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".passwd-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password_hash + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------- sessions


class SessionStore:
    """Sessions en mémoire : token -> expiration (monotonic)."""

    def __init__(self, ttl_seconds: int, password_file: Path | None = None):
        self.ttl = ttl_seconds
        self._password_file = password_file
        self._sessions: dict[str, tuple[float, tuple[int, int, int] | None]] = {}
        self._lock = threading.Lock()

    def _password_epoch(self) -> tuple[int, int, int] | None:
        if self._password_file is None:
            return None
        try:
            stat_result = self._password_file.stat()
        except OSError:
            return None
        return (stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_ctime_ns)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = (time.monotonic() + self.ttl, self._password_epoch())
        return token

    def validate(self, token: str | None) -> bool:
        if not token or len(token) > 128:
            return False
        password_epoch = self._password_epoch()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return False
            expiry, session_epoch = session
            if session_epoch != password_epoch:
                del self._sessions[token]
                return False
            if expiry < time.monotonic():
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [t for t, (exp, _) in self._sessions.items() if exp < now]
        for t in expired:
            del self._sessions[t]

    @property
    def active_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._sessions)


# ------------------------------------------------------------- rate limiter


class LoginRateLimiter:
    """Limitation par IP : après N échecs consécutifs, délai croissant."""

    THRESHOLD = 5
    BASE_DELAY = 30.0
    MAX_DELAY = 900.0
    MAX_CONCURRENT_CHECKS = 4
    MAX_TRACKED_IPS = 4096

    _FORGET_AFTER = 3600.0  # sans échec pendant 1h, on oublie l'historique

    def __init__(self, max_concurrent_checks: int | None = None) -> None:
        # ip -> [échecs consécutifs, verrouillé jusqu'à, dernier événement,
        #        tentatives coûteuses en cours]
        self._state: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        checks = (
            self.MAX_CONCURRENT_CHECKS
            if max_concurrent_checks is None
            else max_concurrent_checks
        )
        if checks < 1:
            raise ValueError("max_concurrent_checks doit être positif")
        self._expensive_slots = threading.BoundedSemaphore(checks)

    def _make_room_locked(self, ip: str) -> bool:
        if ip in self._state or len(self._state) < self.MAX_TRACKED_IPS:
            return True
        idle = [
            (entry[2], candidate)
            for candidate, entry in self._state.items()
            if entry[3] == 0
        ]
        if not idle:
            return False
        _, oldest = min(idle)
        del self._state[oldest]
        return True

    def _prune_locked(self, now: float) -> None:
        stale = [
            ip
            for ip, (_, until, last, in_flight) in self._state.items()
            if not in_flight and until < now and now - last > self._FORGET_AFTER
        ]
        for ip in stale:
            del self._state[ip]

    def acquire(self, ip: str) -> float:
        """Réserve atomiquement au plus une vérification scrypt par IP."""
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            entry = self._state.get(ip)
            if entry:
                retry = max(0.0, entry[1] - now)
                if retry > 0:
                    return retry
                if entry[3] >= 1:
                    return 1.0
            if not self._expensive_slots.acquire(blocking=False):
                return 1.0
            if not self._make_room_locked(ip):
                self._expensive_slots.release()
                return 1.0
            if entry is None:
                self._state[ip] = [0.0, 0.0, now, 1.0]
                return 0.0
            entry[3] += 1
            entry[2] = now
            return 0.0

    def release(self, ip: str) -> None:
        release_slot = False
        with self._lock:
            entry = self._state.get(ip)
            if entry and entry[3] >= 1:
                entry[3] = max(0.0, entry[3] - 1.0)
                entry[2] = time.monotonic()
                release_slot = True
                if entry[3] == 0 and entry[0] == 0 and entry[1] <= entry[2]:
                    self._state.pop(ip, None)
        if release_slot:
            self._expensive_slots.release()

    def complete(self, ip: str, *, success: bool) -> None:
        """Finish a costly check while updating its result atomically."""
        release_slot = False
        with self._lock:
            entry = self._state.get(ip)
            if entry is None or entry[3] < 1:
                return
            now = time.monotonic()
            if success:
                fails = 0
                until = 0.0
            else:
                fails = int(entry[0]) + 1
                delay = 0.0
                if fails >= self.THRESHOLD:
                    delay = min(
                        self.BASE_DELAY * (2 ** (fails - self.THRESHOLD)), self.MAX_DELAY
                    )
                until = now + delay if delay else 0.0
            entry[0] = float(fails)
            entry[1] = until
            entry[2] = now
            entry[3] = max(0.0, entry[3] - 1.0)
            release_slot = True
            if entry[3] == 0 and entry[0] == 0 and entry[1] <= now:
                self._state.pop(ip, None)
        if release_slot:
            self._expensive_slots.release()

    def retry_after(self, ip: str) -> float:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            entry = self._state.get(ip)
            if not entry:
                return 0.0
            return max(0.0, entry[1] - now)

    def register_failure(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            entry = self._state.get(ip)
            if entry is None and not self._make_room_locked(ip):
                return
            fails = int(entry[0]) + 1 if entry else 1
            delay = 0.0
            if fails >= self.THRESHOLD:
                delay = min(
                    self.BASE_DELAY * (2 ** (fails - self.THRESHOLD)), self.MAX_DELAY
                )
            until = now + delay if delay else 0.0
            in_flight = entry[3] if entry else 0.0
            self._state[ip] = [float(fails), until, now, in_flight]

    def register_success(self, ip: str) -> None:
        with self._lock:
            entry = self._state.get(ip)
            if entry is None:
                return
            entry[0] = 0.0
            entry[1] = 0.0
            entry[2] = time.monotonic()
            if entry[3] == 0:
                self._state.pop(ip, None)

    def prune(self) -> None:
        """Oublie les IPs sans activité récente (appelé périodiquement)."""
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
