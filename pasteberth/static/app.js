"use strict";

/* Pasteberth — vanilla interface, no framework.
 *
 * Principles:
 * - the ACTIVE zone is explicit (border + halo + marker), never identified by
 *   background color alone;
 * - after every upload the UI is immediately ready for another paste;
 * - no Blob URL is retained: thumbnails are served by the server;
 * - the displayed/copied link is EXACTLY the one returned by the server.
 */
(() => {
  const URL_PREFIX = document.body.dataset.urlPrefix || "";

  function appPath(path) {
    return `${URL_PREFIX}${path}`;
  }

  const state = {
    zones: [],            // [{id,label,color,retain,count,images:[...]}]
    activeId: null,
    authEnabled: true,
    offline: false,
    selectedByZone: Object.create(null),
    retryTimer: null,
    busyRefreshTimer: null,
    toastTimer: null,
    // Groups
    groups: [],              // [{name, selection, pattern, layout, zone_ids, ...}]
    activeGroupId: null,     // null = implicit All when no group is selected
    openZoneIds: [],
    tabSelectionAnchorId: null,
    groupLayouts: Object.create(null),
    tabSidebarVisibility: Object.create(null),
    selectedItemsByZone: Object.create(null),
    selectionAnchorByZone: Object.create(null),
    batchBusyZoneIds: new Set(),
    hideEmptyGroups: false,
    showZoneCounts: true,
    initialized: false,
  };

  let refreshGeneration = 0;
  let activeRefreshController = null;
  let previewGeneration = 0;
  let activePreviewController = null;
  let groupOptionsClose = null;

  const grid = document.getElementById("grid");
  const groupTabs = document.getElementById("group-tabs");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const logoutForm = document.getElementById("logout-form");
  const toastEl = document.getElementById("toast");
  const pvToastEl = document.getElementById("pv-toast");
  const pv = document.getElementById("pv");
  const pvImg = document.getElementById("pv-img");
  const pvText = document.getElementById("pv-text");
  const pvRef = document.getElementById("pv-ref");
  const pvCopy = document.getElementById("pv-copy");
  const pvCopyImage = document.getElementById("pv-copy-image");
  const pvDownload = document.getElementById("pv-download");
  const pvClear = document.getElementById("pv-clear");
  const pvDelete = document.getElementById("pv-delete");
  const previewBackdrop = document.getElementById("pv-backdrop");
  const replacementDialog = document.getElementById("replace");
  const replacementBackdrop = document.getElementById("replace-backdrop");
  const replacementFilename = document.getElementById("replace-filename");
  const replacementZone = document.getElementById("replace-zone");
  const replacementCancel = document.getElementById("replace-cancel");
  const replacementConfirm = document.getElementById("replace-confirm");
  const filePicker = document.getElementById("file-picker");
  const replacementQueue = [];
  const dialogInvokers = new WeakMap();
  let activeReplacementPrompt = null;
  let pvCopyRawHtml = null;

  // ------------------------------------------------------------- utilities

  function hexToRgb(hex) {
    return [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16),
    ];
  }

  function relativeChannel(value) {
    const channel = value / 255;
    return channel <= 0.04045
      ? channel / 12.92
      : Math.pow((channel + 0.055) / 1.055, 2.4);
  }

  function luminance(hex) {
    const [r, g, b] = hexToRgb(hex);
    return 0.2126 * relativeChannel(r)
      + 0.7152 * relativeChannel(g)
      + 0.0722 * relativeChannel(b);
  }

  function contrastRatio(background, foreground) {
    const a = luminance(background);
    const b = luminance(foreground);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  }

  function readableFg(hex) {
    const dark = "#12161b";
    const light = "#f3f6fa";
    return contrastRatio(hex, dark) >= contrastRatio(hex, light) ? dark : light;
  }

  function rgba(hex, alpha) {
    const [r, g, b] = hexToRgb(hex);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function fmtBytes(n) {
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString("en", { year: "numeric", month: "2-digit", day: "2-digit" })
      + " " + fmtTime(iso);
  }

  function toast(message, kind = "info") {
    const previewOpen = pv.hasAttribute("open");
    const target = previewOpen ? pvToastEl : toastEl;
    const other = previewOpen ? toastEl : pvToastEl;
    target.textContent = message;
    target.className = "toast " + kind;
    target.hidden = false;
    other.hidden = true;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      toastEl.hidden = true;
      pvToastEl.hidden = true;
    }, 2600);
  }

  async function api(path, options) {
    let res;
    try {
      const requestOptions = Object.assign({}, options || {});
      requestOptions.headers = Object.assign({ Accept: "application/json" }, requestOptions.headers || {});
      res = await fetch(appPath(path), requestOptions);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new Error("network unreachable");
    }
    if (res.status === 401 && state.authEnabled) {
      window.location.href = appPath("/login");
      throw new Error("session expired");
    }
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      const code = payload && payload.error ? payload.error.code : "error";
      const knownMessages = {
        unauthorized: "Authentication required",
        forbidden_origin: "Request origin was rejected",
        invalid_request: "Invalid request",
        method_not_allowed: "Method not allowed",
        not_found: "Resource not found",
        internal: "Internal server error",
        unknown_zone: "Unknown zone",
        unknown_image: "Unknown image",
        invalid_filename: "The dropped filename is invalid",
        empty_upload: "The upload is empty",
        invalid_image: "The image is invalid or corrupted",
        unsupported_format: "This image format is not supported",
        unsupported_media_type: "This media type is not supported",
        too_large: "The upload is too large",
        payload_too_large: "The upload is too large",
        storage_low: "Not enough disk space",
        retention_error: "Image retention failed",
        storage_conflict: "Name taken by an unmanaged file",
        replacement_required: "This name already exists; confirm replacement",
        destination_error: "The image destination is unavailable",
        zone_busy: "This zone is busy; try again shortly",
        preview_busy: "Too many previews are currently being served",
        rate_limited: "Too many attempts; try again later",
        upload_busy: "Too many uploads are currently in memory",
      };
      const message = knownMessages[code]
        || (payload && payload.error ? payload.error.message : `error ${res.status}`);
      const err = new Error(message);
      err.code = code;
      err.status = res.status;
      const retryAfter = Number(res.headers.get("Retry-After"));
      if (Number.isFinite(retryAfter) && retryAfter >= 0) err.retryAfter = retryAfter;
      throw err;
    }
    return payload;
  }

  // ---------------------------------------------------------------- clipboard

  async function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext !== false) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) { /* try the fallback */ }
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "copy-fallback";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* unsupported */ }
    ta.remove();
    return ok;
  }

  async function copyLink(reference) {
    const ok = await writeClipboard(reference);
    if (ok) toast("Link copied: " + shortRef(reference));
    else toast("Could not copy the link — select it manually", "error");
    return ok;
  }

  async function toPng(blob) {
    if (blob.type === "image/png") return blob;
    if (typeof createImageBitmap !== "function") throw new Error("image conversion unavailable");
    const bitmap = await createImageBitmap(blob);
    try {
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("canvas unavailable");
      context.drawImage(bitmap, 0, 0);
      return await new Promise((resolve, reject) => {
        canvas.toBlob((result) => {
          if (result) resolve(result);
          else reject(new Error("PNG conversion failed"));
        }, "image/png");
      });
    } finally {
      if (typeof bitmap.close === "function") bitmap.close();
    }
  }

  async function copyImage(previewUrl) {
    if (
      !navigator.clipboard
      || typeof navigator.clipboard.write !== "function"
      || typeof ClipboardItem === "undefined"
      || window.isSecureContext === false
    ) {
      toast("Image copying is not supported by this browser", "error");
      return false;
    }
    try {
      const response = await fetchPreview(previewUrl, {
        credentials: "same-origin",
        headers: { Accept: "image/*" },
      });
      if (!response.ok) throw new Error("preview unavailable");
      const blob = await response.blob();
      if (!/^image\//.test(blob.type)) throw new Error("not an image");
      const png = await toPng(blob);
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      toast("Image copied");
      return true;
    } catch (_) {
      toast("Could not copy the image to the clipboard", "error");
      return false;
    }
  }

  async function clearClipboard() {
    const ok = await writeClipboard("");
    if (ok) toast("Clipboard cleared");
    else toast("Could not clear the clipboard — use the system clipboard", "error");
    return ok;
  }

  const KIND_LABEL = { image: "Image", text: "Text", binary: "Bin" };

  function kindLabel(kind) {
    return KIND_LABEL[kind] || "Content";
  }

  function fileTypeLabel(filename) {
    const match = /\.([A-Za-z0-9]+)$/.exec(filename || "");
    return match ? match[1].toUpperCase() : "FILE";
  }

  function contentActionLabel(item) {
    return `Copy ${kindLabel(item.kind)}`;
  }

  function downloadLabel(filename) {
    return `Download ${fileTypeLabel(filename)}`;
  }

  function downloadContent(previewUrl, filename) {
    const link = document.createElement("a");
    link.href = previewUrl;
    link.download = filename || "download";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function escapeHtmlText(text) {
    return text.replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("read failed"));
      reader.readAsDataURL(blob);
    });
  }

  // Presse-papiers mixte : un seul document HTML, texte + images incrustées
  // dans l'ordre des flavors. Les appels getAsString/getAsFile doivent être
  // émis pendant l'événement ; les slots préservent l'ordre d'origine.
  async function uploadMixedClipboard(zoneId, items) {
    const slots = [];
    const waits = [];
    for (const item of items) {
      if (item.kind === "file" && /^image\//.test(item.type)) {
        const blob = item.getAsFile();
        if (blob) slots.push({ kind: "image", blob });
      } else if (item.kind === "string" && item.type === "text/plain") {
        const slot = { kind: "text", text: "" };
        slots.push(slot);
        waits.push(new Promise((resolve) => item.getAsString((text) => {
          slot.text = text || "";
          resolve();
        })));
      }
    }
    if (waits.length) await Promise.all(waits);
    // Texte absent ou vide : un collage d'image reste une image simple.
    let hasText = false;
    for (const part of slots) {
      if (part && part.kind === "text" && part.text.trim()) {
        hasText = true;
        break;
      }
    }
    if (!hasText) {
      const image = slots.find(part => part && part.kind === "image");
      if (image) {
        upload(zoneId, image.blob);
        return;
      }
      toast("The clipboard does not contain an image or text");
      return;
    }
    const chunks = [];
    for (const part of slots) {
      if (!part) continue;
      if (part.kind === "text") {
        chunks.push(`<pre>${escapeHtmlText(part.text)}</pre>`);
      } else {
        try {
          const url = await blobToDataUrl(part.blob);
          chunks.push(`<p><img alt="" src="${escapeHtmlText(url)}"></p>`);
        } catch (_) { /* image illisible : ignorée */ }
      }
    }
    if (!chunks.length) return;
    const html = `<!doctype html><html><body>${chunks.join("\n")}</body></html>`;
    upload(zoneId, new Blob([html], { type: "text/html" }));
  }

  const HTML_ALLOWED_ELEMENTS = new Set([
    "html", "head", "body", "title",
    "p", "div", "span", "br", "hr",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "small", "sub", "sup",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "img",
  ]);
  const HTML_REMOVE_CONTENT_ELEMENTS = new Set([
    "base", "embed", "iframe", "link", "meta", "object", "script", "style",
    "svg", "math", "canvas", "video", "audio", "source", "track", "template",
  ]);
  const HTML_SAFE_ATTRIBUTES = new Set([
    "alt", "colspan", "dir", "height", "lang", "rowspan", "start", "title", "width",
  ]);
  const HTML_URL_ATTRIBUTES = new Set([
    "action", "background", "formaction", "href", "poster", "src", "srcset", "xlink:href",
  ]);
  const HTML_SAFE_RASTER_DATA_URL = /^data:image\/(?:gif|jpe?g|png|webp);base64,[a-z0-9+/]+={0,2}$/i;

  function safeHtmlAttribute(tag, name, value) {
    if (name === "src") {
      return tag === "img" && HTML_SAFE_RASTER_DATA_URL.test(value.trim());
    }
    if (HTML_URL_ATTRIBUTES.has(name)) return false;
    if (!HTML_SAFE_ATTRIBUTES.has(name)) return false;
    if (name === "alt" || name === "height" || name === "width") return tag === "img";
    if (name === "colspan" || name === "rowspan") return tag === "th" || tag === "td";
    if (name === "start") return tag === "ol";
    return true;
  }

  function sanitizeHtml(source) {
    const doc = new DOMParser().parseFromString(source, "text/html");
    let changed = false;

    const clean = (parent) => {
      for (const child of [...parent.childNodes]) {
        if (child.nodeType === Node.COMMENT_NODE) {
          child.remove();
          changed = true;
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        const tag = child.tagName.toLowerCase();
        if (HTML_REMOVE_CONTENT_ELEMENTS.has(tag)) {
          child.remove();
          changed = true;
          continue;
        }
        if (!HTML_ALLOWED_ELEMENTS.has(tag)) {
          clean(child);
          while (child.firstChild) parent.insertBefore(child.firstChild, child);
          child.remove();
          changed = true;
          continue;
        }
        for (const attribute of [...child.attributes]) {
          if (!safeHtmlAttribute(tag, attribute.name.toLowerCase(), attribute.value)) {
            child.removeAttribute(attribute.name);
            changed = true;
          }
        }
        clean(child);
      }
    };

    clean(doc);
    return {
      html: doc.body ? doc.body.innerHTML : "",
      plain: ((doc.body && doc.body.textContent) || "").trim(),
      changed,
    };
  }

  function canWriteRichClipboard() {
    return Boolean(
      navigator.clipboard
      && typeof navigator.clipboard.write === "function"
      && typeof ClipboardItem !== "undefined"
      && window.isSecureContext !== false
    );
  }

  async function copyHtmlContent(previewUrl, { raw = false } = {}) {
    const loadHtml = async () => {
      const response = await fetchPreview(previewUrl, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error("preview unavailable");
      return response.text();
    };
    try {
      const source = await loadHtml();
      const sanitized = sanitizeHtml(source);
      const html = raw ? source : (sanitized.changed ? sanitized.html : source);
      const plain = sanitized.plain;
      if (canWriteRichClipboard()) {
        const flavors = { "text/html": new Blob([html], { type: "text/html" }) };
        if (plain) flavors["text/plain"] = new Blob([plain], { type: "text/plain" });
        try {
          await navigator.clipboard.write([new ClipboardItem(flavors)]);
          toast(raw ? "Raw HTML copied" : "Text copied");
          return true;
        } catch (_) { /* try the text fallback */ }
      }
      const fallbackText = raw ? source : plain;
      const ok = await writeClipboard(fallbackText);
      if (ok) toast(raw ? "Raw HTML copied" : "Text copied");
      else toast("Could not copy the text to the clipboard", "error");
      return ok;
    } catch (_) {
      toast("Could not copy the text to the clipboard", "error");
      return false;
    }
  }

  async function copyContent(kind, previewUrl, mime) {
    if (kind === "image") return copyImage(previewUrl);
    if (kind === "text") {
      if (mime === "text/html") {
        return copyHtmlContent(previewUrl);
      }
      try {
        const response = await fetchPreview(previewUrl, {
          credentials: "same-origin",
          headers: { Accept: "text/plain" },
        });
        if (!response.ok) throw new Error("preview unavailable");
        const text = await response.text();
        const ok = await writeClipboard(text);
        if (ok) toast("Text copied");
        else toast("Could not copy the text to the clipboard", "error");
        return ok;
      } catch (_) {
        toast("Could not copy the text to the clipboard", "error");
        return false;
      }
    }
    if (
      !navigator.clipboard
      || typeof navigator.clipboard.write !== "function"
      || typeof ClipboardItem === "undefined"
      || window.isSecureContext === false
    ) {
      toast("Binary copying is not supported by this browser", "error");
      return false;
    }
    try {
      const response = await fetchPreview(previewUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error("preview unavailable");
      const blob = await response.blob();
      const type = blob.type || "application/octet-stream";
      await navigator.clipboard.write([new ClipboardItem({ [type]: blob })]);
      toast("Bin copied");
      return true;
    } catch (_) {
      toast("Could not copy the binary to the clipboard", "error");
      return false;
    }
  }

  function shortRef(ref) {
    const name = ref.split("/").pop();
    return name.length > 40 ? name.slice(0, 37) + "…" : name;
  }

  const PREVIEW_MAX_RETRIES = 3;

  function previewAttemptUrl(url, retry) {
    if (!retry) return url;
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}preview_retry=${retry}`;
  }

  function setPreviewSource(img, url) {
    if (img._previewRetryTimer) {
      window.clearTimeout(img._previewRetryTimer);
      img._previewRetryTimer = null;
    }
    if (img._previewErrorHandler) {
      img.removeEventListener("error", img._previewErrorHandler);
    }
    if (!url) {
      img._previewErrorHandler = null;
      img.src = "";
      return;
    }
    let retries = 0;
    const onError = () => {
      if (retries >= PREVIEW_MAX_RETRIES) return;
      retries += 1;
      img._previewRetryTimer = window.setTimeout(() => {
        img._previewRetryTimer = null;
        img.src = previewAttemptUrl(url, retries);
      }, retries * 1000);
    };
    img._previewErrorHandler = onError;
    img.addEventListener("error", onError);
    img.src = url;
  }

  async function fetchPreview(url, options) {
    for (let retry = 0; retry <= PREVIEW_MAX_RETRIES; retry += 1) {
      const response = await fetch(previewAttemptUrl(url, retry), options);
      if (response.status !== 503 || retry === PREVIEW_MAX_RETRIES) return response;
      const retryAfter = Number(response.headers.get("Retry-After"));
      const delay = Number.isFinite(retryAfter) && retryAfter >= 0
        ? retryAfter * 1000
        : (retry + 1) * 1000;
      await new Promise(resolve => window.setTimeout(resolve, Math.min(delay, 5000)));
    }
    throw new Error("preview unavailable");
  }

  // ------------------------------------------------------------- rendering

  function applyZoneColors(el, color) {
    el.style.setProperty("--bg", color);
    el.style.setProperty("--fg", readableFg(color));
    el.style.setProperty("--halo", rgba(readableFg(color), 0.75));
    el.style.setProperty("--halo-soft", rgba(readableFg(color), 0.28));
    el.style.setProperty("--line", rgba(readableFg(color), 0.18));
  }

  function selectedItemIds(zoneId) {
    const selected = state.selectedItemsByZone[zoneId];
    return selected instanceof Set ? selected : new Set();
  }

  function selectedItems(zone) {
    const selected = selectedItemIds(zone.id);
    return zone.images.filter(item => selected.has(item.id));
  }

  function selectHistoryItem(zone, itemId, event) {
    const ids = zone.images.map(item => item.id);
    const itemIndex = ids.indexOf(itemId);
    if (itemIndex < 0) return;
    const toggle = event.ctrlKey || event.metaKey;
    const previous = selectedItemIds(zone.id);
    let next = new Set(previous);
    if (event.shiftKey) {
      let anchor = state.selectionAnchorByZone[zone.id];
      if (!anchor || !ids.includes(anchor)) anchor = state.selectedByZone[zone.id] || itemId;
      const anchorIndex = Math.max(0, ids.indexOf(anchor));
      const start = Math.min(anchorIndex, itemIndex);
      const end = Math.max(anchorIndex, itemIndex);
      const range = ids.slice(start, end + 1);
      if (!toggle) {
        next = new Set(range);
      } else {
        const remove = range.every(id => next.has(id));
        for (const id of range) {
          if (remove) next.delete(id);
          else next.add(id);
        }
      }
    } else if (toggle) {
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      state.selectionAnchorByZone[zone.id] = itemId;
    } else {
      next = new Set([itemId]);
      state.selectionAnchorByZone[zone.id] = itemId;
    }
    state.selectedItemsByZone[zone.id] = next;
    state.selectedByZone[zone.id] = itemId;
    rerenderZone(zone.id);
  }

  function formatReferenceList(zone, items) {
    const prefix = zone.reference_list_prefix ?? "";
    const suffix = zone.reference_list_suffix ?? "";
    const separator = zone.reference_separator ?? ",";
    return `${prefix}${items.map(item => item.reference).join(separator)}${suffix}`;
  }

  async function copySelectedLinks(zone) {
    const items = selectedItems(zone);
    if (!items.length) return false;
    const ok = await writeClipboard(formatReferenceList(zone, items));
    if (ok) toast(`${items.length} links copied`);
    else toast("Could not copy the links — select them manually", "error");
    return ok;
  }

  function downloadArchive(zone, items) {
    if (zone.busy || state.batchBusyZoneIds.has(zone.id)) {
      toast("This zone is busy; try again shortly", "error");
      return;
    }
    if (zone.allow_zip_download === false) {
      toast("ZIP downloads are disabled for this zone", "error");
      return;
    }
    const target = `pb-archive-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const frame = document.createElement("iframe");
    frame.name = target;
    frame.hidden = true;
    const form = document.createElement("form");
    form.method = "post";
    form.action = appPath(`/api/zones/${encodeURIComponent(zone.id)}/images/archive`);
    form.target = target;
    form.hidden = true;
    for (const item of items) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "filename";
      input.value = item.filename;
      form.appendChild(input);
    }
    document.body.append(frame, form);
    form.submit();
    toast(`Preparing ZIP with ${items.length} files`);
    scheduleBusyRefresh();
    window.setTimeout(() => {
      frame.remove();
      form.remove();
    }, 60_000);
  }

  async function deleteSelected(zone, items) {
    if (zone.busy || state.batchBusyZoneIds.has(zone.id)) {
      toast("This zone is busy; try again shortly", "error");
      return;
    }
    if (!window.confirm(`Delete ${items.length} selected files from the disk?`)) return;
    state.batchBusyZoneIds.add(zone.id);
    renderAll();
    try {
      const result = await apiWithZoneRetry(
        `/api/zones/${encodeURIComponent(zone.id)}/images/batch-delete`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filenames: items.map(item => item.filename) }),
        },
      );
      await refresh();
      const failed = result.failed || [];
      if (failed.length) {
        toast(`${result.deleted.length} files deleted, ${failed.length} failed`, "error");
      } else {
        toast(`${result.deleted.length} files deleted`);
      }
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.batchBusyZoneIds.delete(zone.id);
      renderAll();
    }
  }

  async function apiWithZoneRetry(path, options, attempts = 3) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await api(path, options);
      } catch (err) {
        if (err.code !== "zone_busy" || attempt >= attempts - 1) throw err;
        const delay = Math.min((err.retryAfter ?? 1) * 1000, 5000);
        await new Promise(resolve => window.setTimeout(resolve, delay));
      }
    }
  }

  function renderBulkActions(zone) {
    const items = selectedItems(zone);
    if (items.length < 2) return null;
    const actions = document.createElement("div");
    actions.className = "bulk-actions";
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", "Selected files");
    const summary = document.createElement("span");
    summary.className = "bulk-summary";
    summary.textContent = `${items.length} files selected`;
    summary.setAttribute("role", "status");
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-btn";
    copy.textContent = "Copy links";
    copy.setAttribute("aria-label", `Copy ${items.length} links`);
    copy.addEventListener("click", () => copySelectedLinks(zone));
    const archive = document.createElement("button");
    archive.type = "button";
    archive.className = "download-btn";
    archive.textContent = "Download ZIP";
    archive.setAttribute("aria-label", `Download ${items.length} files as ZIP`);
    archive.disabled = zone.allow_zip_download === false;
    archive.title = archive.disabled ? "ZIP downloads are disabled for this zone" : "";
    archive.addEventListener("click", () => downloadArchive(zone, items));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "delete-btn";
    remove.textContent = "Delete selected";
    remove.setAttribute("aria-label", `Delete ${items.length} selected files`);
    remove.addEventListener("click", () => deleteSelected(zone, items));
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "ghost-btn";
    clear.textContent = "Clear selection";
    clear.addEventListener("click", () => {
      state.selectedItemsByZone[zone.id] = new Set();
      state.selectionAnchorByZone[zone.id] = null;
      rerenderZone(zone.id);
    });
    actions.append(summary, copy, archive, remove, clear);
    const busy = zone.busy || state.batchBusyZoneIds.has(zone.id);
    for (const control of [copy, archive, remove]) {
      control.disabled = busy;
      if (busy) control.title = "This zone is busy";
    }
    return actions;
  }

  function chooseFiles(zoneId) {
    if (!filePicker) return;
    filePicker.value = "";
    filePicker.dataset.zone = zoneId;
    filePicker.click();
  }

  function renderZone(zone) {
    const el = document.createElement("section");
    el.className = "zone";
    el.dataset.zone = zone.id;
    if (zone.id === state.activeId) el.classList.add("active");
    if (state.batchBusyZoneIds.has(zone.id)) el.classList.add("busy");
    if (zone.busy) el.classList.add("server-busy");
    applyZoneColors(el, zone.color);

    const head = document.createElement("header");
    head.className = "zone-head";
    const select = document.createElement("button");
    select.type = "button";
    select.className = "zone-select";
    select.setAttribute("aria-current", String(zone.id === state.activeId));
    select.setAttribute("aria-label", `Select zone ${zone.label}`);
    select.innerHTML =
      '<span class="zone-marker" aria-hidden="true"></span>' +
      '<span class="zone-label"></span>' +
      '<span class="zone-count"></span>';
    select.querySelector(".zone-label").textContent = zone.label;
    select.querySelector(".zone-count").textContent = `${zone.images.length} / ${zone.retain}`;
    const uploadButton = document.createElement("button");
    uploadButton.type = "button";
    uploadButton.className = "zone-upload-btn";
    uploadButton.textContent = "Add files";
    uploadButton.setAttribute("aria-label", `Add files to ${zone.label}`);
    uploadButton.disabled = zone.busy || state.batchBusyZoneIds.has(zone.id);
    uploadButton.addEventListener("click", event => {
      event.stopPropagation();
      setActive(zone.id);
      chooseFiles(zone.id);
    });
    head.append(select, uploadButton);
    el.appendChild(head);

    if (zone.busy) {
      const busy = document.createElement("div");
      busy.className = "zone-lock";
      busy.setAttribute("role", "status");
      busy.textContent = "Zone busy; another operation is using it";
      el.appendChild(busy);
    }

    if (zone.images.length === 0) {
      const hint = document.createElement("div");
      hint.className = "drop-hint";
      hint.textContent = zone.id === state.activeId
        ? "Active paste target: Ctrl/Command+V to paste or drag a file here"
        : "Select this zone to make it the paste target, or drag a file here";
      el.appendChild(hint);
    } else {
      const selected = selectedItem(zone);
      const selectedItemsInZone = selectedItems(zone);
      el.appendChild(renderLatest(zone, selected, selectedItemsInZone));
      el.appendChild(renderThumbs(zone.images, selected.id, selectedItemIds(zone.id)));
    }
    return el;
  }

  function selectedItem(zone) {
    return zone.images.find(item => item.id === state.selectedByZone[zone.id]) || zone.images[0];
  }

  function itemForControl(control) {
    const zoneEl = control.closest(".zone");
    if (!zoneEl) return null;
    const zone = state.zones.find(item => item.id === zoneEl.dataset.zone);
    if (!zone) return null;
    const itemId = control.closest("[data-item-id]")?.dataset.itemId;
    return zone.images.find(item => item.id === itemId) || selectedItem(zone);
  }

  function zoneForControl(control) {
    return control.closest(".zone")?.dataset.zone || null;
  }

  function itemMeta(item) {
    const meta = document.createElement("div");
    meta.className = "meta";
    const fname = document.createElement("code");
    fname.className = "fname";
    fname.textContent = item.filename;
    const ref = document.createElement("code");
    ref.className = "ref";
    ref.title = item.reference;
    ref.textContent = item.reference;
    const dims = document.createElement("span");
    dims.className = "dims";
    const sizeInfo = `${fmtBytes(item.size)} · ${fmtTime(item.created_at)}`;
    dims.textContent = item.kind === "image" && item.width != null && item.height != null
      ? `${item.width}×${item.height} · ${sizeInfo}`
      : `${kindLabel(item.kind)} · ${sizeInfo}`;
    meta.append(fname, ref, dims);
    return meta;
  }

  function renderLatest(zone, item, selectedItemsInZone = []) {
    const card = document.createElement("div");
    card.className = "latest";
    card.dataset.itemId = item.id;

    if (selectedItemsInZone.length > 1) {
      card.classList.add("selection-latest");
      const summary = document.createElement("div");
      summary.className = "selection-summary";
      const heading = document.createElement("h3");
      heading.className = "selection-summary-title";
      heading.textContent = "Selected files";
      const list = document.createElement("ul");
      list.className = "selection-summary-list";
      list.setAttribute("aria-label", "Selected files");
      for (const selectedItem of selectedItemsInZone) {
        const entry = document.createElement("li");
        entry.className = "selection-summary-item";
        const name = document.createElement("code");
        name.className = "selection-summary-name";
        name.textContent = selectedItem.filename;
        name.title = selectedItem.filename;
        const metadata = document.createElement("span");
        metadata.className = "selection-summary-meta";
        metadata.textContent = `${fmtBytes(selectedItem.size)} · ${fmtDateTime(selectedItem.created_at)}`;
        entry.append(name, metadata);
        list.appendChild(entry);
      }
      summary.append(heading, list);

      const actions = document.createElement("div");
      actions.className = "latest-right selection-actions";
      actions.appendChild(renderBulkActions(zone));
      card.append(summary, actions);
      return card;
    }

    const zoneId = zone.id;
    const right = document.createElement("div");
    right.className = "latest-right";
    right.appendChild(itemMeta(item));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.dataset.ref = item.reference;
    btn.textContent = "Copy link";
    const imageCopy = document.createElement("button");
    imageCopy.type = "button";
    imageCopy.className = "copy-image-btn";
    const actionLabel = contentActionLabel(item);
    imageCopy.textContent = actionLabel;
    imageCopy.setAttribute(
      "aria-label",
      `${actionLabel} to the clipboard`,
    );
    imageCopy.dataset.preview = item.preview_url;
    imageCopy.dataset.kind = item.kind;
    imageCopy.dataset.filename = item.filename;
    imageCopy.dataset.mime = item.mime || "";
    const download = document.createElement("button");
    download.type = "button";
    download.className = "download-btn";
    download.textContent = downloadLabel(item.filename);
    download.setAttribute("aria-label", downloadLabel(item.filename));
    download.dataset.preview = item.preview_url;
    download.dataset.filename = item.filename;
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "clear-btn";
    clear.textContent = "Clear";
    clear.setAttribute("aria-label", "Clear the clipboard");
    let zoom = null;
    if (item.kind !== "binary") {
      zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "zoom-btn";
      zoom.textContent = item.kind === "image" ? "Zoom" : "Preview";
      zoom.setAttribute(
        "aria-label",
        item.kind === "image"
          ? `Enlarge the image ${item.filename}`
          : `Preview ${item.filename}`,
      );
      zoom.dataset.ref = item.reference;
      zoom.dataset.preview = item.preview_url;
      zoom.dataset.kind = item.kind;
      zoom.dataset.filename = item.filename;
    }
    const del = document.createElement("button");
    del.type = "button";
    del.className = "delete-btn";
    del.textContent = "Delete";
    del.setAttribute("aria-label", `Delete ${item.filename} from the disk`);
    del.dataset.zone = zoneId;
    del.dataset.filename = item.filename;
    const actions = document.createElement("div");
    actions.className = "latest-actions";
    actions.append(btn);
    if (item.kind !== "binary") actions.append(imageCopy);
    actions.append(download, clear);
    if (zoom) actions.append(zoom);
    actions.append(del);
    right.appendChild(actions);
    if (item.kind === "image") {
      const img = document.createElement("img");
      img.className = "thumb-big";
      img.dataset.itemId = item.id;
      setPreviewSource(img, item.preview_url);
      img.alt = `Latest image ${item.filename}`;
      img.loading = "lazy";
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", `Open preview of ${item.filename}`);
      card.append(img, right);
    } else {
      const box = document.createElement("div");
      box.className = "file-box";
      box.dataset.itemId = item.id;
      box.textContent = item.kind === "text" ? "TXT" : "FILE";
      box.tabIndex = 0;
      box.setAttribute("role", "button");
      box.setAttribute(
        "aria-label",
        item.kind === "binary"
          ? `Download ${item.filename}`
          : `Open preview of ${item.filename}`,
      );
      card.append(box, right);
    }
    return card;
  }

  function renderThumbs(items, selectedId, selectedIds = new Set()) {
    const index = document.createElement("div");
    index.className = "history-index";
    const title = document.createElement("div");
    title.className = "index-title";
    title.textContent = items.every(item => item.kind === "image") ? "Image index" : "Content index";
    const row = document.createElement("div");
    row.className = "thumbs";
    row.setAttribute("role", "list");
    for (const item of items) {
      const wrap = document.createElement("button");
      wrap.type = "button";
      wrap.className = "thumb-wrap";
      if (item.id === selectedId) wrap.classList.add("selected");
      if (selectedIds.has(item.id)) wrap.classList.add("bulk-selected");
      wrap.setAttribute("aria-current", String(item.id === selectedId));
      wrap.setAttribute("aria-pressed", String(selectedIds.has(item.id)));
      wrap.setAttribute("aria-label", `Select ${item.filename}`);
      wrap.title = `${item.filename} — ${fmtDateTime(item.created_at)}`;
      wrap.dataset.itemId = item.id;
      if (item.kind === "image") {
        const img = document.createElement("img");
        img.className = "thumb";
        setPreviewSource(img, item.preview_url);
        img.alt = item.filename;
        img.loading = "lazy";
        wrap.appendChild(img);
      } else {
        const label = document.createElement("span");
        label.className = "thumb-content";
        label.textContent = item.kind === "text" ? "TXT" : fileTypeLabel(item.filename);
        label.setAttribute("aria-hidden", "true");
        wrap.appendChild(label);
      }
      row.appendChild(wrap);
    }
    index.append(title, row);
    return index;
  }

  function getVisibleZones() {
    if (!state.groups.length) return state.zones;
    const group = state.groups.find(g => g.name === state.activeGroupId);
    if (!group) return [];
    return state.zones.filter(z => group.zone_ids.includes(z.id));
  }

  function isGroupLayout(value) {
    return value === "area" || value === "tab";
  }

  function groupLayout(group) {
    if (!group) return "area";
    if (isGroupLayout(state.groupLayouts[group.name])) return state.groupLayouts[group.name];
    return isGroupLayout(group.layout) ? group.layout : "area";
  }

  function loadGroupLayouts() {
    try {
      const stored = JSON.parse(localStorage.getItem("pb.groupLayouts") || "{}");
      if (!stored || typeof stored !== "object" || Array.isArray(stored)) return;
      for (const [name, layout] of Object.entries(stored)) {
        if (isGroupLayout(layout)) state.groupLayouts[name] = layout;
      }
    } catch (_) {}
  }

  function saveGroupLayouts() {
    try {
      localStorage.setItem("pb.groupLayouts", JSON.stringify(state.groupLayouts));
    } catch (_) {}
  }

  function tabSidebarVisible(group) {
    return Boolean(group && state.tabSidebarVisibility[group.name] !== false);
  }

  function loadTabSidebarVisibility() {
    try {
      const stored = JSON.parse(localStorage.getItem("pb.tabSidebarVisibility") || "{}");
      if (!stored || typeof stored !== "object" || Array.isArray(stored)) return;
      for (const [name, visible] of Object.entries(stored)) {
        if (typeof visible === "boolean") state.tabSidebarVisibility[name] = visible;
      }
    } catch (_) {}
  }

  function saveTabSidebarVisibility() {
    try {
      localStorage.setItem("pb.tabSidebarVisibility", JSON.stringify(state.tabSidebarVisibility));
    } catch (_) {}
  }

  function groupZoneCount(group) {
    return group.zone_count ?? group.zone_ids?.length ?? 0;
  }

  function groupIsDisplayed(group) {
    return !(groupZoneCount(group) === 0 && (state.hideEmptyGroups || group.hide_empty));
  }

  function displayedGroups() {
    return state.groups.filter(groupIsDisplayed);
  }

  function reconcileActiveGroup() {
    const previous = state.activeGroupId;
    if (!state.groups.length) {
      state.activeGroupId = null;
    } else if (!state.activeGroupId || !state.groups.some(g => g.name === state.activeGroupId)) {
      state.activeGroupId = displayedGroups()[0]?.name || null;
    } else if (!groupIsDisplayed(state.groups.find(g => g.name === state.activeGroupId))) {
      state.activeGroupId = displayedGroups()[0]?.name || null;
    }
    if (state.activeGroupId === previous) return false;
    state.openZoneIds = [];
    state.tabSelectionAnchorId = null;
    try {
      if (state.activeGroupId) localStorage.setItem("pb.activeGroup", state.activeGroupId);
      else localStorage.removeItem("pb.activeGroup");
    } catch (_) {}
    return true;
  }

  function renderAll() {
    grid.replaceChildren();
    const visibleZones = getVisibleZones();
    const visibleIds = new Set(visibleZones.map(zone => zone.id));
    state.openZoneIds = state.openZoneIds.filter(zoneId => visibleIds.has(zoneId));
    const group = state.groups.find(item => item.name === state.activeGroupId);
    const tabLayout = groupLayout(group) === "tab";
    const showTabSidebar = tabLayout && tabSidebarVisible(group);
    if (state.tabSelectionAnchorId && !visibleIds.has(state.tabSelectionAnchorId)) {
      state.tabSelectionAnchorId = null;
    }
    grid.classList.toggle("tab-layout", tabLayout);
    grid.classList.toggle("tab-sidebar-hidden", tabLayout && !showTabSidebar);
    if (!group || !tabLayout) {
      state.tabSelectionAnchorId = null;
      for (const zone of visibleZones) grid.appendChild(renderZone(zone));
      return;
    }

    const list = document.createElement("aside");
    list.className = "tab-zone-list";
    list.setAttribute("aria-label", "Zones");
    for (const zone of visibleZones) {
      const link = document.createElement("button");
      link.type = "button";
      link.className = "tab-zone-link";
      link.dataset.zone = zone.id;
      link.textContent = zone.label;
      link.setAttribute("aria-current", String(zone.id === state.activeId));
      link.setAttribute("aria-expanded", String(state.openZoneIds.includes(zone.id)));
      link.setAttribute("aria-pressed", String(state.openZoneIds.includes(zone.id)));
      link.setAttribute("aria-controls", "tab-zone-main");
      link.addEventListener("mouseenter", () => setActive(zone.id));
      link.addEventListener("focus", () => setActive(zone.id));
      link.addEventListener("click", event => toggleOpenZone(zone.id, event));
      if (zone.id === state.activeId) link.classList.add("active");
      if (state.openZoneIds.includes(zone.id)) link.classList.add("open");
      list.appendChild(link);
    }

    const main = document.createElement("section");
    main.className = "tab-zone-main";
    main.id = "tab-zone-main";
    main.setAttribute("aria-label", "Open zones");
    const openZones = visibleZones.filter(zone => state.openZoneIds.includes(zone.id));
    for (const zone of openZones) main.appendChild(renderZone(zone));
    if (showTabSidebar) grid.append(list, main);
    else grid.append(main);
  }

  function toggleOpenZone(zoneId, event = {}) {
    const visibleZones = getVisibleZones();
    const visibleIds = visibleZones.map(zone => zone.id);
    const clickedIndex = visibleIds.indexOf(zoneId);
    if (clickedIndex < 0) return;
    const restoreFocus = document.activeElement?.classList.contains("tab-zone-link");
    const shiftKey = Boolean(event.shiftKey);
    const toggle = Boolean(event.ctrlKey || event.metaKey);
    const selected = new Set(state.openZoneIds.filter(id => visibleIds.includes(id)));
    let next = new Set(selected);
    if (shiftKey) {
      let anchor = state.tabSelectionAnchorId;
      if (!anchor || !visibleIds.includes(anchor)) {
        anchor = zoneId;
        state.tabSelectionAnchorId = zoneId;
      }
      const anchorIndex = visibleIds.indexOf(anchor);
      const start = Math.min(anchorIndex, clickedIndex);
      const end = Math.max(anchorIndex, clickedIndex);
      const range = visibleIds.slice(start, end + 1);
      if (!toggle) {
        next = new Set(range);
      } else {
        const remove = range.every(id => next.has(id));
        for (const id of range) {
          if (remove) next.delete(id);
          else next.add(id);
        }
      }
    } else if (toggle) {
      if (next.has(zoneId)) next.delete(zoneId);
      else next.add(zoneId);
      state.tabSelectionAnchorId = zoneId;
    } else {
      next = new Set([zoneId]);
      state.tabSelectionAnchorId = zoneId;
    }
    state.openZoneIds = visibleIds.filter(id => next.has(id));
    setActive(zoneId);
    renderAll();
    if (restoreFocus) {
      const link = [...grid.querySelectorAll(".tab-zone-link")]
        .find(item => item.dataset.zone === zoneId);
      if (link) link.focus();
    }
  }

  function isDialogOpen(dialog) {
    return Boolean(dialog && dialog.hasAttribute("open"));
  }

  function applicationDialogOpen() {
    return isDialogOpen(pv) || isDialogOpen(replacementDialog);
  }

  function dialogFocusable(dialog) {
    return [...dialog.querySelectorAll(
      "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
    )].filter(control => !control.disabled && !control.hidden);
  }

  function rememberDialogInvoker(dialog, invoker) {
    const candidate = invoker && typeof invoker.focus === "function"
      ? invoker
      : document.activeElement;
    if (candidate && candidate !== dialog) dialogInvokers.set(dialog, candidate);
  }

  function restoreDialogInvoker(dialog) {
    const invoker = dialogInvokers.get(dialog);
    dialogInvokers.delete(dialog);
    if (invoker && invoker.isConnected && !invoker.disabled) invoker.focus();
  }

  function focusDialog(dialog) {
    const target = dialog.querySelector("[autofocus]") || dialogFocusable(dialog)[0];
    if (target) target.focus();
  }

  function trapFallbackDialog(event, dialog) {
    const controls = dialogFocusable(dialog);
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    const current = document.activeElement;
    const index = controls.indexOf(current);
    if (event.shiftKey) {
      if (index <= 0) {
        event.preventDefault();
        controls[controls.length - 1].focus();
      }
    } else if (index === -1 || index === controls.length - 1) {
      event.preventDefault();
      controls[0].focus();
    }
  }

  function setTabZoneSelection(selectAll) {
    const group = state.groups.find(item => item.name === state.activeGroupId);
    if (groupLayout(group) !== "tab") return false;
    const activeZoneId = state.activeId;
    const focusedZoneId = document.activeElement?.classList.contains("tab-zone-link")
      ? document.activeElement.dataset.zone
      : null;
    const visibleZones = getVisibleZones();
    state.openZoneIds = selectAll ? visibleZones.map(zone => zone.id) : [];
    if (selectAll) {
      const activeVisible = visibleZones.some(zone => zone.id === state.activeId);
      state.tabSelectionAnchorId = activeVisible ? state.activeId : visibleZones[0]?.id || null;
    } else {
      state.tabSelectionAnchorId = null;
    }
    renderAll();
    if (focusedZoneId) {
      grid.querySelector(`.tab-zone-link[data-zone="${CSS.escape(focusedZoneId)}"]`)?.focus();
    }
    if (state.activeId !== activeZoneId) setActive(activeZoneId);
    return true;
  }

  function rerenderZone(zoneId) {
    const zone = state.zones.find(z => z.id === zoneId);
    const old = grid.querySelector(`.zone[data-zone="${CSS.escape(zoneId)}"]`);
    if (zone && old) old.replaceWith(renderZone(zone));
  }

  function refreshUploadedZone(zoneId) {
    const group = state.groups.find(item => item.name === state.activeGroupId);
    if (groupLayout(group) !== "tab" || !getVisibleZones().some(zone => zone.id === zoneId)) {
      rerenderZone(zoneId);
      return;
    }
    if (state.openZoneIds.includes(zoneId)) {
      rerenderZone(zoneId);
      return;
    }
    const focused = document.activeElement?.classList.contains("tab-zone-link")
      && document.activeElement.dataset.zone === zoneId;
    state.openZoneIds.push(zoneId);
    renderAll();
    if (focused) {
      grid.querySelector(`.tab-zone-link[data-zone="${CSS.escape(zoneId)}"]`)?.focus();
    }
  }

  function setActive(zoneId, { announce = false } = {}) {
    if (zoneId && !getVisibleZones().some(zone => zone.id === zoneId)) return;
    state.activeId = zoneId;
    try {
      if (zoneId) localStorage.setItem("pb.activeZone", zoneId);
      else localStorage.removeItem("pb.activeZone");
    } catch (_) {}
    for (const el of grid.querySelectorAll(".zone")) {
      const active = el.dataset.zone === zoneId;
      el.classList.toggle("active", active);
      const select = el.querySelector(".zone-select");
      if (select) select.setAttribute("aria-current", String(active));
      const hint = el.querySelector(".drop-hint");
      if (hint) {
        hint.textContent = active
          ? "Active paste target: Ctrl/Command+V to paste or drag a file here"
          : "Select this zone to make it the paste target, or drag a file here";
      }
    }
    for (const link of grid.querySelectorAll(".tab-zone-link")) {
      const active = link.dataset.zone === zoneId;
      link.classList.toggle("active", active);
      link.setAttribute("aria-current", String(active));
    }
    if (announce) {
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) toast(`Active zone: ${zone.label}`);
    }
  }

  function renderGroups() {
    if (!groupTabs) return;
    const focusedGroup = document.activeElement?.closest(".group-tab")?.dataset.group;
    const focusedOptions = document.activeElement?.classList.contains("group-options-btn");
    if (groupOptionsClose) groupOptionsClose();
    reconcileActiveGroup();
    if (!state.groups.length) {
      groupTabs.hidden = true;
      groupTabs.replaceChildren();
      return;
    }
    groupTabs.hidden = false;
    groupTabs.replaceChildren();
    const groups = displayedGroups();
    for (const group of groups) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "group-tab";
      tab.dataset.group = group.name;
      const active = group.name === state.activeGroupId;
      if (active) tab.setAttribute("aria-current", "page");
      const count = groupZoneCount(group);
      const isEmpty = count === 0;
      tab.appendChild(document.createTextNode(group.name));
      if (state.showZoneCounts && group.show_count) {
        const countEl = document.createElement("span");
        countEl.className = "count";
        countEl.textContent = ` (${count})`;
        tab.appendChild(countEl);
      }
      if (isEmpty) tab.classList.add("empty");
      if (active) tab.classList.add("active");
      tab.addEventListener("click", () => {
        setActiveGroup(group.name);
      });
      groupTabs.appendChild(tab);
    }

    // Add group options dropdown
    const optionsBtn = document.createElement("button");
    optionsBtn.type = "button";
    optionsBtn.className = "group-tab group-options-btn";
    optionsBtn.textContent = "⋮";
    optionsBtn.setAttribute("aria-label", "Group options");
    optionsBtn.setAttribute("aria-expanded", "false");
    optionsBtn.setAttribute("aria-controls", "group-options-dropdown");
    optionsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      showGroupOptions(e.currentTarget);
    });
    groupTabs.appendChild(optionsBtn);
    if (focusedGroup) {
      groupTabs.querySelector(`.group-tab[data-group="${CSS.escape(focusedGroup)}"]`)?.focus();
    } else if (focusedOptions) {
      optionsBtn.focus();
    }
  }

  function showGroupOptions(anchor) {
    if (groupOptionsClose) groupOptionsClose();

    const dropdown = document.createElement("div");
    dropdown.className = "group-options-dropdown";
    dropdown.id = "group-options-dropdown";
    dropdown.setAttribute("aria-label", "Group display options");
    const addToggle = (id, labelText, checked) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = id;
      input.checked = checked;
      const text = document.createElement("span");
      text.textContent = labelText;
      label.append(input, text);
      dropdown.appendChild(label);
      return input;
    };
    const hideEmpty = addToggle("opt-hide-empty", "Hide empty groups", state.hideEmptyGroups);
    const showCount = addToggle("opt-show-count", "Show zone counts", state.showZoneCounts);
    const activeGroup = state.groups.find(group => group.name === state.activeGroupId);
    let layoutSelect = null;
    let sidebarToggle = null;
    if (activeGroup) {
      const layoutLabel = document.createElement("label");
      const layoutText = document.createElement("span");
      layoutText.textContent = "Layout";
      layoutSelect = document.createElement("select");
      layoutSelect.id = "opt-layout";
      for (const [value, labelText] of [["area", "Area"], ["tab", "Tab"]]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = labelText;
        layoutSelect.appendChild(option);
      }
      layoutSelect.value = groupLayout(activeGroup);
      layoutLabel.append(layoutText, layoutSelect);
      dropdown.appendChild(layoutLabel);
      if (groupLayout(activeGroup) === "tab") {
        sidebarToggle = addToggle(
          "opt-tab-sidebar",
          "Show zone column",
          tabSidebarVisible(activeGroup),
        );
      }
    }
    document.body.appendChild(dropdown);

    const rect = anchor.getBoundingClientRect();
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - dropdown.offsetWidth - 8));
    const top = Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - dropdown.offsetHeight - 8));
    dropdown.style.left = `${left}px`;
    dropdown.style.top = `${top}px`;

    const close = () => {
      dropdown.remove();
      document.removeEventListener("click", onClickOutside);
      dropdown.removeEventListener("keydown", onKeyDown);
      anchor.setAttribute("aria-expanded", "false");
      if (groupOptionsClose === close) groupOptionsClose = null;
    };
    const onClickOutside = (e) => {
      if (!dropdown.contains(e.target) && e.target !== anchor) {
        close();
      }
    };
    const onKeyDown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        anchor.focus();
      }
    };
    document.addEventListener("click", onClickOutside);
    dropdown.addEventListener("keydown", onKeyDown);
    groupOptionsClose = close;
    anchor.setAttribute("aria-expanded", "true");
    hideEmpty.focus();

    hideEmpty.addEventListener("change", (e) => {
      state.hideEmptyGroups = e.target.checked;
      try { localStorage.setItem("pb.hideEmptyGroups", e.target.checked); } catch (_) {}
      close();
      const groupChanged = reconcileActiveGroup();
      if (groupChanged && state.activeId && !getVisibleZones().some(z => z.id === state.activeId)) {
        setActive(null);
      }
      renderGroups();
      renderAll();
      groupTabs.querySelector(".group-options-btn")?.focus();
    });
    showCount.addEventListener("change", (e) => {
      state.showZoneCounts = e.target.checked;
      try { localStorage.setItem("pb.showZoneCounts", e.target.checked); } catch (_) {}
      close();
      renderGroups();
      groupTabs.querySelector(".group-options-btn")?.focus();
    });
    if (layoutSelect && activeGroup) {
      layoutSelect.addEventListener("change", (e) => {
        if (!isGroupLayout(e.target.value)) return;
        state.groupLayouts[activeGroup.name] = e.target.value;
        saveGroupLayouts();
        close();
        renderGroups();
        renderAll();
        groupTabs.querySelector(".group-options-btn")?.focus();
      });
    }
    if (sidebarToggle && activeGroup) {
      sidebarToggle.addEventListener("change", (e) => {
        state.tabSidebarVisibility[activeGroup.name] = e.target.checked;
        saveTabSidebarVisibility();
        close();
        renderAll();
        groupTabs.querySelector(".group-options-btn")?.focus();
      });
    }
  }

  function setActiveGroup(groupName) {
    const group = state.groups.find(item => item.name === groupName);
    if (!group || !groupIsDisplayed(group)) return;
    const restoreGroupFocus = groupTabs && groupTabs.contains(document.activeElement);
    const groupChanged = state.activeGroupId !== groupName;
    state.activeGroupId = groupName;
    try {
      localStorage.setItem("pb.activeGroup", groupName);
    } catch (_) {}
    if (groupChanged) {
      state.openZoneIds = [];
      state.tabSelectionAnchorId = null;
      setActive(null);
    }
    renderGroups();
    renderAll();
    if (restoreGroupFocus) {
      const activeTab = [...groupTabs.querySelectorAll(".group-tab")]
        .find(tab => tab.dataset.group === groupName);
      if (activeTab) activeTab.focus();
    }
    toast(`Group: ${groupName}`);
  }

  async function refresh() {
    const generation = ++refreshGeneration;
    if (activeRefreshController) activeRefreshController.abort();
    const controller = new AbortController();
    activeRefreshController = controller;
    try {
      const overview = await api("/api/zones", { signal: controller.signal });
      const previousZones = new Map(state.zones.map(zone => [zone.id, zone]));
      const nextZones = [];
      for (const z of overview.zones) {
        const previous = previousZones.get(z.id);
        if (z.busy) {
          nextZones.push(Object.assign({}, z, {
            images: previous ? previous.images : [],
            busy: true,
          }));
          continue;
        }
        try {
          const data = await api(
            `/api/zones/${encodeURIComponent(z.id)}/images`,
            { signal: controller.signal },
          );
          nextZones.push(Object.assign({}, z, { images: data.images, busy: false }));
        } catch (err) {
          if (err.code !== "zone_busy") throw err;
          nextZones.push(Object.assign({}, z, {
            images: previous ? previous.images : [],
            busy: true,
          }));
        }
      }
      if (generation !== refreshGeneration) return;

      state.authEnabled = overview.auth_enabled !== false;
      logoutForm.hidden = !state.authEnabled;
      state.zones = nextZones;
      state.groups = overview.groups || [];
      for (const zoneId of Object.keys(state.selectedByZone)) {
        const zone = state.zones.find(z => z.id === zoneId);
        if (!zone || !zone.images.some(item => item.id === state.selectedByZone[zoneId])) {
          delete state.selectedByZone[zoneId];
        }
      }
      for (const zoneId of Object.keys(state.selectedItemsByZone)) {
        const zone = state.zones.find(z => z.id === zoneId);
        if (!zone) {
          delete state.selectedItemsByZone[zoneId];
          delete state.selectionAnchorByZone[zoneId];
          continue;
        }
        const validIds = new Set(zone.images.map(item => item.id));
        const selected = selectedItemIds(zoneId);
        for (const itemId of selected) {
          if (!validIds.has(itemId)) selected.delete(itemId);
        }
        const anchor = state.selectionAnchorByZone[zoneId];
        if (anchor && !validIds.has(anchor)) state.selectionAnchorByZone[zoneId] = null;
      }

      // Initialize group state from localStorage
      if (state.groups.length > 0) {
        try {
          const stored = localStorage.getItem("pb.activeGroup");
          if (!state.activeGroupId && stored && state.groups.some(g => g.name === stored)) {
            state.activeGroupId = stored;
          }
        } catch (_) {}
      }
      // Load group UI preferences
      try {
        state.hideEmptyGroups = localStorage.getItem("pb.hideEmptyGroups") === "true";
        state.showZoneCounts = localStorage.getItem("pb.showZoneCounts") !== "false";
      } catch (_) {}
      loadGroupLayouts();
      loadTabSidebarVisibility();

      reconcileActiveGroup();
      renderGroups();
      renderAll();

      let stored = null;
      try { stored = localStorage.getItem("pb.activeZone"); } catch (_) {}
      const visibleZones = getVisibleZones();
      const candidate = state.activeId || stored
        || (!state.initialized && visibleZones.length === 1 ? visibleZones[0].id : null);
      if (candidate && visibleZones.some(z => z.id === candidate)) setActive(candidate);
      else setActive(null);

      state.initialized = true;
      setOnline(true);
      if (nextZones.some(zone => zone.busy)) {
        scheduleBusyRefresh();
      } else if (state.busyRefreshTimer) {
        clearTimeout(state.busyRefreshTimer);
        state.busyRefreshTimer = null;
      }
    } finally {
      if (activeRefreshController === controller) activeRefreshController = null;
    }
  }

  function setOnline(ok, message) {
    state.offline = !ok;
    statusEl.classList.toggle("offline", !ok);
    statusText.textContent = ok ? "online" : (message || "offline");
  }

  function scheduleRetry() {
    if (state.retryTimer) return;
    setOnline(false);
    state.retryTimer = setTimeout(async () => {
      state.retryTimer = null;
      await boot(true);
    }, 8000);
  }

  function scheduleBusyRefresh() {
    if (state.busyRefreshTimer) return;
    state.busyRefreshTimer = setTimeout(async () => {
      state.busyRefreshTimer = null;
      await boot(true);
    }, 1000);
  }

  async function boot(silent = false) {
    try {
      await refresh();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (!silent) renderAll();
      if (err.message === "network unreachable") scheduleRetry();
      else if (!silent) toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------- upload

  function recordUploadedItem(zoneId, item) {
    const zone = state.zones.find(z => z.id === zoneId);
    if (!zone) return;
    // A named drop can replace an existing stored name. Keep one history entry.
    zone.images = zone.images.filter(existing => existing.id !== item.id);
    zone.images.unshift(item);
    if (zone.images.length > zone.retain) zone.images.length = zone.retain;
  }

  async function upload(
    zoneId,
    file,
    {
      preserveName = false,
      allowReplace = false,
      autoCopy = true,
      notify = true,
    } = {},
  ) {
    if (!file) return;
    const zoneEl = grid.querySelector(`.zone[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      if (preserveName && !allowReplace && file.name && hasManagedName(zoneId, file.name)) {
        allowReplace = await askReplacement(file.name, zoneId);
        if (!allowReplace) return null;
      }
      const fd = new FormData();
      // Le serveur conserve le nom seulement pour les fichiers déposés.
      fd.append("image", file, file.name || "clipboard");
      if (preserveName) fd.append("preserve_name", "1");
      if (allowReplace) fd.append("replace", "1");
      const item = await api(`/api/zones/${encodeURIComponent(zoneId)}/images`,
        { method: "POST", body: fd });
      refreshGeneration += 1;
      if (activeRefreshController) activeRefreshController.abort();
      recordUploadedItem(zoneId, item);
      state.selectedByZone[zoneId] = item.id;
      state.selectedItemsByZone[zoneId] = new Set([item.id]);
      state.selectionAnchorByZone[zoneId] = item.id;
      refreshUploadedZone(zoneId);
      if (notify) {
        toast(`${item.kind === "image" ? "Image" : "Content"} uploaded (${shortRef(item.reference)})`);
      }
      if (autoCopy) {
        // Copie automatique best-effort : si elle échoue, on le signale
        // explicitement (le bouton Copy link reste disponible).
        writeClipboard(item.reference).then(ok => {
          if (ok) toast("Link copied");
          else toast("Link NOT copied — use the Copy link button", "error");
        });
      }
      return item;
    } catch (err) {
      if (err.status === 413) {
        if (notify) toast("Content is too large for this server", "error");
      }
      else if (err.status === 507) {
        if (notify) toast("Not enough disk space for this upload", "error");
      }
      else if (
        err.code === "replacement_required"
        && preserveName
        && file.name
        && !allowReplace
      ) {
        const confirmed = await askReplacement(file.name, zoneId);
        if (confirmed) {
          return upload(zoneId, file, {
            preserveName: true,
            allowReplace: true,
            autoCopy,
            notify,
          });
        }
      }
      else {
        if (err.code === "retention_error") {
          try { await refresh(); } catch (_) { /* keep the original error visible */ }
        }
        if (notify) toast(err.message, "error");
      }
    } finally {
      if (zoneEl) zoneEl.classList.remove("busy");
    }
  }

  async function uploadBatch(zoneId, files) {
    const batch = [...files].filter(Boolean);
    if (!batch.length) return;
    if (batch.length === 1) {
      await upload(zoneId, batch[0], { preserveName: true });
      return;
    }
    state.batchBusyZoneIds.add(zoneId);
    renderAll();
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    const successful = [];
    let failed = 0;
    try {
      // Keep each request independent: one bad file must not cancel the rest,
      // and the normal single-upload size/memory limits still apply.
      for (const file of batch) {
        const item = await upload(zoneId, file, {
          preserveName: true,
          autoCopy: false,
          notify: false,
        });
        if (item) successful.push(item);
        else failed += 1;
      }
      const zone = state.zones.find(item => item.id === zoneId);
      if (zone && successful.length) {
        const selected = new Set(successful.map(item => item.id));
        state.selectedItemsByZone[zoneId] = selected;
        state.selectedByZone[zoneId] = successful[successful.length - 1].id;
        state.selectionAnchorByZone[zoneId] = successful[successful.length - 1].id;
      }
      await refresh();
      if (successful.length && failed) {
        toast(`${successful.length} files uploaded, ${failed} failed`, "error");
      } else if (successful.length) {
        toast(`${successful.length} files uploaded`);
      } else {
        toast("No files were uploaded", "error");
      }
    } catch (err) {
      if (err.status === 413) toast("Content is too large for this server", "error");
      else if (err.status === 503) toast("The upload service is busy", "error");
      else toast(err.message, "error");
    } finally {
      state.batchBusyZoneIds.delete(zoneId);
      renderAll();
    }
  }

  function hasManagedName(zoneId, filename) {
    const zone = state.zones.find(z => z.id === zoneId);
    return Boolean(zone && zone.images.some(item => item.filename === filename));
  }

  function showNextReplacementPrompt() {
    if (activeReplacementPrompt || !replacementQueue.length) return;
    activeReplacementPrompt = replacementQueue.shift();
    replacementFilename.textContent = activeReplacementPrompt.filename;
    const zone = state.zones.find(item => item.id === activeReplacementPrompt.zoneId);
    replacementZone.textContent = zone ? zone.label : activeReplacementPrompt.zoneId;
    replacementDialog.returnValue = "";
    openDialog(replacementDialog, activeReplacementPrompt.invoker);
  }

  function askReplacement(filename, zoneId) {
    return new Promise((resolve) => {
      replacementQueue.push({ filename, zoneId, resolve, invoker: document.activeElement });
      showNextReplacementPrompt();
    });
  }

  function settleReplacementPrompt(allowReplace) {
    if (!activeReplacementPrompt) return;
    const prompt = activeReplacementPrompt;
    activeReplacementPrompt = null;
    prompt.resolve(allowReplace);
    showNextReplacementPrompt();
    if (!activeReplacementPrompt) restoreDialogInvoker(replacementDialog);
  }

  function closeReplacementPrompt(allowReplace) {
    if (!activeReplacementPrompt) return;
    if (closeDialog(replacementDialog, allowReplace ? "replace" : "cancel")) return;
    settleReplacementPrompt(allowReplace);
  }

  function openDialog(dialog, invoker = document.activeElement) {
    rememberDialogInvoker(dialog, invoker);
    const backdrop = dialog === pv ? previewBackdrop : replacementBackdrop;
    if (typeof dialog.showModal === "function") {
      dialog.classList.remove("dialog-fallback");
      if (backdrop) backdrop.hidden = true;
      dialog.showModal();
    } else {
      dialog.classList.add("dialog-fallback");
      if (backdrop) backdrop.hidden = false;
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("open", "");
    }
    focusDialog(dialog);
  }

  function closeDialog(dialog, returnValue = "") {
    const backdrop = dialog === pv ? previewBackdrop : replacementBackdrop;
    if (dialog.classList.contains("dialog-fallback")) {
      dialog.classList.remove("dialog-fallback");
      dialog.removeAttribute("open");
      if (backdrop) backdrop.hidden = true;
      return false;
    }
    if (typeof dialog.close === "function" && dialog.open) {
      dialog.close(returnValue);
      return true;
    }
    dialog.removeAttribute("open");
    if (backdrop) backdrop.hidden = true;
    return false;
  }

  function requireActiveZone() {
    if (state.activeId && getVisibleZones().some(z => z.id === state.activeId)) {
      return state.activeId;
    }
    if (state.activeId) setActive(null);
    toast("Choose a zone first (select its zone button or click its card)", "error");
    for (const el of grid.querySelectorAll(".zone")) {
      el.classList.remove("attention");
      void el.offsetWidth; // restart the animation
      el.classList.add("attention");
    }
    return null;
  }

  async function deleteImage(zoneId, filename) {
    if (!window.confirm(`Delete ${filename} from the disk?`)) return;
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      await api(`/api/zones/${encodeURIComponent(zoneId)}/images/${encodeURIComponent(filename)}`,
        { method: "DELETE" });
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) {
        zone.images = zone.images.filter(item => item.id !== filename);
        if (state.selectedByZone[zoneId] === filename) delete state.selectedByZone[zoneId];
        const selected = selectedItemIds(zoneId);
        selected.delete(filename);
        if (state.selectionAnchorByZone[zoneId] === filename) {
          state.selectionAnchorByZone[zoneId] = null;
        }
        rerenderZone(zoneId);
      }
      toast(`Deleted ${filename}`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------- events

  if (filePicker) {
    filePicker.addEventListener("change", () => {
      const zoneId = filePicker.dataset.zone;
      const files = filePicker.files ? [...filePicker.files] : [];
      delete filePicker.dataset.zone;
      if (zoneId && files.length) uploadBatch(zoneId, files);
    });
  }

  grid.addEventListener("focusin", (event) => {
    const tabLink = event.target.closest(".tab-zone-link");
    if (tabLink) {
      setActive(tabLink.dataset.zone);
      return;
    }
    const zoneSelect = event.target.closest(".zone-select");
    if (zoneSelect) {
      const zoneCard = zoneSelect.closest(".zone");
      if (zoneCard) setActive(zoneCard.dataset.zone);
    }
  });

  window.addEventListener("paste", (event) => {
    if (applicationDialogOpen()) {
      event.preventDefault();
      return;
    }
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    let imageItem = null;
    let hasPlainText = false;
    for (const item of items) {
      if (item.kind === "file" && /^image\//.test(item.type)) {
        if (!imageItem) imageItem = item;
      } else if (item.kind === "string" && item.type === "text/plain") {
        hasPlainText = true;
      }
    }
    if (imageItem && hasPlainText) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      uploadMixedClipboard(zoneId, items);
      return;
    }
    let file = null;
    if (imageItem) {
      file = imageItem.getAsFile();
    }
    if (file) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      upload(zoneId, file);
      return;
    }
    // Texte : préférer le texte brut, mais ne pas laisser un flavor vide
    // masquer un autre flavor réellement exploitable.
    const textItems = [...items].filter(i => i.kind === "string");
    const orderedTextItems = [
      ...textItems.filter(i => i.kind === "string" && i.type === "text/plain"),
      ...textItems.filter(i => i.type !== "text/plain"),
    ];
    if (orderedTextItems.length) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      const uploadText = (index) => {
        const textItem = orderedTextItems[index];
        if (!textItem) {
          toast("The clipboard does not contain an image or text");
          return;
        }
        textItem.getAsString((text) => {
          if (typeof text !== "string" || text.length === 0) {
            uploadText(index + 1);
            return;
          }
          const blob = new Blob([text], { type: textItem.type || "text/plain" });
          upload(zoneId, blob);
        });
      };
      uploadText(0);
      return;
    }
    toast("The clipboard does not contain an image or text");
  });

  grid.addEventListener("click", (event) => {
    const copyBtn = event.target.closest(".copy-btn");
    if (copyBtn && copyBtn.dataset.ref) {
      copyLink(copyBtn.dataset.ref);
      return;
    }
    const downloadBtn = event.target.closest(".download-btn");
    if (downloadBtn && downloadBtn.dataset.preview) {
      downloadContent(downloadBtn.dataset.preview, downloadBtn.dataset.filename);
      return;
    }
    const copyImageBtn = event.target.closest(".copy-image-btn");
    if (copyImageBtn && copyImageBtn.dataset.preview) {
      copyContent(copyImageBtn.dataset.kind, copyImageBtn.dataset.preview, copyImageBtn.dataset.mime);
      return;
    }
    const clearBtn = event.target.closest(".clear-btn");
    if (clearBtn) {
      clearClipboard();
      return;
    }
    const zoomBtn = event.target.closest(".zoom-btn");
    if (zoomBtn && zoomBtn.dataset.preview) {
      const item = itemForControl(zoomBtn);
      const zoneId = zoneForControl(zoomBtn);
      if (item && item.kind === "image") {
        openPreview(item.preview_url, item.reference, item.filename, zoneId);
      } else if (item) openContentPreview(item, zoneId);
      return;
    }
    const deleteBtn = event.target.closest(".delete-btn");
    if (deleteBtn && deleteBtn.dataset.filename) {
      deleteImage(deleteBtn.dataset.zone, deleteBtn.dataset.filename);
      return;
    }
    const thumbWrap = event.target.closest(".thumb-wrap");
    if (thumbWrap) {
      const zoneEl = thumbWrap.closest(".zone");
      const zone = state.zones.find(item => item.id === zoneEl?.dataset.zone);
      if (zone) selectHistoryItem(zone, thumbWrap.dataset.itemId, event);
      return;
    }
    const bigThumb = event.target.closest(".thumb-big");
    if (bigThumb) {
      const item = itemForControl(bigThumb);
      const zoneId = zoneForControl(bigThumb);
      if (item && item.kind === "image") {
        openPreview(item.preview_url, item.reference, item.filename, zoneId);
      } else if (item) openContentPreview(item, zoneId);
      return;
    }
    const fileBox = event.target.closest(".file-box");
    if (fileBox) {
      const item = itemForControl(fileBox);
      if (item) openContentPreview(item, zoneForControl(fileBox));
      return;
    }
    const zoneCard = event.target.closest(".zone");
    if (zoneCard) setActive(zoneCard.dataset.zone, { announce: true });
  });

  grid.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const bigThumb = event.target.closest(".thumb-big");
    if (bigThumb) {
      event.preventDefault();
      const item = itemForControl(bigThumb);
      const zoneId = zoneForControl(bigThumb);
      if (item && item.kind === "image") {
        openPreview(item.preview_url, item.reference, item.filename, zoneId);
      } else if (item) openContentPreview(item, zoneId);
      return;
    }
    const fileBox = event.target.closest(".file-box");
    if (fileBox) {
      event.preventDefault();
      const item = itemForControl(fileBox);
      if (item) openContentPreview(item, zoneForControl(fileBox));
      return;
    }
    const zoneSelect = event.target.closest(".zone-select");
    if (zoneSelect) return;
    const zoneCard = event.target.closest(".zone");
    if (zoneCard && event.target === zoneCard) {
      event.preventDefault();
      setActive(zoneCard.dataset.zone, { announce: true });
    }
  });

  grid.addEventListener("dragover", (event) => {
    const zoneTarget = event.target.closest(".zone, .tab-zone-link");
    if (!zoneTarget) return;
    event.preventDefault();
    zoneTarget.classList.add("dragging");
    setActive(zoneTarget.dataset.zone);
  });
  grid.addEventListener("dragleave", (event) => {
    const zoneTarget = event.target.closest(".zone, .tab-zone-link");
    if (zoneTarget) zoneTarget.classList.remove("dragging");
  });
  grid.addEventListener("drop", (event) => {
    const zoneTarget = event.target.closest(".zone, .tab-zone-link");
    if (!zoneTarget) return;
    event.preventDefault();
    zoneTarget.classList.remove("dragging");
    setActive(zoneTarget.dataset.zone);
    const zone = state.zones.find(item => item.id === zoneTarget.dataset.zone);
    if (zone?.busy || state.batchBusyZoneIds.has(zoneTarget.dataset.zone)) {
      toast("This zone is busy; try again shortly", "error");
      return;
    }
    const files = event.dataTransfer && event.dataTransfer.files
      ? [...event.dataTransfer.files]
      : [];
    if (!files.length) return;
    uploadBatch(zoneTarget.dataset.zone, files);
  });

  document.addEventListener("keydown", (event) => {
    const fallbackDialog = [pv, replacementDialog]
      .find(dialog => dialog.classList.contains("dialog-fallback"));
    if (fallbackDialog) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (fallbackDialog === pv) closePreview();
        else closeReplacementPrompt(false);
      } else if (event.key === "Tab") {
        trapFallbackDialog(event, fallbackDialog);
      }
      return;
    }
    if (applicationDialogOpen()) {
      const key = event.key.toLowerCase();
      if (key === "a" || key === "u" || key === "c" || /^[1-9]$/.test(event.key)) {
        event.preventDefault();
      }
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const key = event.key.toLowerCase();
    if (key === "a" || key === "u") {
      const target = event.target;
      const button = target?.closest?.("button");
      const interactive = target?.closest?.(
        "button, a, input, textarea, select, [contenteditable='true'], [role='button']",
      );
      const allowedButton = button && (
        button.classList.contains("tab-zone-link")
        || (button.classList.contains("group-tab")
          && !button.classList.contains("group-options-btn"))
      );
      if ((interactive && !allowedButton) || target?.closest?.("dialog, [contenteditable='true']")) {
        return;
      }
      if (setTabZoneSelection(key === "a")) {
        event.preventDefault();
        return;
      }
    }
    if (/^[1-9]$/.test(event.key)) {
      const zone = getVisibleZones()[Number(event.key) - 1];
      if (zone) setActive(zone.id, { announce: true });
    } else if (event.key === "c" || event.key === "C") {
      const zone = getVisibleZones().find(z => z.id === state.activeId);
      if (zone && zone.images.length) copyLink(selectedItem(zone).reference);
    }
  });

  function setPreviewCopyLabel(kind) {
    const label = `Copy ${kindLabel(kind)}`;
    pvCopyImage.textContent = label;
    pvCopyImage.setAttribute("aria-label", `${label} to the clipboard`);
  }

  function setRawHtmlButton(visible, previewUrl = "") {
    if (visible && !pvCopyRawHtml) {
      pvCopyRawHtml = document.createElement("button");
      pvCopyRawHtml.type = "button";
      pvCopyRawHtml.id = "pv-copy-raw";
      pvCopyRawHtml.className = "raw-html-btn";
      pvCopyRawHtml.textContent = "Copy raw HTML";
      pvCopyRawHtml.setAttribute("aria-label", "Copy raw HTML to the clipboard");
      pvCopyRawHtml.addEventListener("click", () => {
        if (pvCopyRawHtml.dataset.preview) {
          copyHtmlContent(pvCopyRawHtml.dataset.preview, { raw: true });
        }
      });
      pvCopyImage.parentElement.insertBefore(pvCopyRawHtml, pvDownload);
    }
    if (!pvCopyRawHtml) return;
    pvCopyRawHtml.hidden = !visible;
    if (visible) pvCopyRawHtml.dataset.preview = previewUrl;
    else delete pvCopyRawHtml.dataset.preview;
  }

  function invalidatePreviewLoad() {
    previewGeneration += 1;
    if (activePreviewController) {
      activePreviewController.abort();
      activePreviewController = null;
    }
    return previewGeneration;
  }

  function openPreview(url, reference, filename, zoneId) {
    invalidatePreviewLoad();
    setRawHtmlButton(false);
    const storedFilename = filename || decodeURIComponent(url.split("/").pop());
    setPreviewSource(pvImg, url);
    pvImg.hidden = false;
    pvText.hidden = true;
    pvRef.textContent = reference;
    setPreviewCopyLabel("image");
    pvDownload.textContent = downloadLabel(storedFilename);
    pvDownload.setAttribute("aria-label", downloadLabel(storedFilename));
    pvDownload.dataset.preview = url;
    pvDownload.dataset.filename = storedFilename;
    pvCopyImage.dataset.preview = url;
    pvCopyImage.dataset.kind = "image";
    pvCopyImage.dataset.mime = "image/png";
    pvDelete.dataset.zone = zoneId || "";
    pvDelete.dataset.filename = storedFilename;
    setPreviewItemLabels(storedFilename);
    openDialog(pv);
  }

  function setPreviewItemLabels(filename) {
    const label = `Preview of ${filename}`;
    pv.setAttribute("aria-label", label);
    pvImg.alt = label;
    pvText.setAttribute("aria-label", label);
    pvDelete.setAttribute("aria-label", `Delete ${filename} from the disk`);
  }

  function openTextPreview(text, filename, invoker) {
    pvText.textContent = text;
    pvText.hidden = false;
    pvImg.hidden = true;
    setPreviewItemLabels(filename);
    openDialog(pv, invoker);
  }

  async function openContentPreview(item, zoneId) {
    const invoker = document.activeElement;
    const generation = invalidatePreviewLoad();
    setRawHtmlButton(false);
    setPreviewSource(pvImg, "");
    setPreviewCopyLabel(item.kind);
    pvRef.textContent = item.reference;
    pvDownload.textContent = downloadLabel(item.filename);
    pvDownload.setAttribute("aria-label", downloadLabel(item.filename));
    pvDownload.dataset.preview = item.preview_url;
    pvDownload.dataset.filename = item.filename;
    pvDelete.dataset.zone = zoneId || "";
    pvDelete.dataset.filename = item.filename;
    pvCopyImage.dataset.preview = item.preview_url;
    pvCopyImage.dataset.kind = item.kind;
    pvCopyImage.dataset.mime = item.mime || "";
    setPreviewItemLabels(item.filename);
    if (item.kind === "text") {
      const controller = new AbortController();
      activePreviewController = controller;
      try {
        const response = await fetchPreview(item.preview_url, {
          credentials: "same-origin",
          headers: { Accept: "text/plain" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("preview unavailable");
        let text = await response.text();
        let htmlInspection = null;
        if (item.mime === "text/html") {
          // Document hybride : afficher le texte assaini, jamais le HTML rendu.
          htmlInspection = sanitizeHtml(text);
          text = htmlInspection.plain;
        }
        if (generation !== previewGeneration) return;
        setRawHtmlButton(Boolean(htmlInspection && htmlInspection.changed), item.preview_url);
        openTextPreview(text, item.filename, invoker);
      } catch (err) {
        if (err && err.name === "AbortError") return;
        if (generation !== previewGeneration) return;
        toast("Could not load the text preview", "error");
      } finally {
        if (activePreviewController === controller) activePreviewController = null;
      }
      return;
    }
    // Binaire : téléchargement direct (Content-Disposition: attachment).
    downloadContent(item.preview_url, item.filename);
  }
  function closePreview() {
    invalidatePreviewLoad();
    setRawHtmlButton(false);
    if (!closeDialog(pv)) {
      setPreviewSource(pvImg, "");
      pvText.hidden = true;
      pvImg.hidden = false;
      delete pvDelete.dataset.zone;
      restoreDialogInvoker(pv);
    }
  }
  pvCopy.addEventListener("click", () => copyLink(pvRef.textContent));
  pvCopyImage.addEventListener("click", () => copyContent(pvCopyImage.dataset.kind, pvCopyImage.dataset.preview, pvCopyImage.dataset.mime));
  pvDownload.addEventListener("click", () => downloadContent(pvDownload.dataset.preview, pvDownload.dataset.filename));
  pvClear.addEventListener("click", clearClipboard);
  pvDelete.addEventListener("click", () => {
    const zoneId = pvDelete.dataset.zone;
    const filename = pvDelete.dataset.filename;
    const zone = state.zones.find(
      z => z.id === zoneId && z.images.some(i => i.id === filename),
    );
    if (zone) {
      closePreview();
      deleteImage(zone.id, filename);
    }
  });
  document.getElementById("pv-close").addEventListener("click", closePreview);
  pv.addEventListener("click", (event) => { if (event.target === pv) closePreview(); });
  pv.addEventListener("close", () => {
    setRawHtmlButton(false);
    setPreviewSource(pvImg, "");
    pvText.hidden = true;
    pvImg.hidden = false;
    delete pvDelete.dataset.zone;
    restoreDialogInvoker(pv);
  });

  replacementCancel.addEventListener("click", () => closeReplacementPrompt(false));
  replacementConfirm.addEventListener("click", () => closeReplacementPrompt(true));
  replacementDialog.addEventListener("click", (event) => {
    if (event.target === replacementDialog) closeReplacementPrompt(false);
  });
  replacementDialog.addEventListener("close", () => {
    settleReplacementPrompt(replacementDialog.returnValue === "replace");
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") boot(true);
  });

  // Lightweight synchronization across tabs and machines.
  setInterval(() => { if (document.visibilityState === "visible") boot(true); }, 45000);

  boot();
})();
