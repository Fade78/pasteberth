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
        self.assertIn("function toPng", self.app_js)
        self.assertIn('"image/png"', self.app_js)
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
        self.assertIn("function uploadBatch", self.app_js)
        self.assertIn("dataTransfer.files", self.app_js)
        self.assertIn('id="file-picker"', self.index_html)

    def test_selection_multiple_et_actions_de_zone(self):
        self.assertIn("selectedItemsByZone", self.app_js)
        self.assertIn("selectionAnchorByZone", self.app_js)
        self.assertIn("function selectHistoryItem", self.app_js)
        self.assertIn("event.shiftKey", self.app_js)
        self.assertIn("event.ctrlKey || event.metaKey", self.app_js)
        self.assertIn("function copySelectedLinks", self.app_js)
        self.assertIn("function downloadArchive", self.app_js)
        self.assertIn("function deleteSelected", self.app_js)
        self.assertIn("reference_separator", self.app_js)
        self.assertIn("bulk-selected", self.style_css)

    def test_etat_busy_zone(self):
        self.assertIn('zone_busy: "This zone is busy', self.app_js)
        self.assertIn("batchBusyZoneIds", self.app_js)
        self.assertIn("scheduleBusyRefresh", self.app_js)

    def test_responsive_et_dialog(self):
        self.assertIn("@media", self.style_css)
        self.assertIn("<dialog", self.index_html)
        self.assertIn("auto-fit", self.style_css)
        self.assertIn("dialog#pv:not([open])", self.style_css)
        self.assertIn("dialog#replace:not([open])", self.style_css)
        self.assertIn("dialog-fallback-backdrop", self.style_css)
        self.assertIn("dialog-fallback", self.style_css)
        self.assertIn('id="pv-backdrop"', self.index_html)
        self.assertNotIn('aria-live="polite"', self.index_html)

    def test_refresh_ordonne_et_actions_clavier(self):
        self.assertIn("AbortController", self.app_js)
        self.assertIn("refreshGeneration", self.app_js)
        self.assertIn("REFRESH_INTERVAL_MS = 10_000", self.app_js)
        self.assertIn("updateNewItemState", self.app_js)
        self.assertIn("newItemIdsByZone", self.app_js)
        self.assertIn(
            'setInterval(() => { if (document.visibilityState === "visible") boot(true); }, REFRESH_INTERVAL_MS)',
            self.app_js,
        )
        self.assertIn("zone-select", self.app_js)
        self.assertIn('tabIndex = 0', self.app_js)

    def test_nouveaux_fichiers_sont_signales_jusqua_selection(self):
        self.assertIn("createNewBadge", self.app_js)
        self.assertIn("new files available", self.app_js)
        self.assertIn('className = "new-badge"', self.app_js)
        self.assertIn(".new-badge", self.style_css)

    def test_commentaires_et_infobulles_detaillees(self):
        self.assertIn('method: "PATCH"', self.app_js)
        self.assertIn("/comment`", self.app_js)
        self.assertIn("function renderCommentControl", self.app_js)
        self.assertIn('button.textContent = item.comment ? "Edit comment" : "Comment";', self.app_js)
        self.assertIn("comment-text", self.app_js)
        self.assertIn("form.requestSubmit()", self.app_js)
        self.assertIn("function itemDetails", self.app_js)
        self.assertIn("wrap.title = itemDetails", self.app_js)
        self.assertIn("img.title = itemDetails", self.app_js)

    def test_option_chemin_complet(self):
        self.assertIn("showFullPath", self.app_js)
        self.assertIn("show_full_path", self.app_js)
        self.assertIn("pvRef.hidden = !state.showFullPath", self.app_js)

    def test_index_selection_et_zoom(self):
        self.assertIn("selectedByZone", self.app_js)
        self.assertIn("history-index", self.app_js)
        self.assertIn('className = "zoom-btn"', self.app_js)
        self.assertIn('className = "copy-image-btn"', self.app_js)
        self.assertIn('id="pv-clear"', self.index_html)
        self.assertIn('id="pv-copy-image"', self.index_html)
        self.assertIn('id="pv-toast"', self.index_html)

    def test_suppression_image_disponible(self):
        self.assertIn("function deleteImage", self.app_js)
        self.assertIn('className = "delete-btn"', self.app_js)
        self.assertIn('id="pv-delete"', self.index_html)
        self.assertIn('method: "DELETE"', self.app_js)
        self.assertIn("window.confirm", self.app_js)

    def test_reconciliation_apres_erreur_et_dialogue_legacy(self):
        self.assertIn('err.code === "retention_error"', self.app_js)
        self.assertIn("await refresh()", self.app_js)
        self.assertIn("function closePreview", self.app_js)
        self.assertIn('dialog.removeAttribute("open")', self.app_js)
        self.assertIn("function openDialog", self.app_js)
        self.assertIn("function closeDialog", self.app_js)
        self.assertIn("replacementBackdrop", self.app_js)
        self.assertIn('event.key === "Escape"', self.app_js)

    def test_previews_reessaient_les_reponses_temporaires(self):
        self.assertIn("function setPreviewSource", self.app_js)
        self.assertIn("function fetchPreview", self.app_js)
        self.assertIn("preview_retry", self.app_js)
        self.assertIn("PREVIEW_MAX_RETRIES = 3", self.app_js)
        self.assertIn("setPreviewSource(pvImg", self.app_js)
        self.assertIn("clearTimeout(img._previewRetryTimer)", self.app_js)

    def test_paste_texte_et_drop_binaire(self):
        self.assertIn('i.kind === "string"', self.app_js)
        self.assertIn('i.kind === "string" && i.type === "text/plain"', self.app_js)
        self.assertIn("getAsString", self.app_js)
        self.assertIn("new Blob([text]", self.app_js)
        self.assertIn('src="${escapeHtmlText(url)}"', self.app_js)
        self.assertIn("file.name || \"clipboard\"", self.app_js)
        self.assertIn('fd.append("preserve_name", "1")', self.app_js)
        self.assertIn('fd.append("replace", "1")', self.app_js)
        self.assertIn("preserveName: true", self.app_js)
        self.assertIn("askReplacement", self.app_js)
        self.assertIn('id="replace"', self.index_html)
        self.assertIn('id="replace-backdrop"', self.index_html)
        self.assertIn('id="replace-zone"', self.index_html)
        self.assertIn('id="replace-confirm"', self.index_html)
        self.assertIn('id="replace-cancel"', self.index_html)
        self.assertNotIn("Only images are accepted", self.app_js)

    def test_bouton_copie_par_kind(self):
        self.assertIn("KIND_LABEL", self.app_js)
        self.assertIn("Copy ${kindLabel(item.kind)}", self.app_js)
        self.assertIn("function copyContent", self.app_js)
        self.assertIn("function sanitizeHtml", self.app_js)
        self.assertIn("HTML_REMOVE_CONTENT_ELEMENTS", self.app_js)
        self.assertIn("HTML_SAFE_RASTER_DATA_URL", self.app_js)
        self.assertIn("function setRawHtmlButton", self.app_js)
        self.assertIn('id = "pv-copy-raw"', self.app_js)
        self.assertIn("{ raw: true }", self.app_js)
        self.assertIn("Text copied", self.app_js)
        self.assertIn("Raw HTML copied", self.app_js)
        self.assertIn("Bin copied", self.app_js)
        self.assertIn("function downloadLabel", self.app_js)
        self.assertIn("function downloadContent", self.app_js)
        self.assertIn('className = "download-btn"', self.app_js)
        self.assertIn('id="pv-download"', self.index_html)

    def test_clipboard_html_conserve_le_stockage_et_assainit_la_copie(self):
        self.assertIn("const source = await loadHtml()", self.app_js)
        self.assertIn("const html = raw ? source", self.app_js)
        self.assertIn(r"data:image\/", self.app_js)
        self.assertIn("raw ? source : plain", self.app_js)
        self.assertIn(".raw-html-btn", self.style_css)

    def test_preview_texte_et_binaire(self):
        self.assertIn("function openTextPreview", self.app_js)
        self.assertIn("function openContentPreview", self.app_js)
        self.assertIn('id="pv-text"', self.index_html)
        self.assertIn("link.download = filename", self.app_js)
        self.assertIn("pvDelete.dataset.zone", self.app_js)
        self.assertIn("generation !== previewGeneration", self.app_js)
        self.assertIn("activePreviewController.abort()", self.app_js)

    def test_echec_copie_signeale(self):
        self.assertIn("Link NOT copied", self.app_js)

    def test_mode_compact(self):
        self.assertIn("@media (max-width: 600px)", self.style_css)
        self.assertIn(".file-box", self.style_css)

    def test_version_dans_le_branding(self):
        self.assertIn("brand-version", self.index_html)
        self.assertIn("__PASTEBERTH_VERSION__", self.index_html)
        self.assertIn("brand-version", self.style_css)

    def test_raccourcis_clavier(self):
        self.assertIn('event.key === "c"', self.app_js)
        self.assertIn("/^[1-9]$/.test(event.key)", self.app_js)
        self.assertIn('key === "a" || key === "u"', self.app_js)
        self.assertIn("setTabZoneSelection", self.app_js)
        self.assertIn("tabSelectionAnchorId", self.app_js)
        self.assertIn("toggleOpenZone", self.app_js)
        self.assertIn("event.metaKey", self.app_js)
        self.assertIn("opt-tab-sidebar", self.app_js)
        self.assertIn("tabSidebarVisibility", self.app_js)
        self.assertIn('event.ctrlKey || event.metaKey || event.altKey', self.app_js)
        self.assertIn('setAttribute("aria-current"', self.app_js)
        self.assertIn("trapFallbackDialog", self.app_js)
        self.assertIn("applicationDialogOpen", self.app_js)

    def test_groupes_respectent_la_zone_visible_et_le_focus(self):
        self.assertIn("function getVisibleZones", self.app_js)
        self.assertIn("function reconcileActiveGroup", self.app_js)
        self.assertIn("getVisibleZones().some", self.app_js)
        self.assertIn('addEventListener("focusin"', self.app_js)
        self.assertIn("hideEmptyGroups", self.app_js)
        self.assertIn("state.showZoneCounts", self.app_js)
        self.assertIn("openZoneIds", self.app_js)
        self.assertIn("toggleOpenZone", self.app_js)
        self.assertIn("event.shiftKey", self.app_js)
        self.assertIn("groupLayouts", self.app_js)
        self.assertIn('id = "opt-layout"', self.app_js)
        self.assertIn("tab-zone-link", self.app_js)
        self.assertIn('aria-label="Zone groups"', self.index_html)
        self.assertIn('aria-current", "page"', self.app_js)


if __name__ == "__main__":
    unittest.main()
