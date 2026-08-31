"""Contrats de cycle de vie du serveur HTTP."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pasteberth.server import PasteberthServer

from tests.helpers import LiveServer, running_under_wine, write_config


class TestCycleDeVieServeur(unittest.TestCase):
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
