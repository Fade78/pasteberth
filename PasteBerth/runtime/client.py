"""Small standard-library HTTP client for server-backed CLI operations."""
from __future__ import annotations

import http.client
import json
import secrets
import ssl
import urllib.parse
from dataclasses import dataclass
from http.cookies import SimpleCookie


class ClientError(Exception):
    """An HTTP client or API error suitable for CLI output."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ClientResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError("server returned invalid JSON", status=self.status) from exc


class PasteberthClient:
    """Minimal client for login and multipart zone uploads."""

    def __init__(self, base_url: str, *, timeout: float = 60.0, insecure: bool = False):
        try:
            parsed = urllib.parse.urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise ClientError(f"invalid server URL: {base_url!r}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ClientError("server URL must use http:// or https:// and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ClientError("server URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ClientError("server URL must not contain a query or fragment")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = port
        self.base_path = parsed.path.rstrip("/")
        self.timeout = timeout if timeout and timeout > 0 else 60.0
        self._tls_context = (
            ssl._create_unverified_context() if insecure and self.scheme == "https" else None
        )

    def _connection(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=self.timeout,
                context=self._tls_context,
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _path(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return (self.base_path + path) or "/"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        content_type: str | None = None,
        cookie: str | None = None,
    ) -> ClientResponse:
        headers = {
            "Accept": "application/json",
            "Connection": "close",
            "Content-Length": str(len(body)),
        }
        if content_type:
            headers["Content-Type"] = content_type
        if cookie:
            headers["Cookie"] = cookie
        connection = self._connection()
        try:
            connection.request(method, self._path(path), body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            response_headers: dict[str, str] = {}
            for key, value in response.getheaders():
                response_headers.setdefault(key.lower(), value)
            return ClientResponse(response.status, response_headers, data)
        except ssl.SSLCertVerificationError as exc:
            raise ClientError(
                "cannot reach Pasteberth server: "
                f"{exc}; if this is a trusted self-signed HTTPS certificate, "
                "retry with --insecure"
            ) from exc
        except (OSError, ssl.SSLError) as exc:
            raise ClientError(f"cannot reach Pasteberth server: {exc}") from exc
        finally:
            connection.close()

    def login(self, password: str) -> str:
        body = urllib.parse.urlencode({"password": password}).encode("utf-8")
        response = self.request(
            "POST",
            "/login",
            body=body,
            content_type="application/x-www-form-urlencoded",
        )
        if response.status != 303:
            raise api_error(response, "login failed")
        raw_cookie = response.headers.get("set-cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception as exc:
            raise ClientError("server returned an invalid session cookie", status=response.status) from exc
        morsel = cookie.get("pb_session")
        if morsel is None or not morsel.value:
            raise ClientError("server did not return a session cookie", status=response.status)
        return f"pb_session={morsel.value}"

    def upload(
        self,
        zone_id: str,
        data: bytes,
        filename: str,
        declared_mime: str,
        *,
        replace: bool = False,
        cookie: str | None = None,
    ) -> ClientResponse:
        boundary = "pasteberth" + secrets.token_hex(12)
        safe_filename = (
            filename.replace("\\", "_")
            .replace('"', "_")
            .replace("\r", "_")
            .replace("\n", "_")
        )
        chunks = [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
                f'filename="{safe_filename}"\r\nContent-Type: {declared_mime}\r\n\r\n'
            ).encode("utf-8"),
            data,
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="preserve_name"\r\n\r\n1'.encode(),
        ]
        if replace:
            chunks.append(
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="replace"\r\n\r\n1'.encode()
            )
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(chunks)
        path = "/api/zones/" + urllib.parse.quote(zone_id, safe="") + "/images"
        return self.request(
            "POST",
            path,
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            cookie=cookie,
        )

    def regularize(
        self,
        zone_id: str,
        stage_name: str,
        filename: str,
        declared_mime: str,
        *,
        replace: bool = False,
        cookie: str | None = None,
    ) -> ClientResponse:
        body = json.dumps(
            {
                "stage": stage_name,
                "filename": filename,
                "mime": declared_mime,
                "replace": replace,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        path = "/api/zones/" + urllib.parse.quote(zone_id, safe="") + "/images/regularize"
        return self.request(
            "POST",
            path,
            body=body,
            content_type="application/json",
            cookie=cookie,
        )


def api_error(response: ClientResponse, fallback: str = "server request failed") -> ClientError:
    try:
        payload = response.json()
    except ClientError:
        return ClientError(f"{fallback} (HTTP {response.status})", status=response.status)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            if isinstance(message, str) and message:
                return ClientError(
                    f"{message} ({code})" if isinstance(code, str) and code else message,
                    status=response.status,
                    code=code if isinstance(code, str) else None,
                )
    return ClientError(f"{fallback} (HTTP {response.status})", status=response.status)
