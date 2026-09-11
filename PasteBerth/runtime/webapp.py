"""HTTP layer: threaded standard-library server, routing, and security.

Security checks handled here:
- server-side session authentication (HttpOnly / SameSite=Lax cookie, Secure
  whenever the effective scheme is HTTPS);
- Origin checks on every unsafe request (CSRF), combined with SameSite=Lax;
- X-Forwarded-* headers honored ONLY from a peer declared in
  ``trusted_proxies``;
- systematic security headers (strict CSP without inline content, nosniff);
- request bodies capped and parsed within fixed bounds;
- no uncontrolled content reflected in responses.
"""
from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import __version__
from .auth import LoginRateLimiter, SessionStore, load_password_hash, verify_password
from .config import Config, public_path
from .multipart import (
    MultipartError,
    extract_boundary,
    parse_multipart,
)
from .service import PasteService, ServiceError
from .tokens import (
    AccessSnapshot,
    PERMISSION_ALL,
    PERMISSION_LIST,
    PERMISSION_READ,
    PERMISSION_WRITE,
    SCOPE_GLOBAL,
    SCOPE_GROUP,
    SCOPE_PATH,
    SCOPE_TYPES,
    SCOPE_ZONE,
    TokenGrant,
    TokenRecord,
    TokenStore,
    TokenStoreError,
    MAX_TOKEN_DURATION_SECONDS,
    grant_zone_ids,
    normalize_scope_path,
    parse_permissions,
    permission_names,
    token_covers_zone,
    token_zone_access,
)

log = logging.getLogger("pasteberth.http")

COOKIE_NAME = "pb_session"
_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"

_ZONE_RE = r"([a-z0-9][a-z0-9_-]{0,63})"
_FILENAME_RE = r"([^/\\\x00]+)"
_TOKEN_ID_RE = r"([0-9a-f]{24})"
_ROUTES: tuple[tuple[str, re.Pattern, str], ...] = tuple(
    (method, re.compile(pattern), name)
    for method, pattern, name in (
        ("GET", r"^/api/health$", "h_health"),
        ("GET", r"^/api/zones$", "h_zones"),
        ("GET", rf"^/api/zones/{_ZONE_RE}/access$", "h_zone_access"),
        ("GET", rf"^/api/zones/{_ZONE_RE}/items$", "h_zone_images"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/items$", "h_zone_upload"),
        ("GET", rf"^/api/zones/{_ZONE_RE}/items/{_FILENAME_RE}/content$", "h_preview"),
        ("HEAD", rf"^/api/zones/{_ZONE_RE}/items/{_FILENAME_RE}/content$", "h_preview"),
        ("PATCH", rf"^/api/zones/{_ZONE_RE}/items/{_FILENAME_RE}/comment$", "h_zone_comment"),
        ("DELETE", rf"^/api/zones/{_ZONE_RE}/items/{_FILENAME_RE}$", "h_zone_delete"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/items/batch-delete$", "h_zone_delete_batch"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/items/archive$", "h_zone_archive"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/items/regularize$", "h_zone_regularize"),
        ("GET", r"^/api/groups$", "h_groups"),
        ("POST", r"^/api/drop/resolve$", "h_drop_resolve"),
        ("GET", rf"^/api/zones/{_ZONE_RE}/images$", "h_zone_images"),
        ("PATCH", rf"^/api/zones/{_ZONE_RE}/images/{_FILENAME_RE}/comment$", "h_zone_comment"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/images/regularize$", "h_zone_regularize"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/images$", "h_zone_upload"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/images/batch-delete$", "h_zone_delete_batch"),
        ("POST", r"^/api/transfers$", "h_transfer"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/images/archive$", "h_zone_archive"),
        ("DELETE", rf"^/api/zones/{_ZONE_RE}/images/{_FILENAME_RE}$", "h_zone_delete"),
        ("GET", rf"^/previews/{_ZONE_RE}/{_FILENAME_RE}$", "h_preview"),
        ("HEAD", rf"^/previews/{_ZONE_RE}/{_FILENAME_RE}$", "h_preview"),
        ("GET", r"^/api/tokens$", "h_tokens"),
        ("POST", r"^/api/tokens$", "h_token_create"),
        ("POST", rf"^/api/tokens/{_TOKEN_ID_RE}/extend$", "h_token_extend"),
        ("POST", rf"^/api/tokens/{_TOKEN_ID_RE}/rotate$", "h_token_rotate"),
        ("DELETE", rf"^/api/tokens/{_TOKEN_ID_RE}$", "h_token_revoke"),
        ("GET", r"^/api/token-suspensions$", "h_token_suspensions"),
        ("POST", r"^/api/token-suspensions$", "h_token_suspension_set"),
        ("POST", r"^/login$", "h_login_post"),
        ("POST", r"^/logout$", "h_logout"),
        ("GET", r"^/login$", "h_login_page"),
        ("GET", r"^/static/app\.js$", "h_static_app_js"),
        ("GET", r"^/static/style\.css$", "h_static_style_css"),
        ("GET", r"^/static/favicon\.svg$", "h_static_favicon"),
        ("GET", r"^/$", "h_index"),
    )
)

_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
)

_SENSITIVE_QUERY_KEYS = frozenset({
    "accesstoken",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
    "jwt",
    "session",
    "sessionid",
    "sid",
})
_QUERY_COMPONENT_RE = re.compile(r'([?&;])([^=&#;\s"]+)=([^&#;\s"]*)')


def _decode_query_component(value: str) -> str:
    for _ in range(8):
        decoded = urllib.parse.unquote_plus(value)
        if decoded == value:
            break
        value = decoded
    return value


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[-_]", "", key.casefold())
    return normalized in _SENSITIVE_QUERY_KEYS or normalized.endswith(
        ("apikey", "credential", "password", "secret", "token")
    )


def _query_contains_sensitive_credentials(value: str) -> bool:
    if "%25" in value.casefold():
        return True
    decoded = _decode_query_component(value)
    for match in re.finditer(r"(?:^|[?&;])([^=&#;\s]+)=", decoded):
        if _is_sensitive_query_key(match.group(1)):
            return True
    return False


class ClientAbort(Exception):
    """The client interrupted the request (lost network, timeout, etc.)."""


class BodyTooLarge(Exception):
    pass


class HeaderTooLarge(Exception):
    pass


class HeaderBudgetReader:
    """Count bytes read during the HTTP header phase."""

    def __init__(self, raw, max_bytes: int | None):
        self._raw = raw
        self._max_bytes = max_bytes
        self._read_bytes = 0
        self._enabled = True

    def _count(self, data: bytes) -> bytes:
        if self._enabled:
            self._read_bytes += len(data)
            if self._max_bytes is not None and self._read_bytes > self._max_bytes:
                raise HeaderTooLarge()
        return data

    def readline(self, *args, **kwargs):
        return self._count(self._raw.readline(*args, **kwargs))

    def read(self, *args, **kwargs):
        return self._count(self._raw.read(*args, **kwargs))

    def disable(self) -> None:
        self._enabled = False

    def reset(self) -> None:
        self._read_bytes = 0
        self._enabled = True

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _ChunkedWriter:
    """Non-seekable adapter for writing a ZIP with HTTP chunked encoding."""

    def __init__(self, handler):
        self.handler = handler
        self.offset = 0
        self.aborted = False

    def write(self, data) -> int:
        try:
            if self.aborted:
                raise ClientAbort()
            data = bytes(data)
            if not data:
                return 0
            deadline = getattr(self.handler, "_archive_deadline", None)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("archive duration exceeded")
            for chunk in (f"{len(data):X}\r\n".encode("ascii"), data, b"\r\n"):
                self.handler._check_download_active()
                self.handler.wfile.write(chunk)
            self.flush()
            self.offset += len(data)
            self.handler._stream_last_activity = time.monotonic()
            return len(data)
        except BaseException:
            self.aborted = True
            raise

    def tell(self) -> int:
        return self.offset

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        try:
            if self.aborted:
                raise ClientAbort()
            self.handler._check_download_active()
            self.handler.wfile.flush()
        except BaseException:
            self.aborted = True
            raise


def _parse_filename_list(
    body: bytes,
    content_type: str,
    *,
    max_names: int | None = None,
) -> list[str]:
    """Parse a list of names for zone operations."""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON") from exc
        filenames = payload.get("filenames") if isinstance(payload, dict) else None
    elif media_type == "application/x-www-form-urlencoded":
        try:
            fields = urllib.parse.parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=max_names,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid form") from exc
        filenames = fields.get("filename", [])
    else:
        raise ValueError("Content-Type must be JSON or form data")
    if not isinstance(filenames, list) or not filenames:
        raise ValueError("'filenames' list is required")
    if max_names is not None and len(filenames) > max_names:
        raise ValueError("too many files requested")
    if not all(isinstance(filename, str) for filename in filenames):
        raise ValueError("filenames must be strings")
    return filenames


def _json_object_without_duplicates(pairs):
    """Reject duplicate keys instead of silently accepting the last value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _item_api_payload(payload: dict) -> dict:
    """Adapt only API-owned objects and collections, never user metadata."""
    result = dict(payload)
    if "content_url" in result:
        result.pop("preview_url", None)
    if "images" in result:
        result["items"] = result.pop("images")
    for key in ("zones", "items"):
        if key in result:
            result[key] = [_item_api_payload(item) for item in result[key]]
    if "failed" in result:
        result["failed"] = [
            {**failure, "code": "unknown_item"}
            if failure.get("code") == "unknown_image" else dict(failure)
            for failure in result["failed"]
        ]
    if "error" in result and result["error"].get("code") == "unknown_image":
        result["error"] = {**result["error"], "code": "unknown_item"}
    return result


def _if_match_satisfied(values: list[str], etag: str | None) -> bool:
    """Strong comparison for an acquired item; repeated lines form one list.

    A wildcard tests existence even without a stored digest. Empty list members
    and malformed conditions are rejected rather than silently ignored.
    """
    if not values:
        return True
    value = ",".join(values).strip(" \t")
    if value == "*":
        return True
    tag_pattern = r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"'
    if not re.fullmatch(rf'{tag_pattern}(?:[ \t]*,[ \t]*{tag_pattern})*', value):
        raise ServiceError("invalid_request", "invalid If-Match entity-tag list")
    return etag is not None and etag in re.findall(tag_pattern, value)


def _safe_log_text(value: object, *, limit: int | None = None) -> str:
    """Make HTTP control characters harmless in log text output."""
    text = str(value)
    def redact_query(match: re.Match) -> str:
        raw_key = match.group(2)
        key = _decode_query_component(raw_key)
        value = (
            "[REDACTED]"
            if "%25" in raw_key.casefold()
            or _is_sensitive_query_key(key)
            or _query_contains_sensitive_credentials(match.group(3))
            else match.group(3)
        )
        return f"{match.group(1)}{match.group(2)}={value}"

    text = _QUERY_COMPONENT_RE.sub(redact_query, text)
    if limit is not None:
        text = text[:limit]
    return "".join(
        f"\\x{ord(char):02x}"
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        else char
        for char in text
    )


def _query_without_sensitive_credentials(query: str) -> str:
    """Preserve ordinary query parameters without forwarding credential values."""
    safe_parts = []
    for part in re.split(r"[&;]", query):
        raw_key = part.partition("=")[0]
        key = _decode_query_component(raw_key)
        value = part.partition("=")[2]
        if (
            "%25" not in raw_key.casefold()
            and not _is_sensitive_query_key(key)
            and not _query_contains_sensitive_credentials(value)
        ):
            safe_parts.append(part)
    return "&".join(safe_parts)


@dataclass(frozen=True)
class _Principal:
    """The credential type that authenticated one HTTP request."""

    kind: str
    token: TokenRecord | None = None


_UNSET_TOKEN_VALUE = object()


def _parse_token_duration(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_TOKEN_DURATION_SECONDS
    ):
        raise ValueError("duration_seconds must be a positive integer or null")
    return value


def _parse_token_grants(value: object) -> tuple[TokenGrant, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("grants must be a non-empty list")
    grants: list[TokenGrant] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("each grant must be an object")
        allowed = {"scope_type", "scope_value", "permissions", "allow_replace"}
        if set(raw) - allowed or "scope_type" not in raw or "permissions" not in raw:
            raise ValueError("invalid grant fields")
        scope_type = raw["scope_type"]
        if not isinstance(scope_type, str) or scope_type not in SCOPE_TYPES:
            raise ValueError("invalid grant scope type")
        scope_value = raw.get("scope_value", "")
        if not isinstance(scope_value, str):
            raise ValueError("grant scope value must be a string")
        if scope_type == SCOPE_GLOBAL:
            if scope_value:
                raise ValueError("global grant must not have a value")
        elif not scope_value:
            raise ValueError("non-global grant requires a value")
        elif scope_type == SCOPE_ZONE and not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", scope_value
        ):
            raise ValueError("invalid zone ID grant")
        elif scope_type == SCOPE_PATH:
            scope_value = normalize_scope_path(scope_value)
        permissions = parse_permissions(raw["permissions"])
        allow_replace = raw.get("allow_replace", False)
        if not isinstance(allow_replace, bool):
            raise ValueError("allow_replace must be a boolean")
        grant = TokenGrant(
            scope_type=scope_type,
            scope_value=scope_value,
            permissions=permissions,
            allow_replace=allow_replace,
        )
        key = (grant.scope_type, grant.scope_value)
        if key in seen:
            raise ValueError("duplicate grant scope")
        seen.add(key)
        grants.append(grant)
    return tuple(grants)


def make_handler(
    cfg: Config,
    service: PasteService,
    sessions: SessionStore,
    limiter: LoginRateLimiter,
    tokens: TokenStore | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the handler class with injected dependencies."""
    class PasteberthHandler(BaseHTTPRequestHandler):
        server_version = "Pasteberth"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = cfg.limits.http_request_timeout_seconds

        def _expire_request(self, token: object) -> None:
            with self._request_timer_lock:
                if token is not self._request_token or self.timeout is None:
                    return
                delay = 0
                if self._streaming_response:
                    delay = self.timeout - (time.monotonic() - self._stream_last_activity)
                if delay <= 0:
                    self.close_connection = True
                    self._request_expired = True
                    # Ownership must remain locked through shutdown, not just its check.
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
            timer = threading.Timer(delay, self._expire_request, args=(token,))
            timer.daemon = True
            with self._request_timer_lock:
                if token is not self._request_token:
                    timer.cancel()
                    return
                self._request_timer = timer
                timer.start()

        def _expire_archive(self, token: object) -> None:
            with self._request_timer_lock:
                if token is not self._request_token or self._archive_deadline is None:
                    return
                self._archive_deadline = 0
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def handle_one_request(self) -> None:
            # Socket timeouts reset on reads; this timer imposes a real maximum
            # duration for the whole request.
            token = object()
            self._principal_value = None
            self._access_snapshot_value = None
            self._token_suspensions_value = None
            with self._request_timer_lock:
                self._request_token = token
                self._response_started = False
                self._streaming_response = False
                self._request_expired = False
                self._stream_last_activity = time.monotonic()
                self._request_deadline = (
                    None if self.timeout is None else self._stream_last_activity + self.timeout
                )
            self.rfile.reset()
            try:
                if self.timeout is not None:
                    timer = threading.Timer(self.timeout, self._expire_request, args=(token,))
                    timer.daemon = True
                    with self._request_timer_lock:
                        self._request_timer = timer
                        timer.start()
                super().handle_one_request()
            except HeaderTooLarge:
                self.close_connection = True
                try:
                    self.send_error(431, "HTTP headers are too large")
                except OSError:
                    pass
            except (OSError, TimeoutError):
                self.close_connection = True
            finally:
                with self._request_timer_lock:
                    self._request_token = None
                    if self._request_timer is not None:
                        self._request_timer.cancel()
                        self._request_timer = None
                    self._streaming_response = False

        def setup(self) -> None:
            self._request_timer_lock = threading.Lock()
            self._request_token = None
            self._request_timer = None
            super().setup()
            # The server admits a connection to a short pending-header pool
            # before this handler can promote it after parsing the headers.
            self.connection.settimeout(self.server.header_timeout)
            self.rfile = HeaderBudgetReader(self.rfile, cfg.limits.max_http_header_bytes)

        def parse_request(self) -> bool:
            try:
                parsed = super().parse_request()
                if parsed and not self._host_allowed():
                    self.close_connection = True
                    self._route_path = self.path.split("?")[0]
                    self._error(403, "forbidden_host", "host is not allowed")
                    return False
                if parsed and not self.server.promote_request(self.connection):
                    self.close_connection = True
                    self._route_path = self.path.split("?")[0]
                    self._error(
                        503,
                        "server_busy",
                        "request capacity is temporarily exhausted",
                        extra_headers=[("Retry-After", "1")],
                    )
                    return False
                return parsed
            except HeaderTooLarge:
                self.close_connection = True
                self.send_error(431, "HTTP headers are too large")
                return False
            finally:
                self.rfile.disable()

        # ---------------------------------------------------- network context

        def _peer_ip(self) -> str:
            try:
                return self.client_address[0]
            except Exception:
                return ""

        def _trusted_peer(self) -> bool:
            try:
                peer = ipaddress.ip_address(self._peer_ip())
            except ValueError:
                return False
            return any(peer in net for net in cfg.trusted_proxies)

        def _client_ip(self) -> str:
            if self._trusted_peer():
                xff = self.headers.get("X-Forwarded-For")
                if xff:
                    # A trusted proxy may overwrite XFF or append the real
                    # client on the right; only the nearest hop is
                    # Therefore accept it, never a client value farther left.
                    candidate = xff.rsplit(",", 1)[-1].strip()
                    try:
                        return str(ipaddress.ip_address(candidate))
                    except ValueError:
                        pass
            return self._peer_ip()

        def _scheme(self) -> str:
            if isinstance(self.connection, ssl.SSLSocket):
                return "https"
            if self._trusted_peer():
                proto = self.headers.get("X-Forwarded-Proto", "")
                if proto.split(",")[0].strip().lower() == "https":
                    return "https"
            return "http"

        def _host(self) -> str:
            host = self.headers.get("Host", "").strip()
            return host[:253] if host else "localhost"

        @staticmethod
        def _host_name(netloc: str) -> str | None:
            raw = netloc.strip()
            ip_candidate = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
            try:
                return str(ipaddress.ip_address(ip_candidate)).lower()
            except ValueError:
                pass
            try:
                parsed = urllib.parse.urlsplit("//" + raw)
                hostname = parsed.hostname
                parsed.port  # Validate a possible port before accepting the host.
            except ValueError:
                return None
            if (
                not hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return None
            try:
                return str(ipaddress.ip_address(hostname)).lower()
            except ValueError:
                pass
            return hostname.lower().rstrip(".")

        def _host_allowed(self) -> bool:
            host_name = self._host_name(self._host())
            if host_name is None:
                return False
            # An empty allowed_hosts list is a wildcard. Every hostname is
            # accepted, while Origin must still match the request Host.
            # An explicit list restores strict checking.
            if not cfg.allowed_hosts:
                return True
            return host_name in cfg.allowed_hosts

        def _public_path(self, path: str) -> str:
            return public_path(cfg.url_prefix, path)

        def _expected_origin(self) -> str:
            scheme = self._scheme()
            return f"{scheme}://{self._normalize_netloc(self._host(), scheme)}"

        @staticmethod
        def _normalize_netloc(netloc: str, scheme: str) -> str:
            netloc = netloc.strip().lower()
            m = re.fullmatch(r"([^@]*@)?(\[[^\]]+\]|[^:]+)(?::(\d+))?", netloc)
            if not m:
                return netloc
            userinfo = m.group(1) or ""
            host_part = m.group(2)
            port = m.group(3)
            default_port = "443" if scheme.lower() == "https" else "80"
            try:
                address = ipaddress.ip_address(host_part.strip("[]"))
            except ValueError:
                host_part = host_part.rstrip(".")
            else:
                host_part = str(address).lower()
                if address.version == 6:
                    host_part = f"[{host_part}]"
            normalized = userinfo + host_part
            if port == default_port:
                return normalized
            if port:
                normalized += f":{port}"
            return normalized

        def _origin_allowed(self) -> bool:
            """CSRF: a browser-provided Origin/Referer must match the request."""
            if not self._host_allowed():
                return False
            origin = self.headers.get("Origin")
            opaque_origin = bool(origin and origin.strip().lower() == "null")
            if opaque_origin:
                origin = None
            if not origin:
                referer = self.headers.get("Referer")
                if not referer:
                    if opaque_origin:
                        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
                        return fetch_site == "same-origin"
                    # Non-browser clients (curl, scripts) are allowed.
                    return True
                try:
                    parsed = urllib.parse.urlsplit(referer)
                    if (
                        not parsed.scheme
                        or not parsed.netloc
                        or parsed.username is not None
                        or parsed.password is not None
                    ):
                        return False
                    parsed.port  # Validate a possible port before accepting the referer.
                except ValueError:
                    return False
                origin = f"{parsed.scheme}://{parsed.netloc}"
            try:
                got = urllib.parse.urlsplit(origin)
                if (
                    not got.scheme
                    or not got.netloc
                    or got.username is not None
                    or got.password is not None
                    or got.path
                    or got.query
                    or got.fragment
                ):
                    return False
                got.port  # Validate a possible port before accepting the origin.
            except ValueError:
                return False
            got_scheme = got.scheme.lower()
            got_origin = f"{got_scheme}://{self._normalize_netloc(got.netloc, got_scheme)}"
            return got_origin == self._expected_origin()

        # ----------------------------------------------------------- cookies

        def _session_token(self) -> str | None:
            raw = self.headers.get("Cookie", "")
            try:
                jar = SimpleCookie(raw)
            except Exception:
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None

        def _principal(self) -> _Principal:
            cached = getattr(self, "_principal_value", None)
            if cached is not None:
                return cached
            authorization = self.headers.get_all("Authorization", [])
            session = self._session_token()
            if authorization:
                principal = _Principal("invalid")
                if len(authorization) == 1 and session is None:
                    parts = authorization[0].strip().split()
                    if (
                        cfg.auth.enabled
                        and tokens is not None
                        and len(parts) == 2
                        and parts[0].lower() == "bearer"
                    ):
                        try:
                            record = tokens.authenticate(parts[1])
                        except TokenStoreError:
                            log.exception("token registry authentication failed")
                            principal = _Principal("token_store_error")
                            record = None
                        if record is not None:
                            principal = _Principal("token", record)
            elif cfg.auth.enabled:
                principal = _Principal("admin") if sessions.validate(session) else _Principal("invalid")
            else:
                principal = _Principal("anonymous")
            self._principal_value = principal
            return principal

        def _is_authenticated(self) -> bool:
            return self._principal().kind in {"admin", "anonymous"}

        def _session_cookie(self, token: str) -> str:
            attrs = [
                f"{COOKIE_NAME}={token}",
                f"Path={cfg.url_prefix or '/'}",
                f"Max-Age={cfg.auth.session_ttl_hours * 3600}",
                "HttpOnly",
                "SameSite=Lax",
            ]
            if self._scheme() == "https":
                attrs.append("Secure")
            return "; ".join(attrs)

        def _clear_cookie(self) -> str:
            attrs = [
                f"{COOKIE_NAME}=",
                f"Path={cfg.url_prefix or '/'}",
                "Max-Age=0",
                "HttpOnly",
                "SameSite=Lax",
            ]
            if self._scheme() == "https":
                attrs.append("Secure")
            return "; ".join(attrs)

        # ------------------------------------------------------------ responses

        def _finish(
            self,
            status: int,
            ctype: str,
            body: bytes,
            *,
            extra_headers: list[tuple[str, str]] | None = None,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, value in self._security_headers():
                self.send_header(key, value)
            for key, value in extra_headers or []:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            log.info(
                "%s %s -> %d (%.1f ms) client=%s",
                _safe_log_text(self.command),
                _safe_log_text(self.path.split("?")[0], limit=200),
                status,
                (time.monotonic() - getattr(self, "_t_start", time.monotonic())) * 1000,
                _safe_log_text(self._client_ip()),
            )

        def _security_headers(self) -> tuple[tuple[str, str], ...]:
            headers = list(_SECURITY_HEADERS)
            if self._scheme() == "https":
                headers.append(("Strict-Transport-Security", "max-age=31536000"))
            return tuple(headers)

        def _json(self, status: int, payload: dict, **kwargs) -> None:
            if getattr(self, "_item_schema", False):
                payload = _item_api_payload(payload)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._finish(status, "application/json; charset=utf-8", body, **kwargs)

        def _error(self, status: int, code: str, message: str, **kwargs) -> None:
            request_path = getattr(self, "_route_path", self.path)
            if request_path.startswith("/api/") or request_path.startswith("/previews/"):
                self._json(status, {"error": {"code": code, "message": message}}, **kwargs)
            else:
                body = (
                    f"<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    f"<title>{status}</title><body><h1>{html.escape(str(status))}</h1>"
                    f"<p>{html.escape(message)}</p></body></html>".encode("utf-8")
                )
                self._finish(status, "text/html; charset=utf-8", body, **kwargs)

        def _service_error(self, exc: ServiceError) -> None:
            if getattr(self, "_response_started", False):
                self.close_connection = True
                return
            self._check_download_active()
            extra = [("Retry-After", "1")] if exc.code in {"zone_busy", "server_busy"} else []
            self._error(exc.status, exc.code, str(exc), extra_headers=extra)

        def _write_service_error(
            self,
            access: tuple[int, bool],
            exc: ServiceError,
            *,
            code: str,
            message: str,
            blind_permission: int = PERMISSION_READ,
        ) -> None:
            principal = self._principal()
            if principal.kind == "token" and not access[0] & blind_permission:
                self._error(exc.status, code, message)
            else:
                self._service_error(exc)

        @staticmethod
        def _redact_write_only_failures(result: dict, message: str) -> dict:
            failures = []
            for failure in result.get("failed", []):
                if not isinstance(failure, dict):
                    continue
                safe_failure = {
                    key: value
                    for key, value in failure.items()
                    if key not in {"message", "target_published"}
                }
                if "message" in failure:
                    safe_failure["message"] = message
                failures.append(safe_failure)
            return {**result, "failed": failures}

        # ------------------------------------------------------------- lecture

        def _validate_request_framing(self) -> bool:
            self.close_connection = self.close_connection or self.command != "GET"
            get_all = getattr(self.headers, "get_all", None)
            host_values = get_all("Host", []) if get_all else []
            content_lengths = get_all("Content-Length", []) if get_all else []
            transfer_encodings = get_all("Transfer-Encoding", []) if get_all else []
            connection_values = get_all("Connection", []) if get_all else []
            if any(
                token.strip().lower() == "close"
                for value in connection_values
                for token in value.split(",")
            ):
                self.close_connection = True
            if len(host_values) > 1 or len(content_lengths) > 1 or transfer_encodings:
                self.close_connection = True
                self._error(400, "invalid_request", "ambiguous request framing")
                return False
            if content_lengths:
                value = content_lengths[0].strip()
                if not value.isdigit():
                    self.close_connection = True
                    self._error(400, "invalid_request", "invalid Content-Length")
                    return False
            if self.headers.get("Content-Length") is not None:
                self.close_connection = True
            return True

        def _read_body(
            self,
            max_bytes: int | None = None,
        ) -> tuple[bytes, int]:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise BodyTooLarge()  # Chunked encoding is not supported in V1.
            length_raw = self.headers.get("Content-Length")
            if length_raw is None:
                return b"", 0
            try:
                length = int(length_raw)
            except ValueError:
                raise ClientAbort()
            if length < 0:
                raise ClientAbort()
            limit = max_bytes
            if limit is not None and length > limit:
                self.close_connection = True
                raise BodyTooLarge()
            chunks: list[bytes] = []
            remaining = length
            while remaining > 0:
                try:
                    chunk = self.rfile.read(min(remaining, 65536))
                except OSError as exc:
                    raise ClientAbort() from exc
                if not chunk:
                    raise ClientAbort()
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks), 0

        # --------------------------------------------------------- dispatch

        def _dispatch(self) -> None:
            self._t_start = time.monotonic()
            self._item_schema = False
            self._route_path = self.path.split("?")[0]
            if not self._validate_request_framing():
                return
            if re.search(r"%(?![0-9A-Fa-f]{2})", self.path):
                self.close_connection = True
                self._error(400, "invalid_request", "invalid path encoding")
                return
            try:
                path = urllib.parse.unquote(self.path.split("?")[0], errors="strict")
            except (UnicodeDecodeError, ValueError):
                self.close_connection = True
                self._error(400, "invalid_request", "invalid path encoding")
                return
            self._route_path = path
            if "\\" in path or any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
                self.close_connection = True
                self._error(400, "invalid_request", "invalid request")
                return
            if cfg.url_prefix:
                if path == cfg.url_prefix:
                    if self.command == "GET":
                        query = self.path.split("?", 1)[1] if "?" in self.path else ""
                        query = _query_without_sensitive_credentials(query)
                        location = self._public_path("/")
                        if query:
                            location += "?" + query
                        self._redirect(location)
                    else:
                        self._error(404, "not_found", "resource not found")
                    return
                prefix = cfg.url_prefix + "/"
                if not path.startswith(prefix):
                    self._error(404, "not_found", "resource not found")
                    return
                path = path[len(cfg.url_prefix):]
                self._route_path = path
            self._item_schema = bool(
                re.fullmatch(rf"/api/zones/{_ZONE_RE}/items(?:/.*)?", path)
            )
            if not self._host_allowed():
                self._error(403, "forbidden_host", "host is not allowed")
                return
            for method, pattern, name in _ROUTES:
                if self.command != method:
                    continue
                match = pattern.fullmatch(path)
                if match:
                    if method in ("POST", "DELETE", "PATCH") and not self._origin_allowed():
                        log.warning(
                            "origin rejected %s from %s",
                            _safe_log_text(
                                self.headers.get("Origin") or self.headers.get("Referer")
                            ),
                            _safe_log_text(self._client_ip()),
                        )
                        self._error(403, "forbidden_origin", "origin is not allowed")
                        return
                    handler = getattr(self, "_" + name)
                    handler(*match.groups())
                    return
            if self.command not in ("GET", "HEAD", "POST"):
                self._error(405, "method_not_allowed", "method is not allowed")
            elif any(p.fullmatch(path) for _, p, _ in _ROUTES):
                self._error(405, "method_not_allowed", "method is not allowed for this resource")
            else:
                self._error(404, "not_found", "resource not found")

        def do_GET(self) -> None:
            try:
                self._dispatch()
            except ClientAbort:
                self.close_connection = True
                log.info(
                    "client disconnected during request (%s)",
                    _safe_log_text(self.path, limit=100),
                )
            except BrokenPipeError:
                self.close_connection = True
            except Exception:
                log.exception(
                    "internal error on %s",
                    _safe_log_text(self.path, limit=200),
                )
                if getattr(self, "_response_started", False) or getattr(self, "_request_expired", False):
                    self.close_connection = True
                    return
                try:
                    self._error(500, "internal", "internal error")
                except Exception:
                    self.close_connection = True

        do_POST = do_GET
        do_HEAD = do_GET
        do_PUT = do_GET
        do_DELETE = do_GET
        do_PATCH = do_GET
        do_OPTIONS = do_GET

        def log_message(self, fmt: str, *args) -> None:  # Disable the default logger.
            try:
                rendered = fmt % args
            except (TypeError, ValueError):
                rendered = f"{fmt} {args!r}"
            log.debug(
                "peer %s %s",
                _safe_log_text(self.address_string()),
                _safe_log_text(rendered),
            )

        # ------------------------------------------------------ pages statiques

        _STATIC_FILES = {
            "h_static_app_js": (_STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
            "h_static_style_css": (_STATIC_DIR / "style.css", "text/css; charset=utf-8"),
            "h_static_favicon": (_STATIC_DIR / "favicon.svg", "image/svg+xml"),
        }

        def _serve_static(
            self,
            path: Path,
            ctype: str,
            *,
            cache_control: str = "no-store",
        ) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._error(404, "not_found", "resource not found")
                return
            self._finish(200, ctype, data, cache_control=cache_control)

        def _h_static_app_js(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_app_js"][0],
                               self._STATIC_FILES["h_static_app_js"][1])

        def _h_static_style_css(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_style_css"][0],
                               self._STATIC_FILES["h_static_style_css"][1])

        def _h_static_favicon(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_favicon"][0],
                               self._STATIC_FILES["h_static_favicon"][1],
                               cache_control="public, max-age=300")

        def _h_health(self) -> None:
            self._json(200, {"ok": True})

        def _h_index(self) -> None:
            if not self._is_authenticated():
                self._redirect(self._public_path("/login"))
                return
            try:
                data = (_TEMPLATES_DIR / "index.html").read_bytes()
            except OSError:
                self._error(500, "internal", "interface unavailable")
                return
            data = data.replace(b"__PASTEBERTH_VERSION__", __version__.encode("ascii"))
            data = data.replace(
                b"__PASTEBERTH_URL_PREFIX__",
                cfg.url_prefix.encode("ascii"),
            )
            self._finish(200, "text/html; charset=utf-8", data)

        def _redirect(self, location: str, status: int = 303) -> None:
            body = b""
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, value in self._security_headers():
                self.send_header(key, value)
            self.end_headers()
            log.info(
                "%s %s -> %d redirect %s",
                _safe_log_text(self.command),
                _safe_log_text(self.path, limit=200),
                status,
                _safe_log_text(location, limit=200),
            )

        # -------------------------------------------------------------- login

        def _render_login(self, status: int, message: str = "") -> None:
            try:
                template = (_TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
            except OSError:
                self._error(500, "internal", "interface unavailable")
                return
            block = (
                f'<p class="login-error" role="alert">{html.escape(message)}</p>'
                if message
                else ""
            )
            page = template.replace("__ERROR_BLOCK__", block).replace(
                "__PASTEBERTH_URL_PREFIX__",
                html.escape(cfg.url_prefix, quote=True),
            )
            self._finish(status, "text/html; charset=utf-8", page.encode("utf-8"))

        def _h_login_page(self) -> None:
            if not cfg.auth.enabled:
                self._redirect(self._public_path("/"))
                return
            if self._is_authenticated():
                self._redirect(self._public_path("/"))
                return
            self._render_login(200)

        def _h_login_post(self) -> None:
            if not cfg.auth.enabled:
                self._redirect(self._public_path("/"))
                return
            ip = self._client_ip()
            acquired = False
            released = False
            try:
                try:
                    body, _ = self._read_body(cfg.limits.max_login_body_bytes)
                except BodyTooLarge:
                    self.close_connection = True
                    self._error(413, "too_large", "login body is too large")
                    return
                except ClientAbort:
                    raise
                password = ""
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "multipart/form-data" in ctype:
                    boundary = extract_boundary(self.headers.get("Content-Type", ""))
                    try:
                        fields = parse_multipart(
                            body,
                            boundary or "",
                            max_parts=cfg.limits.max_multipart_parts,
                            max_header_bytes=cfg.limits.max_multipart_header_bytes,
                            max_field_name_length=cfg.limits.max_multipart_field_name_length,
                        )
                    except MultipartError:
                        password = ""
                    else:
                        _, _, raw = fields.get("password", (None, None, b""))
                        password = (raw or b"").decode("utf-8", "replace")
                elif "application/json" in ctype:
                    try:
                        parsed = json.loads(body.decode("utf-8"))
                        password = str(parsed.get("password", "")) if isinstance(parsed, dict) else ""
                    except (ValueError, AttributeError):
                        password = ""
                else:
                    try:
                        values = urllib.parse.parse_qs(
                            body.decode("utf-8", "replace"),
                            max_num_fields=cfg.limits.max_login_fields,
                        )
                    except ValueError:
                        values = {}
                    password = values.get("password", [""])[0]

                retry_after = limiter.acquire(ip)
                if retry_after > 0:
                    self._json(
                        429,
                        {"error": {"code": "rate_limited",
                                   "message": "too many attempts, try again later"}},
                        extra_headers=[("Retry-After", str(int(retry_after) + 1))],
                    )
                    return
                acquired = True
                stored_hash = load_password_hash(
                    cfg.password_file(),
                    max_bytes=cfg.limits.max_password_file_bytes,
                )
                if password and verify_password(
                    password,
                    stored_hash,
                    maxmem=cfg.limits.max_scrypt_memory_bytes,
                ):
                    limiter.complete(ip, success=True)
                    released = True
                    log.info("login succeeded (%s)", _safe_log_text(ip))
                    self._do_login_success()
                else:
                    time.sleep(0.5)
                    limiter.complete(ip, success=False)
                    released = True
                    log.warning("login failed (%s)", _safe_log_text(ip))
                    self._render_login(401, "Incorrect password.")
            finally:
                if acquired and not released:
                    limiter.release(ip)

        # Successful login: redirect and set the session cookie in one response.
        def _do_login_success(self) -> None:
            token = sessions.create()
            self.send_response(303)
            self.send_header("Location", self._public_path("/"))
            self.send_header("Set-Cookie", self._session_cookie(token))
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, value in self._security_headers():
                self.send_header(key, value)
            self.end_headers()

        def _h_logout(self) -> None:
            authorization = self.headers.get_all("Authorization", [])
            if authorization:
                principal = self._principal()
                if principal.kind == "token_store_error":
                    self._error(503, "token_store_error", "token registry is unavailable")
                    return
                if principal.kind == "token":
                    self._error(403, "forbidden", "bearer tokens cannot log out sessions")
                else:
                    self._error(
                        401,
                        "unauthorized",
                        "ambiguous or invalid authentication credentials",
                        extra_headers=[("WWW-Authenticate", 'Bearer realm="PasteBerth"')],
                    )
                return
            sessions.revoke(self._session_token())
            self.send_response(303)
            self.send_header("Location", self._public_path("/login"))
            self.send_header("Set-Cookie", self._clear_cookie())
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, value in self._security_headers():
                self.send_header(key, value)
            self.end_headers()

        # ---------------------------------------------------------------- API

        def _require_auth_api(self) -> bool:
            if self._principal().kind == "token_store_error":
                self._error(503, "token_store_error", "token registry is unavailable")
                return False
            if self._principal().kind in {"admin", "anonymous", "token"}:
                return True
            self._json(
                401,
                {"error": {"code": "unauthorized", "message": "authentication required"}},
                extra_headers=[("WWW-Authenticate", 'Bearer realm="PasteBerth"')],
            )
            return False

        def _require_admin_api(self) -> bool:
            principal = self._principal()
            if principal.kind == "admin":
                return True
            if principal.kind == "token_store_error":
                self._error(503, "token_store_error", "token registry is unavailable")
                return False
            if principal.kind == "token":
                self._json(
                    403,
                    {"error": {"code": "admin_required", "message": "administrator access required"}},
                )
            else:
                self._json(
                    401,
                    {"error": {"code": "unauthorized", "message": "administrator authentication required"}},
                    extra_headers=[("WWW-Authenticate", 'Bearer realm="PasteBerth"')],
                )
            return False

        def _access_snapshot(self) -> AccessSnapshot:
            snapshot = getattr(self, "_access_snapshot_value", None)
            if snapshot is None:
                snapshot = service.access_snapshot()
                self._access_snapshot_value = snapshot
            return snapshot

        def _token_suspensions(self) -> set[tuple[str, str]] | None:
            suspensions = getattr(self, "_token_suspensions_value", None)
            if suspensions is None:
                if tokens is None:
                    suspensions = set()
                else:
                    try:
                        suspensions = tokens.suspensions()
                    except TokenStoreError:
                        log.exception("token registry suspension lookup failed")
                        self._error(503, "token_store_error", "token registry is unavailable")
                        suspensions = None
                self._token_suspensions_value = suspensions
            return suspensions

        def _zone_permissions(
            self,
            zid: str,
            required: int,
        ) -> tuple[int, bool] | None:
            principal = self._principal()
            if principal.kind in {"admin", "anonymous"}:
                return PERMISSION_ALL, True
            if principal.kind == "token_store_error":
                self._error(503, "token_store_error", "token registry is unavailable")
                return None
            if principal.kind != "token" or principal.token is None:
                self._json(
                    401,
                    {"error": {"code": "unauthorized", "message": "authentication required"}},
                    extra_headers=[("WWW-Authenticate", 'Bearer realm="PasteBerth"')],
                )
                return None
            snapshot = self._access_snapshot()
            suspensions = self._token_suspensions()
            if suspensions is None:
                return None
            if (
                zid not in snapshot.zone_paths
                or not token_covers_zone(
                    principal.token,
                    zid,
                    snapshot,
                    suspensions,
                )
            ):
                self._error(404, "unknown_zone", f"unknown zone: {zid}")
                return None
            permissions, allow_replace = token_zone_access(
                principal.token,
                zid,
                snapshot,
                suspensions,
            )
            if required and permissions & required != required:
                self._json(
                    403,
                    {"error": {"code": "forbidden", "message": "token permission denied"}},
                )
                return None
            return permissions, allow_replace

        def _token_zone_ids_with_permission(self, permission: int) -> set[str] | None:
            principal = self._principal()
            if principal.kind in {"admin", "anonymous"}:
                return set(self._access_snapshot().zone_paths)
            if principal.kind != "token" or principal.token is None:
                return set()
            snapshot = self._access_snapshot()
            suspensions = self._token_suspensions()
            if suspensions is None:
                return None
            result = set()
            for zid in snapshot.zone_paths:
                permissions, _allow_replace = token_zone_access(
                    principal.token,
                    zid,
                    snapshot,
                    suspensions,
                )
                if permissions & permission == permission:
                    result.add(zid)
            return result

        def _require_direct_drop_auth(self) -> bool:
            principal = self._principal()
            if principal.kind in {"admin", "anonymous"}:
                return True
            if principal.kind == "token_store_error":
                self._error(503, "token_store_error", "token registry is unavailable")
                return False
            if principal.kind == "token":
                self._json(
                    403,
                    {"error": {"code": "forbidden", "message": "bearer tokens cannot use direct-drop routes"}},
                )
                return False
            if self.headers.get_all("Authorization", []):
                self._json(
                    401,
                    {"error": {"code": "unauthorized", "message": "invalid authentication credentials"}},
                    extra_headers=[("WWW-Authenticate", 'Bearer realm="PasteBerth"')],
                )
                return False
            try:
                local_peer = ipaddress.ip_address(self._peer_ip()).is_loopback
            except ValueError:
                local_peer = False
            if local_peer:
                # The service still requires a random staging file in the zone;
                # loopback alone is not sufficient authorization.
                return True
            self._json(
                401,
                {"error": {"code": "unauthorized", "message": "authentication required"}},
            )
            return False

        def _select_item_schema(self) -> bool:
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
            )
            schemas = query.get("schema", ["images"])
            if len(schemas) != 1 or schemas[0] not in {"images", "items"}:
                self._error(400, "invalid_request", "schema must be 'images' or 'items' exactly once")
                return False
            self._item_schema = schemas[0] == "items"
            return True

        def _h_zones(self) -> None:
            if not self._require_auth_api() or not self._select_item_schema():
                return
            principal = self._principal()
            allowed_zone_ids = None
            if principal.kind == "token":
                allowed_zone_ids = self._token_zone_ids_with_permission(PERMISSION_LIST)
                if allowed_zone_ids is None:
                    return
                if not allowed_zone_ids:
                    self._json(
                        403,
                        {"error": {"code": "forbidden", "message": "token permission denied"}},
                    )
                    return
            try:
                overview = service.overview(
                    blocking=False,
                    zone_ids=allowed_zone_ids,
                )
                if principal.kind == "token":
                    overview["groups"] = [
                        {
                            key: value
                            for key, value in group.items()
                            if key != "pattern"
                        }
                        for group in overview.get("groups", [])
                        if group.get("zone_ids")
                    ]
            except ServiceError as exc:
                self._service_error(exc)
                return
            self._json(200, overview)

        def _h_groups(self) -> None:
            if not self._require_auth_api():
                return
            principal = self._principal()
            groups = service.group_overview()
            if principal.kind == "token":
                allowed_zone_ids = self._token_zone_ids_with_permission(PERMISSION_LIST)
                if allowed_zone_ids is None:
                    return
                if not allowed_zone_ids:
                    self._json(
                        403,
                        {"error": {"code": "forbidden", "message": "token permission denied"}},
                    )
                    return
                groups = [
                    {
                        **group,
                        "zone_ids": [
                            zid
                            for zid in group.get("zone_ids", [])
                            if zid in allowed_zone_ids
                        ],
                    }
                    for group in groups
                ]
                groups = [
                    {
                        **{
                            key: value
                            for key, value in group.items()
                            if key != "pattern"
                        },
                        "zone_count": len(group["zone_ids"]),
                    }
                    for group in groups
                ]
                groups = [group for group in groups if group["zone_ids"]]
            self._json(200, {"groups": groups})

        def _h_zone_access(self, zid: str) -> None:
            if not self._require_auth_api():
                return
            principal = self._principal()
            if principal.kind == "token":
                if principal.token is None:
                    self._error(503, "token_store_error", "token registry is unavailable")
                    return
                snapshot = self._access_snapshot()
                suspensions = self._token_suspensions()
                if suspensions is None:
                    return
                if not token_covers_zone(
                    principal.token,
                    zid,
                    snapshot,
                    suspensions,
                ):
                    self._error(404, "unknown_zone", f"unknown zone: {zid}")
                    return
                permissions, allow_replace = token_zone_access(
                    principal.token,
                    zid,
                    snapshot,
                    suspensions,
                )
                self._json(
                    200,
                    {
                        "zone": zid,
                        "exists": True,
                        "permissions": [
                            name
                            for bit, name in zip(
                                (PERMISSION_LIST, PERMISSION_READ, PERMISSION_WRITE),
                                ("L", "R", "W"),
                            )
                            if permissions & bit
                        ],
                        "allow_replace": allow_replace,
                    },
                )
                return
            try:
                exists = service.has_zone(zid)
            except ServiceError as exc:
                self._service_error(exc)
                return
            if not exists:
                self._error(404, "unknown_zone", f"unknown zone: {zid}")
                return
            self._json(
                200,
                {
                    "zone": zid,
                    "exists": True,
                    "permissions": ["L", "R", "W"],
                    "allow_replace": True,
                },
            )

        def _read_json_object(self, label: str) -> dict | None:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported_media_type", f"{label} must use application/json")
                return None
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_batch_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", f"{label} is too large")
                return None
            except ClientAbort:
                raise
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                self._error(400, "invalid_request", f"{label} must contain valid JSON")
                return None
            if not isinstance(payload, dict):
                self._error(400, "invalid_request", f"{label} must contain a JSON object")
                return None
            return payload

        def _token_failure(self, exc: BaseException) -> None:
            if isinstance(exc, KeyError):
                self._error(404, "unknown_token", "unknown token")
            elif isinstance(exc, ValueError):
                self._error(400, "invalid_request", str(exc))
            else:
                log.exception("token registry operation failed")
                self._error(503, "token_store_error", "token registry is unavailable")

        def _grant_status(
            self,
            grant: TokenGrant,
            snapshot: AccessSnapshot,
            suspensions: set[tuple[str, str]],
        ) -> dict[str, object]:
            resolved = grant_zone_ids(grant, snapshot)
            ordered_zone_ids = [
                zid for zid in snapshot.zone_paths if zid in resolved
            ]
            if grant.scope_type == SCOPE_GLOBAL:
                missing = False
            elif grant.scope_type == SCOPE_ZONE:
                missing = grant.scope_value not in snapshot.zone_paths
            elif grant.scope_type == SCOPE_GROUP:
                missing = grant.scope_value not in snapshot.group_zone_ids
            else:
                matching_paths = [
                    zid
                    for zid, path in snapshot.zone_paths.items()
                    if path == grant.scope_value
                ]
                missing = len(matching_paths) != 1
            def zone_suspended(zid: str) -> bool:
                return (
                    (SCOPE_GLOBAL, "") in suspensions
                    or (SCOPE_ZONE, zid) in suspensions
                    or any(
                        (SCOPE_GROUP, group_name) in suspensions
                        for group_name, zone_ids in snapshot.group_zone_ids.items()
                        if zid in zone_ids
                    )
                )

            suspended_zone_ids = {
                zid for zid in ordered_zone_ids if zone_suspended(zid)
            }
            if grant.scope_type == SCOPE_GROUP and (
                SCOPE_GROUP, grant.scope_value
            ) in suspensions:
                suspended_zone_ids = set(ordered_zone_ids)
            suspended = bool(ordered_zone_ids) and (
                len(suspended_zone_ids) == len(ordered_zone_ids)
            )
            result = grant.as_dict()
            result["resolved_zone_ids"] = ordered_zone_ids
            result["status"] = (
                "missing" if missing else "suspended" if suspended else "active"
            )
            return result

        def _token_admin_payload(self) -> dict[str, object] | None:
            if tokens is None:
                self._error(503, "token_store_error", "token registry is unavailable")
                return None
            try:
                records = tokens.list()
                suspension_list = tokens.suspension_list()
            except TokenStoreError as exc:
                self._token_failure(exc)
                return None
            snapshot = self._access_snapshot()
            suspensions = self._token_suspensions()
            if suspensions is None:
                return None
            now = time.time()
            token_payload = []
            for record in records:
                item = record.as_dict(now)
                item["grants"] = [
                    self._grant_status(grant, snapshot, suspensions)
                    for grant in record.grants
                ]
                if record.revoked_at is not None:
                    item["state"] = "revoked"
                elif not record.is_active(now):
                    item["state"] = "expired"
                elif any(grant["status"] == "active" for grant in item["grants"]):
                    item["state"] = "active"
                elif any(grant["status"] == "suspended" for grant in item["grants"]):
                    item["state"] = "suspended"
                else:
                    item["state"] = "missing"
                token_payload.append(item)
            try:
                catalog = service.access_catalog()
            except ServiceError as exc:
                self._service_error(exc)
                return None
            return {
                "tokens": token_payload,
                "suspensions": suspension_list,
                "catalog": catalog,
            }

        def _h_tokens(self) -> None:
            if not self._require_admin_api():
                return
            payload = self._token_admin_payload()
            if payload is not None:
                self._json(200, payload)

        def _h_token_create(self) -> None:
            if not self._require_admin_api():
                return
            payload = self._read_json_object("token request")
            if payload is None:
                return
            if set(payload) != {"label", "grants", "duration_seconds"}:
                self._error(
                    400,
                    "invalid_request",
                    "token request must contain only label, grants, and duration_seconds",
                )
                return
            try:
                duration = _parse_token_duration(payload["duration_seconds"])
                grants = _parse_token_grants(payload["grants"])
                if tokens is None:
                    raise TokenStoreError("token registry is unavailable")
                record, credential = tokens.create(
                    payload["label"],
                    grants,
                    duration_seconds=duration,
                )
            except (KeyError, ValueError, TokenStoreError) as exc:
                self._token_failure(exc)
                return
            result = record.as_dict()
            result["token"] = credential
            self._json(201, result)

        def _token_duration_request(self, label: str) -> int | None | object:
            payload = self._read_json_object(label)
            if payload is None:
                return _UNSET_TOKEN_VALUE
            if set(payload) != {"duration_seconds"}:
                self._error(
                    400,
                    "invalid_request",
                    f"{label} must contain only duration_seconds",
                )
                return _UNSET_TOKEN_VALUE
            try:
                return _parse_token_duration(payload["duration_seconds"])
            except ValueError as exc:
                self._token_failure(exc)
                return _UNSET_TOKEN_VALUE

        def _h_token_extend(self, token_id: str) -> None:
            if not self._require_admin_api():
                return
            duration = self._token_duration_request("token extension")
            if duration is _UNSET_TOKEN_VALUE:
                return
            assert duration is None or isinstance(duration, int)
            try:
                if tokens is None:
                    raise TokenStoreError("token registry is unavailable")
                record = tokens.extend(token_id, duration_seconds=duration)
            except (KeyError, ValueError, TokenStoreError) as exc:
                self._token_failure(exc)
                return
            self._json(200, record.as_dict())

        def _h_token_rotate(self, token_id: str) -> None:
            if not self._require_admin_api():
                return
            duration = self._token_duration_request("token rotation")
            if duration is _UNSET_TOKEN_VALUE:
                return
            assert duration is None or isinstance(duration, int)
            try:
                if tokens is None:
                    raise TokenStoreError("token registry is unavailable")
                record, credential = tokens.rotate(token_id, duration_seconds=duration)
            except (KeyError, ValueError, TokenStoreError) as exc:
                self._token_failure(exc)
                return
            result = record.as_dict()
            result["token"] = credential
            self._json(200, result)

        def _h_token_revoke(self, token_id: str) -> None:
            if not self._require_admin_api():
                return
            try:
                if tokens is None:
                    raise TokenStoreError("token registry is unavailable")
                tokens.revoke(token_id)
            except (KeyError, ValueError, TokenStoreError) as exc:
                self._token_failure(exc)
                return
            self._json(200, {"revoked": token_id})

        def _h_token_suspensions(self) -> None:
            if not self._require_admin_api():
                return
            if tokens is None:
                self._error(503, "token_store_error", "token registry is unavailable")
                return
            try:
                suspension_list = tokens.suspension_list()
            except TokenStoreError as exc:
                self._token_failure(exc)
                return
            self._json(200, {"suspensions": suspension_list})

        def _h_token_suspension_set(self) -> None:
            if not self._require_admin_api():
                return
            payload = self._read_json_object("token suspension request")
            if payload is None:
                return
            if set(payload) - {"scope_type", "scope_value", "suspended"} or {
                "scope_type",
                "suspended",
            } - set(payload):
                self._error(
                    400,
                    "invalid_request",
                    "suspension request must contain scope_type and suspended",
                )
                return
            scope_type = payload["scope_type"]
            scope_value = payload.get("scope_value", "")
            suspended = payload["suspended"]
            if not isinstance(scope_type, str) or not isinstance(scope_value, str):
                self._error(400, "invalid_request", "invalid suspension scope")
                return
            try:
                if tokens is None:
                    raise TokenStoreError("token registry is unavailable")
                tokens.set_suspension(scope_type, scope_value, suspended)
            except (KeyError, ValueError, TokenStoreError) as exc:
                self._token_failure(exc)
                return
            self._json(
                200,
                {
                    "scope_type": scope_type,
                    **({} if scope_type == SCOPE_GLOBAL else {"scope_value": scope_value}),
                    "suspended": suspended,
                },
            )

        def _h_drop_resolve(self) -> None:
            if not self._require_direct_drop_auth():
                return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                return
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_batch_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "resolve request is too large")
                return
            except ClientAbort:
                raise
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                self._error(400, "invalid_request", "resolve request must contain valid JSON")
                return
            if not isinstance(payload, dict) or set(payload) != {"directory"}:
                self._error(400, "invalid_request", "resolve request must contain only 'directory'")
                return
            directory = payload["directory"]
            if not isinstance(directory, str) or not directory:
                self._error(400, "invalid_request", "directory must be a non-empty string")
                return
            try:
                zone_id = service.zone_id_for_directory(directory)
            except ServiceError as exc:
                self._service_error(exc)
                return
            self._json(200, {"zone": zone_id})

        def _h_zone_images(self, zid: str) -> None:
            if self._zone_permissions(zid, PERMISSION_LIST) is None:
                return
            try:
                items = (
                    service.history(zid, blocking=False, _refresh=False)
                    if self._item_schema else service.history(zid)
                )
            except ServiceError as exc:
                self._service_error(exc)
                return
            self._json(200, {"zone": zid, "images": items})

        def _h_zone_regularize(self, zid: str) -> None:
            if not self._require_direct_drop_auth():
                return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                return
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_batch_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "request body is too large")
                return
            except ClientAbort:
                raise
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._error(400, "invalid_request", "invalid JSON")
                return
            if not isinstance(payload, dict):
                self._error(400, "invalid_request", "JSON object expected")
                return
            stage_name = payload.get("stage")
            filename = payload.get("filename")
            declared_mime = payload.get("mime")
            replace = payload.get("replace", False)
            if (
                not isinstance(stage_name, str)
                or not isinstance(filename, str)
                or not isinstance(declared_mime, str)
                or not isinstance(replace, bool)
            ):
                self._error(400, "invalid_request", "invalid direct-drop request")
                return
            try:
                item = service.regularize_staged_upload(
                    zid,
                    stage_name,
                    filename,
                    declared_mime,
                    allow_replace=replace,
                )
            except ServiceError as exc:
                self._service_error(exc)
                return
            self._json(200 if item.get("duplicate") else 201, item)

        def _h_zone_upload(self, zid: str) -> None:
            zone_access = self._zone_permissions(zid, PERMISSION_WRITE)
            if zone_access is None:
                return
            principal = self._principal()
            ctype_raw = self.headers.get("Content-Type") or ""
            ctype = ctype_raw.split(";")[0].strip().lower()
            creation_method = "web_paste"
            body_limit = (
                cfg.limits.max_multipart_body_bytes
                if ctype == "multipart/form-data"
                else cfg.max_upload_bytes
            )
            try:
                body, _ = self._read_body(max_bytes=body_limit)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "request body is too large")
                return
            except ClientAbort:
                raise
            try:
                if ctype == "multipart/form-data":
                    boundary = extract_boundary(
                        ctype_raw,
                        max_length=cfg.limits.max_multipart_boundary_length,
                    )
                    if not boundary:
                        self._error(400, "invalid_request", "multipart boundary is missing")
                        return
                    try:
                        fields = parse_multipart(
                            body,
                            boundary,
                            max_parts=cfg.limits.max_multipart_parts,
                            max_header_bytes=cfg.limits.max_multipart_header_bytes,
                            max_field_name_length=cfg.limits.max_multipart_field_name_length,
                        )
                    except MultipartError as exc:
                        self._error(400, "invalid_request", f"invalid multipart body: {exc}")
                        return
                    controls = {"preserve_name", "replace", "creation_method"}
                    payload_fields = set(fields) - controls
                    recognized = payload_fields & {"image", "file"}
                    # Legacy clients may use a sole unknown field, but never
                    # alongside controls or another potential payload.
                    legacy_fallback = (
                        not self._item_schema and len(fields) == 1 and len(payload_fields) == 1
                    )
                    if (
                        len(payload_fields) != 1
                        or (not recognized and not legacy_fallback)
                        or any(fields[name][0] is not None for name in controls & fields.keys())
                    ):
                        self._error(400, "invalid_request", "exactly one 'file' or 'image' payload field is required")
                        return
                    filename_client, part_ctype, data = fields[next(iter(payload_fields))]
                    declared = part_ctype or "application/octet-stream"
                    preserve_name = (
                        fields.get("preserve_name", (None, None, b""))[2].strip() == b"1"
                    )
                    requested_replace = (
                        fields.get("replace", (None, None, b""))[2].strip() == b"1"
                    )
                    raw_creation_method = fields.get("creation_method")
                    if raw_creation_method is not None:
                        try:
                            creation_method = raw_creation_method[2].decode("ascii")
                        except UnicodeDecodeError:
                            self._error(400, "invalid_request", "invalid creation method")
                            return
                elif ctype.startswith("image/") or ctype.startswith("text/") or ctype in (
                    "application/octet-stream",
                    "application/json",
                    "application/xml",
                    "application/x-yaml",
                    "",
                ):
                    data = body
                    filename_client = None
                    declared = ctype or "application/octet-stream"
                    preserve_name = False
                    requested_replace = False
                else:
                    self._error(415, "unsupported_media_type",
                                f"Content-Type is not allowed: {ctype!r}")
                    return
                allow_replace = requested_replace
                if principal.kind == "token":
                    allow_replace = requested_replace and bool(zone_access[1]) and preserve_name
                item = service.upload(
                    zid,
                    data,
                    declared,
                    filename_client,
                    preserve_filename=preserve_name,
                    allow_replace=allow_replace and preserve_name,
                    creation_method=creation_method,
                )
            except ServiceError as exc:
                self._write_service_error(
                    zone_access,
                    exc,
                    code="upload_failed",
                    message="upload failed",
                )
                return
            if principal.kind == "token" and not zone_access[0] & PERMISSION_READ:
                self._json(201, {"accepted": True})
                return
            self._json(200 if item.get("duplicate") else 201, item)

        def _read_filename_request(self) -> list[str] | None:
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_batch_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "file list is too large")
                return None
            except ClientAbort:
                raise
            try:
                return _parse_filename_list(
                    body,
                    self.headers.get("Content-Type") or "",
                    max_names=cfg.limits.max_batch_names,
                )
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return None

        def _h_zone_delete_batch(self, zid: str) -> None:
            zone_access = self._zone_permissions(zid, PERMISSION_WRITE)
            if zone_access is None:
                return
            filenames = self._read_filename_request()
            if filenames is None:
                return
            try:
                result = service.delete_many(zid, filenames, blocking=False)
            except ServiceError as exc:
                self._write_service_error(
                    zone_access,
                    exc,
                    code="delete_failed",
                    message="delete failed",
                )
                return
            principal = self._principal()
            if principal.kind == "token" and not zone_access[0] & PERMISSION_READ:
                result = self._redact_write_only_failures(result, "delete failed")
            self._json(200, result)

        def _h_transfer(self) -> None:
            if not self._require_auth_api() or not self._select_item_schema():
                return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                return
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_batch_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "transfer request is too large")
                return
            except ClientAbort:
                raise
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                self._error(400, "invalid_request", "transfer request must contain valid JSON")
                return
            required = {"mode", "source_zone", "target_zone", "filenames"}
            if not isinstance(payload, dict) or set(payload) != required:
                self._error(
                    400,
                    "invalid_request",
                    "transfer request must contain only mode, source_zone, target_zone, and filenames",
                )
                return
            mode = payload["mode"]
            source_zone = payload["source_zone"]
            target_zone = payload["target_zone"]
            filenames = payload["filenames"]
            if (
                not isinstance(mode, str)
                or not isinstance(source_zone, str)
                or not isinstance(target_zone, str)
                or not isinstance(filenames, list)
                or not filenames
                or (
                    cfg.limits.max_batch_names is not None
                    and len(filenames) > cfg.limits.max_batch_names
                )
                or not all(isinstance(filename, str) for filename in filenames)
            ):
                self._error(400, "invalid_request", "invalid transfer fields")
                return
            source_permissions = PERMISSION_READ
            if mode == "move":
                source_permissions |= PERMISSION_WRITE
            source_access = self._zone_permissions(source_zone, source_permissions)
            if source_access is None:
                return
            target_access = self._zone_permissions(target_zone, PERMISSION_WRITE)
            if target_access is None:
                return
            principal = self._principal()
            try:
                result = service.transfer(
                    source_zone,
                    target_zone,
                    filenames,
                    mode=mode,
                    blocking=False,
                )
            except ServiceError as exc:
                self._write_service_error(
                    target_access,
                    exc,
                    code="transfer_failed",
                    message="transfer failed",
                    blind_permission=PERMISSION_LIST,
                )
                return
            if principal.kind == "token" and not target_access[0] & PERMISSION_LIST:
                result = self._redact_write_only_failures(result, "transfer failed")
                result = {**result, "items": [], "retention_deleted": []}
            self._json(200, result)

        def _h_zone_comment(self, zid: str, filename: str) -> None:
            zone_access = self._zone_permissions(zid, PERMISSION_WRITE)
            if zone_access is None:
                return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._error(415, "unsupported_media_type", "Content-Type must be application/json")
                return
            try:
                body, _ = self._read_body(max_bytes=cfg.limits.max_comment_body_bytes)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "comment request is too large")
                return
            try:
                payload = json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_json_object_without_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                self._error(400, "invalid_request", "comment request must contain valid JSON")
                return
            if not isinstance(payload, dict) or set(payload) != {"comment"}:
                self._error(400, "invalid_request", "comment request must contain only 'comment'")
                return
            try:
                item = service.update_comment(zid, filename, payload["comment"])
            except ServiceError as exc:
                self._write_service_error(
                    zone_access,
                    exc,
                    code="comment_failed",
                    message="comment failed",
                )
                return
            principal = self._principal()
            if principal.kind == "token" and not zone_access[0] & PERMISSION_LIST:
                self._json(200, {"updated": True})
                return
            self._json(200, item)

        def _check_download_active(self) -> None:
            deadline = getattr(self, "_request_deadline", None)
            if (
                getattr(self, "_request_expired", False)
                or self.connection.fileno() < 0
                or (
                    not getattr(self, "_streaming_response", False)
                    and deadline is not None
                    and time.monotonic() >= deadline
                )
            ):
                raise ClientAbort()
            archive_deadline = getattr(self, "_archive_deadline", None)
            if archive_deadline is not None and time.monotonic() >= archive_deadline:
                raise TimeoutError("archive duration exceeded")

        def _start_download_stream(self) -> None:
            with self._request_timer_lock:
                self._check_download_active()
                self._stream_last_activity = time.monotonic()
                self._streaming_response = True
                self._response_started = True

        def _send_zip_response(self, zid: str, items) -> None:
            self._start_download_stream()
            duration = cfg.limits.max_archive_duration_seconds
            self._archive_deadline = (
                None if duration is None else self._stream_last_activity + duration
            )
            archive_timer = None
            try:
                if duration is not None:
                    archive_timer = threading.Timer(
                        duration, self._expire_archive, args=(self._request_token,)
                    )
                    archive_timer.daemon = True
                    archive_timer.start()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="pasteberth-{zid}.zip"',
                )
                if self.close_connection:
                    self.send_header("Connection", "close")
                for key, value in self._security_headers():
                    self.send_header(key, value)
                self.end_headers()

                writer = _ChunkedWriter(self)
                with zipfile.ZipFile(
                    writer,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    try:
                        for item, source in items:
                            zip_info = zipfile.ZipInfo(item.filename)
                            zip_info.compress_type = zipfile.ZIP_DEFLATED
                            zip_info.external_attr = 0o600 << 16
                            with archive.open(zip_info, mode="w", force_zip64=True) as target:
                                try:
                                    remaining = item.size
                                    while remaining:
                                        self._check_download_active()
                                        chunk = source.read(min(64 * 1024, remaining))
                                        self._check_download_active()
                                        if not chunk:
                                            raise OSError("archive source ended early")
                                        target.write(chunk)
                                        remaining -= len(chunk)
                                except BaseException:
                                    # ZIP entry/archive exits otherwise write trailers on failure.
                                    writer.aborted = True
                                    raise
                    except BaseException:
                        writer.aborted = True
                        raise
                self._check_download_active()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                self._stream_last_activity = time.monotonic()
                log.info(
                    "archive zone=%s files=%d zip_size=%d",
                    zid,
                    len(items),
                    writer.offset,
                )
            except BaseException:
                self.close_connection = True
                raise
            finally:
                with self._request_timer_lock:
                    self._streaming_response = False
                    self._archive_deadline = None
                    if archive_timer is not None:
                        archive_timer.cancel()

        def _h_zone_archive(self, zid: str) -> None:
            if self._zone_permissions(zid, PERMISSION_READ) is None:
                return
            filenames = self._read_filename_request()
            if filenames is None:
                return
            try:
                with service.archive_files(zid, filenames, blocking=False) as items:
                    self._send_zip_response(zid, items)
            except ServiceError as exc:
                self._service_error(exc)
                return

        def _h_zone_delete(self, zid: str, filename: str) -> None:
            zone_access = self._zone_permissions(zid, PERMISSION_WRITE)
            if zone_access is None:
                return
            try:
                service.delete(zid, filename)
            except ServiceError as exc:
                self._write_service_error(
                    zone_access,
                    exc,
                    code="delete_failed",
                    message="delete failed",
                )
                return
            self._json(200, {"deleted": filename})

        def _h_preview(self, zid: str, filename: str) -> None:
            if self._zone_permissions(zid, PERMISSION_READ) is None:
                return
            try:
                with service.open_preview(
                    zid, filename, blocking=not getattr(self, "_item_schema", False),
                ) as (item, source):
                    headers = getattr(self, "headers", None)
                    conditions = headers.get_all("If-Match", []) if headers is not None else []
                    if not _if_match_satisfied(conditions, item.etag):
                        raise ServiceError(
                            "precondition_failed", "content does not match If-Match",
                        )
                    self._start_download_stream()
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", item.mime)
                        self.send_header("Content-Length", str(item.size))
                        self.send_header("Cache-Control", "no-store")
                        if item.etag is not None:
                            self.send_header("ETag", item.etag)
                        if self.close_connection:
                            self.send_header("Connection", "close")
                        for key, value in self._security_headers():
                            self.send_header(key, value)
                        if item.mime not in ("image/png", "image/jpeg", "image/webp"):
                            # Never render stored HTML on the application's origin.
                            fallback = "".join(
                                char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_"
                                for char in filename
                            ) or "download"
                            encoded = urllib.parse.quote(filename, safe="")
                            self.send_header(
                                "Content-Disposition",
                                f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}',
                            )
                        self.end_headers()
                        remaining = 0 if self.command == "HEAD" else item.size
                        while remaining:
                            self._check_download_active()
                            chunk = source.read(min(64 * 1024, remaining))
                            self._check_download_active()
                            if not chunk:
                                raise OSError("preview source ended early")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            self._stream_last_activity = time.monotonic()
                            remaining -= len(chunk)
                    except BaseException:
                        self.close_connection = True
                        raise
                    finally:
                        with self._request_timer_lock:
                            self._streaming_response = False
            except ServiceError as exc:
                self._service_error(exc)

    return PasteberthHandler
