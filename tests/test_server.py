"""Contrats de cycle de vie du serveur HTTP."""
import unittest

from pasteberth.server import PasteberthServer


class TestCycleDeVieServeur(unittest.TestCase):
    def test_arret_attend_les_handlers_actifs(self):
        self.assertFalse(PasteberthServer.daemon_threads)
        self.assertTrue(PasteberthServer.block_on_close)


if __name__ == "__main__":
    unittest.main()
