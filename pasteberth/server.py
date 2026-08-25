"""Lancement du serveur HTTP threadé."""
from __future__ import annotations

import logging
import signal
import threading
from http.server import ThreadingHTTPServer

log = logging.getLogger("pasteberth.server")


class PasteberthServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 32
    max_active_requests = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._request_slots = threading.BoundedSemaphore(self.max_active_requests)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


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
