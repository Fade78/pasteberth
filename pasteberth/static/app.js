"use strict";

/* Pasteberth — interface vanilla, sans framework.
 *
 * Principes :
 * - la zone ACTIVE est explicite (bordure + halo + marqueur), jamais
 *   uniquement une couleur de fond ;
 * - après chaque upload l'UI est immédiatement prête pour un nouveau collage ;
 * - aucune Blob URL n'est conservée : les miniatures viennent du serveur ;
 * - la référence affichée/copiée est EXACTEMENT celle renvoyée par le serveur.
 */
(() => {
  const state = {
    zones: [],            // [{id,label,color,retain,count,images:[...]}]
    activeId: null,
    authEnabled: true,
    offline: false,
    retryTimer: null,
    pollTimer: null,
    toastTimer: null,
  };

  const grid = document.getElementById("grid");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const logoutForm = document.getElementById("logout-form");
  const toastEl = document.getElementById("toast");
  const pv = document.getElementById("pv");
  const pvImg = document.getElementById("pv-img");
  const pvRef = document.getElementById("pv-ref");
  const pvCopy = document.getElementById("pv-copy");

  // ------------------------------------------------------------- utilitaires

  function hexToRgb(hex) {
    return [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16),
    ];
  }

  function readableFg(hex) {
    const [r, g, b] = hexToRgb(hex);
    const yiq = (r * 299 + g * 587 + b * 114) / 1000;
    return yiq >= 150 ? "#12161b" : "#f3f6fa";
  }

  function rgba(hex, alpha) {
    const [r, g, b] = hexToRgb(hex);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function fmtBytes(n) {
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " Mo";
    if (n >= 1024) return Math.round(n / 1024) + " Ko";
    return n + " o";
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString([], { year: "numeric", month: "2-digit", day: "2-digit" })
      + " " + fmtTime(iso);
  }

  function toast(message, kind = "info") {
    toastEl.textContent = message;
    toastEl.className = "toast " + kind;
    toastEl.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
  }

  async function api(path, options) {
    let res;
    try {
      res = await fetch(path, Object.assign({ headers: { Accept: "application/json" } }, options));
    } catch (err) {
      throw new Error("réseau injoignable");
    }
    if (res.status === 401 && state.authEnabled) {
      window.location.href = "/login";
      throw new Error("session expirée");
    }
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* corps vide */ }
    if (!res.ok) {
      const message = payload && payload.error ? payload.error.message : `erreur ${res.status}`;
      const err = new Error(message);
      err.code = payload && payload.error ? payload.error.code : "error";
      err.status = res.status;
      throw err;
    }
    return payload;
  }

  // ---------------------------------------------------------------- copie

  async function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext !== false) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) { /* on tente le repli */ }
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
    try { ok = document.execCommand("copy"); } catch (_) { /* non supporté */ }
    ta.remove();
    return ok;
  }

  async function copyReference(reference) {
    const ok = await writeClipboard(reference);
    if (ok) toast("Référence copiée : " + shortRef(reference));
    else toast("Copie impossible — sélectionnez la référence manuellement", "error");
    return ok;
  }

  function shortRef(ref) {
    const name = ref.split("/").pop();
    return name.length > 40 ? name.slice(0, 37) + "…" : name;
  }

  // ------------------------------------------------------------- rendu

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
    head.innerHTML =
      '<span class="zone-marker" aria-hidden="true"></span>' +
      '<h2 class="zone-label"></h2>' +
      '<span class="zone-count"></span>';
    head.querySelector(".zone-label").textContent = zone.label;
    head.querySelector(".zone-count").textContent = `${zone.images.length} / ${zone.retain}`;
    el.appendChild(head);

    if (zone.images.length === 0) {
      const hint = document.createElement("div");
      hint.className = "drop-hint";
      hint.textContent = "Ctrl+V pour coller une image ici";
      el.appendChild(hint);
    } else {
      el.appendChild(renderLatest(zone.images[0]));
      if (zone.images.length > 1) el.appendChild(renderThumbs(zone.images.slice(1)));
    }
    return el;
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
    img.alt = "dernière image";
    img.loading = "lazy";
    const right = document.createElement("div");
    right.className = "latest-right";
    right.appendChild(itemMeta(item));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.dataset.ref = item.reference;
    btn.textContent = "Copier";
    right.appendChild(btn);
    card.append(img, right);
    return card;
  }

  function renderThumbs(items) {
    const row = document.createElement("div");
    row.className = "thumbs";
    for (const item of items) {
      const wrap = document.createElement("button");
      wrap.type = "button";
      wrap.className = "thumb-wrap";
      wrap.title = `${item.filename} — ${fmtDateTime(item.created_at)}`;
      wrap.dataset.ref = item.reference;
      wrap.dataset.preview = item.preview_url;
      const img = document.createElement("img");
      img.className = "thumb";
      img.src = item.preview_url;
      img.alt = item.filename;
      img.loading = "lazy";
      wrap.appendChild(img);
      row.appendChild(wrap);
    }
    return row;
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
    try { localStorage.setItem("pb.activeZone", zoneId); } catch (_) {}
    for (const el of grid.querySelectorAll(".zone")) {
      el.classList.toggle("active", el.dataset.zone === zoneId);
    }
    if (announce) {
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) toast(`Zone active : ${zone.label}`);
    }
  }

  // ------------------------------------------------------------- données

  async function refresh() {
    const overview = await api("/api/zones");
    state.authEnabled = overview.auth_enabled !== false;
    logoutForm.hidden = !state.authEnabled;

    const previous = new Map(state.zones.map(z => [z.id, z]));
    state.zones = [];
    for (const z of overview.zones) {
      const data = await api(`/api/zones/${encodeURIComponent(z.id)}/images`);
      state.zones.push(Object.assign({}, z, { images: data.images }));
    }
    renderAll();

    let stored = null;
    try { stored = localStorage.getItem("pb.activeZone"); } catch (_) {}
    if (stored && state.zones.some(z => z.id === stored)) setActive(stored);
    else if (state.zones.length === 1) setActive(state.zones[0].id);
    else setActive(null);

    setOnline(true);
  }

  function setOnline(ok, message) {
    state.offline = !ok;
    statusEl.classList.toggle("offline", !ok);
    statusText.textContent = ok ? "en ligne" : (message || "hors ligne");
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
      if (!silent) renderAll();
      if (err.message === "réseau injoignable") scheduleRetry();
      else if (!silent) toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------- upload

  async function upload(zoneId, file) {
    if (!file) return;
    if (!/image\//.test(file.type || "") && !/\.(png|jpe?g|webp)$/i.test(file.name || "")) {
      toast("Le contenu collé n'est pas une image reconnue", "error");
      return;
    }
    const zoneEl = grid.querySelector(`[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    try {
      const fd = new FormData();
      fd.append("image", file, "clipboard.png"); // nom ignoré par le serveur
      const item = await api(`/api/zones/${encodeURIComponent(zoneId)}/images`,
        { method: "POST", body: fd });
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) {
        zone.images.unshift(item);
        if (zone.images.length > zone.retain) zone.images.length = zone.retain;
        rerenderZone(zoneId);
      }
      toast(`Image envoyée (${shortRef(item.reference)})`);
      // Copie automatique best-effort : en cas d'échec, le bouton Copier reste là.
      writeClipboard(item.reference).then(ok => { if (ok) toast("Référence copiée"); });
    } catch (err) {
      if (err.status === 413) toast("Image trop grande pour ce serveur", "error");
      else toast(err.message, "error");
    } finally {
      if (zoneEl) zoneEl.classList.remove("busy");
    }
  }

  function requireActiveZone() {
    if (state.activeId && state.zones.some(z => z.id === state.activeId)) return state.activeId;
    toast("Choisissez d'abord une zone (cliquez sur sa carte)", "error");
    for (const el of grid.querySelectorAll(".zone")) {
      el.classList.remove("attention");
      void el.offsetWidth; // relance l'animation
      el.classList.add("attention");
    }
    return null;
  }

  // ------------------------------------------------------------- événements

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
      if (hasText) toast("Le presse-papiers ne contient pas d'image");
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
      copyReference(copyBtn.dataset.ref);
      return;
    }
    const thumbWrap = event.target.closest(".thumb-wrap");
    if (thumbWrap) {
      openPreview(thumbWrap.dataset.preview, thumbWrap.dataset.ref);
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
      toast("Seules les images sont acceptées", "error");
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
      if (zone && zone.images[0]) copyReference(zone.images[0].reference);
    }
  });

  function openPreview(url, reference) {
    pvImg.src = url;
    pvRef.textContent = reference;
    if (typeof pv.showModal === "function") pv.showModal();
    else pv.setAttribute("open", "");
  }

  pvCopy.addEventListener("click", () => copyReference(pvRef.textContent));
  document.getElementById("pv-close").addEventListener("click", () => pv.close());
  pv.addEventListener("click", (event) => { if (event.target === pv) pv.close(); });
  pv.addEventListener("close", () => { pvImg.src = ""; });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") boot(true);
  });

  // Synchronisation légère multi-onglets / multi-machines.
  setInterval(() => { if (document.visibilityState === "visible") boot(true); }, 45000);

  boot();
})();
