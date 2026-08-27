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
  };

  let refreshGeneration = 0;
  let activeRefreshController = null;

  const grid = document.getElementById("grid");
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
    const target = pv.open ? pvToastEl : toastEl;
    const other = pv.open ? toastEl : pvToastEl;
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

  async function copyContent(kind, previewUrl) {
    if (kind === "image") return copyImage(previewUrl);
    if (kind === "text") {
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

  function renderAll() {
    grid.replaceChildren();
    for (const zone of state.zones) grid.appendChild(renderZone(zone));
  }

  function rerenderZone(zoneId) {
    const zone = state.zones.find(z => z.id === zoneId);
    const old = grid.querySelector(`[data-zone="${CSS.escape(zoneId)}"]`);
    if (zone && old) old.replaceWith(renderZone(zone));
  }

  function setActive(zoneId, { announce = false } = {}) {
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
    if (announce) {
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) toast(`Active zone: ${zone.label}`);
    }
  }

  // ------------------------------------------------------------- data

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
      for (const zoneId of Object.keys(state.selectedByZone)) {
        const zone = state.zones.find(z => z.id === zoneId);
        if (!zone || !zone.images.some(item => item.id === state.selectedByZone[zoneId])) {
          delete state.selectedByZone[zoneId];
        }
      }
      renderAll();

      let stored = null;
      try { stored = localStorage.getItem("pb.activeZone"); } catch (_) {}
      if (stored && state.zones.some(z => z.id === stored)) setActive(stored);
      else if (state.zones.length === 1) setActive(state.zones[0].id);
      else setActive(null);

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

  async function upload(zoneId, file, { preserveName = false } = {}) {
    if (!file) return;
    const zoneEl = grid.querySelector(`[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      const fd = new FormData();
      // Le serveur conserve le nom seulement pour les fichiers déposés.
      fd.append("image", file, file.name || "clipboard");
      if (preserveName) fd.append("preserve_name", "1");
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
        rerenderZone(zoneId);
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

  function requireActiveZone() {
    if (state.activeId && state.zones.some(z => z.id === state.activeId)) return state.activeId;
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

  window.addEventListener("paste", (event) => {
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    let file = null;
    for (const item of items) {
      if (item.kind === "file" && /^image\//.test(item.type)) {
        file = item.getAsFile();
        break;
      }
    }
    if (file) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      upload(zoneId, file);
      return;
    }
    // Texte : on garde l'identité du clipboard (text/plain, text/html, …).
    const textItem = [...items].find(i => i.kind === "string");
    if (textItem) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      textItem.getAsString((text) => {
        if (!text) return;
        const blob = new Blob([text], { type: textItem.type || "text/plain" });
        upload(zoneId, blob);
      });
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
      copyContent(copyImageBtn.dataset.kind, copyImageBtn.dataset.preview);
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
      if (item && item.kind === "image") openPreview(item.preview_url, item.reference, item.filename);
      else if (item) openContentPreview(item);
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
      if (item && item.kind === "image") openPreview(item.preview_url, item.reference, item.filename);
      else if (item) openContentPreview(item);
      return;
    }
    const fileBox = event.target.closest(".file-box");
    if (fileBox) {
      const item = itemForControl(fileBox);
      if (item) openContentPreview(item);
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
      if (item && item.kind === "image") openPreview(item.preview_url, item.reference, item.filename);
      else if (item) openContentPreview(item);
      return;
    }
    const fileBox = event.target.closest(".file-box");
    if (fileBox) {
      event.preventDefault();
      const item = itemForControl(fileBox);
      if (item) openContentPreview(item);
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
    const zoneCard = event.target.closest(".zone");
    if (!zoneCard) return;
    event.preventDefault();
    zoneCard.classList.add("dragging");
  });
  grid.addEventListener("dragleave", (event) => {
    const zoneCard = event.target.closest(".zone");
    if (zoneCard) zoneCard.classList.remove("dragging");
  });
  grid.addEventListener("drop", (event) => {
    const zoneCard = event.target.closest(".zone");
    if (!zoneCard) return;
    event.preventDefault();
    zoneCard.classList.remove("dragging");
    setActive(zoneCard.dataset.zone);
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (!file) return;
    upload(zoneCard.dataset.zone, file, { preserveName: true });
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (/^[1-9]$/.test(event.key)) {
      const zone = state.zones[Number(event.key) - 1];
      if (zone) setActive(zone.id, { announce: true });
    } else if (event.key === "c" || event.key === "C") {
      const zone = state.zones.find(z => z.id === state.activeId);
      if (zone && zone.images.length) copyLink(selectedItem(zone).reference);
    }
  });

  function setPreviewCopyLabel(kind) {
    const label = `Copy ${kindLabel(kind)}`;
    pvCopyImage.textContent = label;
    pvCopyImage.setAttribute("aria-label", `${label} to the clipboard`);
  }

  function openPreview(url, reference, filename) {
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
    pvDelete.dataset.filename = storedFilename;
    if (typeof pv.showModal === "function") pv.showModal();
    else pv.setAttribute("open", "");
  }

  function openTextPreview(text) {
    pvText.textContent = text;
    pvText.hidden = false;
    pvImg.hidden = true;
    if (typeof pv.showModal === "function") pv.showModal();
    else pv.setAttribute("open", "");
  }

  async function openContentPreview(item) {
    setPreviewCopyLabel(item.kind);
    pvRef.textContent = item.reference;
    pvDownload.textContent = downloadLabel(item.filename);
    pvDownload.setAttribute("aria-label", downloadLabel(item.filename));
    pvDownload.dataset.preview = item.preview_url;
    pvDownload.dataset.filename = item.filename;
    pvDelete.dataset.filename = item.filename;
    pvCopyImage.dataset.preview = item.preview_url;
    pvCopyImage.dataset.kind = item.kind;
    if (item.kind === "text") {
      try {
        const response = await fetchPreview(item.preview_url, {
          credentials: "same-origin",
          headers: { Accept: "text/plain" },
        });
        if (!response.ok) throw new Error("preview unavailable");
        const text = await response.text();
        openTextPreview(text);
      } catch (_) {
        toast("Could not load the text preview", "error");
      }
      return;
    }
    // Binaire : téléchargement direct (Content-Disposition: attachment).
    downloadContent(item.preview_url, item.filename);
  }
  function closePreview() {
    if (typeof pv.close === "function") pv.close();
    else {
      pv.removeAttribute("open");
      setPreviewSource(pvImg, "");
    }
  }
  pvCopy.addEventListener("click", () => copyLink(pvRef.textContent));
  pvCopyImage.addEventListener("click", () => copyContent(pvCopyImage.dataset.kind, pvCopyImage.dataset.preview));
  pvDownload.addEventListener("click", () => downloadContent(pvDownload.dataset.preview, pvDownload.dataset.filename));
  pvClear.addEventListener("click", clearClipboard);
  pvDelete.addEventListener("click", () => {
    const zoneId = state.zones.find(z => z.images.some(i => i.id === pvDelete.dataset.filename));
    if (zoneId) {
      closePreview();
      deleteImage(zoneId.id, pvDelete.dataset.filename);
    }
  });
  document.getElementById("pv-close").addEventListener("click", closePreview);
  pv.addEventListener("click", (event) => { if (event.target === pv) closePreview(); });
  pv.addEventListener("close", () => {
    setPreviewSource(pvImg, "");
    pvText.hidden = true;
    pvImg.hidden = false;
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") boot(true);
  });

  // Lightweight synchronization across tabs and machines.
  setInterval(() => { if (document.visibilityState === "visible") boot(true); }, 45000);

  boot();
})();
