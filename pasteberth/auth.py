"""Authentication: scrypt password hashes, server sessions, and login rate
limiting.

- The password is NEVER stored in plaintext: a salted scrypt hash is kept in a
  ``passwd`` file (mode 0600) next to the configuration.
- Sessions are server-side (revocable by logout), identified by a random
  256-bit token; the cookie is only a reference.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from pasteberth.platformfs import UnsupportedFilesystemError, platform_fs

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024
_MAX_PASSWORD_FILE_BYTES = 16 * 1024
DEFAULT_MAX_SESSIONS = 4096
MAX_SESSIONS = 1_000_000


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
    """Compare safely; return false when no hash is configured."""
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
    """Check hash structure without running scrypt."""
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
    """Read the first line of ``passwd`` on every attempt.

    A change made by `pasteberth passwd` takes effect without a restart.
    """
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    fs = platform_fs()
    try:
        with fs.open_directory(path.parent) as parent:
            with fs.open_existing(parent, path.name, mode="rb") as fh:
                entry = fs.entry_info(parent, path.name)
                if (
                    entry is None
                    or not entry.is_regular
                    or entry.is_symlink
                    or entry.identity != fh.identity
                ):
                    raise RuntimeError(f"passwd is not a regular file: {path}")
                if entry.mode is not None and entry.mode & 0o077:
                    raise RuntimeError(f"permissions are too open on {path} (0600 required)")
                if not fs.is_owned(entry):
                    raise RuntimeError(f"passwd is not owned by the process: {path}")
                encoded = fh.read(_MAX_PASSWORD_FILE_BYTES + 1)
        if len(encoded) > _MAX_PASSWORD_FILE_BYTES:
            raise RuntimeError(f"passwd file is too large: {path}")
        raw = encoded.decode("utf-8").strip()
    except FileNotFoundError:
        return None
    except RuntimeError:
        raise
    except (OSError, ValueError, UnicodeError, UnsupportedFilesystemError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    return raw.splitlines()[0] if raw else None


def save_password_hash(path: Path, password_hash: str) -> None:
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    fs = platform_fs()
    temp_name = f".passwd-{secrets.token_hex(12)}.tmp"
    with fs.open_directory(path.parent, create=True, mode=0o700) as parent:
        file_handle = None
        temp_identity = None
        try:
            file_handle = fs.create_exclusive(
                parent,
                temp_name,
                mode="w",
                permissions=0o600,
            )
            temp_identity = file_handle.identity
            with file_handle as fh:
                fh.write(password_hash + "\n")
                fh.sync()
            fs.replace(
                parent,
                temp_name,
                path.name,
                expected_source=temp_identity,
            )
            fs.flush_directory(parent)
        except BaseException:
            if file_handle is not None and not file_handle.closed:
                file_handle.close()
            if temp_identity is not None:
                try:
                    fs.remove_expected(parent, temp_name, temp_identity)
                except OSError:
                    pass
            raise


# ----------------------------------------------------------------- sessions


class SessionStore:
    """In-memory sessions: token -> expiration (monotonic clock)."""

    def __init__(
        self,
        ttl_seconds: int,
        password_file: Path | None = None,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ):
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int):
            raise ValueError("max_sessions must be a positive integer")
        if not (1 <= max_sessions <= MAX_SESSIONS):
            raise ValueError(f"max_sessions must be between 1 and {MAX_SESSIONS}")
        self.ttl = ttl_seconds
        self._password_file = password_file
        self.max_sessions = max_sessions
        self._sessions: dict[str, tuple[float, tuple[int, int, int] | None]] = {}
        self._lock = threading.Lock()

    def _password_epoch(self) -> tuple[int, int, int] | None:
        if self._password_file is None:
            return None
        return platform_fs().path_version(self._password_file)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked()
            while len(self._sessions) >= self.max_sessions:
                oldest = next(iter(self._sessions))
                del self._sessions[oldest]
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
    """Per-IP limiting: consecutive failures cause an increasing delay."""

    THRESHOLD = 5
    BASE_DELAY = 30.0
    MAX_DELAY = 900.0
    MAX_CONCURRENT_CHECKS = 4
    MAX_TRACKED_IPS = 4096

    _FORGET_AFTER = 3600.0  # Forget the history after one hour without failure.

    def __init__(self, max_concurrent_checks: int | None = None) -> None:
        # ip -> [consecutive failures, locked until, last event,
        #        expensive checks in progress]
        self._state: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        checks = (
            self.MAX_CONCURRENT_CHECKS
            if max_concurrent_checks is None
            else max_concurrent_checks
        )
        if checks < 1:
            raise ValueError("max_concurrent_checks must be positive")
        self._expensive_slots = threading.BoundedSemaphore(checks)

    def _make_room_locked(self, ip: str, now: float) -> bool:
        if ip in self._state or len(self._state) < self.MAX_TRACKED_IPS:
            return True
        idle = [
            (entry[2], candidate)
            for candidate, entry in self._state.items()
            if entry[3] == 0 and entry[1] <= now
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
        """Atomically reserve at most one scrypt check per IP."""
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
            if not self._make_room_locked(ip, now):
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
            if entry is None and not self._make_room_locked(ip, now):
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
