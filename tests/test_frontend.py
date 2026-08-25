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
        self.assertIn("Choose a zone first", self.app_js)

    def test_copy_clipboard_api_avec_repli(self):
        self.assertIn("navigator.clipboard.writeText", self.app_js)
        self.assertIn("navigator.clipboard.write", self.app_js)
        self.assertIn("function copyImage", self.app_js)
        self.assertIn("execCommand(\"copy\")", self.app_js.replace("'", '"'))

    def test_effacement_clipboard(self):
        self.assertIn("function clearClipboard", self.app_js)
        self.assertIn('writeClipboard("")', self.app_js)
        self.assertIn('className = "clear-btn"', self.app_js)

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
        # luminance relative WCAG -> meilleur premier-plan automatique
        self.assertIn("relativeChannel", self.app_js)
        self.assertIn("contrastRatio", self.app_js)
        self.assertIn("Math.pow", self.app_js)

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
        self.assertNotIn('aria-live="polite"', self.index_html)

    def test_refresh_ordonne_et_actions_clavier(self):
        self.assertIn("AbortController", self.app_js)
        self.assertIn("refreshGeneration", self.app_js)
        self.assertIn("zone-select", self.app_js)
        self.assertIn('tabIndex = 0', self.app_js)

    def test_index_selection_et_zoom(self):
        self.assertIn("selectedByZone", self.app_js)
        self.assertIn("history-index", self.app_js)
        self.assertIn('className = "zoom-btn"', self.app_js)
        self.assertIn('className = "copy-image-btn"', self.app_js)
        self.assertIn('id="pv-clear"', self.index_html)
        self.assertIn('id="pv-copy-image"', self.index_html)

    def test_version_dans_le_branding(self):
        self.assertIn("brand-version", self.index_html)
        self.assertIn("__PASTEBERTH_VERSION__", self.index_html)
        self.assertIn("brand-version", self.style_css)

    def test_raccourcis_clavier(self):
        self.assertIn('event.key === "c"', self.app_js)
        self.assertIn("/^[1-9]$/.test(event.key)", self.app_js)


if __name__ == "__main__":
    unittest.main()
