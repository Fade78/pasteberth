"""Lancement du serveur HTTP threadé."""
from __future__ import annotations

import logging
import ipaddress
import ssl
import socket
import signal
import threading
from http.server import ThreadingHTTPServer

log = logging.getLogger("pasteberth.server")


def create_tls_context(certificate, private_key) -> ssl.SSLContext:
    """Charge un contexte TLS serveur avec TLS 1.2 minimum."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(private_key))
    return context


def address_family_for(host: str) -> int:
    """Retourne la famille d'adresse correspondant à l'écoute demandée."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"adresse d'écoute introuvable : {host}")
        return infos[0][0]
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


class PasteberthServer(ThreadingHTTPServer):
    # Let server_close() drain active handlers before the process exits.
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = True
    request_queue_size = 32
    max_active_requests = 64

    def __init__(self, server_address, handler_class, bind_and_activate=True, tls_context=None):
        self.address_family = address_family_for(server_address[0])
        self.tls_context = tls_context
        super().__init__(server_address, handler_class, bind_and_activate)
        self._request_slots = threading.BoundedSemaphore(self.max_active_requests)

    def get_request(self):
        request, client_address = super().get_request()
        if self.tls_context is None:
            return request, client_address
        try:
            request = self.tls_context.wrap_socket(
                request,
                server_side=True,
                do_handshake_on_connect=False,
            )
        except BaseException:
            request.close()
            raise
        return request, client_address

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


def serve_forever(handler_class, listen_address: str, port: int, tls_context=None) -> None:
    server = PasteberthServer((listen_address, port), handler_class, tls_context=tls_context)

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
