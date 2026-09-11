"""Persistent bearer-token credentials and scope grants.

The token registry is deliberately separate from file sidecars and from the
in-memory browser sessions.  Tokens are long-lived capabilities: only a hash
of their secret is persisted, while grants and suspension rules remain
transactional and survive a process restart.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .platformfs import platform_fs


PERMISSION_LIST = 1
PERMISSION_READ = 2
PERMISSION_WRITE = 4
PERMISSION_ALL = PERMISSION_LIST | PERMISSION_READ | PERMISSION_WRITE
PERMISSION_NAMES = ("L", "R", "W")
SCOPE_ZONE = "zone"
SCOPE_GROUP = "group"
SCOPE_PATH = "path"
SCOPE_GLOBAL = "global"
SCOPE_TYPES = frozenset({SCOPE_ZONE, SCOPE_GROUP, SCOPE_PATH, SCOPE_GLOBAL})
SUSPENSION_SCOPE_TYPES = frozenset({SCOPE_ZONE, SCOPE_GROUP, SCOPE_GLOBAL})

_TOKEN_PREFIX = "pb_"
_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_TOKEN_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_TOKEN_LENGTH = 160
_MAX_LABEL_LENGTH = 120
_MAX_SCOPE_VALUE_LENGTH = 4096
_MAX_GRANTS = 128
# Keep explicit expirations representable by both SQLite and Python time
# arithmetic. ``None`` remains the opt-in value for a permanent token.
MAX_TOKEN_DURATION_SECONDS = 2**63 - 1


class TokenStoreError(RuntimeError):
    """The token registry cannot provide a safe credential operation."""


def permission_names(permissions: int) -> list[str]:
    """Return stable human/API names for a permission bit mask."""
    if isinstance(permissions, bool) or not isinstance(permissions, int):
        raise ValueError("permissions must be an integer")
    if permissions < 0 or permissions & ~PERMISSION_ALL:
        raise ValueError("invalid permission mask")
    return [
        name
        for bit, name in zip(
            (PERMISSION_LIST, PERMISSION_READ, PERMISSION_WRITE),
            PERMISSION_NAMES,
        )
        if permissions & bit
    ]


def parse_permissions(value: object) -> int:
    """Parse a JSON permission list such as ``["L", "R"]``."""
    if not isinstance(value, list):
        raise ValueError("permissions must be a list")
    if not all(isinstance(item, str) and item in PERMISSION_NAMES for item in value):
        raise ValueError("permissions must contain only L, R, and W")
    if len(value) != len(set(value)):
        raise ValueError("permissions must not contain duplicates")
    return sum(
        bit
        for bit, name in zip(
            (PERMISSION_LIST, PERMISSION_READ, PERMISSION_WRITE),
            PERMISSION_NAMES,
        )
        if name in value
    )


@dataclass(frozen=True)
class TokenGrant:
    """One scope and its effective permissions for a token."""

    scope_type: str
    scope_value: str
    permissions: int
    allow_replace: bool = False

    def __post_init__(self) -> None:
        if self.scope_type not in SCOPE_TYPES:
            raise ValueError(f"invalid token scope type: {self.scope_type!r}")
        if not isinstance(self.scope_value, str):
            raise ValueError("token scope value must be a string")
        if len(self.scope_value) > _MAX_SCOPE_VALUE_LENGTH:
            raise ValueError("token scope value is too long")
        if self.scope_type == SCOPE_GLOBAL and self.scope_value:
            raise ValueError("global token scope must not have a value")
        if self.scope_type != SCOPE_GLOBAL and not self.scope_value:
            raise ValueError("non-global token scope requires a value")
        if self.scope_type == SCOPE_PATH:
            object.__setattr__(self, "scope_value", normalize_scope_path(self.scope_value))
        if isinstance(self.permissions, bool) or not isinstance(self.permissions, int):
            raise ValueError("token permissions must be an integer")
        if self.permissions < 0 or self.permissions & ~PERMISSION_ALL:
            raise ValueError("invalid token permissions")
        if not isinstance(self.allow_replace, bool):
            raise ValueError("allow_replace must be a boolean")
        if self.allow_replace and not (self.permissions & PERMISSION_WRITE):
            raise ValueError("allow_replace requires write permission")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "scope_type": self.scope_type,
            "permissions": permission_names(self.permissions),
            "allow_replace": self.allow_replace,
        }
        if self.scope_type != SCOPE_GLOBAL:
            result["scope_value"] = self.scope_value
        return result


@dataclass(frozen=True)
class TokenRecord:
    """Persisted token metadata without the bearer secret."""

    token_id: str
    label: str
    grants: tuple[TokenGrant, ...]
    created_at: float
    expires_at: float | None
    revoked_at: float | None = None
    rotated_at: float | None = None

    def is_active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > now
        )

    def as_dict(self, now: float | None = None) -> dict[str, object]:
        now = time.time() if now is None else now
        return {
            "token_id": self.token_id,
            "label": self.label,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "rotated_at": self.rotated_at,
            "active": self.is_active(now),
            "grants": [grant.as_dict() for grant in self.grants],
        }


@dataclass(frozen=True)
class AccessSnapshot:
    """Published zone/group identities used for capability evaluation."""

    zone_paths: Mapping[str, str]
    group_zone_ids: Mapping[str, tuple[str, ...]]


def normalize_scope_path(value: object) -> str:
    """Normalize a PATH grant using the same path identity as zone lookup."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("path scope must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("path scope must be absolute")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path scope cannot be resolved") from exc
    return os.path.normcase(os.path.normpath(str(resolved)))


def grant_zone_ids(grant: TokenGrant, snapshot: AccessSnapshot) -> set[str]:
    """Resolve one grant to active zones; ambiguous PATH scopes fail closed."""
    if grant.scope_type == SCOPE_GLOBAL:
        return set(snapshot.zone_paths)
    if grant.scope_type == SCOPE_ZONE:
        return {grant.scope_value} if grant.scope_value in snapshot.zone_paths else set()
    if grant.scope_type == SCOPE_GROUP:
        return set(snapshot.group_zone_ids.get(grant.scope_value, ()))
    if grant.scope_type == SCOPE_PATH:
        matches = {
            zone_id
            for zone_id, path in snapshot.zone_paths.items()
            if path == grant.scope_value
        }
        return matches if len(matches) == 1 else set()
    return set()


def token_zone_access(
    record: TokenRecord,
    zone_id: str,
    snapshot: AccessSnapshot,
    suspensions: set[tuple[str, str]],
    *,
    now: float | None = None,
) -> tuple[int, bool]:
    """Return effective permissions and replacement policy for one zone."""
    if not record.is_active(now):
        return 0, False
    if zone_id not in snapshot.zone_paths:
        return 0, False
    if (SCOPE_GLOBAL, "") in suspensions or (SCOPE_ZONE, zone_id) in suspensions:
        return 0, False
    if any(
        (SCOPE_GROUP, group_name) in suspensions
        for group_name, zone_ids in snapshot.group_zone_ids.items()
        if zone_id in zone_ids
    ):
        return 0, False
    permissions = 0
    allow_replace = False
    for grant in record.grants:
        if zone_id not in grant_zone_ids(grant, snapshot):
            continue
        permissions |= grant.permissions
        allow_replace = allow_replace or grant.allow_replace
    return permissions, allow_replace


def token_covers_zone(
    record: TokenRecord,
    zone_id: str,
    snapshot: AccessSnapshot,
    suspensions: set[tuple[str, str]],
    *,
    now: float | None = None,
) -> bool:
    """Return whether a token has an active, non-suspended scope for a zone."""
    if not record.is_active(now) or zone_id not in snapshot.zone_paths:
        return False
    if (SCOPE_GLOBAL, "") in suspensions or (SCOPE_ZONE, zone_id) in suspensions:
        return False
    if any(
        (SCOPE_GROUP, group_name) in suspensions
        for group_name, zone_ids in snapshot.group_zone_ids.items()
        if zone_id in zone_ids
    ):
        return False
    return any(zone_id in grant_zone_ids(grant, snapshot) for grant in record.grants)


def parse_token(value: object) -> tuple[str, str] | None:
    """Return the public selector and secret from a bearer credential."""
    if not isinstance(value, str) or not value or len(value) > _MAX_TOKEN_LENGTH:
        return None
    if not value.startswith(_TOKEN_PREFIX):
        return None
    selector, separator, secret = value[len(_TOKEN_PREFIX):].partition(".")
    if (
        not separator
        or not _TOKEN_ID_RE.fullmatch(selector)
        or not _TOKEN_SECRET_RE.fullmatch(secret)
    ):
        return None
    return selector, secret


def _secret_digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("ascii")).digest()


class TokenStore:
    """SQLite-backed token registry with private-file checks."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raw_path = Path(path).expanduser()
        if not raw_path.is_absolute():
            raw_path = Path.cwd() / raw_path
        try:
            if raw_path.is_symlink():
                raise TokenStoreError(f"token registry must not be a symlink: {raw_path}")
            self.path = raw_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            if isinstance(exc, TokenStoreError):
                raise
            raise TokenStoreError(f"cannot resolve token registry: {raw_path}") from exc
        self._clock = clock
        self._lock = threading.RLock()
        self._ensure_private_file()
        self._initialize()

    def _ensure_private_file(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise TokenStoreError(
                f"cannot create token registry directory: {self.path.parent}"
            ) from exc
        try:
            parent_info = self.path.parent.lstat()
        except OSError as exc:
            raise TokenStoreError(
                f"cannot inspect token registry directory: {self.path.parent}"
            ) from exc
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise TokenStoreError(
                f"token registry parent is not a regular directory: {self.path.parent}"
            )
        try:
            parent_audit = platform_fs().audit_permissions(self.path.parent, directory=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise TokenStoreError(
                f"cannot audit token registry directory: {self.path.parent}"
            ) from exc
        if parent_audit.directory_is_writable_by_other():
            raise TokenStoreError(
                f"permissions are too open on token registry directory: {self.path.parent}"
            )
        current = self.path.parent
        fs = platform_fs()
        while True:
            try:
                ancestor_audit = fs.audit_permissions(current, directory=True)
            except (OSError, ValueError, RuntimeError) as exc:
                raise TokenStoreError(
                    f"cannot audit token registry ancestor: {current}"
                ) from exc
            if ancestor_audit.directory_is_writable_by_other():
                raise TokenStoreError(
                    f"permissions are too open on token registry ancestor: {current}"
                )
            if current == Path(current.anchor):
                break
            current = current.parent
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            fd = -1
            try:
                fd = os.open(self.path, flags | nofollow, 0o600)
            except FileExistsError:
                pass
            except OSError as exc:
                raise TokenStoreError(f"cannot create token registry: {self.path}") from exc
            finally:
                if fd >= 0:
                    os.close(fd)
            if fd >= 0:
                return
            try:
                info = self.path.lstat()
            except OSError as exc:
                raise TokenStoreError(f"cannot inspect token registry: {self.path}") from exc
        except OSError as exc:
            raise TokenStoreError(f"cannot inspect token registry: {self.path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TokenStoreError(f"token registry is not a regular file: {self.path}")
        try:
            audit = platform_fs().audit_permissions(self.path, directory=False)
        except (OSError, ValueError, RuntimeError) as exc:
            raise TokenStoreError(f"cannot audit token registry: {self.path}") from exc
        if not audit.private:
            raise TokenStoreError(
                f"permissions are too open on token registry: {self.path}"
            )

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_file()
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise TokenStoreError(f"cannot open token registry: {self.path}") from exc

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tokens (
                        token_id TEXT PRIMARY KEY,
                        label TEXT NOT NULL,
                        secret_hash BLOB NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL,
                        revoked_at REAL,
                        rotated_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS token_grants (
                        token_id TEXT NOT NULL REFERENCES tokens(token_id) ON DELETE CASCADE,
                        scope_type TEXT NOT NULL,
                        scope_value TEXT NOT NULL,
                        permissions INTEGER NOT NULL,
                        allow_replace INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (token_id, scope_type, scope_value),
                        CHECK (scope_type IN ('zone', 'group', 'path', 'global')),
                        CHECK (permissions >= 0 AND permissions <= 7),
                        CHECK (allow_replace IN (0, 1))
                    );
                    CREATE TABLE IF NOT EXISTS token_suspensions (
                        scope_type TEXT NOT NULL,
                        scope_value TEXT NOT NULL,
                        suspended_at REAL NOT NULL,
                        PRIMARY KEY (scope_type, scope_value),
                        CHECK (scope_type IN ('zone', 'group', 'global'))
                    );
                    CREATE INDEX IF NOT EXISTS token_grants_scope
                        ON token_grants(scope_type, scope_value);
                    """
                )
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot initialize token registry: {self.path}") from exc
            finally:
                connection.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except TokenStoreError:
                connection.rollback()
                raise
            except (OSError, sqlite3.Error) as exc:
                connection.rollback()
                raise TokenStoreError(f"token registry transaction failed: {self.path}") from exc
            finally:
                connection.close()

    @staticmethod
    def _validate_label(label: object) -> str:
        if not isinstance(label, str):
            raise ValueError("token label must be a string")
        label = label.strip()
        if not label:
            raise ValueError("token label must not be empty")
        if len(label) > _MAX_LABEL_LENGTH:
            raise ValueError("token label is too long")
        return label

    @staticmethod
    def _validate_grants(grants: Iterable[TokenGrant]) -> tuple[TokenGrant, ...]:
        result = tuple(grants)
        if not result:
            raise ValueError("at least one token grant is required")
        if len(result) > _MAX_GRANTS:
            raise ValueError("too many token grants")
        if not all(isinstance(grant, TokenGrant) for grant in result):
            raise ValueError("token grants must be TokenGrant values")
        keys = [(grant.scope_type, grant.scope_value) for grant in result]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate token grant scope")
        return result

    @staticmethod
    def _validate_duration(duration_seconds: object) -> float | None:
        if duration_seconds is None:
            return None
        if isinstance(duration_seconds, bool):
            raise ValueError("duration_seconds must be positive or null")
        if isinstance(duration_seconds, int):
            if duration_seconds <= 0 or duration_seconds > MAX_TOKEN_DURATION_SECONDS:
                raise ValueError("duration_seconds must be positive or null")
            return float(duration_seconds)
        if not isinstance(duration_seconds, float):
            raise ValueError("duration_seconds must be positive or null")
        try:
            duration = float(duration_seconds)
        except (OverflowError, ValueError):
            raise ValueError("duration_seconds must be positive or null") from None
        if (
            not math.isfinite(duration)
            or duration <= 0
            or duration > MAX_TOKEN_DURATION_SECONDS
        ):
            raise ValueError("duration_seconds must be positive or null")
        return duration

    @staticmethod
    def _new_secret() -> tuple[str, str, str]:
        token_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        return token_id, secret, f"{_TOKEN_PREFIX}{token_id}.{secret}"

    @staticmethod
    def _grants_for(connection: sqlite3.Connection, token_id: str) -> tuple[TokenGrant, ...]:
        rows = connection.execute(
            """
            SELECT scope_type, scope_value, permissions, allow_replace
            FROM token_grants
            WHERE token_id = ?
            ORDER BY scope_type, scope_value
            """,
            (token_id,),
        ).fetchall()
        try:
            return tuple(
                TokenGrant(
                    scope_type=row["scope_type"],
                    scope_value=row["scope_value"],
                    permissions=int(row["permissions"]),
                    allow_replace=bool(row["allow_replace"]),
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenStoreError(f"invalid grant in token registry: {token_id}") from exc

    @classmethod
    def _record_from_row(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TokenRecord:
        try:
            return TokenRecord(
                token_id=str(row["token_id"]),
                label=str(row["label"]),
                grants=cls._grants_for(connection, str(row["token_id"])),
                created_at=float(row["created_at"]),
                expires_at=(
                    None if row["expires_at"] is None else float(row["expires_at"])
                ),
                revoked_at=(
                    None if row["revoked_at"] is None else float(row["revoked_at"])
                ),
                rotated_at=(
                    None if row["rotated_at"] is None else float(row["rotated_at"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenStoreError("invalid token record in token registry") from exc

    def create(
        self,
        label: str,
        grants: Iterable[TokenGrant],
        *,
        duration_seconds: int | float | None,
    ) -> tuple[TokenRecord, str]:
        label = self._validate_label(label)
        grants = self._validate_grants(grants)
        duration = self._validate_duration(duration_seconds)
        now = float(self._clock())
        expires_at = None if duration is None else now + duration
        with self._transaction() as connection:
            for _ in range(5):
                token_id, secret, credential = self._new_secret()
                try:
                    connection.execute(
                        """
                        INSERT INTO tokens
                            (token_id, label, secret_hash, created_at, expires_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            token_id,
                            label,
                            sqlite3.Binary(_secret_digest(secret)),
                            now,
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                connection.executemany(
                    """
                    INSERT INTO token_grants
                        (token_id, scope_type, scope_value, permissions, allow_replace)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            token_id,
                            grant.scope_type,
                            grant.scope_value,
                            grant.permissions,
                            int(grant.allow_replace),
                        )
                        for grant in grants
                    ],
                )
                row = connection.execute(
                    "SELECT * FROM tokens WHERE token_id = ?",
                    (token_id,),
                ).fetchone()
                if row is None:
                    raise TokenStoreError("created token disappeared from registry")
                return self._record_from_row(connection, row), credential
        raise TokenStoreError("could not allocate a unique token identifier")

    def get(self, token_id: str) -> TokenRecord | None:
        if not _TOKEN_ID_RE.fullmatch(token_id or ""):
            return None
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM tokens WHERE token_id = ?",
                    (token_id,),
                ).fetchone()
                return None if row is None else self._record_from_row(connection, row)
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot read token registry: {self.path}") from exc
            finally:
                connection.close()

    def list(self) -> list[TokenRecord]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM tokens ORDER BY created_at, token_id"
                ).fetchall()
                return [self._record_from_row(connection, row) for row in rows]
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot read token registry: {self.path}") from exc
            finally:
                connection.close()

    def authenticate(self, credential: object) -> TokenRecord | None:
        parsed = parse_token(credential)
        if parsed is None:
            return None
        token_id, secret = parsed
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM tokens WHERE token_id = ?",
                    (token_id,),
                ).fetchone()
                if row is None:
                    return None
                stored = row["secret_hash"]
                if not isinstance(stored, (bytes, bytearray, memoryview)):
                    return None
                if not hmac.compare_digest(bytes(stored), _secret_digest(secret)):
                    return None
                now = float(self._clock())
                if row["revoked_at"] is not None:
                    return None
                if row["expires_at"] is not None and float(row["expires_at"]) <= now:
                    return None
                return self._record_from_row(connection, row)
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot authenticate token: {self.path}") from exc
            finally:
                connection.close()

    def _mutate_expiry(
        self,
        token_id: str,
        duration_seconds: int | float | None,
        *,
        rotate: bool,
    ) -> tuple[TokenRecord, str | None]:
        if not _TOKEN_ID_RE.fullmatch(token_id or ""):
            raise KeyError(token_id)
        duration = self._validate_duration(duration_seconds)
        now = float(self._clock())
        expires_at = None if duration is None else now + duration
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if row is None:
                raise KeyError(token_id)
            if row["revoked_at"] is not None:
                raise ValueError("cannot modify a revoked token")
            credential = None
            if rotate:
                _selector, secret, _unused_credential = self._new_secret()
                credential = f"{_TOKEN_PREFIX}{token_id}.{secret}"
                connection.execute(
                    """
                    UPDATE tokens
                    SET secret_hash = ?, expires_at = ?, rotated_at = ?
                    WHERE token_id = ?
                    """,
                    (sqlite3.Binary(_secret_digest(secret)), expires_at, now, token_id),
                )
            else:
                connection.execute(
                    "UPDATE tokens SET expires_at = ? WHERE token_id = ?",
                    (expires_at, token_id),
                )
            updated = connection.execute(
                "SELECT * FROM tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if updated is None:
                raise TokenStoreError("modified token disappeared from registry")
            return self._record_from_row(connection, updated), credential

    def extend(
        self,
        token_id: str,
        *,
        duration_seconds: int | float | None,
    ) -> TokenRecord:
        record, _credential = self._mutate_expiry(
            token_id,
            duration_seconds,
            rotate=False,
        )
        return record

    def rotate(
        self,
        token_id: str,
        *,
        duration_seconds: int | float | None,
    ) -> tuple[TokenRecord, str]:
        record, credential = self._mutate_expiry(
            token_id,
            duration_seconds,
            rotate=True,
        )
        if credential is None:
            raise TokenStoreError("rotated token did not produce a secret")
        return record, credential

    def revoke(self, token_id: str) -> TokenRecord:
        if not _TOKEN_ID_RE.fullmatch(token_id or ""):
            raise KeyError(token_id)
        now = float(self._clock())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if row is None:
                raise KeyError(token_id)
            if row["revoked_at"] is None:
                connection.execute(
                    "UPDATE tokens SET revoked_at = ? WHERE token_id = ?",
                    (now, token_id),
                )
            updated = connection.execute(
                "SELECT * FROM tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if updated is None:
                raise TokenStoreError("revoked token disappeared from registry")
            return self._record_from_row(connection, updated)

    @staticmethod
    def _validate_suspension(scope_type: str, scope_value: str) -> tuple[str, str]:
        if scope_type not in SUSPENSION_SCOPE_TYPES:
            raise ValueError("invalid suspension scope type")
        if not isinstance(scope_value, str):
            raise ValueError("suspension scope value must be a string")
        if scope_type == SCOPE_GLOBAL:
            if scope_value:
                raise ValueError("global suspension must not have a value")
        elif not scope_value:
            raise ValueError("non-global suspension requires a value")
        if len(scope_value) > _MAX_SCOPE_VALUE_LENGTH:
            raise ValueError("suspension scope value is too long")
        return scope_type, scope_value

    def set_suspension(self, scope_type: str, scope_value: str, suspended: bool) -> None:
        scope_type, scope_value = self._validate_suspension(scope_type, scope_value)
        if not isinstance(suspended, bool):
            raise ValueError("suspended must be a boolean")
        now = float(self._clock())
        with self._transaction() as connection:
            if suspended:
                connection.execute(
                    """
                    INSERT INTO token_suspensions(scope_type, scope_value, suspended_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(scope_type, scope_value)
                    DO UPDATE SET suspended_at = excluded.suspended_at
                    """,
                    (scope_type, scope_value, now),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM token_suspensions
                    WHERE scope_type = ? AND scope_value = ?
                    """,
                    (scope_type, scope_value),
                )

    def suspensions(self) -> set[tuple[str, str]]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT scope_type, scope_value FROM token_suspensions"
                ).fetchall()
                return {(str(row["scope_type"]), str(row["scope_value"])) for row in rows}
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot read token suspensions: {self.path}") from exc
            finally:
                connection.close()

    def suspension_list(self) -> list[dict[str, object]]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT scope_type, scope_value, suspended_at
                    FROM token_suspensions
                    ORDER BY scope_type, scope_value
                    """
                ).fetchall()
                return [
                    {
                        "scope_type": str(row["scope_type"]),
                        **(
                            {}
                            if row["scope_type"] == SCOPE_GLOBAL
                            else {"scope_value": str(row["scope_value"])}
                        ),
                        "suspended_at": float(row["suspended_at"]),
                    }
                    for row in rows
                ]
            except sqlite3.Error as exc:
                raise TokenStoreError(f"cannot read token suspensions: {self.path}") from exc
            finally:
                connection.close()
