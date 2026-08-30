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
  const state = {
    zones: [],            // [{id,label,color,retain,count,images:[...]}]
    activeId: null,
    authEnabled: true,
    offline: false,
    selectedByZone: Object.create(null),
    retryTimer: null,
    toastTimer: null,
    // Groups
    groups: [],              // [{name, selection, pattern, layout, zone_ids, ...}]
    activeGroupId: null,     // null = implicit All when no group is selected
    openZoneIds: [],
    groupLayouts: Object.create(null),
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
  const replacementDialog = document.getElementById("replace");
  const replacementBackdrop = document.getElementById("replace-backdrop");
  const replacementFilename = document.getElementById("replace-filename");
  const replacementCancel = document.getElementById("replace-cancel");
  const replacementConfirm = document.getElementById("replace-confirm");
  const replacementQueue = [];
  let activeReplacementPrompt = null;

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
      res = await fetch(path, requestOptions);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new Error("network unreachable");
    }
    if (res.status === 401 && state.authEnabled) {
      window.location.href = "/login";
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
        preview_busy: "Too many previews are currently being served",
        rate_limited: "Too many attempts; try again later",
        upload_busy: "Too many uploads are currently in memory",
      };
      const message = knownMessages[code]
        || (payload && payload.error ? payload.error.message : `error ${res.status}`);
      const err = new Error(message);
      err.code = code;
      err.status = res.status;
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

  async function copyHtmlContent(previewUrl) {
    const loadHtml = async () => {
      const response = await fetchPreview(previewUrl, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error("preview unavailable");
      return response.text();
    };
    try {
      const html = await loadHtml();
      const doc = new DOMParser().parseFromString(html, "text/html");
      const plain = ((doc.body && doc.body.textContent) || "").trim();
      const flavors = { "text/html": new Blob([html], { type: "text/html" }) };
      if (plain) flavors["text/plain"] = new Blob([plain], { type: "text/plain" });
      await navigator.clipboard.write([new ClipboardItem(flavors)]);
      toast("Text copied");
      return true;
    } catch (_) {
      // Repli : texte seul (navigateurs sans ClipboardItem multi-flavors).
      try {
        const html = await loadHtml();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const plain = ((doc.body && doc.body.textContent) || "").trim();
        const ok = await writeClipboard(plain);
        if (ok) toast("Text copied");
        else toast("Could not copy the text to the clipboard", "error");
        return ok;
      } catch (_2) {
        toast("Could not copy the text to the clipboard", "error");
        return false;
      }
    }
  }

  async function copyContent(kind, previewUrl, mime) {
    if (kind === "image") return copyImage(previewUrl);
    if (kind === "text") {
      if (
        mime === "text/html"
        && navigator.clipboard
        && typeof navigator.clipboard.write === "function"
        && typeof ClipboardItem !== "undefined"
        && window.isSecureContext !== false
      ) {
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

  function renderZone(zone) {
    const el = document.createElement("section");
    el.className = "zone";
    el.dataset.zone = zone.id;
    if (zone.id === state.activeId) el.classList.add("active");
    applyZoneColors(el, zone.color);

    const head = document.createElement("header");
    head.className = "zone-head";
    const select = document.createElement("button");
    select.type = "button";
    select.className = "zone-select";
    select.setAttribute("aria-pressed", String(zone.id === state.activeId));
    select.setAttribute("aria-label", `Select zone ${zone.label}`);
    select.innerHTML =
      '<span class="zone-marker" aria-hidden="true"></span>' +
      '<span class="zone-label"></span>' +
      '<span class="zone-count"></span>';
    select.querySelector(".zone-label").textContent = zone.label;
    select.querySelector(".zone-count").textContent = `${zone.images.length} / ${zone.retain}`;
    head.appendChild(select);
    el.appendChild(head);

    if (zone.images.length === 0) {
      const hint = document.createElement("div");
      hint.className = "drop-hint";
      hint.textContent = "Ctrl+V to paste or drag a file here";
      el.appendChild(hint);
    } else {
      const selected = selectedItem(zone);
      el.appendChild(renderLatest(zone.id, selected));
      el.appendChild(renderThumbs(zone.images, selected.id));
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

  function renderLatest(zoneId, item) {
    const card = document.createElement("div");
    card.className = "latest";
    card.dataset.itemId = item.id;
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
        item.kind === "image" ? "Enlarge the image" : "Preview the text",
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
    del.setAttribute("aria-label", "Delete this image from the disk");
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
      img.alt = "latest image";
      img.loading = "lazy";
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", "Open the latest image preview");
      card.append(img, right);
    } else {
      const box = document.createElement("div");
      box.className = "file-box";
      box.dataset.itemId = item.id;
      box.textContent = item.kind === "text" ? "TXT" : "FILE";
      box.tabIndex = 0;
      box.setAttribute("role", "button");
      box.setAttribute("aria-label", "Open the latest content preview");
      card.append(box, right);
    }
    return card;
  }

  function renderThumbs(items, selectedId) {
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
      wrap.setAttribute("aria-pressed", String(item.id === selectedId));
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
    grid.classList.toggle("tab-layout", groupLayout(group) === "tab");
    if (!group || groupLayout(group) !== "tab") {
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
      link.setAttribute("aria-pressed", String(zone.id === state.activeId));
      link.setAttribute("aria-expanded", String(state.openZoneIds.includes(zone.id)));
      link.setAttribute("aria-controls", "tab-zone-main");
      link.addEventListener("mouseenter", () => setActive(zone.id));
      link.addEventListener("focus", () => setActive(zone.id));
      link.addEventListener("click", event => toggleOpenZone(zone.id, event.shiftKey));
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
    grid.append(list, main);
  }

  function toggleOpenZone(zoneId, shiftKey) {
    if (!getVisibleZones().some(zone => zone.id === zoneId)) return;
    const restoreFocus = document.activeElement?.classList.contains("tab-zone-link");
    const index = state.openZoneIds.indexOf(zoneId);
    if (shiftKey) {
      if (index === -1) state.openZoneIds.push(zoneId);
      else state.openZoneIds.splice(index, 1);
    } else if (index !== -1 && state.openZoneIds.length === 1) {
      state.openZoneIds = [];
    } else {
      state.openZoneIds = [zoneId];
    }
    setActive(zoneId);
    renderAll();
    if (restoreFocus) {
      const link = [...grid.querySelectorAll(".tab-zone-link")]
        .find(item => item.dataset.zone === zoneId);
      if (link) link.focus();
    }
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
      if (select) select.setAttribute("aria-pressed", String(active));
    }
    for (const link of grid.querySelectorAll(".tab-zone-link")) {
      const active = link.dataset.zone === zoneId;
      link.classList.toggle("active", active);
      link.setAttribute("aria-pressed", String(active));
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
      const nextZones = [];
      for (const z of overview.zones) {
        const data = await api(
          `/api/zones/${encodeURIComponent(z.id)}/images`,
          { signal: controller.signal },
        );
        nextZones.push(Object.assign({}, z, { images: data.images }));
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

  async function upload(
    zoneId,
    file,
    { preserveName = false, allowReplace = false } = {},
  ) {
    if (!file) return;
    const zoneEl = grid.querySelector(`.zone[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      if (preserveName && !allowReplace && file.name && hasManagedName(zoneId, file.name)) {
        allowReplace = await askReplacement(file.name);
        if (!allowReplace) return;
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
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) {
        // A drag-and-drop can replace an existing stored name. Do not show a
        // second history entry for the same server-side item.
        zone.images = zone.images.filter(existing => existing.id !== item.id);
        zone.images.unshift(item);
        if (zone.images.length > zone.retain) zone.images.length = zone.retain;
        state.selectedByZone[zoneId] = item.id;
         refreshUploadedZone(zoneId);
      }
      toast(`${item.kind === "image" ? "Image" : "Content"} uploaded (${shortRef(item.reference)})`);
      // Copie automatique best-effort : si elle échoue, on le signale
      // explicitement (le bouton Copy link reste disponible).
      writeClipboard(item.reference).then(ok => {
        if (ok) toast("Link copied");
        else toast("Link NOT copied — use the Copy link button", "error");
      });
    } catch (err) {
      if (err.status === 413) toast("Content is too large for this server", "error");
      else if (err.status === 507) toast("Not enough disk space for this upload", "error");
      else if (
        err.code === "replacement_required"
        && preserveName
        && file.name
        && !allowReplace
      ) {
        const confirmed = await askReplacement(file.name);
        if (confirmed) {
          await upload(zoneId, file, { preserveName: true, allowReplace: true });
        }
      }
      else {
        if (err.code === "retention_error") {
          try { await refresh(); } catch (_) { /* keep the original error visible */ }
        }
        toast(err.message, "error");
      }
    } finally {
      if (zoneEl) zoneEl.classList.remove("busy");
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
    replacementDialog.returnValue = "";
    openDialog(replacementDialog);
    replacementConfirm.focus();
  }

  function askReplacement(filename) {
    return new Promise((resolve) => {
      replacementQueue.push({ filename, resolve });
      showNextReplacementPrompt();
    });
  }

  function settleReplacementPrompt(allowReplace) {
    if (!activeReplacementPrompt) return;
    const prompt = activeReplacementPrompt;
    activeReplacementPrompt = null;
    prompt.resolve(allowReplace);
    showNextReplacementPrompt();
  }

  function closeReplacementPrompt(allowReplace) {
    if (!activeReplacementPrompt) return;
    if (closeDialog(replacementDialog, allowReplace ? "replace" : "cancel")) return;
    settleReplacementPrompt(allowReplace);
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") {
      dialog.classList.remove("dialog-fallback");
      if (dialog === replacementDialog) replacementBackdrop.hidden = true;
      dialog.showModal();
    } else {
      dialog.classList.add("dialog-fallback");
      if (dialog === replacementDialog) replacementBackdrop.hidden = false;
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog, returnValue = "") {
    if (dialog.classList.contains("dialog-fallback")) {
      dialog.classList.remove("dialog-fallback");
      dialog.removeAttribute("open");
      if (dialog === replacementDialog) replacementBackdrop.hidden = true;
      return false;
    }
    if (typeof dialog.close === "function" && dialog.open) {
      dialog.close(returnValue);
      return true;
    }
    dialog.removeAttribute("open");
    return false;
  }

  function requireActiveZone() {
    if (state.activeId && getVisibleZones().some(z => z.id === state.activeId)) {
      return state.activeId;
    }
    if (state.activeId) setActive(null);
    toast("Choose a zone first (click its card)", "error");
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
        rerenderZone(zoneId);
      }
      toast(`Deleted ${filename}`);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------- events

  grid.addEventListener("focusin", (event) => {
    const tabLink = event.target.closest(".tab-zone-link");
    if (tabLink) {
      setActive(tabLink.dataset.zone);
      return;
    }
    const zoneCard = event.target.closest(".zone");
    if (zoneCard) setActive(zoneCard.dataset.zone);
  });

  window.addEventListener("paste", (event) => {
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
      state.selectedByZone[zoneEl.dataset.zone] = thumbWrap.dataset.itemId;
      rerenderZone(zoneEl.dataset.zone);
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
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (!file) return;
    upload(zoneTarget.dataset.zone, file, { preserveName: true });
  });

  document.addEventListener("keydown", (event) => {
    if (replacementDialog.classList.contains("dialog-fallback")) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeReplacementPrompt(false);
      } else if (event.key === "Tab") {
        const controls = [replacementCancel, replacementConfirm];
        const current = document.activeElement;
        if (!replacementDialog.contains(current)) {
          event.preventDefault();
          controls[event.shiftKey ? 1 : 0].focus();
        } else if (event.shiftKey && current === controls[0]) {
          event.preventDefault();
          controls[1].focus();
        } else if (!event.shiftKey && current === controls[1]) {
          event.preventDefault();
          controls[0].focus();
        }
      }
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
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
    openDialog(pv);
  }

  function openTextPreview(text) {
    pvText.textContent = text;
    pvText.hidden = false;
    pvImg.hidden = true;
    openDialog(pv);
  }

  async function openContentPreview(item, zoneId) {
    const generation = invalidatePreviewLoad();
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
        if (item.mime === "text/html") {
          // Document hybride : afficher le texte extrait, jamais le HTML rendu.
          const doc = new DOMParser().parseFromString(text, "text/html");
          text = ((doc.body && doc.body.textContent) || "").trim();
        }
        if (generation !== previewGeneration) return;
        openTextPreview(text);
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
    if (!closeDialog(pv)) {
      setPreviewSource(pvImg, "");
      pvText.hidden = true;
      pvImg.hidden = false;
      delete pvDelete.dataset.zone;
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
    setPreviewSource(pvImg, "");
    pvText.hidden = true;
    pvImg.hidden = false;
    delete pvDelete.dataset.zone;
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
