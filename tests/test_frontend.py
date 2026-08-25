"""Contrats frontend : mécanismes clés vérifiés par inspection du code livré.

Ces tests ne remplacent pas un test navigateur ; ils garantissent que les
mécanismes essentiels du workflow sont bien présents dans le JS/CSS servi :
interception du paste, copie via Clipboard API + repli, état actif non
uniquement chromatique, absence d'accumulation de Blob URLs.
"""
import unittest
from pathlib import Path

import pasteberth


class TestContratsFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(pasteberth.__file__).parent / "static"
        cls.app_js = (base / "app.js").read_text(encoding="utf-8")
        cls.style_css = (base / "style.css").read_text(encoding="utf-8")
        cls.index_html = (base.parent / "templates" / "index.html").read_text(encoding="utf-8")

    def test_paste_intercepte_au_niveau_page(self):
        self.assertIn('addEventListener("paste"', self.app_js)
        self.assertIn("clipboardData", self.app_js)
        self.assertIn('item.kind === "file"', self.app_js)

    def test_aucune_zone_devinee(self):
        # Sans zone active : demande explicite, jamais d'envoi ambigu.
        self.assertIn("requireActiveZone", self.app_js)
        self.assertIn("Choisissez d'abord une zone", self.app_js)

    def test_copy_clipboard_api_avec_repli(self):
        self.assertIn("navigator.clipboard.writeText", self.app_js)
        self.assertIn("execCommand(\"copy\")", self.app_js.replace("'", '"'))

    def test_pas_de_blob_urls_accumulees(self):
        self.assertNotIn("createObjectURL", self.app_js)
        self.assertIn("preview_url", self.app_js)  # miniatures servies par le serveur

    def test_etat_actif_non_chromatique(self):
        # bordure + halo + marqueur en plus de la couleur de fond
        self.assertIn(".zone.active", self.style_css)
        self.assertIn(".zone-marker", self.style_css)
        self.assertIn("box-shadow", self.style_css.split(".zone.active")[1][:400])
        self.assertIn("classList.toggle(\"active\"", self.app_js)

    def test_contraste_calcule(self):
        # luminance YIQ -> texte lisible automatique sur fond configuré
        self.assertIn("yiq", self.app_js.lower())

    def test_prete_pour_repetition(self):
        # après upload : re-rendu de la zone uniquement, pas de reload global
        self.assertIn("rerenderZone(", self.app_js)
        self.assertNotIn("location.reload", self.app_js)

    def test_drag_and_drop_sur_zones(self):
        self.assertIn('"drop"', self.app_js)
        self.assertIn('"dragover"', self.app_js)

    def test_responsive_et_dialog(self):
        self.assertIn("@media", self.style_css)
        self.assertIn("<dialog", self.index_html)
        self.assertIn("auto-fit", self.style_css)

    def test_raccourcis_clavier(self):
        self.assertIn('event.key === "c"', self.app_js)
        self.assertIn("/^[1-9]$/.test(event.key)", self.app_js)


if __name__ == "__main__":
    unittest.main()
