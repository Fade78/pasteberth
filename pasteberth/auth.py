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
import secrets
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
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def load_password_hash(path: Path) -> str | None:
    """Lit le fichier ``passwd`` (première ligne). Rechargé à chaque essai :
    un changement via `pasteberth passwd` est effectif sans redémarrage."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"lecture impossible de {path} : {exc}") from exc
    return raw.splitlines()[0] if raw else None


def save_password_hash(path: Path, password_hash: str) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password_hash + "\n")


# ----------------------------------------------------------------- sessions


class SessionStore:
    """Sessions en mémoire : token -> expiration (monotonic)."""

    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = time.monotonic() + self.ttl
        return token

    def validate(self, token: str | None) -> bool:
        if not token or len(token) > 128:
            return False
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
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
        expired = [t for t, exp in self._sessions.items() if exp < now]
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

    _FORGET_AFTER = 3600.0  # sans échec pendant 1h, on oublie l'historique

    def __init__(self) -> None:
        # ip -> [échecs consécutifs, verrouillé jusqu'à, dernier événement]
        self._state: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, ip: str) -> float:
        with self._lock:
            entry = self._state.get(ip)
            if not entry:
                return 0.0
            return max(0.0, entry[1] - time.monotonic())

    def register_failure(self, ip: str) -> None:
        with self._lock:
            entry = self._state.get(ip)
            fails = int(entry[0]) + 1 if entry else 1
            delay = 0.0
            if fails >= self.THRESHOLD:
                delay = min(
                    self.BASE_DELAY * (2 ** (fails - self.THRESHOLD)), self.MAX_DELAY
                )
            until = time.monotonic() + delay if delay else 0.0
            self._state[ip] = [float(fails), until, time.monotonic()]

    def register_success(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)

    def prune(self) -> None:
        """Oublie les IPs sans activité récente (appelé périodiquement)."""
        now = time.monotonic()
        with self._lock:
            stale = [
                ip
                for ip, (_, until, last) in self._state.items()
                if until < now and now - last > self._FORGET_AFTER
            ]
            for ip in stale:
                del self._state[ip]
