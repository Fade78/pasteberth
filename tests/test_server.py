"""Contrats de cycle de vie du serveur HTTP."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pasteberth.server import PasteberthServer

from tests.helpers import LiveServer, write_config


class TestCycleDeVieServeur(unittest.TestCase):
    def test_arret_attend_les_handlers_actifs(self):
        self.assertFalse(PasteberthServer.daemon_threads)
        self.assertTrue(PasteberthServer.block_on_close)

    def test_arret_ferme_les_connexions_keepalive(self):
        """Une connexion keep-alive ouverte ne doit pas bloquer l'arrêt."""
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
