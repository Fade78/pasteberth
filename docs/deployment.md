# Deployment

[Documentation map](../GUIDE.md).

## Requirements and Support

The 2.1.21 implementation requires:

- Python 3.11 or newer;
- a local filesystem supported by the active platform backend;
- a modern browser for the Web UI;
- no third-party Python runtime dependency.

Linux is the current official and tested server platform for v2.1.21. The
Windows backend has broad Wine coverage, but native Windows/NTFS validation is
still outstanding. A Darwin backend exists, but native macOS validation and
official support are also outstanding. Only supported local filesystems are
within the official storage boundary; a network mount is not supported merely
because Linux can mount it.

The version is reported by `PasteBerth/pasteberth --version` and recorded in
`PasteBerth/runtime/__init__.py` and `pyproject.toml`. The support matrix is not
a promise about future releases or untested operating systems.

The supplied browser suite runs in Chromium and Firefox in CI when the
corresponding Playwright browsers are installed. Its native-input smoke test
uses the browser file chooser; operating-system drag-and-drop still requires
manual validation on the target desktop.

## Installation

### Deployable copy

The supported v2.1.21 installation is the tracked `PasteBerth/` directory. It is
the complete code-only deployment unit: it needs no root access, installation
script, Python package installation, or build step.

```sh
git clone --branch v2.1.21 --depth 1 https://github.com/Fade78/pasteberth.git
cd pasteberth
cp -a PasteBerth "$HOME/PasteBerth"
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/PasteBerth/pasteberth" "$HOME/.local/bin/pasteberth"
export PATH="$HOME/.local/bin:$PATH"
```

These commands assume `$HOME/PasteBerth` and the symlink do not already exist.
For an existing installation, use the [upgrade procedure](operations.md#upgrade)
rather than nesting a new copy inside the old deployment. Shell commands using
`PasteBerth/support/...` below run from the repository root; after copying the
bundle, the same files are under `$HOME/PasteBerth/support/...`.

The executable is at the root of the deployment directory:

```sh
pasteberth --help
```

The executable resolves its physical target, so a symbolic link can live in any
directory on `PATH`:

```sh
export PATH="$HOME/.local/bin:$PATH"
pasteberth --version
```

The executable resolves the deployment directory before launching its private
runtime, replaces the inherited `PYTHONPATH` with that deployment directory,
and uses Python's `-P` safe-path mode. It therefore does not provide a plugin
mechanism through the current directory or `PYTHONPATH`, and it also works when
the deployment is reached through a cross-user symbolic link whose parent home
directory cannot be listed. A copy of the executable without the rest of the
deployment requires `PASTEBERTH_HOME=/absolute/path/to/PasteBerth`; `--config`
only selects configuration and never locates the runtime.

`pyproject.toml`, tests, browser tooling, documentation sources, and Git
metadata are repository material. They are not needed in the copied deployment.

### First start

Generate a configuration, edit its zones, set the password, audit it, and
start the server:

```sh
pasteberth --generate-config
# edit ~/.config/pasteberth/config.toml: paths, zones, limits, and options
pasteberth passwd
pasteberth audit
pasteberth
```

Open `http://127.0.0.1:8765/` in the browser unless the configuration changes
the address or port. The generated configuration enables authentication. The
password hash is kept in a separate `passwd` file and is never written to
`config.toml`.

For a local trial, running without any configuration intentionally uses a
loopback-only minimal mode with `$XDG_DATA_HOME/pasteberth/storage/default`
(normally `~/.local/share/pasteberth/storage/default`) and no authentication.
This mode is suitable for a first look only; it must not be exposed through a
proxy or a non-loopback listener.

See [configuration discovery](reference/configuration.md#configuration-discovery)
for XDG and environment precedence. Use the same `--config PATH` for service,
password, audit, and local commands when several configurations exist.

## Run The Service

### Foreground operation

For a manual start:

```sh
pasteberth audit --config /absolute/path/config.toml
pasteberth serve --config /absolute/path/config.toml
```

Keep the service bound to `127.0.0.1` when a reverse proxy terminates HTTPS.
For a directly exposed listener, configure TLS and an explicit `allowed_hosts`
list. The generated configuration enables authentication; with authentication
enabled, `allowed_hosts = []` deliberately accepts a hostname chosen by the
deployment. An anonymous configuration with an empty allowlist is rejected at
startup.

When the public URL is mounted below a path, configure the same path in
Pasteberth and forward it unchanged:

```toml
url_prefix = "/paste"
listen_address = "127.0.0.1"
trusted_proxies = ["127.0.0.1"]
allowed_hosts = []
```

Pasteberth does not infer this prefix from `Host`, `X-Forwarded-Host`, or any
other request header. The proxy must preserve `Host[:port]`, overwrite incoming
`X-Forwarded-*` headers, and be the actual peer listed in `trusted_proxies`.
The browser `Origin` is still `scheme://Host[:port]`, without `/paste`.

The direct-drop endpoints use the immediate peer for their loopback exception.
A public request forwarded by a loopback proxy therefore qualifies even without
a session. The examples below block `/api/drop/resolve` and both
`/api/zones/{id}/images/regularize` and `/api/zones/{id}/items/regularize` at the
public proxy. The `items` alias is **Unreleased, after 2.1.21**; blocking only
`images` would leave the same direct-drop handler exposed through `items`.
Local CLI clients can
still reach the backend directly; remote clients use `drop --zone ID` or the
normal multipart upload endpoint. If you deliberately expose those routes,
provide an appropriate proxy access policy and understand the
[API authentication contract](reference/api.md#routes).

With `url_prefix = "/paste"`, even direct loopback clients need the prefix, for
example `--server http://127.0.0.1:8765/paste`. If you use an explicit host
allowlist, include the loopback hostname/address actually used by those clients
as well as the public hostname. An explicit public-only allowlist rejects a
direct request whose Host is `127.0.0.1:8765`.

### systemd user service

[`PasteBerth/support/deploy/pasteberth.service`](../PasteBerth/support/deploy/pasteberth.service)
is an optional user service template. It needs no root and contains example
paths. Copy and adapt it only after generating, authenticating, and auditing the
configuration:

```sh
mkdir -p ~/.config/systemd/user
cp PasteBerth/support/deploy/pasteberth.service ~/.config/systemd/user/pasteberth.service
# edit --config and ReadWritePaths for this deployment
systemctl --user daemon-reload
systemctl --user enable --now pasteberth.service
journalctl --user -u pasteberth -f
```

The template uses `PrivateTmp=true`; zones below `/tmp` or `/var/tmp` are not
usable by other host processes in that mode. If optional `ProtectSystem`,
`ProtectHome`, or `ReadWritePaths` hardening is enabled, include every zone,
parent directory that may be created, and the password path when it must be
written.

To keep a user service running after logout and start it at boot:

```sh
loginctl enable-linger "$USER"
```

Enable linger only when that persistence is wanted.

### Caddy

Keep Pasteberth on loopback and proxy the public HTTPS hostname:

```caddy
pasteberth.example.internal {
    @direct_drop path /paste/api/drop/resolve /paste/api/zones/*/images/regularize /paste/api/zones/*/items/regularize
    respond @direct_drop 403
    @paste path /paste /paste/*
    reverse_proxy @paste 127.0.0.1:8765 {
        # Keep /paste in the upstream request; do not use handle_path here.
        header_up Host {http.request.hostport}
        header_up X-Forwarded-Proto {http.request.scheme}
        header_up X-Forwarded-For {http.request.remote.host}
        header_up X-Forwarded-Host {http.request.hostport}
    }
}
```

Use an authenticated wildcard or a matching explicit host allowlist:

```toml
listen_address = "127.0.0.1"
url_prefix = "/paste"
trusted_proxies = ["127.0.0.1"]
allowed_hosts = []                         # auth enabled: dynamic hostname
# allowed_hosts = ["pasteberth.example.internal"]  # optional strict host policy
```

Trusting `127.0.0.1` trusts every local process that can connect to the
listener, not only Caddy. Use it only when that boundary is acceptable.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name pasteberth.example.internal;
    ssl_certificate /absolute/path/fullchain.pem;
    ssl_certificate_key /absolute/path/private-key.pem;

    location ~ ^/paste/api/(drop/resolve|zones/[^/]+/(images|items)/regularize)$ {
        return 403;
    }

    location = /paste {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Host $http_host;
    }

    location /paste/ {
        # No URI suffix on proxy_pass: preserve /paste/... upstream.
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Host $http_host;
        client_max_body_size 51m;
    }
}
```

The proxy body limit must be at least the configured upload and multipart
budgets. If the application upload limit is `"unlimited"`, configure the proxy
according to the maximum size appropriate for that deployment.
`X-Forwarded-*` headers from an untrusted peer are ignored.

Replace the hostname and certificate paths before using these examples. A
private hostname may need your organization's CA or another explicit trust
setup. Check the proxy configuration with `caddy validate --config /path/Caddyfile`
or `nginx -t` before reload. For root deployment, remove `/paste` from both
Pasteberth's prefix and the proxy matchers; do not merely strip it upstream.

### Direct TLS

Without a reverse proxy, configure `[tls]` with a certificate and private key
outside the deployment bundle, bind the intended interface, and set an explicit
host allowlist. See [TLS configuration](reference/configuration.md#tls).
The certificate must match the hostname or IP clients use. Direct TLS does not
require `trusted_proxies`. A non-loopback plain-HTTP listener additionally
requires `allow_insecure_http_remote = true`; that exception is for a controlled
private network, not a substitute for HTTPS on an untrusted connection.

## Security and Trust Boundaries

Pasteberth is designed for a controlled project boundary, not anonymous public
uploads.

The password is shared across the service. There are no individual Web accounts
or per-zone ACLs: groups, collections, labels, and colors organize the UI, not
authorization. Use separate service/security boundaries if users must not access
one another's zones. `show_full_path = false` hides paths on screen only; the
API and copy-reference actions still return them.

- Use HTTPS for every untrusted network connection. Passwords, sessions, and
  filesystem paths transit the application.
- Keep the backend on loopback behind a reverse proxy whenever possible.
- Keep authentication enabled and create the password with `pasteberth passwd`.
- With authentication enabled, leave `allowed_hosts` empty for a deployment-chosen
  hostname or list explicit canonical hostnames to restrict the service.
- Never leave `allowed_hosts` empty in an anonymous configuration.
- Configure only actual trusted proxy peers in `trusted_proxies`.
- Treat shared writable zones as a trust relationship between users who can
  modify the directory.
- Use private `0700` zones and private `0600` files when other local users must
  not inspect them.

The server uses salted scrypt hashes, constant-time password comparison,
server-side revocable sessions, `HttpOnly`/`SameSite=Lax` cookies, CSRF Origin
or Referer checks, strict security headers, configurable request and image
budgets, and progressive login throttling. The password hash uses the scrypt
`N=16384` work factor. Set the operational budgets in `[limits]`, or use
`"unlimited"` where the deployment accepts the associated resource risk.
Cookies gain `Secure` when the effective request scheme is HTTPS. The UI and
API responses are marked `no-store` and include CSP, `X-Frame-Options: DENY`,
`nosniff`, and `Referrer-Policy: no-referrer` headers.

A file without a coherent Pasteberth sidecar is foreign. Pasteberth never
overwrites, renames, or deletes such a file through managed operations. Linked
configuration, zone, source, and credential paths are resolved and checked;
unsafe entries inside a managed zone remain foreign. Transaction cleanup checks
object identity before destructive actions.

Filesystem write access is a separate trust boundary from Web authentication.
Local `register` and managed-pair CLI operations do not require the daemon's
password. Locks and recovery coordinate Pasteberth operations, not arbitrary
editors or external writers; see [transaction scope](reference/storage.md#transaction-scope).
HTML previews are not rendered as active stored documents, but an explicit
`Copy raw HTML` action and downloads retain the original HTML. Do not promise
that all clipboard or downloaded content is sanitized.

## Current Limits and Roadmap

- only the `local` destination type is implemented;
- one service uses one shared authentication password;
- sessions are in memory and a restart disconnects browsers;
- there is no directory watcher;
- there is no browser extension or CORS API;
- TLS is either direct or delegated to a reverse proxy;
- network and exotic filesystems are not automatically supported;
- mixed clipboard HTML may be larger than the original image-only payload;
- image validation is structural and not a full codec decode.

The next major platform goal is native Windows and macOS support with the same
transaction and security guarantees. That work is intentionally separate from
the v2.1.21 support matrix and must not be represented as already supported.
The repository contains opt-in `platform_windows` and `platform_macos` CI jobs;
enable them only after registering native runners with
`PASTEBERTH_NATIVE_WINDOWS_CI=1` or `PASTEBERTH_NATIVE_MACOS_CI=1`.
