"""Lancement du serveur HTTP threadé."""
from __future__ import annotations

import logging
import signal
import threading
from http.server import ThreadingHTTPServer

log = logging.getLogger("pasteberth.server")


class PasteberthServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def serve_forever(handler_class, listen_address: str, port: int) -> None:
    server = PasteberthServer((listen_address, port), handler_class)

    def _shutdown(signum, _frame) -> None:
        log.info("signal %s reçu, arrêt…", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous_term = signal.signal(signal.SIGTERM, _shutdown)
    previous_int = signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        server.server_close()
