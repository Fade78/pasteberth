"""Serveur Pasteberth éphémère utilisé par les tests Playwright."""
from __future__ import annotations

import os
import json
import signal
import tempfile
import threading
from pathlib import Path

from PasteBerth.runtime.auth import LoginRateLimiter, SessionStore
from PasteBerth.runtime.config import load_config, prepare_directories
from PasteBerth.runtime.server import PasteberthServer
from PasteBerth.runtime.service import PasteService
from PasteBerth.runtime.webapp import make_handler


PORT = int(os.environ.get("PASTEBERTH_E2E_PORT", "8876"))
URL_PREFIX = os.environ.get("PASTEBERTH_E2E_PREFIX", "")


def _write_config(root: Path) -> Path:
    text = f'''listen_address = "127.0.0.1"
port = {PORT}
max_upload_size = "20MiB"
max_image_pixels = 25000000
url_prefix = {json.dumps(URL_PREFIX)}
allowed_hosts = ["localhost", "127.0.0.1", "::1"]
trusted_proxies = ["127.0.0.1", "::1"]
allow_unauthenticated_local = true
log_level = "WARNING"

[auth]
enabled = false

[[zones]]
id = "default"
label = "Default"
type = "local"
directory = "{(root / 'default-images').as_posix()}"
retain = 5
reference_prefix = "@"
color = "#304237"
min_free_percent = 0.0

[[zones]]
id = "secondary"
label = "Secondary"
type = "local"
directory = "{(root / 'secondary-images').as_posix()}"
retain = 5
reference_prefix = "@"
color = "#26394a"
min_free_percent = 0.0

[[groups]]
name = "All"
selection = "all"
show_count = true

[[groups]]
name = "Secondary"
pattern = ["^secondary$"]
layout = "area"
show_count = true

[[groups]]
name = "Tabbed"
pattern = [".*"]
layout = "tab"
show_count = true

[[groups]]
name = "Empty"
pattern = ["^missing-.*$"]
show_count = true
'''
    path = root / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _test_handler(base_handler, service, prefix):
    class BrowserHandler(base_handler):
        """Ajoute uniquement la remise à zéro locale entre scénarios."""

        def do_POST(self) -> None:
            reset_paths = {"/__e2e/reset"}
            if prefix:
                reset_paths.add(prefix + "/__e2e/reset")
            if self.path.split("?", 1)[0] not in reset_paths:
                super().do_POST()
                return
            try:
                for zid in service._destinations:
                    for item in service.history(zid):
                        service._destinations[zid].delete(item["filename"])
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

    return BrowserHandler


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pasteberth-e2e-") as raw_root:
        root = Path(raw_root)
        cfg = load_config(_write_config(root))
        prepare_directories(cfg)
        service = PasteService(cfg)
        sessions = SessionStore(
            cfg.auth.session_ttl_hours * 3600,
            password_file=cfg.password_file() if cfg.auth.enabled else None,
            max_sessions=cfg.auth.max_sessions,
        )
        limiter = LoginRateLimiter(
            max_concurrent_checks=cfg.limits.max_login_concurrent_checks,
            max_tracked_ips=cfg.limits.max_login_tracked_ips,
            max_delay=cfg.limits.max_login_delay_seconds,
            forget_after=cfg.limits.login_forget_after_seconds,
        )
        base_handler = make_handler(cfg, service, sessions, limiter)
        server = PasteberthServer(
            (cfg.listen_address, cfg.port),
            _test_handler(base_handler, service, URL_PREFIX),
            limits=cfg.limits,
        )

        def stop(_signum, _frame) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
