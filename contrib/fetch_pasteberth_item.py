#!/usr/bin/env python3
r"""Standalone, standard-library-only HTTP consumer example for Siftport.

Usage (PASTEBERTH_PASSWORD must already be set in the environment):
    python3 contrib/fetch_pasteberth_item.py --server https://host/paste \
        --zone default --filename 'report.csv' ./report.csv
    python3 contrib/fetch_pasteberth_item.py --server http://127.0.0.1:8080/paste \
        --zone default --filename 'report.csv' ./report.csv

Use HTTPS outside a trusted local/loopback connection. TLS certificates are
verified by default; there is no insecure switch. The server URL includes the
mount prefix, but no credentials, query, or fragment. No request follows a
redirect, including the successful login's 303. Cookies stay in memory.

The items listing is limited to 16 MiB. content_url must be an ASCII, percent-
encoded server-relative path matching the selected item's canonical API route
within the mount, without query or fragment. Paths are decoded once for safety
checks, as on the backend; literal percent signs in filenames are supported.
The output parent must exist and be trusted: staging uses a private file there,
64 KiB reads, length/digest verification, and os.replace. An existing output
symlink is replaced, not followed. No sidecars, locks, destination chmod, or
downstream SFTP are involved. Atomic replacement is not a crash-durability or
concurrent-writer guarantee.

Failures before os.replace preserve any previous output. Publication and stdout
reporting are separate phases, not one atomic operation; there is no rollback.
Exit 0 means publication succeeded and JSON was flushed to stdout (local sha256,
size, and listing_identity_verified). Exit 1 means a pre-publication failure;
exit 2 means invalid arguments. Exit 3 means the output was published but writing
or flushing its report failed: the published output remains in place. Exit 130
means interruption, which may occur before or after publication.

A legacy null digest gets an explicit stderr warning: the local hash cannot
establish listing identity.
HTTP 200 alone is never sufficient. There are no retries or resumable transfers;
each network operation has a 30-second timeout (not a total transfer deadline).
"""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
from urllib.parse import quote, unquote, urlencode, urlsplit


MAX_LISTING_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class ConsumerError(Exception):
    """An error message safe to show without leaking credentials or responses."""


def validate_path(path):
    """Reject ambiguous paths before sending cookies to a server-provided URL."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConsumerError("Invalid server-relative path")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in path):
        raise ConsumerError("URL paths must be ASCII and percent-encoded")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or "?" in path or "#" in path:
        raise ConsumerError("URL must be a server-relative path without query or fragment")
    if re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ConsumerError("Invalid URL percent encoding")
    decoded = unquote(path, errors="strict")
    if ("\\" in decoded or "//" in decoded
            or any(c in (".", "..") for c in decoded.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
        raise ConsumerError("Unsafe URL path")
    if decoded.count("/") != path.count("/"):
        raise ConsumerError("Encoded path separators are not allowed")
    return path


class Consumer:
    def __init__(self, server):
        if any(ord(c) <= 32 or ord(c) >= 127 for c in server):
            raise ConsumerError("Invalid server URL; use an ASCII hostname and encoded path")
        parsed = urlsplit(server)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in server or "#" in server):
            raise ConsumerError("Server URL must be HTTP(S), without credentials, query, or fragment")
        self.host = parsed.hostname
        self.port = parsed.port
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.prefix = validate_path(parsed.path or "/").rstrip("/")
        self.tls = parsed.scheme == "https"
        self.cookie = ""

    @contextmanager
    def request(self, method, path, body=None, headers=None):
        validate_path(path)
        if not path.startswith(self.prefix + "/"):
            raise ConsumerError("Content URL is outside the configured mount")
        connection = (
            http.client.HTTPSConnection(self.host, self.port, timeout=30,
                                        context=ssl.create_default_context())
            if self.tls else http.client.HTTPConnection(self.host, self.port, timeout=30)
        )
        request_headers = {"Accept-Encoding": "identity", **(headers or {})}
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            try:
                yield response
            finally:
                response.close()
        finally:
            connection.close()

    def login(self, password):
        with self.request("POST", self.prefix + "/login",
                          body=urlencode({"password": password}).encode("ascii"),
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "Origin": self.origin}) as response:
            if response.status != 303:
                raise ConsumerError(f"Login failed (HTTP {response.status})")
            cookies = SimpleCookie()
            for header in response.headers.get_all("Set-Cookie", []):
                cookies.load(header)
            session = cookies.get("pb_session")
            if session is None or not session.value:
                raise ConsumerError("Login did not return a session cookie")
            if session["secure"] and not self.tls:
                raise ConsumerError("Secure session cookie requires HTTPS")
            self.cookie = session.OutputString(attrs=[])

    def item(self, zone, filename):
        path = self.prefix + "/api/zones/" + quote(zone, safe="") + "/items"
        with self.request("GET", path) as response:
            if response.status != 200:
                raise ConsumerError(f"Listing failed (HTTP {response.status})")
            raw = response.read(MAX_LISTING_BYTES + 1)
            if len(raw) > MAX_LISTING_BYTES:
                raise ConsumerError("Listing exceeds the 16 MiB limit")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isascii() or not length.isdecimal()
                                       or int(length) != len(raw)):
                raise ConsumerError("Listing length mismatch")
        listing = json.loads(raw)
        if (not isinstance(listing, dict) or listing.get("zone") != zone
                or not isinstance(listing.get("items"), list) or "error" in listing):
            raise ConsumerError("Invalid items listing")
        matches = [item for item in listing["items"]
                   if isinstance(item, dict) and item.get("filename") == filename]
        if len(matches) != 1:
            raise ConsumerError("Requested filename is missing or ambiguous in listing")
        item = matches[0]
        if (not isinstance(item.get("filename"), str) or not item["filename"]
                or type(item.get("size")) is not int or item["size"] < 0
                or "sha256" not in item or "etag" not in item):
            raise ConsumerError("Invalid item metadata")
        digest, etag = item["sha256"], item["etag"]
        if digest is not None and (not isinstance(digest, str)
                                   or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ConsumerError("Invalid item SHA-256")
        if etag is not None:
            if (not isinstance(etag, str)
                    or not re.fullmatch(r'"sha256-[0-9a-fA-F]{64}"', etag)
                    or (digest is not None and etag[8:-1].lower() != digest)):
                raise ConsumerError("Invalid or inconsistent item ETag")
        content_url = validate_path(item.get("content_url"))
        if content_url != path + "/" + quote(filename, safe="") + "/content":
            raise ConsumerError("Content URL does not match the selected item's canonical route")
        return item

    def download(self, item, output):
        digest, etag, size = item["sha256"], item["etag"], item["size"]
        if digest is None:
            print("Warning: listing SHA-256 is null; unable to assert listing identity. "
                  "The reported SHA-256 is only a local hash.", file=sys.stderr)
        headers = {"If-Match": etag} if etag is not None else {}
        staging = None
        try:
            with self.request("GET", item["content_url"], headers=headers) as response:
                if response.status != 200:
                    raise ConsumerError(f"Download failed (HTTP {response.status})")
                if etag is not None and response.getheader("ETag") != etag:
                    raise ConsumerError("Download ETag does not match listing")
                length = response.getheader("Content-Length")
                if (length is None or not length.isascii() or not length.isdecimal()
                        or int(length) != size or response.getheader("Transfer-Encoding")
                        or response.getheader("Content-Encoding") not in (None, "identity")):
                    raise ConsumerError("Download Content-Length or encoding does not match metadata")
                sha256 = hashlib.sha256()
                received = 0
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".pasteberth-",
                                                 dir=Path(output).absolute().parent,
                                                 delete=False) as handle:
                    staging = Path(handle.name)
                    while True:
                        chunk = response.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > size:
                            raise ConsumerError("Download exceeds listed size")
                        sha256.update(chunk)
                        handle.write(chunk)
                    local_digest = sha256.hexdigest()
                    if received != size:
                        raise ConsumerError("Download was truncated")
                    if digest is not None and local_digest != digest:
                        raise ConsumerError("Download SHA-256 does not match listing")
            os.replace(staging, output)
            staging = None
            return {"sha256": local_digest, "size": received,
                    "listing_identity_verified": digest is not None}
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)


def diagnostic(message):
    """Best-effort stderr: a broken stream must not override the exit status."""
    if sys.stderr is None:
        return
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        try:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), sys.stderr.fileno())
        except (OSError, ValueError):
            sys.stderr = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True, help="HTTP(S) base URL including mount prefix")
    parser.add_argument("--zone", required=True, help="zone ID")
    parser.add_argument("--filename", required=True, help="exact filename from the items listing")
    parser.add_argument("output", type=Path, help="local output file (trusted parent must exist)")
    args = parser.parse_args(argv)
    password = os.environ.get("PASTEBERTH_PASSWORD")
    if not password:
        diagnostic("Error: PASTEBERTH_PASSWORD must be set and nonempty")
        return 1
    try:
        consumer = Consumer(args.server)
        consumer.login(password)
        item = consumer.item(args.zone, args.filename)
        result = consumer.download(item, args.output)
    except ConsumerError as error:
        diagnostic(f"Error: {error}")
        return 1
    except (OSError, ValueError, CookieError, http.client.HTTPException, RecursionError):
        # Never echo a URL, credentials, response body, or raw network exception.
        diagnostic("Error: network, TLS, local I/O, or malformed response failure")
        return 1
    except KeyboardInterrupt:
        diagnostic("Error: interrupted")
        return 130
    try:
        print(json.dumps(result, sort_keys=True), flush=True)
    except (OSError, ValueError):
        # Prevent a failed final stdout flush from changing exit 3 to exit 120.
        try:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (OSError, ValueError):
            sys.stdout = None
        diagnostic("Error: output published but report failed; published output remains in place")
        return 3
    except KeyboardInterrupt:
        diagnostic("Error: interrupted after publication; published output remains in place")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
