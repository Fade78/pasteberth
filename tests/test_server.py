"""Contrats de cycle de vie du serveur HTTP."""
import errno
import socket
from http.server import BaseHTTPRequestHandler
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pasteberth.server import PasteberthServer

from tests.helpers import LiveServer, running_under_wine, write_config


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _IdleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.header_timeout)

    def handle_one_request(self):
        try:
            self.rfile.readline()
        except (OSError, TimeoutError):
            pass

    def log_message(self, *_args):
        pass


class _PromotingHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.header_timeout)

    def parse_request(self):
        parsed = super().parse_request()
        if parsed and not self.server.promote_request(self.connection):
            self.close_connection = True
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.send_header("Retry-After", "1")
            self.send_header("Connection", "close")
            self.end_headers()
            return False
        return parsed

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


class _SlowBodyHandler(_PromotingHandler):
    timeout = 0.8

    def do_POST(self):
        self.server.observed_body_timeout = self.connection.gettimeout()
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.server.observed_body = body
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _KeepAliveHandler(_PromotingHandler):
    timeout = 0.2


class _WrappedSocket:
    def __init__(self, inner):
        self.inner = inner
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)
        return self.inner.settimeout(value)

    def shutdown(self, how):
        return self.inner.shutdown(how)

    def close(self):
        self.closed = True
        return self.inner.close()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _DeferredTLSContext:
    def __init__(self):
        self.calls = []
        self.wrapped = []

    def wrap_socket(self, request, **kwargs):
        self.calls.append(kwargs)
        wrapped = _WrappedSocket(request)
        self.wrapped.append(wrapped)
        return wrapped


class _PendingLimitedServer(PasteberthServer):
    max_pending_requests = 2
    header_timeout = 0.15


class _NoActiveServer(PasteberthServer):
    max_pending_requests = 1
    max_active_requests = 0
    header_timeout = 1.0


class TestCycleDeVieServeur(unittest.TestCase):
    def test_bind_sur_port_occupe_ne_masque_pas_erreur(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with self.assertRaises(OSError) as context:
                PasteberthServer(("127.0.0.1", port), _IdleHandler)
            error = context.exception
            self.assertTrue(
                error.errno == errno.EADDRINUSE
                or getattr(error, "winerror", None) == 10048,
                repr(error),
            )
        finally:
            blocker.close()

    def test_connexions_sans_headers_sont_bornees_par_le_pool_pending(self):
        server = _PendingLimitedServer(("127.0.0.1", 0), _IdleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sockets = []
        try:
            sockets.extend(
                socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
                for _ in range(2)
            )
            self.assertTrue(_wait_until(lambda: len(server._pending_sockets) == 2))

            rejected = socket.create_connection(
                ("127.0.0.1", server.server_address[1]), timeout=2
            )
            sockets.append(rejected)
            rejected.settimeout(2)
            self.assertEqual(rejected.recv(1), b"")
        finally:
            for client in sockets:
                client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_header_partiel_expire_et_libere_le_slot_pending(self):
        class ShortPendingServer(PasteberthServer):
            max_pending_requests = 1
            header_timeout = 0.1

        server = ShortPendingServer(("127.0.0.1", 0), _IdleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        first = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        second = None
        try:
            first.sendall(b"GET /incomplete")
            self.assertTrue(_wait_until(lambda: not server._pending_sockets, timeout=2))
            second = socket.create_connection(
                ("127.0.0.1", server.server_address[1]), timeout=2
            )
            self.assertTrue(_wait_until(lambda: len(server._pending_sockets) == 1))
        finally:
            first.close()
            if second is not None:
                second.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_headers_complets_promouvent_le_corps_lent_avec_le_timeout_actif(self):
        class SlowBodyServer(PasteberthServer):
            max_pending_requests = 1
            header_timeout = 0.1

        server = SlowBodyServer(("127.0.0.1", 0), _SlowBodyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        try:
            client.sendall(
                b"POST / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"a"
            )
            time.sleep(0.2)
            client.sendall(b"b")
            response = client.recv(4096)
            self.assertIn(b"200", response)
            self.assertEqual(server.observed_body_timeout, _SlowBodyHandler.timeout)
            self.assertEqual(server.observed_body, b"ab")
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_keepalive_idle_est_ferme_apres_promotion(self):
        class ShortKeepAliveServer(PasteberthServer):
            max_pending_requests = 1
            header_timeout = 0.1

        server = ShortKeepAliveServer(("127.0.0.1", 0), _KeepAliveHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        try:
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            response = client.recv(4096)
            self.assertIn(b"200", response)
            self.assertTrue(_wait_until(lambda: not server._active_sockets, timeout=2))
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_tls_handshake_differe_et_socket_enveloppee_nettoyee(self):
        context = _DeferredTLSContext()
        server = PasteberthServer(("127.0.0.1", 0), _IdleHandler, tls_context=context)
        client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
        try:
            request, _ = server.get_request()
            self.assertEqual(
                context.calls,
                [{"server_side": True, "do_handshake_on_connect": False}],
            )
            server.process_request(request, None)
            self.assertTrue(_wait_until(lambda: request in server._pending_sockets))
            self.assertIn(server.header_timeout, context.wrapped[0].timeouts)
            server.close_active_connections()
            self.assertTrue(context.wrapped[0].closed)
        finally:
            client.close()
            server.server_close()
        self.assertFalse(server._active_sockets)

    def test_capacite_active_pleine_repond_503_apres_headers(self):
        server = _NoActiveServer(("127.0.0.1", 0), _PromotingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(
                ("127.0.0.1", server.server_address[1]), timeout=2
            ) as client:
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = client.recv(4096)
            self.assertIn(b"503", response)
            self.assertIn(b"Retry-After", response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_nom_hote_est_resolu_avant_le_bind(self):
        resolved = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0))
        ]
        with mock.patch("pasteberth.server.socket.getaddrinfo", return_value=resolved):
            server = PasteberthServer(("pasteberth.test", 0), object)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()

    def test_arret_attend_les_handlers_actifs(self):
        self.assertFalse(PasteberthServer.daemon_threads)
        self.assertTrue(PasteberthServer.block_on_close)

    def test_arret_ferme_les_connexions_keepalive(self):
        """Une connexion keep-alive ouverte ne doit pas bloquer l'arrêt."""
        if running_under_wine():
            self.skipTest("Wine ne réveille pas de façon fiable un socket Windows fermé à distance")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = write_config(Path(tmp), zones=[{"id": "d", "directory": str(Path(tmp) / "z")}])
            server = LiveServer(cfg)
            sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            data = sock.recv(4096)
            self.assertIn(b"200", data)

            started = time.monotonic()
            server.stop()
            elapsed = time.monotonic() - started
            sock.close()
            self.assertLess(elapsed, 5.0, "l'arrêt a été bloqué par une connexion keep-alive")


if __name__ == "__main__":
    unittest.main()
