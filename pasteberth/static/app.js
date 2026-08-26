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
  const pvRef = document.getElementById("pv-ref");
  const pvCopy = document.getElementById("pv-copy");
  const pvCopyImage = document.getElementById("pv-copy-image");
  const pvClear = document.getElementById("pv-clear");

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
        empty_upload: "The upload is empty",
        invalid_image: "The image is invalid or corrupted",
        unsupported_format: "This image format is not supported",
        unsupported_media_type: "This media type is not supported",
        too_large: "The upload is too large",
        payload_too_large: "The upload is too large",
        storage_low: "Not enough disk space",
        retention_error: "Image retention failed",
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
      const response = await fetch(previewUrl, {
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

  function shortRef(ref) {
    const name = ref.split("/").pop();
    return name.length > 40 ? name.slice(0, 37) + "…" : name;
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
      hint.textContent = "Ctrl+V to paste an image here";
      el.appendChild(hint);
    } else {
      const selected = selectedItem(zone);
      el.appendChild(renderLatest(selected));
      el.appendChild(renderThumbs(zone.images, selected.id));
    }
    return el;
  }

  function selectedItem(zone) {
    return zone.images.find(item => item.id === state.selectedByZone[zone.id]) || zone.images[0];
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
    dims.textContent = `${item.width}×${item.height} · ${fmtBytes(item.size)} · ${fmtTime(item.created_at)}`;
    meta.append(fname, ref, dims);
    return meta;
  }

  function renderLatest(item) {
    const card = document.createElement("div");
    card.className = "latest";
    const img = document.createElement("img");
    img.className = "thumb-big";
    img.src = item.preview_url;
    img.alt = "latest image";
    img.loading = "lazy";
    img.tabIndex = 0;
    img.setAttribute("role", "button");
    img.setAttribute("aria-label", "Open the latest image preview");
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
    imageCopy.textContent = "Copy image";
    imageCopy.setAttribute("aria-label", "Copy image to the clipboard");
    imageCopy.dataset.preview = item.preview_url;
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "clear-btn";
    clear.textContent = "Clear";
    clear.setAttribute("aria-label", "Clear the clipboard");
    const zoom = document.createElement("button");
    zoom.type = "button";
    zoom.className = "zoom-btn";
    zoom.textContent = "Zoom";
    zoom.setAttribute("aria-label", "Enlarge the image");
    zoom.dataset.ref = item.reference;
    zoom.dataset.preview = item.preview_url;
    const actions = document.createElement("div");
    actions.className = "latest-actions";
    actions.append(btn, imageCopy, clear, zoom);
    right.appendChild(actions);
    card.append(img, right);
    return card;
  }

  function renderThumbs(items, selectedId) {
    const index = document.createElement("div");
    index.className = "history-index";
    const title = document.createElement("div");
    title.className = "index-title";
    title.textContent = "Image index";
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
      const img = document.createElement("img");
      img.className = "thumb";
      img.src = item.preview_url;
      img.alt = item.filename;
      img.loading = "lazy";
      wrap.appendChild(img);
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

  async function upload(zoneId, file) {
    if (!file) return;
    if (!/image\//.test(file.type || "") && !/\.(png|jpe?g|webp)$/i.test(file.name || "")) {
      toast("The pasted content is not a recognized image", "error");
      return;
    }
    const zoneEl = grid.querySelector(`[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      const fd = new FormData();
      fd.append("image", file, "clipboard.png"); // filename ignored by the server
      const item = await api(`/api/zones/${encodeURIComponent(zoneId)}/images`,
        { method: "POST", body: fd });
      refreshGeneration += 1;
      if (activeRefreshController) activeRefreshController.abort();
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) {
        zone.images.unshift(item);
        if (zone.images.length > zone.retain) zone.images.length = zone.retain;
        state.selectedByZone[zoneId] = item.id;
        rerenderZone(zoneId);
      }
      toast(`Image uploaded (${shortRef(item.reference)})`);
      // Best-effort automatic copy: the Copy link button remains available on failure.
      writeClipboard(item.reference).then(ok => { if (ok) toast("Link copied"); });
    } catch (err) {
      if (err.status === 413) toast("Image is too large for this server", "error");
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
    if (!file) {
      const hasText = [...items].some(i => i.kind === "string");
      if (hasText) toast("The clipboard does not contain an image");
      return;
    }
    event.preventDefault();
    const zoneId = requireActiveZone();
    if (!zoneId) return;
    upload(zoneId, file);
  });

  grid.addEventListener("click", (event) => {
    const copyBtn = event.target.closest(".copy-btn");
    if (copyBtn && copyBtn.dataset.ref) {
      copyLink(copyBtn.dataset.ref);
      return;
    }
    const copyImageBtn = event.target.closest(".copy-image-btn");
    if (copyImageBtn && copyImageBtn.dataset.preview) {
      copyImage(copyImageBtn.dataset.preview);
      return;
    }
    const clearBtn = event.target.closest(".clear-btn");
    if (clearBtn) {
      clearClipboard();
      return;
    }
    const zoomBtn = event.target.closest(".zoom-btn");
    if (zoomBtn && zoomBtn.dataset.preview) {
      openPreview(zoomBtn.dataset.preview, zoomBtn.dataset.ref);
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
      const zoneEl = bigThumb.closest(".zone");
      const zone = state.zones.find(z => z.id === zoneEl.dataset.zone);
      if (zone && zone.images[0]) openPreview(zone.images[0].preview_url, zone.images[0].reference);
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
      const zoneEl = bigThumb.closest(".zone");
      const zone = state.zones.find(z => z.id === zoneEl.dataset.zone);
      if (zone && zone.images[0]) openPreview(zone.images[0].preview_url, zone.images[0].reference);
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
    if (!/^image\//.test(file.type)) {
      toast("Only images are accepted", "error");
      return;
    }
    upload(zoneCard.dataset.zone, file);
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

  function openPreview(url, reference) {
    pvImg.src = url;
    pvRef.textContent = reference;
    pvCopyImage.dataset.preview = url;
    if (typeof pv.showModal === "function") pv.showModal();
    else pv.setAttribute("open", "");
  }

  function closePreview() {
    if (typeof pv.close === "function") pv.close();
    else {
      pv.removeAttribute("open");
      pvImg.src = "";
    }
  }

  pvCopy.addEventListener("click", () => copyLink(pvRef.textContent));
  pvCopyImage.addEventListener("click", () => copyImage(pvCopyImage.dataset.preview));
  pvClear.addEventListener("click", clearClipboard);
  document.getElementById("pv-close").addEventListener("click", closePreview);
  pv.addEventListener("click", (event) => { if (event.target === pv) closePreview(); });
  pv.addEventListener("close", () => { pvImg.src = ""; });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") boot(true);
  });

  // Lightweight synchronization across tabs and machines.
  setInterval(() => { if (document.visibilityState === "visible") boot(true); }, 45000);

  boot();
})();
