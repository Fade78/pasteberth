"""Couche HTTP : serveur threadé standard, routage, sécurité.

Points de sécurité traités ici :
- authentification par session côté serveur (cookie HttpOnly / SameSite=Lax,
  Secure dès que le schéma effectif est https) ;
- vérification d'Origin sur toute requête non sûre (CSRF), combinée à
  SameSite=Lax ;
- en-têtes X-Forwarded-* honorés UNIQUEMENT depuis un pair déclaré dans
  ``trusted_proxies`` ;
- en-têtes de sécurité systématiques (CSP stricte sans inline, nosniff…) ;
- corps de requête plafonnés et parsés de façon bornée ;
- aucune réflexion de contenu non contrôlé dans les réponses.
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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from pasteberth import __version__
from pasteberth.auth import LoginRateLimiter, SessionStore, load_password_hash, verify_password
from pasteberth.config import Config
from pasteberth.multipart import MultipartError, extract_boundary, parse_multipart
from pasteberth.service import PasteService, ServiceError

log = logging.getLogger("pasteberth.http")

COOKIE_NAME = "pb_session"
_PACKAGE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PACKAGE_DIR / "static"
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"

_ZONE_RE = r"([a-z0-9][a-z0-9_-]{0,63})"
_FILENAME_RE = r"([A-Za-z0-9._\-]{1,200})"

_ROUTES: tuple[tuple[str, re.Pattern, str], ...] = tuple(
    (method, re.compile(pattern), name)
    for method, pattern, name in (
        ("GET", r"^/api/health$", "h_health"),
        ("GET", r"^/api/zones$", "h_zones"),
        ("GET", rf"^/api/zones/{_ZONE_RE}/images$", "h_zone_images"),
        ("POST", rf"^/api/zones/{_ZONE_RE}/images$", "h_zone_upload"),
        ("GET", rf"^/previews/{_ZONE_RE}/{_FILENAME_RE}$", "h_preview"),
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


class ClientAbort(Exception):
    """Le client a interrompu la requête (réseau perdu, timeout…)."""


class BodyTooLarge(Exception):
    pass


class BodyMemoryUnavailable(Exception):
    pass


class HeaderTooLarge(Exception):
    pass


class HeaderBudgetReader:
    """Compteur de bytes lu pendant la phase d'en-têtes HTTP."""

    def __init__(self, raw, max_bytes: int):
        self._raw = raw
        self._max_bytes = max_bytes
        self._read_bytes = 0
        self._enabled = True

    def _count(self, data: bytes) -> bytes:
        if self._enabled:
            self._read_bytes += len(data)
            if self._read_bytes > self._max_bytes:
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


class BodyMemoryBudget:
    """Budget non bloquant pour les copies temporaires des uploads."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._used = 0
        self._lock = threading.Lock()

    def reserve(self, body_bytes: int) -> int | None:
        # Le corps brut et une copie extraite par multipart peuvent coexister.
        charge = body_bytes * 2 + 64 * 1024
        if charge > self.max_bytes:
            return None
        with self._lock:
            if self._used + charge > self.max_bytes:
                return None
            self._used += charge
        return charge

    def release(self, charge: int) -> None:
        if not charge:
            return
        with self._lock:
            self._used = max(0, self._used - charge)


_UPLOAD_MEMORY_BUDGET = 128 * 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024


def make_handler(cfg: Config, service: PasteService, sessions: SessionStore,
                 limiter: LoginRateLimiter) -> type[BaseHTTPRequestHandler]:
    """Construit la classe de handler avec les dépendances injectées."""
    upload_memory = BodyMemoryBudget(_UPLOAD_MEMORY_BUDGET)
    preview_slots = threading.BoundedSemaphore(
        max(1, _UPLOAD_MEMORY_BUDGET // cfg.max_upload_bytes)
    )

    class PasteberthHandler(BaseHTTPRequestHandler):
        server_version = "Pasteberth"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 60

        def _expire_request(self) -> None:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def handle_one_request(self) -> None:
            # Le timeout socket est réinitialisé par les lectures ; ce timer
            # impose une vraie durée maximale à la requête entière.
            self.rfile.reset()
            timer = threading.Timer(self.timeout, self._expire_request)
            timer.daemon = True
            timer.start()
            try:
                super().handle_one_request()
            except HeaderTooLarge:
                self.close_connection = True
                try:
                    self.send_error(431, "en-têtes HTTP trop volumineux")
                except OSError:
                    pass
            except (OSError, TimeoutError):
                self.close_connection = True
            finally:
                timer.cancel()

        def setup(self) -> None:
            super().setup()
            self.rfile = HeaderBudgetReader(self.rfile, _MAX_HEADER_BYTES)

        def parse_request(self) -> bool:
            try:
                return super().parse_request()
            except HeaderTooLarge:
                self.close_connection = True
                self.send_error(431, "en-têtes HTTP trop volumineux")
                return False
            finally:
                self.rfile.disable()

        # ---------------------------------------------------- contexte réseau

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
                    # Le proxy de confiance peut écraser XFF ou ajouter le
                    # client réel à droite ; seul le saut le plus proche est
                    # donc accepté, jamais une valeur client plus à gauche.
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
            if cfg.allowed_hosts:
                return host_name in cfg.allowed_hosts
            listen_name = self._host_name(cfg.listen_address)
            if listen_name in (None, "0.0.0.0", "::"):
                return False
            if host_name == listen_name:
                return True
            if listen_name in ("127.0.0.1", "::1", "localhost"):
                return host_name in ("127.0.0.1", "::1", "localhost")
            return False

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
            """CSRF : si le navigateur fournit Origin/Referer, il doit correspondre."""
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
                    # Clients non-navigateurs (curl, scripts) : autorisés.
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

        def _is_authenticated(self) -> bool:
            if not cfg.auth.enabled:
                return True
            return sessions.validate(self._session_token())

        def _session_cookie(self, token: str) -> str:
            attrs = [
                f"{COOKIE_NAME}={token}",
                "Path=/",
                f"Max-Age={cfg.auth.session_ttl_hours * 3600}",
                "HttpOnly",
                "SameSite=Lax",
            ]
            if self._scheme() == "https":
                attrs.append("Secure")
            return "; ".join(attrs)

        def _clear_cookie(self) -> str:
            attrs = [f"{COOKIE_NAME}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
            if self._scheme() == "https":
                attrs.append("Secure")
            return "; ".join(attrs)

        # ------------------------------------------------------------ réponses

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
                self.command,
                self.path.split("?")[0][:200],
                status,
                (time.monotonic() - getattr(self, "_t_start", time.monotonic())) * 1000,
                self._client_ip(),
            )

        def _security_headers(self) -> tuple[tuple[str, str], ...]:
            headers = list(_SECURITY_HEADERS)
            if self._scheme() == "https":
                headers.append(("Strict-Transport-Security", "max-age=31536000"))
            return tuple(headers)

        def _json(self, status: int, payload: dict, **kwargs) -> None:
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

        # ------------------------------------------------------------- lecture

        def _validate_request_framing(self) -> bool:
            self.close_connection = self.close_connection or self.command != "GET"
            get_all = getattr(self.headers, "get_all", None)
            content_lengths = get_all("Content-Length", []) if get_all else []
            transfer_encodings = get_all("Transfer-Encoding", []) if get_all else []
            connection_values = get_all("Connection", []) if get_all else []
            if any(
                token.strip().lower() == "close"
                for value in connection_values
                for token in value.split(",")
            ):
                self.close_connection = True
            if len(content_lengths) > 1 or transfer_encodings:
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
            *,
            memory_budget: BodyMemoryBudget | None = None,
        ) -> tuple[bytes, int]:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise BodyTooLarge()  # le protocole chunked n'est pas supporté en V1
            length_raw = self.headers.get("Content-Length")
            if length_raw is None:
                return b"", 0
            try:
                length = int(length_raw)
            except ValueError:
                raise ClientAbort()
            if length < 0:
                raise ClientAbort()
            limit = cfg.max_upload_bytes if max_bytes is None else max_bytes
            if length > limit:
                self.close_connection = True
                raise BodyTooLarge()
            reservation = memory_budget.reserve(length) if memory_budget else 0
            if memory_budget and reservation is None:
                self.close_connection = True
                raise BodyMemoryUnavailable()
            chunks: list[bytes] = []
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        raise ClientAbort()
                    chunks.append(chunk)
                    remaining -= len(chunk)
                return b"".join(chunks), reservation or 0
            except BaseException:
                if memory_budget:
                    memory_budget.release(reservation or 0)
                raise

        # --------------------------------------------------------- dispatch

        def _dispatch(self) -> None:
            self._t_start = time.monotonic()
            self._route_path = self.path.split("?")[0]
            if not self._validate_request_framing():
                return
            if re.search(r"%(?![0-9A-Fa-f]{2})", self.path):
                self.close_connection = True
                self._error(400, "invalid_request", "encodage de chemin invalide")
                return
            try:
                path = urllib.parse.unquote(self.path.split("?")[0], errors="strict")
            except (UnicodeDecodeError, ValueError):
                self.close_connection = True
                self._error(400, "invalid_request", "encodage de chemin invalide")
                return
            self._route_path = path
            if "\x00" in path or "\\" in path:
                self.close_connection = True
                self._error(400, "invalid_request", "requête invalide")
                return
            for method, pattern, name in _ROUTES:
                if self.command != method:
                    continue
                match = pattern.match(path)
                if match:
                    if method == "POST" and not self._origin_allowed():
                        log.warning(
                            "origine refusée %s depuis %s",
                            self.headers.get("Origin") or self.headers.get("Referer"),
                            self._client_ip(),
                        )
                        self._error(403, "forbidden_origin", "origine non autorisée")
                        return
                    handler = getattr(self, "_" + name)
                    handler(*match.groups())
                    return
            if self.command not in ("GET", "POST"):
                self._error(405, "method_not_allowed", "méthode non autorisée")
            elif any(p.match(path) for _, p, _ in _ROUTES):
                self._error(405, "method_not_allowed", "méthode non autorisée pour cette ressource")
            else:
                self._error(404, "not_found", "ressource introuvable")

        def do_GET(self) -> None:
            try:
                self._dispatch()
            except ClientAbort:
                self.close_connection = True
                log.info("client déconnecté pendant la requête (%s)", self.path[:100])
            except BrokenPipeError:
                self.close_connection = True
            except Exception:
                log.exception("erreur interne sur %s", self.path[:200])
                try:
                    self._error(500, "internal", "erreur interne")
                except Exception:
                    self.close_connection = True

        do_POST = do_GET
        do_PUT = do_GET
        do_DELETE = do_GET
        do_PATCH = do_GET
        do_OPTIONS = do_GET

        def log_message(self, fmt: str, *args) -> None:  # neutralise le logger par défaut
            log.debug("peer %s " + fmt, self.address_string(), *args)

        # ------------------------------------------------------ pages statiques

        _STATIC_FILES = {
            "h_static_app_js": (_STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
            "h_static_style_css": (_STATIC_DIR / "style.css", "text/css; charset=utf-8"),
            "h_static_favicon": (_STATIC_DIR / "favicon.svg", "image/svg+xml"),
        }

        def _serve_static(self, path: Path, ctype: str) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._error(404, "not_found", "ressource introuvable")
                return
            self._finish(200, ctype, data, cache_control="public, max-age=300")

        def _h_static_app_js(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_app_js"][0],
                               self._STATIC_FILES["h_static_app_js"][1])

        def _h_static_style_css(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_style_css"][0],
                               self._STATIC_FILES["h_static_style_css"][1])

        def _h_static_favicon(self) -> None:
            self._serve_static(self._STATIC_FILES["h_static_favicon"][0],
                               self._STATIC_FILES["h_static_favicon"][1])

        def _h_health(self) -> None:
            self._json(200, {"ok": True})

        def _h_index(self) -> None:
            if not self._is_authenticated():
                self._redirect("/login")
                return
            try:
                data = (_TEMPLATES_DIR / "index.html").read_bytes()
            except OSError:
                self._error(500, "internal", "interface indisponible")
                return
            data = data.replace(b"__PASTEBERTH_VERSION__", __version__.encode("ascii"))
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
            log.info("%s %s -> %d redirect %s", self.command, self.path, status, location)

        # -------------------------------------------------------------- login

        def _render_login(self, status: int, message: str = "") -> None:
            try:
                template = (_TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
            except OSError:
                self._error(500, "internal", "interface indisponible")
                return
            block = (
                f'<p class="login-error" role="alert">{html.escape(message)}</p>'
                if message
                else ""
            )
            page = template.replace("__ERROR_BLOCK__", block)
            self._finish(status, "text/html; charset=utf-8", page.encode("utf-8"))

        def _h_login_page(self) -> None:
            if not cfg.auth.enabled:
                self._redirect("/")
                return
            if self._is_authenticated():
                self._redirect("/")
                return
            self._render_login(200)

        def _h_login_post(self) -> None:
            if not cfg.auth.enabled:
                self._redirect("/")
                return
            ip = self._client_ip()
            retry_after = limiter.acquire(ip)
            if retry_after > 0:
                self._json(
                    429,
                    {"error": {"code": "rate_limited",
                               "message": "too many attempts, try again later"}},
                    extra_headers=[("Retry-After", str(int(retry_after) + 1))],
                )
                return
            try:
                body, _ = self._read_body(16 * 1024)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "corps de login trop grand")
                limiter.release(ip)
                return
            except ClientAbort:
                limiter.release(ip)
                raise
            released = False
            try:
                password = ""
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "multipart/form-data" in ctype:
                    boundary = extract_boundary(self.headers.get("Content-Type", ""))
                    try:
                        fields = parse_multipart(body, boundary or "")
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
                            max_num_fields=8,
                        )
                    except ValueError:
                        values = {}
                    password = values.get("password", [""])[0]

                stored_hash = load_password_hash(cfg.password_file())
                if password and verify_password(password, stored_hash):
                    limiter.complete(ip, success=True)
                    released = True
                    log.info("connexion réussie (%s)", ip)
                    self._do_login_success()
                else:
                    time.sleep(0.5)
                    limiter.complete(ip, success=False)
                    released = True
                    log.warning("échec de connexion (%s)", ip)
                    self._render_login(401, "Incorrect password.")
            finally:
                if not released:
                    limiter.release(ip)

        # Connexion réussie : redirection + cookie de session sur la même réponse.
        def _do_login_success(self) -> None:
            token = sessions.create()
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", self._session_cookie(token))
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for key, value in self._security_headers():
                self.send_header(key, value)
            self.end_headers()

        def _h_logout(self) -> None:
            sessions.revoke(self._session_token())
            self.send_response(303)
            self.send_header("Location", "/login")
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
            if self._is_authenticated():
                return True
            self._json(
                401,
                {"error": {"code": "unauthorized", "message": "authentification requise"}},
            )
            return False

        def _h_zones(self) -> None:
            if not self._require_auth_api():
                return
            try:
                overview = service.overview()
            except ServiceError as exc:
                self._error(exc.status, exc.code, str(exc))
                return
            self._json(200, overview)

        def _h_zone_images(self, zid: str) -> None:
            if not self._require_auth_api():
                return
            try:
                items = service.history(zid)
            except ServiceError as exc:
                self._error(exc.status, exc.code, str(exc))
                return
            self._json(200, {"zone": zid, "images": items})

        def _h_zone_upload(self, zid: str) -> None:
            if not self._require_auth_api():
                return
            reservation = 0
            try:
                body, reservation = self._read_body(memory_budget=upload_memory)
            except BodyTooLarge:
                self.close_connection = True
                self._error(413, "too_large", "corps trop grand")
                return
            except BodyMemoryUnavailable:
                self.close_connection = True
                self._error(
                    503,
                    "upload_busy",
                    "trop de données d'upload sont actuellement en mémoire",
                    extra_headers=[("Retry-After", "1")],
                )
                return
            except ClientAbort:
                raise
            try:
                ctype_raw = self.headers.get("Content-Type") or ""
                ctype = ctype_raw.split(";")[0].strip().lower()
                if ctype == "multipart/form-data":
                    boundary = extract_boundary(ctype_raw)
                    if not boundary:
                        self._error(400, "invalid_request", "boundary multipart manquant")
                        return
                    try:
                        fields = parse_multipart(body, boundary)
                    except MultipartError as exc:
                        self._error(400, "invalid_request", f"multipart invalide : {exc}")
                        return
                    if "image" in fields:
                        filename_client, part_ctype, data = fields["image"]
                    elif len(fields) == 1:
                        filename_client, part_ctype, data = next(iter(fields.values()))
                    else:
                        self._error(400, "invalid_request",
                                    "champ 'image' attendu (multipart)")
                        return
                    del filename_client  # jamais utilisé pour nommer un fichier
                    declared = part_ctype or "application/octet-stream"
                elif ctype.startswith("image/") or ctype in ("application/octet-stream", ""):
                    data = body
                    declared = ctype or "application/octet-stream"
                else:
                    self._error(415, "unsupported_media_type",
                                f"Content-Type refusé : {ctype!r}")
                    return
                item = service.upload(zid, data, declared)
            except ServiceError as exc:
                self._error(exc.status, exc.code, str(exc))
                return
            finally:
                upload_memory.release(reservation)
            self._json(201, item)

        def _h_preview(self, zid: str, filename: str) -> None:
            if not self._require_auth_api():
                return
            if not preview_slots.acquire(blocking=False):
                self._error(
                    503,
                    "preview_busy",
                    "trop de previews sont actuellement servies",
                    extra_headers=[("Retry-After", "1")],
                )
                return
            try:
                try:
                    data, mime = service.preview(zid, filename)
                except ServiceError as exc:
                    self._error(exc.status, exc.code, str(exc))
                    return
                self._finish(
                    200,
                    mime,
                    data,
                    cache_control="no-store",
                )
            finally:
                preview_slots.release()

    return PasteberthHandler
