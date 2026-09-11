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
  const REFRESH_INTERVAL_MS = 10_000;
  const INTERNAL_TRANSFER_MIME = "application/x-pasteberth-transfer";

  function appPath(path) {
    return `${URL_PREFIX}${path}`;
  }

  const state = {
    zones: [],            // [{id,label,color,retain,count,upload_limit_bytes,items:[...]}]
    activeId: null,
    authEnabled: true,
    accessAdmin: false,
    accessAdminLoaded: false,
    tokenAdmin: null,
    accessRequestInFlight: false,
    showFullPath: true,
    maxArchiveFiles: null,
    offline: false,
    selectedByZone: Object.create(null),
    knownItemSignaturesByZone: Object.create(null),
    newItemIdsByZone: Object.create(null),
    copyFeedbackByItem: Object.create(null),
    copyFeedbackTimers: Object.create(null),
    copyAttemptByItem: Object.create(null),
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
  let bootInFlight = false;
  let previewGeneration = 0;
  let copyAttemptSequence = 0;
  let activePreviewController = null;
  let groupOptionsClose = null;

  const grid = document.getElementById("grid");
  const groupTabs = document.getElementById("group-tabs");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("status-text");
  const accessButton = document.getElementById("access-button");
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
  const accessDialog = document.getElementById("access-dialog");
  const accessBackdrop = document.getElementById("access-backdrop");
  const accessClose = document.getElementById("access-close");
  const tokenForm = document.getElementById("token-form");
  const tokenLabel = document.getElementById("token-label");
  const tokenDuration = document.getElementById("token-duration");
  const tokenPermanent = document.getElementById("token-permanent");
  const addGrantButton = document.getElementById("add-grant");
  const tokenGrants = document.getElementById("token-grants");
  const tokenSecret = document.getElementById("token-secret");
  const tokenList = document.getElementById("token-list");
  const refreshTokensButton = document.getElementById("refresh-tokens");
  const suspensionList = document.getElementById("suspension-list");
  const accessMessage = document.getElementById("access-message");
  const filePicker = document.getElementById("file-picker");
  const replacementQueue = [];
  const dialogInvokers = new WeakMap();
  let activeReplacementPrompt = null;
  let pvCopyRawHtml = null;
  let pasteShortcutActive = false;
  let pasteShortcutHandled = false;

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

  function fmtTime(iso, includeDate = false) {
    const d = new Date(iso);
    if (!iso || Number.isNaN(d.getTime())) return "Unknown time";
    const time = d.toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (includeDate || Date.now() - d.getTime() > 24 * 60 * 60 * 1000) {
      return d.toLocaleDateString("en", { year: "numeric", month: "2-digit", day: "2-digit" })
        + " " + time;
    }
    return time;
  }

  function fmtDateTime(iso) {
    return fmtTime(iso, true);
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

  function copyFeedbackKey(zoneId, itemId) {
    return `${zoneId}\u0000${itemId}`;
  }

  function copyButtonForItem(zoneId, itemId) {
    const zone = grid.querySelector(`.zone[data-zone="${CSS.escape(zoneId)}"]`);
    return zone?.querySelector(`.latest[data-item-id="${CSS.escape(itemId)}"] .copy-btn`) || null;
  }

  function applyCopyFeedback(button, status) {
    if (!button) return;
    const statusEl = button.parentElement?.querySelector(".copy-status");
    button.classList.toggle("copy-success", status === "copied");
    button.classList.toggle(
      "copy-attention",
      status === "attention" || status === "manual-attention",
    );
    if (statusEl) {
      statusEl.hidden = !status;
      statusEl.classList.toggle(
        "error",
        status === "attention" || status === "manual-attention",
      );
      statusEl.textContent = status === "copied"
        ? "Link copied"
        : status === "attention"
          ? "Automatic copy failed - click Copy link"
          : status === "manual-attention"
            ? "Copy failed - try again"
          : "";
    }
    if (!status) button.removeAttribute("title");
    else if (status === "copied") button.title = "Link copied";
    else if (status === "attention") button.title = "Automatic copy failed; click Copy link";
    else button.title = "Copy failed; try again";
  }

  function copyFeedbackFor(zoneId, itemId) {
    return state.copyFeedbackByItem[copyFeedbackKey(zoneId, itemId)] || null;
  }

  function beginCopyAttempt(zoneId, itemId) {
    const key = copyFeedbackKey(zoneId, itemId);
    const previousTimer = state.copyFeedbackTimers[key];
    if (previousTimer) clearTimeout(previousTimer);
    delete state.copyFeedbackTimers[key];
    delete state.copyFeedbackByItem[key];
    applyCopyFeedback(copyButtonForItem(zoneId, itemId), null);
    const attempt = ++copyAttemptSequence;
    state.copyAttemptByItem[key] = attempt;
    return attempt;
  }

  function setCopyFeedback(zoneId, itemId, status, attempt = null) {
    const key = copyFeedbackKey(zoneId, itemId);
    if (attempt !== null && state.copyAttemptByItem[key] !== attempt) return;
    const previousTimer = state.copyFeedbackTimers[key];
    if (previousTimer) clearTimeout(previousTimer);
    delete state.copyFeedbackTimers[key];
    if (status) state.copyFeedbackByItem[key] = status;
    else delete state.copyFeedbackByItem[key];
    applyCopyFeedback(copyButtonForItem(zoneId, itemId), status);
    if (!status) return;
    state.copyFeedbackTimers[key] = setTimeout(() => {
      if (attempt !== null && state.copyAttemptByItem[key] !== attempt) return;
      delete state.copyFeedbackTimers[key];
      delete state.copyFeedbackByItem[key];
      delete state.copyAttemptByItem[key];
      applyCopyFeedback(copyButtonForItem(zoneId, itemId), null);
    }, 2600);
  }

  async function api(path, options) {
    let res;
    try {
      const requestOptions = Object.assign({}, options || {});
      const redirectOnUnauthorized = requestOptions.redirectOnUnauthorized !== false;
      delete requestOptions.redirectOnUnauthorized;
      requestOptions.headers = Object.assign({ Accept: "application/json" }, requestOptions.headers || {});
      res = await fetch(appPath(path), requestOptions);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new Error("network unreachable");
    }
    if (res.status === 401 && state.authEnabled && redirectOnUnauthorized) {
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
        unknown_item: "Unknown item",
        invalid_filename: "The dropped filename is invalid",
        empty_upload: "The upload is empty",
        invalid_image: "The image is invalid or corrupted",
        unsupported_format: "This image format is not supported",
        unsupported_media_type: "This media type is not supported",
        too_large: "The upload is too large",
        payload_too_large: "The upload is too large",
        storage_low: "Not enough disk space",
        retention_error: "Item retention failed",
        storage_conflict: "Name taken by an unmanaged file",
        replacement_required: "This name already exists; confirm replacement",
        destination_error: "The item destination is unavailable",
        zone_busy: "This zone is busy; try again shortly",
        preview_busy: "Too many previews are currently being served",
        rate_limited: "Too many attempts; try again later",
        upload_busy: "Too many uploads are currently in memory",
        invalid_comment: "The comment is invalid or too long",
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

  // ---------------------------------------------------------- access admin

  function tokenCatalog() {
    const catalog = state.tokenAdmin?.catalog;
    return {
      zones: Array.isArray(catalog?.zones) ? catalog.zones : [],
      groups: Array.isArray(catalog?.groups) ? catalog.groups : [],
    };
  }

  function setAccessMessage(message, kind = "") {
    if (!accessMessage) return;
    accessMessage.textContent = message || "";
    accessMessage.className = `access-message ${kind}`.trim();
    accessMessage.hidden = !message;
  }

  function clearTokenSecret() {
    if (!tokenSecret) return;
    tokenSecret.replaceChildren();
    tokenSecret.hidden = true;
  }

  function showTokenSecret(credential, action = "Created") {
    if (!tokenSecret || typeof credential !== "string" || !credential) return;
    tokenSecret.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = `${action} token secret`;
    const warning = document.createElement("p");
    warning.textContent = "Copy it now. Pasteberth cannot recover this secret later.";
    const line = document.createElement("div");
    line.className = "token-secret-line";
    const value = document.createElement("code");
    value.textContent = credential;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "small-btn";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      if (await writeClipboard(credential)) toast("Token secret copied");
      else toast("Could not copy the token secret", "error");
    });
    line.append(value, copy);
    tokenSecret.append(title, warning, line);
    tokenSecret.hidden = false;
  }

  function addGrantRow(initial = {}) {
    if (!tokenGrants) return;
    const row = document.createElement("fieldset");
    row.className = "token-grant-row";
    const legend = document.createElement("legend");
    legend.textContent = "Grant";
    row.appendChild(legend);

    const controls = document.createElement("div");
    controls.className = "grant-controls";
    const scopeLabel = document.createElement("label");
    scopeLabel.textContent = "Scope";
    const scopeSelect = document.createElement("select");
    scopeSelect.className = "grant-scope";
    for (const [value, label] of [
      ["zone", "Zone"],
      ["group", "Group"],
      ["path", "PATH"],
      ["global", "Global"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      scopeSelect.appendChild(option);
    }
    scopeSelect.value = initial.scope_type || "zone";
    scopeLabel.appendChild(scopeSelect);
    const valueLabel = document.createElement("label");
    valueLabel.className = "grant-value-label";
    const valueCaption = document.createElement("span");
    valueLabel.appendChild(valueCaption);
    const valueWrap = document.createElement("span");
    valueLabel.appendChild(valueWrap);
    controls.append(scopeLabel, valueLabel);

    const permissions = document.createElement("div");
    permissions.className = "grant-permissions";
    const permissionInputs = {};
    for (const name of ["L", "R", "W"]) {
      const label = document.createElement("label");
      label.className = "check-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.permission = name;
      input.checked = Array.isArray(initial.permissions) && initial.permissions.includes(name);
      const text = document.createElement("span");
      text.textContent = name;
      label.append(input, text);
      permissions.appendChild(label);
      permissionInputs[name] = input;
    }
    controls.appendChild(permissions);

    const replaceLabel = document.createElement("label");
    replaceLabel.className = "check-row grant-replace-label";
    const replaceInput = document.createElement("input");
    replaceInput.type = "checkbox";
    replaceInput.className = "grant-replace";
    replaceInput.checked = initial.allow_replace === true;
    const replaceText = document.createElement("span");
    replaceText.textContent = "Allow named replacement";
    replaceLabel.append(replaceInput, replaceText);
    controls.appendChild(replaceLabel);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "small-btn danger-btn grant-remove";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => row.remove());
    controls.appendChild(remove);
    row.appendChild(controls);

    let currentValue = typeof initial.scope_value === "string" ? initial.scope_value : "";
    const updateValueControl = () => {
      const scope = scopeSelect.value;
      valueWrap.replaceChildren();
      if (scope === "global") {
        valueLabel.hidden = true;
      } else {
        valueLabel.hidden = false;
        valueCaption.textContent = scope === "zone"
          ? "Zone ID"
          : scope === "group"
            ? "Group"
            : "Absolute path";
        if (scope === "path") {
          const input = document.createElement("input");
          input.type = "text";
          input.className = "grant-value";
          input.maxLength = 4096;
          input.value = currentValue;
          input.placeholder = "/srv/pasteberth/project";
          valueWrap.appendChild(input);
        } else {
          const select = document.createElement("select");
          select.className = "grant-value";
          const empty = document.createElement("option");
          empty.value = "";
          empty.textContent = scope === "zone" ? "Select a zone" : "Select a group";
          select.appendChild(empty);
          const entries = scope === "zone" ? tokenCatalog().zones : tokenCatalog().groups;
          for (const entry of entries) {
            const option = document.createElement("option");
            option.value = scope === "zone" ? entry.id : entry.name;
            option.textContent = scope === "zone"
              ? `${entry.label || entry.id} (${entry.id})`
              : entry.name;
            select.appendChild(option);
          }
          if (currentValue && !entries.some(entry => (
            scope === "zone" ? entry.id : entry.name
          ) === currentValue)) {
            const option = document.createElement("option");
            option.value = currentValue;
            option.textContent = `${currentValue} (not active)`;
            select.appendChild(option);
          }
          select.value = currentValue;
          valueWrap.appendChild(select);
        }
      }
    };
    scopeSelect.addEventListener("change", () => {
      currentValue = "";
      updateValueControl();
    });
    valueWrap.addEventListener("change", () => {
      currentValue = valueWrap.querySelector(".grant-value")?.value || "";
    });
    valueWrap.addEventListener("input", () => {
      currentValue = valueWrap.querySelector(".grant-value")?.value || "";
    });
    const syncReplacement = () => {
      const enabled = permissionInputs.W.checked;
      replaceInput.disabled = !enabled;
      if (!enabled) replaceInput.checked = false;
    };
    permissionInputs.W.addEventListener("change", syncReplacement);
    syncReplacement();
    updateValueControl();
    tokenGrants.appendChild(row);
  }

  function collectTokenGrants() {
    const rows = [...(tokenGrants?.querySelectorAll(".token-grant-row") || [])];
    if (!rows.length) throw new Error("Add at least one grant");
    return rows.map((row) => {
      const scopeType = row.querySelector(".grant-scope")?.value;
      const scopeValue = row.querySelector(".grant-value")?.value || "";
      if (scopeType !== "global" && !scopeValue.trim()) {
        throw new Error("Every non-global grant needs a target");
      }
      const permissions = ["L", "R", "W"].filter((name) => (
        row.querySelector(`[data-permission="${name}"]`)?.checked
      ));
      return {
        scope_type: scopeType,
        scope_value: scopeType === "global" ? "" : scopeValue,
        permissions,
        allow_replace: Boolean(row.querySelector(".grant-replace")?.checked),
      };
    });
  }

  function resetTokenForm() {
    if (!tokenForm) return;
    tokenForm.reset();
    tokenDuration.disabled = false;
    tokenGrants.replaceChildren();
    addGrantRow();
  }

  function tokenTime(value) {
    if (value === null || value === undefined) return "Never";
    const milliseconds = Number(value) * 1000;
    if (!Number.isFinite(milliseconds) || milliseconds > 8640000000000000) return "Far future";
    const date = new Date(milliseconds);
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }

  function suspensionKey(scopeType, scopeValue = "") {
    return `${scopeType}\u0000${scopeValue}`;
  }

  function suspensionSet() {
    return new Set((state.tokenAdmin?.suspensions || []).map((item) => (
      suspensionKey(item.scope_type, item.scope_value || "")
    )));
  }

  function renderTokenList() {
    if (!tokenList) return;
    tokenList.replaceChildren();
    const records = Array.isArray(state.tokenAdmin?.tokens) ? state.tokenAdmin.tokens : [];
    if (!records.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No bearer tokens have been issued.";
      tokenList.appendChild(empty);
      return;
    }
    for (const token of records) {
      const card = document.createElement("article");
      card.className = `token-card token-state-${token.state || "unknown"}`;
      const head = document.createElement("div");
      head.className = "token-card-head";
      const title = document.createElement("h4");
      title.textContent = token.label || "Unnamed token";
      const stateBadge = document.createElement("span");
      stateBadge.className = "token-state";
      stateBadge.textContent = token.state || "unknown";
      head.append(title, stateBadge);
      const identifier = document.createElement("code");
      identifier.textContent = token.token_id || "";
      const expiry = document.createElement("p");
      expiry.className = "muted token-meta";
      expiry.textContent = `Created ${tokenTime(token.created_at)}; expires ${tokenTime(token.expires_at)}`;
      const grants = document.createElement("ul");
      grants.className = "token-grant-summary";
      for (const grant of token.grants || []) {
        const item = document.createElement("li");
        const target = grant.scope_type === "global"
          ? "all zones"
          : `${grant.scope_type}: ${grant.scope_value || ""}`;
        const rights = (grant.permissions || []).join("") || "none";
        item.textContent = `${target} [${rights}] - ${grant.status || "unknown"}`;
        grants.appendChild(item);
      }
      const actions = document.createElement("div");
      actions.className = "token-actions";
      const extend = document.createElement("button");
      extend.type = "button";
      extend.className = "small-btn";
      extend.textContent = "Extend";
      extend.disabled = token.state === "revoked";
      extend.addEventListener("click", () => tokenLifecycleAction(token, "extend"));
      const rotate = document.createElement("button");
      rotate.type = "button";
      rotate.className = "small-btn";
      rotate.textContent = "Rotate";
      rotate.disabled = token.state === "revoked";
      rotate.addEventListener("click", () => tokenLifecycleAction(token, "rotate"));
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "small-btn danger-btn";
      revoke.textContent = "Revoke";
      revoke.disabled = token.state === "revoked";
      revoke.addEventListener("click", () => tokenRevokeAction(token));
      actions.append(extend, rotate, revoke);
      card.append(head, identifier, expiry, grants, actions);
      tokenList.appendChild(card);
    }
  }

  function renderSuspensions() {
    if (!suspensionList) return;
    suspensionList.replaceChildren();
    const catalog = tokenCatalog();
    const definitions = [
      { scope_type: "global", scope_value: "", label: "All bearer tokens" },
      ...catalog.zones.map((zone) => ({
        scope_type: "zone", scope_value: zone.id,
        label: `Zone: ${zone.label || zone.id} (${zone.id})`,
      })),
      ...catalog.groups.map((group) => ({
        scope_type: "group", scope_value: group.name,
        label: `Group: ${group.name}`,
      })),
    ];
    if (!definitions.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No suspendable scopes are configured.";
      suspensionList.appendChild(empty);
      return;
    }
    const active = suspensionSet();
    for (const definition of definitions) {
      const row = document.createElement("div");
      row.className = "suspension-row";
      const label = document.createElement("span");
      label.textContent = definition.label;
      const status = document.createElement("span");
      const suspended = active.has(suspensionKey(definition.scope_type, definition.scope_value));
      status.className = suspended ? "token-state token-state-suspended" : "muted";
      status.textContent = suspended ? "suspended" : "active";
      const button = document.createElement("button");
      button.type = "button";
      button.className = suspended ? "small-btn" : "small-btn danger-btn";
      button.textContent = suspended ? "Resume" : "Suspend";
      button.addEventListener("click", () => setTokenSuspension(
        definition.scope_type,
        definition.scope_value,
        !suspended,
      ));
      row.append(label, status, button);
      suspensionList.appendChild(row);
    }
  }

  function renderTokenAdmin() {
    renderTokenList();
    renderSuspensions();
    if (tokenGrants && !tokenGrants.children.length) addGrantRow();
  }

  async function loadTokenAdmin() {
    if (!state.authEnabled || !accessButton) return null;
    if (state.accessRequestInFlight) return state.tokenAdmin;
    state.accessRequestInFlight = true;
    try {
      const payload = await api("/api/tokens", { redirectOnUnauthorized: false });
      if (!payload || !Array.isArray(payload.tokens)) throw new Error("invalid access response");
      state.tokenAdmin = payload;
      state.accessAdmin = true;
      state.accessAdminLoaded = true;
      accessButton.hidden = false;
      renderTokenAdmin();
      return payload;
    } catch (err) {
      state.accessAdmin = false;
      state.accessAdminLoaded = true;
      accessButton.hidden = true;
      if (isDialogOpen(accessDialog)) setAccessMessage(err.message, "error");
      return null;
    } finally {
      state.accessRequestInFlight = false;
    }
  }

  function tokenDurationPrompt(action) {
    const raw = window.prompt(
      `${action} duration in seconds (leave blank for a permanent token):`,
      "2592000",
    );
    if (raw === null) return undefined;
    if (!raw.trim()) return null;
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value <= 0) {
      setAccessMessage("Duration must be a positive integer.", "error");
      return undefined;
    }
    return value;
  }

  async function tokenLifecycleAction(token, action) {
    const duration = tokenDurationPrompt(action === "rotate" ? "Rotation" : "Extension");
    if (duration === undefined) return;
    try {
      const payload = await api(
        `/api/tokens/${encodeURIComponent(token.token_id)}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ duration_seconds: duration }),
        },
      );
      if (action === "rotate") showTokenSecret(payload.token, "Rotated");
      setAccessMessage(`${action === "rotate" ? "Token rotated" : "Token extended"}.`);
      await loadTokenAdmin();
    } catch (err) {
      setAccessMessage(err.message, "error");
    }
  }

  async function tokenRevokeAction(token) {
    if (!window.confirm(`Revoke token ${token.label || token.token_id}?`)) return;
    try {
      await api(`/api/tokens/${encodeURIComponent(token.token_id)}`, { method: "DELETE" });
      setAccessMessage("Token revoked.");
      await loadTokenAdmin();
    } catch (err) {
      setAccessMessage(err.message, "error");
    }
  }

  async function setTokenSuspension(scopeType, scopeValue, suspended) {
    try {
      await api("/api/token-suspensions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scope_type: scopeType,
          scope_value: scopeValue,
          suspended,
        }),
      });
      setAccessMessage(suspended ? "Scope suspended." : "Scope resumed.");
      await loadTokenAdmin();
    } catch (err) {
      setAccessMessage(err.message, "error");
    }
  }

  async function openAccessPanel() {
    if (!state.accessAdmin && !(await loadTokenAdmin())) return;
    clearTokenSecret();
    setAccessMessage("");
    openDialog(accessDialog, accessButton);
  }

  function closeAccessPanel() {
    if (!accessDialog) return;
    if (closeDialog(accessDialog)) return;
    clearTokenSecret();
    setAccessMessage("");
    restoreDialogInvoker(accessDialog);
  }

  if (accessButton) accessButton.addEventListener("click", openAccessPanel);
  if (accessClose) accessClose.addEventListener("click", closeAccessPanel);
  if (accessDialog) accessDialog.addEventListener("close", () => {
    clearTokenSecret();
    setAccessMessage("");
    restoreDialogInvoker(accessDialog);
  });
  if (accessDialog) accessDialog.addEventListener("click", (event) => {
    if (event.target === accessDialog) closeAccessPanel();
  });
  if (accessBackdrop) accessBackdrop.addEventListener("click", closeAccessPanel);
  if (addGrantButton) addGrantButton.addEventListener("click", () => addGrantRow());
  if (refreshTokensButton) refreshTokensButton.addEventListener("click", async () => {
    await loadTokenAdmin();
    setAccessMessage("Access data refreshed.");
  });
  if (tokenPermanent) tokenPermanent.addEventListener("change", () => {
    tokenDuration.disabled = tokenPermanent.checked;
    if (tokenPermanent.checked) tokenDuration.value = "";
  });
  if (tokenForm) tokenForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const duration = tokenPermanent.checked ? null : Number(tokenDuration.value);
      if (duration !== null && (!Number.isSafeInteger(duration) || duration <= 0)) {
        throw new Error("Duration must be a positive integer or permanent.");
      }
      const label = tokenLabel.value.trim();
      if (!label) throw new Error("A token label is required.");
      const payload = await api("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label,
          duration_seconds: duration,
          grants: collectTokenGrants(),
        }),
      });
      showTokenSecret(payload.token, "Created");
      setAccessMessage("Token created. Copy the secret before closing this panel.");
      resetTokenForm();
      await loadTokenAdmin();
    } catch (err) {
      setAccessMessage(err.message, "error");
    }
  });

  // ---------------------------------------------------------------- clipboard

  async function writeClipboard(text, { allowLegacyFallback = true } = {}) {
    if (navigator.clipboard && window.isSecureContext !== false) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) { /* use the legacy fallback only when allowed */ }
    }
    if (!allowLegacyFallback) return false;
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

  async function copyLink(reference, button = null, announce = false, target = null) {
    const latest = button?.closest(".latest[data-item-id]");
    const zone = button?.closest(".zone[data-zone]");
    const zoneId = zone?.dataset.zone || target?.zoneId;
    const itemId = latest?.dataset.itemId || target?.itemId;
    const hasTarget = Boolean(zoneId && itemId);
    const attempt = hasTarget
      ? beginCopyAttempt(zoneId, itemId)
      : null;
    const ok = await writeClipboard(reference);
    if (hasTarget) {
      setCopyFeedback(
        zoneId,
        itemId,
        ok ? "copied" : "manual-attention",
        attempt,
      );
      if (announce) {
        if (ok) toast("Link copied: " + shortRef(reference));
        else toast("Could not copy the link — select it manually", "error");
      }
    } else if (ok) toast("Link copied: " + shortRef(reference));
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

  // Mixed clipboard: one HTML document with text and embedded images, in
  // flavor order. getAsString/getAsFile must be called during the event;
  // slots preserve the original order.
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
    // Missing or empty text: an image paste remains a simple image upload.
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
        uploadCandidate(zoneId, browserUploadCandidate(image.blob));
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
        } catch (_) { /* unreadable image: ignore it */ }
      }
    }
    if (!chunks.length) return;
    const html = `<!doctype html><html><body>${chunks.join("\n")}</body></html>`;
    uploadCandidate(
      zoneId,
      browserUploadCandidate(new Blob([html], { type: "text/html" })),
    );
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

  function itemSignature(item) {
    return JSON.stringify([
      item.created_at,
      item.size,
      item.kind,
      item.mime,
      item.format,
      item.width,
      item.height,
      item.changed_at,
      item.sha256,
      item.etag,
    ]);
  }

  function itemDetails(zoneId, item) {
    const details = [
      `Name: ${item.filename}`,
      `Type: ${kindLabel(item.kind)}`,
      `MIME: ${item.mime || "unknown"}`,
      `Size: ${fmtBytes(item.size)}`,
      `Created: ${fmtDateTime(item.created_at)}`,
    ];
    if (item.kind === "image" && item.width != null && item.height != null) {
      details.splice(3, 0, `Dimensions: ${item.width}×${item.height}`);
    }
    if (item.format) details.splice(3, 0, `Format: ${item.format.toUpperCase()}`);
    if (state.showFullPath && item.reference) details.push(`Reference: ${item.reference}`);
    if (item.comment) details.push(`Comment: ${item.comment}`);
    return details.join("\n");
  }

  function newItemIds(zoneId) {
    const ids = state.newItemIdsByZone[zoneId];
    return ids instanceof Set ? ids : new Set();
  }

  function hasNewItems(zoneId) {
    return newItemIds(zoneId).size > 0;
  }

  function isNewItem(zoneId, itemId) {
    return newItemIds(zoneId).has(itemId);
  }

  function clearNewItems(zoneId, itemIds) {
    const ids = state.newItemIdsByZone[zoneId];
    if (!(ids instanceof Set)) return;
    for (const itemId of itemIds) ids.delete(itemId);
  }

  function updateNewItemState(zones) {
    const detectNewItems = state.initialized;
    const currentZoneIds = new Set();
    for (const zone of zones) {
      currentZoneIds.add(zone.id);
      const current = new Map(zone.items.map(item => [item.id, itemSignature(item)]));
      const known = state.knownItemSignaturesByZone[zone.id];
      const newIds = newItemIds(zone.id);
      if (detectNewItems) {
        for (const [itemId, signature] of current) {
          if (!(known instanceof Map) || known.get(itemId) !== signature) {
            newIds.add(itemId);
          }
        }
      }
      for (const itemId of newIds) {
        if (!current.has(itemId)) newIds.delete(itemId);
      }
      state.knownItemSignaturesByZone[zone.id] = current;
      state.newItemIdsByZone[zone.id] = newIds;
    }
    for (const zoneId of Object.keys(state.knownItemSignaturesByZone)) {
      if (!currentZoneIds.has(zoneId)) {
        delete state.knownItemSignaturesByZone[zoneId];
        delete state.newItemIdsByZone[zoneId];
      }
    }
  }

  function rememberItem(zoneId, item) {
    const known = state.knownItemSignaturesByZone[zoneId] instanceof Map
      ? state.knownItemSignaturesByZone[zoneId]
      : new Map();
    known.set(item.id, itemSignature(item));
    state.knownItemSignaturesByZone[zoneId] = known;
    clearNewItems(zoneId, [item.id]);
  }

  function createNewBadge() {
    const badge = document.createElement("span");
    badge.className = "new-badge";
    badge.textContent = "NEW";
    badge.setAttribute("aria-hidden", "true");
    return badge;
  }

  function newAccessibleSuffix(zoneId, itemId) {
    return isNewItem(zoneId, itemId) ? ", new" : "";
  }

  function selectedItemIds(zoneId) {
    const selected = state.selectedItemsByZone[zoneId];
    return selected instanceof Set ? selected : new Set();
  }

  function selectedItems(zone) {
    const selected = selectedItemIds(zone.id);
    return zone.items.filter(item => selected.has(item.id));
  }

  function selectHistoryItem(zone, itemId, event) {
    const ids = zone.items.map(item => item.id);
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
    clearNewItems(zone.id, next);
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

  function downloadArchive(zone, items, event) {
    if (zone.busy || state.batchBusyZoneIds.has(zone.id)) {
      toast("This zone is busy; try again shortly", "error");
      return;
    }
    if (zone.allow_zip_download === false) {
      toast("ZIP downloads are disabled for this zone", "error");
      return;
    }
    if (state.maxArchiveFiles !== null && items.length > state.maxArchiveFiles) {
      event.stopPropagation();
      toast(`ZIP downloads allow a maximum of ${state.maxArchiveFiles} files; ${items.length} selected`, "error");
      return;
    }
    const target = `pb-archive-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const frame = document.createElement("iframe");
    frame.name = target;
    frame.hidden = true;
    const form = document.createElement("form");
    form.method = "post";
    form.action = appPath(`/api/zones/${encodeURIComponent(zone.id)}/items/archive`);
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
        `/api/zones/${encodeURIComponent(zone.id)}/items/batch-delete`,
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

  async function transferSelected(sourceZone, targetZoneId, items, mode) {
    const targetZone = state.zones.find(zone => zone.id === targetZoneId);
    if (!targetZone || targetZone.id === sourceZone.id || !items.length) return;
    if (
      sourceZone.busy
      || targetZone.busy
      || state.batchBusyZoneIds.has(sourceZone.id)
      || state.batchBusyZoneIds.has(targetZone.id)
    ) {
      toast("One of these zones is busy; try again shortly", "error");
      return;
    }
    state.batchBusyZoneIds.add(sourceZone.id);
    state.batchBusyZoneIds.add(targetZone.id);
    renderAll();
    try {
      const result = await apiWithZoneRetry("/api/transfers?schema=items", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          source_zone: sourceZone.id,
          target_zone: targetZone.id,
          filenames: items.map(item => item.filename),
        }),
      });
      await refresh();
      if (mode === "move") {
        state.selectedItemsByZone[sourceZone.id] = new Set();
        state.selectionAnchorByZone[sourceZone.id] = null;
      }
      const transferred = Array.isArray(result.transferred) ? result.transferred : [];
      const failed = Array.isArray(result.failed) ? result.failed : [];
      const verb = mode === "copy" ? "copied" : "moved";
      const itemLabel = transferred.length === 1 ? "file" : "files";
      const retentionWarning = retentionWarningMessage(
        Array.isArray(result.retention_deleted) ? result.retention_deleted.length : 0,
      );
      const suffix = retentionWarning ? `; ${retentionWarning}` : "";
      if (failed.length) {
        toast(
          `${transferred.length} ${itemLabel} ${verb} to ${targetZone.label}, ${failed.length} failed${suffix}`,
          "error",
        );
      } else {
        toast(
          `${transferred.length} ${itemLabel} ${verb} to ${targetZone.label}${suffix}`,
          retentionWarning ? "warning" : "info",
        );
      }
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.batchBusyZoneIds.delete(sourceZone.id);
      state.batchBusyZoneIds.delete(targetZone.id);
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

  function renderTransferControls(zone, items) {
    if (!items.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "transfer-actions";
    const target = document.createElement("select");
    target.className = "transfer-target";
    target.setAttribute("aria-label", `Destination for ${items.length} selected files`);
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose destination";
    target.appendChild(placeholder);
    const destinations = state.zones.filter(candidate => candidate.id !== zone.id);
    for (const destination of destinations) {
      const option = document.createElement("option");
      option.value = destination.id;
      option.textContent = destination.label;
      target.appendChild(option);
    }
    const busy = zone.busy || state.batchBusyZoneIds.has(zone.id);
    const run = mode => {
      if (!target.value) {
        target.focus();
        toast("Choose a destination zone first", "error");
        return;
      }
      transferSelected(zone, target.value, items, mode);
    };
    const move = document.createElement("button");
    move.type = "button";
    move.className = "transfer-btn move-btn";
    move.textContent = "Move";
    move.setAttribute("aria-label", `Move ${items.length} selected files`);
    move.disabled = busy || !destinations.length;
    move.addEventListener("click", () => run("move"));
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "transfer-btn copy-transfer-btn";
    copy.textContent = "Copy";
    copy.setAttribute("aria-label", `Copy ${items.length} selected files`);
    copy.disabled = busy || !destinations.length;
    copy.addEventListener("click", () => run("copy"));
    wrap.append(target, move, copy);
    return wrap;
  }

  function renderBulkActions(zone) {
    const items = selectedItems(zone);
    if (!items.length) return null;
    const actions = document.createElement("div");
    actions.className = "bulk-actions";
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", "Selected files");
    if (items.length > 1) {
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
      archive.addEventListener("click", event => downloadArchive(zone, items, event));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete-btn";
      remove.textContent = "Delete selected";
      remove.setAttribute("aria-label", `Delete ${items.length} selected files`);
      remove.addEventListener("click", () => deleteSelected(zone, items));
      actions.append(summary, copy, archive, remove);
    }
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "ghost-btn";
    clear.textContent = "Clear selection";
    clear.addEventListener("click", () => {
      state.selectedItemsByZone[zone.id] = new Set();
      state.selectionAnchorByZone[zone.id] = null;
      rerenderZone(zone.id);
    });
    actions.append(renderTransferControls(zone, items), clear);
    const busy = zone.busy || state.batchBusyZoneIds.has(zone.id);
    if (items.length > 1) {
      for (const control of actions.querySelectorAll(".copy-btn, .download-btn, .delete-btn")) {
        control.disabled = busy;
        if (busy) control.title = "This zone is busy";
      }
    }
    return actions;
  }

  function chooseFiles(zoneId) {
    if (!filePicker) return;
    filePicker.value = "";
    filePicker.dataset.zone = zoneId;
    filePicker.click();
  }

  function renderZoneCapacity(zone) {
    const wrap = document.createElement("div");
    wrap.className = "zone-capacity";
    const count = document.createElement("button");
    count.type = "button";
    count.className = "zone-count";
    const limit = zone.retain;
    const retention = limit == null ? "" : ` / ${limit}`;
    count.textContent = `${zone.items.length}${retention}`;
    count.setAttribute(
      "aria-label",
      `${zone.items.length} files in ${zone.label}; show upload details`,
    );
    count.setAttribute("aria-expanded", "false");

    const panel = document.createElement("div");
    panel.className = "zone-capacity-panel";
    panel.hidden = true;
    const uploadLimit = document.createElement("p");
    uploadLimit.textContent = zone.upload_limit_bytes == null
      ? "Maximum upload: unavailable"
      : `Maximum upload: ${fmtBytes(zone.upload_limit_bytes)}`;
    const retentionInfo = document.createElement("p");
    retentionInfo.textContent = limit == null
      ? "Retention: unlimited"
      : `Retention: ${limit} files`;
    panel.append(uploadLimit, retentionInfo);
    wrap.append(count, panel);

    let hovering = false;
    let focused = false;
    let clickedOpen = false;
    const sync = () => {
      const visible = hovering || focused || clickedOpen;
      panel.hidden = !visible;
      count.setAttribute("aria-expanded", String(visible));
    };
    wrap.addEventListener("pointerenter", event => {
      if (event.pointerType !== "touch") {
        hovering = true;
        sync();
      }
    });
    wrap.addEventListener("pointerleave", event => {
      if (event.pointerType !== "touch") {
        hovering = false;
        sync();
      }
    });
    count.addEventListener("focus", () => {
      focused = true;
      sync();
    });
    count.addEventListener("blur", () => {
      focused = false;
      sync();
    });
    count.addEventListener("click", event => {
      event.stopPropagation();
      clickedOpen = !clickedOpen;
      if (!clickedOpen) count.blur();
      sync();
    });
    return wrap;
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
    select.setAttribute(
      "aria-label",
      `Select zone ${zone.label}${hasNewItems(zone.id) ? ", new files available" : ""}`,
    );
    select.innerHTML =
      '<span class="zone-marker" aria-hidden="true"></span>' +
      '<span class="zone-label"></span>';
    const zoneLabel = select.querySelector(".zone-label");
    zoneLabel.textContent = zone.label;
    if (hasNewItems(zone.id)) {
      const badge = createNewBadge();
      badge.classList.add("zone-new-badge");
      zoneLabel.appendChild(badge);
    }
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
    head.append(select, renderZoneCapacity(zone), uploadButton);
    el.appendChild(head);

    if (zone.busy) {
      const busy = document.createElement("div");
      busy.className = "zone-lock";
      busy.setAttribute("role", "status");
      busy.textContent = "Zone busy; another operation is using it";
      el.appendChild(busy);
    }

    if (zone.items.length === 0) {
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
      el.appendChild(renderThumbs(zone.id, zone.items, selected.id, selectedItemIds(zone.id)));
    }
    return el;
  }

  function selectedItem(zone) {
    return zone.items.find(item => item.id === state.selectedByZone[zone.id]) || zone.items[0];
  }

  function itemForControl(control) {
    const zoneEl = control.closest(".zone");
    if (!zoneEl) return null;
    const zone = state.zones.find(item => item.id === zoneEl.dataset.zone);
    if (!zone) return null;
    const itemId = control.closest("[data-item-id]")?.dataset.itemId;
    return zone.items.find(item => item.id === itemId) || selectedItem(zone);
  }

  function zoneForControl(control) {
    return control.closest(".zone")?.dataset.zone || null;
  }

  function itemMeta(zoneId, item) {
    const meta = document.createElement("div");
    meta.className = "meta";
    const fname = document.createElement("code");
    fname.className = "fname";
    fname.textContent = item.filename;
    const name = document.createElement("div");
    name.className = "meta-name";
    name.appendChild(fname);
    if (isNewItem(zoneId, item.id)) name.appendChild(createNewBadge());
    const comment = item.comment ? document.createElement("p") : null;
    if (comment) {
      comment.className = "comment-text";
      comment.textContent = item.comment;
    }
    const ref = state.showFullPath ? document.createElement("code") : null;
    if (ref) {
      ref.className = "ref";
      ref.title = item.reference;
      ref.textContent = item.reference;
    }
    const dims = document.createElement("span");
    dims.className = "dims";
    const sizeInfo = `${fmtBytes(item.size)} · ${fmtTime(item.created_at)}`;
    dims.textContent = item.kind === "image" && item.width != null && item.height != null
      ? `${item.width}×${item.height} · ${sizeInfo}`
      : `${kindLabel(item.kind)} · ${sizeInfo}`;
    meta.append(name);
    if (comment) meta.appendChild(comment);
    if (ref) meta.appendChild(ref);
    meta.appendChild(dims);
    return meta;
  }

  function saveComment(zoneId, itemId, comment, form) {
    const submit = form.querySelector("button[type='submit']");
    if (submit) submit.disabled = true;
    return api(
      `/api/zones/${encodeURIComponent(zoneId)}/items/${encodeURIComponent(itemId)}/comment`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment }),
      },
    ).then(updated => {
      const zone = state.zones.find(item => item.id === zoneId);
      const index = zone ? zone.items.findIndex(item => item.id === itemId) : -1;
      if (zone && index >= 0) zone.items[index] = Object.assign({}, zone.items[index], updated);
      rerenderZone(zoneId);
      toast("Comment saved");
    }).catch(err => {
      toast(err.message, "error");
      if (submit) submit.disabled = false;
    });
  }

  function renderCommentControl(zoneId, item) {
    const control = document.createElement("div");
    control.className = "comment-control";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "comment-btn";
    button.textContent = item.comment ? "Edit comment" : "Comment";
    button.setAttribute("aria-label", `${button.textContent} for ${item.filename}`);
    button.addEventListener("click", event => {
      event.stopPropagation();
      const form = document.createElement("form");
      form.className = "comment-editor";
      const input = document.createElement("textarea");
      input.value = item.comment || "";
      input.maxLength = 280;
      input.rows = 2;
      input.setAttribute("aria-label", `Comment for ${item.filename}`);
      const save = document.createElement("button");
      save.type = "submit";
      save.className = "comment-save-btn";
      save.textContent = "Save";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "ghost-btn";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => rerenderZone(zoneId));
      input.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        event.stopPropagation();
        if (!(event.ctrlKey || event.metaKey)) return;
        event.preventDefault();
        if (!save.disabled) form.requestSubmit();
      });
      form.addEventListener("submit", submitEvent => {
        submitEvent.preventDefault();
        saveComment(zoneId, item.id, input.value, form);
      });
      form.append(input, save, cancel);
      control.replaceChildren(form);
      input.focus();
    });
    control.appendChild(button);
    return control;
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
    right.appendChild(itemMeta(zoneId, item));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.dataset.ref = item.reference;
    btn.textContent = "Copy link";
    const copyStatus = document.createElement("span");
    copyStatus.className = "copy-status";
    copyStatus.hidden = true;
    copyStatus.setAttribute("role", "status");
    copyStatus.setAttribute("aria-live", "polite");
    const imageCopy = document.createElement("button");
    imageCopy.type = "button";
    imageCopy.className = "copy-image-btn";
    const actionLabel = contentActionLabel(item);
    imageCopy.textContent = actionLabel;
    imageCopy.setAttribute(
      "aria-label",
      `${actionLabel} to the clipboard`,
    );
    imageCopy.dataset.preview = item.content_url;
    imageCopy.dataset.kind = item.kind;
    imageCopy.dataset.filename = item.filename;
    imageCopy.dataset.mime = item.mime || "";
    const download = document.createElement("button");
    download.type = "button";
    download.className = "download-btn";
    download.textContent = downloadLabel(item.filename);
    download.setAttribute("aria-label", downloadLabel(item.filename));
    download.dataset.preview = item.content_url;
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
      zoom.dataset.preview = item.content_url;
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
    actions.append(btn, copyStatus);
    applyCopyFeedback(btn, copyFeedbackFor(zoneId, item.id));
    if (item.kind !== "binary") actions.append(imageCopy);
    actions.append(download, clear);
    if (zoom) actions.append(zoom);
    actions.append(renderCommentControl(zoneId, item));
    actions.append(del);
    actions.append(renderTransferControls(zone, [item]));
    right.appendChild(actions);
    if (item.kind === "image") {
      const img = document.createElement("img");
      img.className = "thumb-big";
      img.dataset.itemId = item.id;
      setPreviewSource(img, item.content_url);
      img.alt = `Latest image ${item.filename}`;
      img.title = itemDetails(zoneId, item);
      img.loading = "lazy";
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute(
        "aria-label",
        `Open preview of ${item.filename}${newAccessibleSuffix(zoneId, item.id)}`,
      );
      card.append(img, right);
    } else {
      const box = document.createElement("div");
      box.className = "file-box";
      box.dataset.itemId = item.id;
      box.textContent = fileTypeLabel(item.filename);
      box.title = itemDetails(zoneId, item);
      box.tabIndex = 0;
      box.setAttribute("role", "button");
      box.setAttribute(
        "aria-label",
        `${item.kind === "binary" ? "Download" : "Open preview of"} ${item.filename}`
          + newAccessibleSuffix(zoneId, item.id),
      );
      card.append(box, right);
    }
    return card;
  }

  function renderThumbs(zoneId, items, selectedId, selectedIds = new Set()) {
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
      wrap.draggable = true;
      if (item.id === selectedId) wrap.classList.add("selected");
      if (selectedIds.has(item.id)) wrap.classList.add("bulk-selected");
      wrap.setAttribute("aria-current", String(item.id === selectedId));
      wrap.setAttribute("aria-pressed", String(selectedIds.has(item.id)));
      wrap.setAttribute(
        "aria-label",
        `Select ${item.filename}${newAccessibleSuffix(zoneId, item.id)}`,
      );
      wrap.title = itemDetails(zoneId, item);
      wrap.dataset.itemId = item.id;
      if (item.kind === "image") {
        const img = document.createElement("img");
        img.className = "thumb";
        setPreviewSource(img, item.content_url);
        img.alt = item.filename;
        img.loading = "lazy";
        wrap.appendChild(img);
      } else {
        const label = document.createElement("span");
        label.className = "thumb-content";
        label.textContent = fileTypeLabel(item.filename);
        label.setAttribute("aria-hidden", "true");
        wrap.appendChild(label);
      }
      if (isNewItem(zoneId, item.id)) wrap.appendChild(createNewBadge());
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

  function activeTargetIsAvailable(zoneId = state.activeId) {
    if (!zoneId || !getVisibleZones().some(zone => zone.id === zoneId)) return false;
    const group = state.groups.find(item => item.name === state.activeGroupId);
    return groupLayout(group) !== "tab"
      || tabSidebarVisible(group)
      || state.openZoneIds.includes(zoneId);
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

  function loadOpenZones() {
    try {
      const raw = localStorage.getItem("pb.openZones");
      if (raw === null) {
        const activeZoneId = localStorage.getItem("pb.activeZone");
        state.openZoneIds = activeZoneId ? [activeZoneId] : [];
        return;
      }
      const stored = JSON.parse(raw);
      if (Array.isArray(stored)) {
        state.openZoneIds = stored.filter(zoneId => typeof zoneId === "string");
      }
    } catch (_) {}
  }

  function saveOpenZones() {
    try {
      localStorage.setItem("pb.openZones", JSON.stringify(state.openZoneIds));
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
    if (state.initialized) saveOpenZones();
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
    const filteredOpenZoneIds = state.openZoneIds.filter(zoneId => visibleIds.has(zoneId));
    state.openZoneIds = filteredOpenZoneIds;
    const group = state.groups.find(item => item.name === state.activeGroupId);
    const tabLayout = groupLayout(group) === "tab";
    const showTabSidebar = tabLayout && tabSidebarVisible(group);
    if (tabLayout && !showTabSidebar && state.activeId
        && !state.openZoneIds.includes(state.activeId)) {
      setActive(null);
    }
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
      if (hasNewItems(zone.id)) {
        const badge = createNewBadge();
        badge.classList.add("zone-new-badge");
        link.appendChild(badge);
      }
      link.setAttribute("aria-current", String(zone.id === state.activeId));
      link.setAttribute(
        "aria-label",
        `Select zone ${zone.label}${hasNewItems(zone.id) ? ", new files available" : ""}`,
      );
      link.setAttribute("aria-expanded", String(state.openZoneIds.includes(zone.id)));
      link.setAttribute("aria-pressed", String(state.openZoneIds.includes(zone.id)));
      link.setAttribute("aria-controls", "tab-zone-main");
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
    saveOpenZones();
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
    return isDialogOpen(pv) || isDialogOpen(replacementDialog) || isDialogOpen(accessDialog);
  }

  function dialogInvokerSelector(candidate) {
    if (candidate.id) return `#${CSS.escape(candidate.id)}`;
    const classes = [...candidate.classList]
      .map(className => `.${CSS.escape(className)}`)
      .join("");
    const target = `${candidate.tagName.toLowerCase()}${classes}`;
    const zone = candidate.closest(".zone");
    const item = candidate.closest("[data-item-id]");
    const zoneSelector = zone?.dataset.zone
      ? `.zone[data-zone="${CSS.escape(zone.dataset.zone)}"] `
      : "";
    if (item?.dataset.itemId) {
      const itemSelector = `[data-item-id="${CSS.escape(item.dataset.itemId)}"]`;
      return item === candidate
        ? `${zoneSelector}${itemSelector}${classes}`
        : `${zoneSelector}${itemSelector} ${target}`;
    }
    if (candidate.classList.contains("tab-zone-link") && candidate.dataset.zone) {
      return `.tab-zone-link[data-zone="${CSS.escape(candidate.dataset.zone)}"]`;
    }
    if (zone?.dataset.zone) return `${zoneSelector}${target}`;
    return null;
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
    if (candidate && candidate !== dialog) {
      dialogInvokers.set(dialog, {
        element: candidate,
        selector: dialogInvokerSelector(candidate),
      });
    }
  }

  function restoreDialogInvoker(dialog) {
    const record = dialogInvokers.get(dialog);
    dialogInvokers.delete(dialog);
    const invoker = record?.element?.isConnected
      ? record.element
      : record?.selector
        ? document.querySelector(record.selector)
        : null;
    if (invoker && !invoker.disabled) invoker.focus();
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
    saveOpenZones();
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
    saveOpenZones();
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
      saveOpenZones();
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
      const overview = await api("/api/zones?schema=items", { signal: controller.signal });
      const previousZones = new Map(state.zones.map(zone => [zone.id, zone]));
      const nextZones = [];
      for (const z of overview.zones) {
        const previous = previousZones.get(z.id);
        if (z.busy) {
          nextZones.push(Object.assign({}, z, {
            items: previous ? previous.items : [],
            busy: true,
          }));
          continue;
        }
        try {
          const items = Array.isArray(z.items)
            ? z.items
            : (await api(
              `/api/zones/${encodeURIComponent(z.id)}/items`,
              { signal: controller.signal },
            )).items;
          nextZones.push(Object.assign({}, z, { items, busy: false }));
        } catch (err) {
          if (err.code !== "zone_busy") throw err;
          nextZones.push(Object.assign({}, z, {
            items: previous ? previous.items : [],
            busy: true,
          }));
        }
      }
      if (generation !== refreshGeneration) return;

      updateNewItemState(nextZones);
      state.authEnabled = overview.auth_enabled !== false;
      state.showFullPath = overview.show_full_path !== false;
      state.maxArchiveFiles = overview.max_archive_files ?? null;
      logoutForm.hidden = !state.authEnabled;
      if (!state.authEnabled) {
        state.accessAdmin = false;
        state.accessAdminLoaded = true;
        if (accessButton) accessButton.hidden = true;
      } else if (!state.accessAdminLoaded || isDialogOpen(accessDialog)) {
        await loadTokenAdmin();
      }
      state.zones = nextZones;
      state.groups = overview.groups || [];
      for (const zoneId of Object.keys(state.selectedByZone)) {
        const zone = state.zones.find(z => z.id === zoneId);
        if (!zone || !zone.items.some(item => item.id === state.selectedByZone[zoneId])) {
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
        const validIds = new Set(zone.items.map(item => item.id));
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
      if (!state.initialized) loadOpenZones();
      renderGroups();
      renderAll();
      saveOpenZones();

      let stored = null;
      try { stored = localStorage.getItem("pb.activeZone"); } catch (_) {}
      const visibleZones = getVisibleZones();
      const candidate = state.activeId || stored
        || (!state.initialized && visibleZones.length === 1 ? visibleZones[0].id : null);
      if (candidate && activeTargetIsAvailable(candidate)) setActive(candidate);
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
    if (bootInFlight) return;
    bootInFlight = true;
    try {
      await refresh();
    } catch (err) {
      if (err && err.name === "AbortError") return;
      if (!silent) renderAll();
      if (err.message === "network unreachable") scheduleRetry();
      else if (!silent) toast(err.message, "error");
    } finally {
      bootInFlight = false;
    }
  }

  // ------------------------------------------------------------- upload

  function filesFromDataTransfer(dataTransfer) {
    if (!dataTransfer) return [];
    const files = dataTransfer.files ? [...dataTransfer.files] : [];
    if (files.length) return files;
    const items = dataTransfer.items ? [...dataTransfer.items] : [];
    return items
      .filter(item => item.kind === "file" && typeof item.getAsFile === "function")
      .map(item => item.getAsFile())
      .filter(Boolean);
  }

  function browserUploadCandidate(file, {
    creationMethod = "web_paste",
    preserveName = false,
  } = {}) {
    if (!file) return null;
    return { file, creationMethod, preserveName };
  }

  function browserUploadCandidates(files, options) {
    return [...files]
      .map(file => browserUploadCandidate(file, options))
      .filter(Boolean);
  }

  function browserUploadCandidatesFromDataTransfer(dataTransfer, options) {
    return browserUploadCandidates(filesFromDataTransfer(dataTransfer), options);
  }

  function recordUploadedItem(zoneId, item) {
    const zone = state.zones.find(z => z.id === zoneId);
    if (!zone) return;
    // A named drop can replace an existing stored name. Keep one history entry.
    zone.items = zone.items.filter(existing => existing.id !== item.id);
    zone.items.unshift(item);
    if (zone.items.length > zone.retain) {
      zone.items.length = zone.retain;
    }
    rememberItem(zoneId, item);
  }

  function countNewUploads(zoneId, candidates) {
    const zone = state.zones.find(item => item.id === zoneId);
    const knownNames = new Set(zone ? zone.items.map(item => item.filename) : []);
    let additions = 0;
    for (const candidate of candidates) {
      const file = candidate.file;
      const name = candidate.preserveName && file.name ? file.name : null;
      if (name && knownNames.has(name)) continue;
      additions += 1;
      if (name) knownNames.add(name);
    }
    return additions;
  }

  function confirmRetention(zoneId, incomingCount) {
    const zone = state.zones.find(item => item.id === zoneId);
    if (!zone || incomingCount <= 0) return true;
    const excess = zone.items.length + incomingCount - zone.retain;
    if (excess <= 0) return true;
    const itemLabel = excess === 1 ? "item" : "items";
    const uploadLabel = incomingCount === 1 ? "this upload" : `${incomingCount} uploads`;
    return window.confirm(
      `${zone.label} retains at most ${zone.retain} items. `
      + `${uploadLabel} will remove ${excess} oldest managed ${itemLabel}. Continue?`,
    );
  }

  function retentionWarningMessage(deletedCount) {
    if (!deletedCount) return "";
    const itemLabel = deletedCount === 1 ? "item was" : "items were";
    return `Zone is full; ${deletedCount} oldest managed ${itemLabel} removed`;
  }

  async function upload(
    zoneId,
    file,
    {
      preserveName = false,
      allowReplace = false,
      autoCopy = true,
      notify = true,
      warnCapacity = true,
      creationMethod = "web_paste",
    } = {},
  ) {
    if (!file) return;
    const zone = state.zones.find(item => item.id === zoneId);
    const zoneEl = grid.querySelector(`.zone[data-zone="${CSS.escape(zoneId)}"]`);
    if (zoneEl) zoneEl.classList.add("busy");
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      if (preserveName && !allowReplace && file.name && hasManagedName(zoneId, file.name)) {
        allowReplace = await askReplacement(file.name, zoneId);
        if (!allowReplace) return null;
      }
      if (
        warnCapacity
        && !confirmRetention(
          zoneId,
          countNewUploads(zoneId, [browserUploadCandidate(file, {
            creationMethod,
            preserveName,
          })]),
        )
      ) return null;
      const fd = new FormData();
      // Preserve the filename only for candidates that carry a named-file policy.
      fd.append("file", file, file.name || "clipboard");
      if (preserveName) fd.append("preserve_name", "1");
      if (allowReplace) fd.append("replace", "1");
      fd.append("creation_method", creationMethod);
      const item = await api(`/api/zones/${encodeURIComponent(zoneId)}/items`,
        { method: "POST", body: fd });
      refreshGeneration += 1;
      if (activeRefreshController) activeRefreshController.abort();
      recordUploadedItem(zoneId, item);
      state.selectedByZone[zoneId] = item.id;
      state.selectedItemsByZone[zoneId] = new Set([item.id]);
      state.selectionAnchorByZone[zoneId] = item.id;
      refreshUploadedZone(zoneId);
      const retentionWarning = retentionWarningMessage(
        Array.isArray(item.retention_deleted) ? item.retention_deleted.length : 0,
      );
      if (notify) {
        const contentLabel = item.kind === "image" ? "Image" : "Content";
        const uploaded = item.duplicate
          ? `${contentLabel} already present (${shortRef(item.reference)})`
          : `${contentLabel} uploaded (${shortRef(item.reference)})`;
        toast(retentionWarning ? `${retentionWarning}; ${uploaded}` : uploaded,
          retentionWarning ? "warning" : "info");
      }
      if (autoCopy) {
        // Best-effort automatic copy: report failures explicitly; the Copy link
        // button remains available.
        const copyAttempt = beginCopyAttempt(zoneId, item.id);
        writeClipboard(item.reference, { allowLegacyFallback: false }).then(ok => {
          setCopyFeedback(zoneId, item.id, ok ? "copied" : "attention", copyAttempt);
        });
      }
      return item;
    } catch (err) {
      if (err.status === 413) {
        if (notify) toast("Content is too large for this server", "error");
      }
      else if (err.status === 507) {
        if (notify) toast(
          "Not enough disk space for this upload",
          "error",
        );
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
            creationMethod,
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

  function uploadCandidate(zoneId, candidate, options = {}) {
    if (!candidate?.file) return null;
    return upload(zoneId, candidate.file, {
      ...options,
      preserveName: candidate.preserveName,
      creationMethod: candidate.creationMethod,
    });
  }

  async function uploadBatch(
    zoneId,
    candidates,
  ) {
    const batch = [...candidates].filter(candidate => candidate?.file);
    if (!batch.length) return;
    if (batch.length === 1) {
      await uploadCandidate(zoneId, batch[0]);
      return;
    }
    if (!confirmRetention(zoneId, countNewUploads(zoneId, batch))) return;
    state.batchBusyZoneIds.add(zoneId);
    renderAll();
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    const successful = [];
    let failed = 0;
    try {
      // Keep each request independent: one bad file must not cancel the rest,
      // and the normal single-upload size/memory limits still apply.
      for (const candidate of batch) {
        const item = await uploadCandidate(zoneId, candidate, {
          autoCopy: false,
          notify: false,
          warnCapacity: false,
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
      const retentionDeleted = successful.reduce(
        (count, item) => count + (
          Array.isArray(item.retention_deleted) ? item.retention_deleted.length : 0
        ),
        0,
      );
      const retentionWarning = retentionWarningMessage(retentionDeleted);
      if (successful.length && failed) {
        toast(
          `${successful.length} files uploaded, ${failed} failed`
            + (retentionWarning ? `; ${retentionWarning}` : ""),
          "error",
        );
      } else if (successful.length) {
        toast(
          `${successful.length} files uploaded`
            + (retentionWarning ? `; ${retentionWarning}` : ""),
          retentionWarning ? "warning" : "info",
        );
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

  function submitBrowserFiles(zoneId, files, options) {
    const candidates = browserUploadCandidates(files, options);
    if (!candidates.length) return null;
    return uploadBatch(zoneId, candidates);
  }

  function hasManagedName(zoneId, filename) {
    const zone = state.zones.find(z => z.id === zoneId);
    return Boolean(zone && zone.items.some(item => item.filename === filename));
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
    const backdrop = dialog === pv
      ? previewBackdrop
      : dialog === replacementDialog
        ? replacementBackdrop
        : accessBackdrop;
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
    const backdrop = dialog === pv
      ? previewBackdrop
      : dialog === replacementDialog
        ? replacementBackdrop
        : accessBackdrop;
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
    if (activeTargetIsAvailable()) {
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

  async function deleteItem(zoneId, filename) {
    if (!window.confirm(`Delete ${filename} from the disk?`)) return;
    refreshGeneration += 1;
    if (activeRefreshController) activeRefreshController.abort();
    try {
      await api(`/api/zones/${encodeURIComponent(zoneId)}/items/${encodeURIComponent(filename)}`,
        { method: "DELETE" });
      const zone = state.zones.find(z => z.id === zoneId);
      if (zone) {
        zone.items = zone.items.filter(item => item.id !== filename);
        clearNewItems(zoneId, [filename]);
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

  function isPasteShortcut(event) {
    return (event.ctrlKey || event.metaKey)
      && !event.altKey
      && event.key.toLowerCase() === "v";
  }

  document.addEventListener("keydown", (event) => {
    if (!isPasteShortcut(event)) return;
    if (event.repeat) {
      event.preventDefault();
      return;
    }
    pasteShortcutActive = true;
    pasteShortcutHandled = false;
  });

  document.addEventListener("keyup", (event) => {
    if (event.key === "Control" || event.key === "Meta" || event.key.toLowerCase() === "v") {
      pasteShortcutActive = false;
      pasteShortcutHandled = false;
    }
  });

  window.addEventListener("blur", () => {
    pasteShortcutActive = false;
    pasteShortcutHandled = false;
  });

  if (filePicker) {
    filePicker.addEventListener("change", () => {
      const zoneId = filePicker.dataset.zone;
      const files = filePicker.files ? [...filePicker.files] : [];
      delete filePicker.dataset.zone;
      if (zoneId && files.length) {
        submitBrowserFiles(zoneId, files, {
          creationMethod: "web_mouse_drop",
          preserveName: true,
        });
      }
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
    if (pasteShortcutActive) {
      if (pasteShortcutHandled) {
        event.preventDefault();
        return;
      }
      pasteShortcutHandled = true;
    }
    const clipboardData = event.clipboardData;
    const items = clipboardData && clipboardData.items;
    const files = filesFromDataTransfer(clipboardData);
    if (!items && !files.length) return;
    let imageItem = null;
    let hasPlainText = false;
    for (const item of items || []) {
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
    if (files.length === 1 && imageItem) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      uploadCandidate(zoneId, browserUploadCandidate(files[0], {
        creationMethod: "web_paste",
      }));
      return;
    }
    if (files.length) {
      event.preventDefault();
      const zoneId = requireActiveZone();
      if (!zoneId) return;
      submitBrowserFiles(zoneId, files, {
        creationMethod: "web_paste",
        preserveName: true,
      });
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
      uploadCandidate(zoneId, browserUploadCandidate(file));
      return;
    }
    // Prefer plain text, but do not let an empty flavor hide another usable one.
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
          uploadCandidate(zoneId, browserUploadCandidate(blob));
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
      copyLink(copyBtn.dataset.ref, copyBtn);
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
        openPreview(item.content_url, item.reference, item.filename, zoneId, item.id);
      } else if (item) openContentPreview(item, zoneId);
      return;
    }
    const deleteBtn = event.target.closest(".delete-btn");
    if (deleteBtn && deleteBtn.dataset.filename) {
      deleteItem(deleteBtn.dataset.zone, deleteBtn.dataset.filename);
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
        openPreview(item.content_url, item.reference, item.filename, zoneId, item.id);
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
        openPreview(item.content_url, item.reference, item.filename, zoneId, item.id);
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

  function readInternalTransfer(dataTransfer) {
    if (!dataTransfer || !Array.from(dataTransfer.types || []).includes(INTERNAL_TRANSFER_MIME)) {
      return null;
    }
    try {
      const payload = JSON.parse(dataTransfer.getData(INTERNAL_TRANSFER_MIME));
      if (
        !payload
        || typeof payload.source_zone !== "string"
        || !Array.isArray(payload.filenames)
        || !payload.filenames.length
        || !payload.filenames.every(filename => typeof filename === "string")
      ) return null;
      return payload;
    } catch (_) {
      return null;
    }
  }

  grid.addEventListener("dragstart", (event) => {
    const thumbWrap = event.target.closest(".thumb-wrap");
    if (!thumbWrap || !event.dataTransfer) return;
    const zone = state.zones.find(item => item.id === thumbWrap.closest(".zone")?.dataset.zone);
    const item = zone?.items.find(candidate => candidate.id === thumbWrap.dataset.itemId);
    if (!zone || !item) return;
    const selected = selectedItems(zone);
    const items = selected.some(candidate => candidate.id === item.id) ? selected : [item];
    event.dataTransfer.setData(
      INTERNAL_TRANSFER_MIME,
      JSON.stringify({
        source_zone: zone.id,
        filenames: items.map(candidate => candidate.filename),
      }),
    );
    event.dataTransfer.effectAllowed = "copyMove";
    thumbWrap.classList.add("dragging-item");
  });
  grid.addEventListener("dragend", (event) => {
    event.target.closest(".thumb-wrap")?.classList.remove("dragging-item");
  });
  grid.addEventListener("dragover", (event) => {
    const zoneTarget = event.target.closest(".zone, .tab-zone-link");
    if (!zoneTarget) return;
    const internal = readInternalTransfer(event.dataTransfer);
    if (internal && internal.source_zone === zoneTarget.dataset.zone) {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "none";
      zoneTarget.classList.add("dragging");
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = internal && (event.ctrlKey || event.metaKey)
        ? "copy"
        : internal ? "move" : "copy";
    }
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
    const internal = readInternalTransfer(event.dataTransfer);
    if (internal) {
      const sourceZone = state.zones.find(zone => zone.id === internal.source_zone);
      if (!sourceZone || sourceZone.id === zoneTarget.dataset.zone) {
        toast("Choose a different destination zone", "error");
        return;
      }
      const filenames = [...new Set(internal.filenames)];
      if (!filenames.length) {
        toast("The dragged selection is empty", "error");
        return;
      }
      const items = filenames.map(filename => ({ filename }));
      transferSelected(
        sourceZone,
        zoneTarget.dataset.zone,
        items,
        event.ctrlKey || event.metaKey ? "copy" : "move",
      );
      return;
    }
    const zone = state.zones.find(item => item.id === zoneTarget.dataset.zone);
    if (zone?.busy || state.batchBusyZoneIds.has(zoneTarget.dataset.zone)) {
      toast("This zone is busy; try again shortly", "error");
      return;
    }
    const candidates = browserUploadCandidatesFromDataTransfer(event.dataTransfer, {
      creationMethod: "web_mouse_drop",
      preserveName: true,
    });
    if (!candidates.length) {
      toast("The drop does not contain a file", "error");
      return;
    }
    uploadBatch(zoneTarget.dataset.zone, candidates);
  });

  document.addEventListener("keydown", (event) => {
    const fallbackDialog = [pv, replacementDialog, accessDialog]
      .find(dialog => dialog.classList.contains("dialog-fallback"));
    if (fallbackDialog) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (fallbackDialog === pv) closePreview();
        else if (fallbackDialog === replacementDialog) closeReplacementPrompt(false);
        else closeAccessPanel();
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
      if (zone && zone.items.length) {
        const item = selectedItem(zone);
        copyLink(
          item.reference,
          copyButtonForItem(zone.id, item.id),
          true,
          { zoneId: zone.id, itemId: item.id },
        );
      }
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

  function setPreviewCopyTarget(zoneId, itemId) {
    if (zoneId && itemId) {
      pvCopy.dataset.zone = zoneId;
      pvCopy.dataset.itemId = itemId;
    } else {
      delete pvCopy.dataset.zone;
      delete pvCopy.dataset.itemId;
    }
  }

  function openPreview(url, reference, filename, zoneId, itemId) {
    invalidatePreviewLoad();
    setRawHtmlButton(false);
    const storedFilename = filename || decodeURIComponent(url.split("/").pop());
    setPreviewSource(pvImg, url);
    pvImg.hidden = false;
    pvText.hidden = true;
    pvRef.textContent = reference;
    setPreviewCopyTarget(zoneId, itemId);
    pvRef.hidden = !state.showFullPath;
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
    setPreviewCopyTarget(zoneId, item.id);
    pvRef.hidden = !state.showFullPath;
    pvDownload.textContent = downloadLabel(item.filename);
    pvDownload.setAttribute("aria-label", downloadLabel(item.filename));
    pvDownload.dataset.preview = item.content_url;
    pvDownload.dataset.filename = item.filename;
    pvDelete.dataset.zone = zoneId || "";
    pvDelete.dataset.filename = item.filename;
    pvCopyImage.dataset.preview = item.content_url;
    pvCopyImage.dataset.kind = item.kind;
    pvCopyImage.dataset.mime = item.mime || "";
    setPreviewItemLabels(item.filename);
    if (item.kind === "text") {
      const controller = new AbortController();
      activePreviewController = controller;
      try {
        const response = await fetchPreview(item.content_url, {
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
        setRawHtmlButton(Boolean(htmlInspection && htmlInspection.changed), item.content_url);
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
    // Binary content: direct download (Content-Disposition: attachment).
    downloadContent(item.content_url, item.filename);
  }
  function closePreview() {
    invalidatePreviewLoad();
    setRawHtmlButton(false);
    setPreviewCopyTarget(null, null);
    if (!closeDialog(pv)) {
      setPreviewSource(pvImg, "");
      pvText.hidden = true;
      pvImg.hidden = false;
      delete pvDelete.dataset.zone;
      restoreDialogInvoker(pv);
    }
  }
  pvCopy.addEventListener("click", () => {
    const button = pvCopy.dataset.zone && pvCopy.dataset.itemId
      ? copyButtonForItem(pvCopy.dataset.zone, pvCopy.dataset.itemId)
      : null;
    copyLink(
      pvRef.textContent,
      button,
      true,
      { zoneId: pvCopy.dataset.zone, itemId: pvCopy.dataset.itemId },
    );
  });
  pvCopyImage.addEventListener("click", () => copyContent(pvCopyImage.dataset.kind, pvCopyImage.dataset.preview, pvCopyImage.dataset.mime));
  pvDownload.addEventListener("click", () => downloadContent(pvDownload.dataset.preview, pvDownload.dataset.filename));
  pvClear.addEventListener("click", clearClipboard);
  pvDelete.addEventListener("click", () => {
    const zoneId = pvDelete.dataset.zone;
    const filename = pvDelete.dataset.filename;
    const zone = state.zones.find(
      z => z.id === zoneId && z.items.some(i => i.id === filename),
    );
    if (zone) {
      closePreview();
      deleteItem(zone.id, filename);
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
  setInterval(() => { if (document.visibilityState === "visible") boot(true); }, REFRESH_INTERVAL_MS);

  boot();
})();
