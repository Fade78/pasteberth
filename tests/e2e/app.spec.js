const { test, expect } = require("@playwright/test");

const ONE_PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNwaDgAAAKEAYEml6crAAAAAElFTkSuQmCC";

test.describe.configure({ mode: "serial" });

async function resetServer(request) {
  const response = await request.post("/__e2e/reset");
  expect(response.status()).toBe(204);
}

async function openApp(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".zone")).toHaveCount(2);
  await expect(page.locator("#status-text")).toHaveText("online");
}

async function dispatchPaste(page) {
  await page.evaluate((base64) => {
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const file = new File([bytes], "clipboard.png", { type: "image/png" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, ONE_PIXEL_PNG);
}

async function dispatchTextPaste(page, text, type = "text/plain") {
  await page.evaluate(({ text, type }) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.setData(type, text);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, { text, type });
}

async function dispatchTextPasteFlavors(page, flavors) {
  await page.evaluate((flavors) => {
    const dataTransfer = new DataTransfer();
    for (const [type, text] of flavors) dataTransfer.setData(type, text);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, flavors);
}

async function dispatchTextDrop(page, selector, name, text) {
  await page.locator(selector).evaluate((element, payload) => {
    const file = new File([payload.text], payload.name, { type: "text/plain" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    });
    element.dispatchEvent(event);
  }, { name, text });
}

async function dispatchMixedPaste(page, text = "hello mixed world") {
  await page.evaluate((payload) => {
    const bytes = Uint8Array.from(atob(payload.base64), (char) => char.charCodeAt(0));
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File([bytes], "clipboard.png", { type: "image/png" }));
    dataTransfer.items.add(payload.text, "text/plain");
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, { base64: ONE_PIXEL_PNG, text });
}

async function dispatchCanvasPaste(page, type) {
  const payload = await page.evaluate(async (mime) => {
    const canvas = document.createElement("canvas");
    canvas.width = 2;
    canvas.height = 2;
    const context = canvas.getContext("2d");
    context.fillStyle = "#d56b36";
    context.fillRect(0, 0, 2, 2);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, mime));
    if (!blob) throw new Error(`could not encode ${mime}`);
    const buffer = await blob.arrayBuffer();
    return {
      type: blob.type,
      base64: btoa(String.fromCharCode(...new Uint8Array(buffer))),
    };
  }, type);
  await page.evaluate(({ base64, type }) => {
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const file = new File([bytes], "clipboard-image", { type });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, payload);
}

async function dispatchDrop(page, selector) {
  await page.locator(selector).evaluate((element, base64) => {
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const file = new File([bytes], "dropped.png", { type: "image/png" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    });
    element.dispatchEvent(event);
  }, ONE_PIXEL_PNG);
}

async function dispatchMultiDrop(page, selector) {
  await page.locator(selector).evaluate((element, base64) => {
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const dataTransfer = new DataTransfer();
    for (const name of ["first.png", "second.png"]) {
      dataTransfer.items.add(new File([bytes], name, { type: "image/png" }));
    }
    const event = new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    });
    element.dispatchEvent(event);
  }, ONE_PIXEL_PNG);
}

async function dispatchBinaryDrop(page, selector, name = "archive.zip") {
  await page.locator(selector).evaluate((element, name) => {
    const file = new File([new Uint8Array([0, 1, 2, 3])], name, {
      type: "application/zip",
    });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new DragEvent("drop", {
      bubbles: true,
      cancelable: true,
      dataTransfer,
    });
    element.dispatchEvent(event);
  }, name);
}

test.beforeEach(async ({ request }) => {
  await resetServer(request);
});

test("charge les zones et expose une sélection clavier accessible", async ({ page }) => {
  await openApp(page);
  const brandIcon = page.locator(".brand-icon");
  await expect(brandIcon).toHaveAttribute("src", "/static/favicon.svg");
  expect(await brandIcon.evaluate(icon => icon.complete && icon.naturalWidth > 0)).toBe(true);

  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await expect(defaultZone.getByRole("button", { name: "Select zone Default" }))
    .toHaveAttribute("aria-current", "false");

  await page.locator("body").press("2");
  await expect(secondary.getByRole("button", { name: "Select zone Secondary" }))
    .toHaveAttribute("aria-current", "true");
  await expect(secondary).toHaveClass(/active/);
  await expect(defaultZone).not.toHaveClass(/active/);
});

test("passe en mode compact à 500px", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 525 });
  await openApp(page);

  await page.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);
  await expect(page.locator('[data-zone="default"] .latest')).toBeVisible();
  await expect(page.locator(".grid")).toHaveCSS("gap", "8px");
  await expect(page.locator(".zone").first()).toHaveCSS("min-height", "0px");
  const widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBe(widths.client);
});

test("refuse un collage sans zone active", async ({ page }) => {
  await openApp(page);
  const before = await page.locator(".latest").count();

  await dispatchPaste(page);

  await expect(page.locator("#toast")).toContainText("Choose a zone first");
  await expect.poll(() => page.locator(".latest").count()).toBe(before);
});

test("les contrôles d'action ne changent pas la cible de collage", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await defaultZone.locator(".zone-select").click();
  await dispatchPaste(page);
  await secondary.locator(".zone-select").click();
  await defaultZone.locator(".copy-btn").focus();
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-current", "true");

  const pasteRequest = page.waitForRequest(request => (
    request.method() === "POST" && request.url().includes("/api/zones/secondary/images")
  ));
  await dispatchPaste(page);
  await pasteRequest;
  await expect(secondary.locator(".latest")).toBeVisible();
  await expect(defaultZone.locator(".zone-select")).toHaveAttribute("aria-current", "false");
});

test("les dialogues isolent le collage et les raccourcis globaux", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await defaultZone.locator(".zone-select").click();
  await dispatchPaste(page);
  await defaultZone.locator(".thumb-big").press("Enter");
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv")).toHaveAttribute("aria-label", /Preview of/);
  await expect(page.locator("#pv-close")).toBeFocused();

  await page.keyboard.press("2");
  await page.keyboard.press("a");
  await page.keyboard.press("u");
  await page.keyboard.press("c");
  await dispatchPaste(page);
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
  await expect(defaultZone.locator(".zone-select")).toHaveAttribute("aria-current", "true");
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-current", "false");
  await page.locator("#pv-close").click();
  await expect(page.locator("#pv")).toBeHidden();
});

test("la confirmation de remplacement commence par Cancel et restaure le focus", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await dispatchBinaryDrop(page, ".zone[data-zone=\"default\"]");
  await defaultZone.locator(".zone-select").focus();
  await dispatchBinaryDrop(page, ".zone[data-zone=\"default\"]");
  await expect(page.locator("#replace")).toBeVisible();
  await expect(page.locator("#replace-cancel")).toBeFocused();
  await expect(page.locator("#replace-message")).toContainText("Default");
  await expect(page.locator("#replace")).toHaveAttribute(
    "aria-describedby",
    "replace-message replace-filename",
  );
  await page.keyboard.press("2");
  await page.keyboard.press("c");
  await dispatchPaste(page);
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
  await expect(defaultZone.locator(".zone-select")).toHaveAttribute("aria-current", "true");

  await page.keyboard.press("Enter");
  await expect(page.locator("#replace")).toBeHidden();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
  await expect(defaultZone.locator(".zone-select")).toBeFocused();
});

test("désélectionne la zone absente du groupe actif", async ({ page }) => {
  await openApp(page);
  await page.getByRole("button", { name: "Select zone Default" }).click();
  await page.locator('.group-tab[data-group="Secondary"]').click();

  await expect(page.locator(".zone")).toHaveCount(1);
  await expect(page.locator('[data-zone="secondary"] .zone-select'))
    .toHaveAttribute("aria-current", "false");

  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator('[data-zone="secondary"] .zone-select'))
    .toHaveAttribute("aria-current", "false");

  await page.locator('.group-tab[data-group="Secondary"]').click();
  await page.locator('[data-zone="secondary"] .zone-select').click();
  await page.getByRole("button", { name: "All" }).click();
  await expect(page.locator('[data-zone="secondary"] .zone-select'))
    .toHaveAttribute("aria-current", "false");

  let uploadSeen = false;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/zones/")) {
      uploadSeen = true;
    }
  });
  await dispatchPaste(page);
  await expect(page.locator("#toast")).toContainText("Choose a zone first");
  expect(uploadSeen).toBe(false);
});

test("le focus Tab sélectionne la première zone visible", async ({ page }) => {
  await openApp(page);
  await page.locator('.group-tab[data-group="Secondary"]').click();
  await page.getByRole("button", { name: "Group options" }).focus();
  await page.keyboard.press("Tab");
  await expect(page.locator('[data-zone="secondary"] .zone-select'))
    .toHaveAttribute("aria-current", "true");
});

test("conserve le focus sur le groupe après son changement au clavier", async ({ page }) => {
  await openApp(page);
  const secondaryTab = page.locator('.group-tab[data-group="Secondary"]');
  await secondaryTab.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator('.group-tab[data-group="Secondary"]')).toBeFocused();
});

test("le layout tab ouvre une zone et permet une sélection multiple au Shift-clic", async ({ page }) => {
  await openApp(page);
  await page.locator('.group-tab[data-group="Tabbed"]').click();
  await expect(page.locator(".grid")).toHaveClass(/tab-layout/);
  await expect(page.locator(".tab-zone-link")).toHaveCount(2);
  await expect(page.locator(".zone")).toHaveCount(0);

  const defaultLink = page.locator('.tab-zone-link[data-zone="default"]');
  const secondaryLink = page.locator('.tab-zone-link[data-zone="secondary"]');
  await defaultLink.focus();
  await page.keyboard.press("2");
  await expect(secondaryLink).toHaveAttribute("aria-current", "true");
  await page.keyboard.press("a");
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(2);
  await expect(defaultLink).toBeFocused();
  await expect(secondaryLink).toHaveAttribute("aria-current", "true");
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "a",
    ctrlKey: true,
    bubbles: true,
  })));
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(2);
  await page.evaluate(() => document.dispatchEvent(new KeyboardEvent("keydown", {
    key: "u",
    ctrlKey: true,
    bubbles: true,
  })));
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(2);
  await page.keyboard.press("u");
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
  await expect(defaultLink).toBeFocused();
  await expect(secondaryLink).toHaveAttribute("aria-current", "true");
  await defaultLink.click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(1);
  const zoneSelect = page.locator('.tab-zone-main .zone[data-zone="default"] .zone-select');
  await zoneSelect.focus();
  await page.keyboard.press("u");
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(1);
  await defaultLink.click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
  await secondaryLink.hover();
  await expect(secondaryLink).toHaveAttribute("aria-current", "true");
  const pasteRequest = page.waitForRequest(request => (
    request.method() === "POST" && request.url().includes("/api/zones/secondary/images")
  ));
  await dispatchPaste(page);
  await pasteRequest;
  await expect(page.locator('.tab-zone-main .zone[data-zone="secondary"]')).toHaveCount(1);
  await secondaryLink.click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
  const dropRequest = page.waitForRequest(request => (
    request.method() === "POST" && request.url().includes("/api/zones/secondary/images")
  ));
  await dispatchDrop(page, '.tab-zone-link[data-zone="secondary"]');
  await dropRequest;
  await expect(page.locator('.tab-zone-main .zone[data-zone="secondary"]')).toHaveCount(1);
  await defaultLink.click();
  await expect(page.locator('.tab-zone-main .zone[data-zone="default"]')).toHaveCount(1);
  await page.locator('.group-tab[data-group="All"]').click();
  await expect(page.locator(".grid")).not.toHaveClass(/tab-layout/);
  await page.locator('.group-tab[data-group="Tabbed"]').click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
  await secondaryLink.click();
  await expect(page.locator('.tab-zone-main .zone[data-zone="secondary"]')).toHaveCount(1);
  await expect(page.locator('.tab-zone-main .zone[data-zone="default"]')).toHaveCount(0);

  await defaultLink.click({ modifiers: ["Shift"] });
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(2);
  await secondaryLink.click();
  await expect(page.locator('.tab-zone-main .zone[data-zone="secondary"]')).toHaveCount(1);
  await secondaryLink.click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
  await defaultLink.click({ modifiers: ["Shift"] });
  await secondaryLink.click({ modifiers: ["Shift"] });
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(2);
  await secondaryLink.click({ modifiers: ["Shift"] });
  await expect(page.locator('.tab-zone-main .zone[data-zone="default"]')).toHaveCount(1);
  await expect(page.locator('.tab-zone-main .zone[data-zone="secondary"]')).toHaveCount(0);
  await defaultLink.click();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);

  await page.getByRole("button", { name: "Group options" }).click();
  await page.getByLabel("Layout").selectOption("area");
  await expect(page.locator(".tab-zone-list")).toHaveCount(0);
  await expect(page.locator(".zone")).toHaveCount(2);
  await page.getByRole("button", { name: "Group options" }).click();
  await page.getByLabel("Layout").selectOption("tab");
  await expect(page.locator(".tab-zone-list")).toBeVisible();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#status-text")).toHaveText("online");
  await expect(page.locator(".tab-zone-list")).toBeVisible();
  await expect(page.locator(".tab-zone-main .zone")).toHaveCount(0);
});

test("les options filtrent les groupes vides et les compteurs", async ({ page }) => {
  await openApp(page);
  await expect(page.locator('.group-tab[data-group="Empty"]')).toBeVisible();

  const options = page.getByRole("button", { name: "Group options" });
  await options.click();
  await expect(options).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("checkbox", { name: "Hide empty groups" })).toBeFocused();
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator(".group-options-dropdown")).toHaveCount(0);
  await expect(options).toHaveAttribute("aria-expanded", "false");

  await options.click();
  await expect(options).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(options).toHaveAttribute("aria-expanded", "false");
  await expect(options).toBeFocused();

  await options.click();
  await page.getByRole("checkbox", { name: "Hide empty groups" }).click();
  await expect(page.locator('.group-tab[data-group="Empty"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Group options" })).toBeFocused();

  await page.getByRole("button", { name: "Group options" }).click();
  await page.getByRole("checkbox", { name: "Show zone counts" }).click();
  await expect(page.getByRole("button", { name: "All" })).toHaveText("All");
  await expect(page.getByRole("button", { name: "Group options" })).toBeFocused();
});

test("sélectionne automatiquement l'unique zone visible", async ({ page }) => {
  await page.route("**/api/zones", async (route) => {
    const response = await route.fetch();
    const overview = await response.json();
    overview.zones = overview.zones.filter((zone) => zone.id === "default");
    await route.fulfill({ response, json: overview });
  });
  await page.addInitScript(() => localStorage.removeItem("pb.activeZone"));
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#status-text")).toHaveText("online");
  await expect(page.locator('[data-zone="default"] .zone-select'))
    .toHaveAttribute("aria-current", "true");
});

test("sans groupes configurés, conserve toutes les zones sans barre de groupes", async ({ page }) => {
  await page.route("**/api/zones", async (route) => {
    const response = await route.fetch();
    const overview = await response.json();
    overview.groups = [];
    await route.fulfill({ response, json: overview });
  });
  await openApp(page);
  await expect(page.locator("#group-tabs")).toBeHidden();
});

test("ne réaffiche pas les zones quand tous les groupes sont masqués", async ({ page }) => {
  await page.route("**/api/zones", async (route) => {
    const response = await route.fetch();
    const overview = await response.json();
    overview.groups = [{
      name: "Empty",
      pattern: ["^missing-.*$"],
      zone_ids: [],
      zone_count: 0,
      hide_empty: false,
      show_count: true,
    }];
    await route.fulfill({ response, json: overview });
  });
  await page.addInitScript(() => localStorage.setItem("pb.hideEmptyGroups", "true"));
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator("#status-text")).toHaveText("online");
  await expect(page.locator(".zone")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Group options" })).toBeVisible();
});

test("colle une image et ouvre son aperçu au clavier", async ({ page }) => {
  await openApp(page);
  await page.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchPaste(page);

  const defaultZone = page.locator('[data-zone="default"]');
  await expect(defaultZone.locator(".latest")).toBeVisible();
  await expect(defaultZone.locator(".fname")).toHaveText(/\.png$/);
  const reference = await defaultZone.locator(".ref").textContent();
  expect(reference).toMatch(/^@.*\.png$/);
  await expect(defaultZone.locator(".thumb-big")).toHaveAttribute(
    "src",
    /\/previews\/default\//,
  );
  await expect(defaultZone.locator(".history-index")).toBeVisible();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
  await expect(defaultZone.locator(".thumb-wrap")).toHaveAttribute("aria-current", "true");
  await defaultZone.getByRole("button", { name: "Copy link" }).click();
  await expect(page.locator("#toast")).toContainText("Link copied");
  await defaultZone.getByRole("button", { name: "Copy image to the clipboard" }).click();
  await expect(page.locator("#toast")).toContainText("Image copied");
  const imageName = await defaultZone.locator(".fname").textContent();
  const imageDownloadPromise = page.waitForEvent("download");
  await defaultZone.locator(".download-btn").click();
  const imageDownload = await imageDownloadPromise;
  expect(imageDownload.suggestedFilename()).toBe(imageName);

  await defaultZone.locator(".thumb-big").press("Enter");
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv-ref")).toHaveText(reference);
  await page.locator("#pv-copy").click();
  await expect(page.locator("#pv-toast")).toBeVisible();
  await expect(page.locator("#pv-toast")).toContainText("Link copied");
  await page.locator("#pv-copy-image").click();
  await expect(page.locator("#pv-toast")).toContainText("Image copied");
  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.locator("#pv")).not.toBeVisible();

  await defaultZone.getByRole("button", { name: /Enlarge the image/ }).click();
  await expect(page.locator("#pv")).toBeVisible();
  await page.locator("#pv-clear").click();
  await expect(page.locator("#pv-toast")).toContainText("Clipboard cleared");
  await page.getByRole("button", { name: "Close" }).click();
});

test("dépose plusieurs fichiers séquentiellement et permet la sélection groupée", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchMultiDrop(page, '[data-zone="default"]');

  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(2);
  await expect(defaultZone.locator(".selection-latest")).toBeVisible();
  await expect(defaultZone.locator(".selection-summary-item")).toHaveCount(2);
  await expect(defaultZone.locator(".selection-summary-meta")).toHaveCount(2);
  const summaryNames = defaultZone.locator(".selection-summary-name");
  await expect(summaryNames).toHaveCount(2);
  expect(await summaryNames.evaluateAll(names => names.map(name => ({
    text: name.textContent,
    title: name.title,
  })))).toEqual([
    { text: "second.png", title: "second.png" },
    { text: "first.png", title: "first.png" },
  ]);
  await expect(defaultZone.locator(".selection-latest .download-btn")).toHaveText("Download ZIP");
  await expect(defaultZone.locator(".selection-latest .zoom-btn")).toHaveCount(0);
  await expect(defaultZone.locator(".bulk-actions")).toHaveCount(1);
  await expect(defaultZone.locator(".bulk-summary")).toHaveText("2 files selected");

  const thumbnails = defaultZone.locator(".thumb-wrap");
  await thumbnails.nth(0).click();
  await thumbnails.nth(1).click({ modifiers: ["Shift"] });
  await expect(defaultZone.locator(".bulk-summary")).toHaveText("2 files selected");
  await expect(thumbnails).toHaveCount(2);
  await expect(thumbnails.nth(0)).toHaveAttribute("aria-pressed", "true");
  await expect(thumbnails.nth(1)).toHaveAttribute("aria-pressed", "true");
});

test("copie, télécharge et supprime la sélection d'une zone", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchMultiDrop(page, '[data-zone="default"]');
  await expect(defaultZone.locator(".bulk-summary")).toHaveText("2 files selected");

  await defaultZone.getByRole("button", { name: "Copy 2 links" }).click();
  await expect(page.locator("#toast")).toContainText("2 links copied");

  const downloadPromise = page.waitForEvent("download");
  await defaultZone.getByRole("button", { name: "Download 2 files as ZIP" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("pasteberth-default.zip");
  await expect.poll(async () => {
    const response = await page.request.get("/api/zones");
    const overview = await response.json();
    return overview.zones.find((zone) => zone.id === "default").busy;
  }).toBe(false);
  await page.waitForTimeout(500);

  page.once("dialog", (dialog) => dialog.accept());
  const deleteResponse = page.waitForResponse((response) => (
    response.url().includes("/api/zones/default/images/batch-delete")
      && response.status() === 200
  ));
  await defaultZone.getByRole("button", { name: "Delete 2 selected files" }).click();
  await deleteResponse;
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(0);
});

test("réessaie une preview temporairement indisponible", async ({ page }) => {
  let previewAttempts = 0;
  await page.route("**/previews/**", async (route) => {
    previewAttempts += 1;
    if (previewAttempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        headers: { "Retry-After": "1" },
        body: JSON.stringify({ error: { code: "preview_busy", message: "busy" } }),
      });
      return;
    }
    await route.continue();
  });

  await openApp(page);
  await page.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);

  const preview = page.locator('[data-zone="default"] .thumb-big');
  await expect.poll(() => previewAttempts).toBeGreaterThan(1);
  await expect.poll(() => preview.evaluate((image) => image.naturalWidth)).toBeGreaterThan(0);
});

test("réessaie la copie d'une preview temporaire", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "ClipboardItem image support is validated in Chromium");
  let copyAttempts = 0;
  await page.route("**/previews/**", async (route) => {
    if (route.request().headers().accept === "image/*") {
      copyAttempts += 1;
      if (copyAttempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          headers: { "Retry-After": "1" },
          body: JSON.stringify({ error: { code: "preview_busy", message: "busy" } }),
        });
        return;
      }
    }
    await route.continue();
  });

  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);
  await expect(defaultZone.locator(".latest")).toBeVisible();
  await defaultZone.getByRole("button", { name: "Copy image to the clipboard" }).click();
  await expect(page.locator("#toast")).toContainText("Image copied");
  expect(copyAttempts).toBeGreaterThan(1);
});

test("copie les previews JPEG et WebP comme PNG", async ({ page, browserName }) => {
  test.skip(browserName !== "chromium", "ClipboardItem image support is validated in Chromium");
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  for (const [index, [type, extension]] of [
    [1, ["image/jpeg", "jpg"]],
    [2, ["image/webp", "webp"]],
  ]) {
    await dispatchCanvasPaste(page, type);
    await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(index);
    await expect(defaultZone.locator(".fname")).toHaveText(new RegExp(`\\.${extension}$`));
    await defaultZone.getByRole("button", { name: "Copy image to the clipboard" }).click();
    await expect(page.locator("#toast")).toContainText("Image copied");
  }
});

test("sélectionne une image depuis l'index", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchPaste(page);
  await expect(defaultZone.locator(".latest")).toBeVisible();
  const firstName = await defaultZone.locator(".fname").textContent();
  await dispatchPaste(page);
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(2);

  await defaultZone.locator(".thumb-wrap").last().click();
  await expect(defaultZone.locator(".thumb-wrap").last()).toHaveAttribute("aria-current", "true");
  await expect(defaultZone.locator(".latest .fname")).toHaveText(firstName);
});

test("accepte le glisser-déposer sur une zone", async ({ page }) => {
  await openApp(page);
  await dispatchDrop(page, '[data-zone="secondary"]');

  const secondary = page.locator('[data-zone="secondary"]');
  await expect(secondary.locator(".latest")).toBeVisible();
  await expect(secondary.locator(".fname")).toHaveText("dropped.png");
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-current", "true");
});

test("conserve le nom et confirme le remplacement d'un binaire déposé", async ({ page }) => {
  await openApp(page);
  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');

  const defaultZone = page.locator('[data-zone="default"]');
  await expect(defaultZone.locator(".fname")).toHaveText("archive.zip");
  await expect(defaultZone.locator(".download-btn")).toHaveText("Download ZIP");
  await expect(defaultZone.locator(".download-btn")).toHaveAttribute(
    "aria-label",
    "Download ZIP",
  );
  await expect(defaultZone.locator(".file-box")).toHaveAttribute(
    "aria-label",
    "Download archive.zip",
  );
  const downloadPromise = page.waitForEvent("download");
  await defaultZone.locator(".download-btn").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("archive.zip");

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(page.locator("#replace")).toBeVisible();
  await expect(page.locator("#replace-filename")).toHaveText("archive.zip");
  await page.locator("#replace-cancel").click();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(page.locator("#replace")).toBeVisible();
  await page.locator("#replace-confirm").click();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
});

test("utilise le fallback du dialogue de remplacement sans API native", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { value: undefined });
    Object.defineProperty(HTMLDialogElement.prototype, "close", { value: undefined });
  });
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(defaultZone.locator(".fname")).toHaveText("archive.zip");
  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(page.locator("#replace")).toBeVisible();
  await expect(page.locator("#replace")).toHaveClass(/dialog-fallback/);
  const secondaryBox = await page.locator('[data-zone="secondary"]').boundingBox();
  await page.mouse.click(
    secondaryBox.x + secondaryBox.width / 2,
    secondaryBox.y + secondaryBox.height / 2,
  );
  await expect(defaultZone.getByRole("button", { name: "Select zone Default" }))
    .toHaveAttribute("aria-current", "true");
  await expect(page.locator("#replace")).toBeVisible();
  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await page.locator("#replace-confirm").click();
  await expect(page.locator("#replace")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#replace")).toBeHidden();

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(page.locator("#replace")).toBeVisible();
  await page.locator("#replace-confirm").focus();
  await page.keyboard.press("Tab");
  await expect(page.locator("#replace-cancel")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.locator("#replace-confirm")).toBeFocused();
  await page.locator("#replace-cancel").click();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await page.locator("#replace-confirm").click();
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
});

test("utilise le fallback du dialogue de preview sans API native", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { value: undefined });
    Object.defineProperty(HTMLDialogElement.prototype, "close", { value: undefined });
  });
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await defaultZone.locator(".zone-select").click();
  await dispatchPaste(page);
  await defaultZone.locator(".thumb-big").press("Enter");
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv")).toHaveClass(/dialog-fallback/);
  await expect(page.locator("#pv-backdrop")).toBeVisible();
  await expect(page.locator("#pv-close")).toBeFocused();

  const secondaryBox = await secondary.boundingBox();
  await page.mouse.click(
    secondaryBox.x + secondaryBox.width / 2,
    secondaryBox.y + secondaryBox.height / 2,
  );
  await expect(page.locator("#pv")).toBeVisible();
  await expect(defaultZone.locator(".zone-select")).toHaveAttribute("aria-current", "true");
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-current", "false");

  await page.keyboard.press("Tab");
  await expect(page.locator("#pv-copy")).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.locator("#pv-close")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#pv")).toBeHidden();
  await expect(page.locator("#pv-backdrop")).toBeHidden();
  await expect(defaultZone.locator(".thumb-big")).toBeFocused();
});

test("affiche un fichier cache depose dans l'index apres rechargement", async ({ page }) => {
  await openApp(page);
  await dispatchBinaryDrop(page, '.zone[data-zone="default"]', ".env");

  const defaultZone = page.locator('[data-zone="default"]');
  await expect(defaultZone.locator(".fname")).toHaveText(".env");
  await expect(defaultZone.locator(".download-btn")).toHaveText("Download ENV");
  const downloadPromise = page.waitForEvent("download");
  await defaultZone.locator(".download-btn").click();
  const download = await downloadPromise;
  expect(new URL(download.url()).pathname).toBe("/previews/default/.env");

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator('[data-zone="default"] .fname')).toHaveText(".env");
  await expect(page.locator('[data-zone="default"] .thumb-content')).toHaveText("ENV");
});

test("supprime une image depuis la carte", async ({ page }) => {  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);
  await expect(defaultZone.locator(".latest")).toBeVisible();
  const filename = await defaultZone.locator(".fname").textContent();

  page.on("dialog", (dialog) => dialog.accept());
  await defaultZone.getByRole("button", { name: /Delete .* from the disk/ }).click();

  await expect(defaultZone.locator(".latest")).toHaveCount(0);
  await expect(defaultZone.locator(".drop-hint")).toBeVisible();
  const response = await page.request.get(`/api/zones/default/images`);
  const payload = await response.json();
  expect(payload.images.some(i => i.filename === filename)).toBe(false);
});

test("ne réaffiche pas une image supprimée après un refresh périmé", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);
  await expect(defaultZone.locator(".latest")).toBeVisible();

  let releaseRefresh;
  let refreshCaptured;
  const refreshReady = new Promise((resolve) => { refreshCaptured = resolve; });
  await page.route("**/api/zones/default/images", async (route) => {
    if (route.request().method() !== "GET" || releaseRefresh) {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const body = await response.body();
    refreshCaptured();
    await new Promise((resolve) => { releaseRefresh = resolve; });
    try {
      await route.fulfill({ response, body });
    } catch (_) {
      // The browser may have aborted this deliberately stale response.
    }
  });
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await refreshReady;

  const filename = await defaultZone.locator(".fname").textContent();
  page.on("dialog", (dialog) => dialog.accept());
  await defaultZone.getByRole("button", { name: /Delete .* from the disk/ }).click();
  await expect(defaultZone.locator(".latest")).toHaveCount(0);
  releaseRefresh();
  await expect.poll(() => defaultZone.locator(".latest").count()).toBe(0);
  const responseAfterDelete = await page.request.get("/api/zones/default/images");
  const payload = await responseAfterDelete.json();
  expect(payload.images.some((item) => item.filename === filename)).toBe(false);
});

test("colle du texte et l'affiche", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchTextPaste(page, "# hello world", "text/markdown");

  await expect(defaultZone.locator(".latest")).toBeVisible();
  await expect(defaultZone.locator(".fname")).toHaveText(/\.md$/);
  await expect(defaultZone.locator(".dims")).not.toContainText("null");
  await expect(defaultZone.locator(".index-title")).toHaveText("Content index");
  await expect(defaultZone.locator(".thumb-content")).toHaveText("TXT");
  await expect(defaultZone.locator(".copy-image-btn")).toHaveText("Copy Text");
  await expect(defaultZone.locator(".copy-image-btn")).toHaveAttribute(
    "aria-label",
    "Copy Text to the clipboard",
  );
  await expect(defaultZone.locator(".download-btn")).toHaveText("Download MD");
  const textName = await defaultZone.locator(".fname").textContent();
  const textDownloadPromise = page.waitForEvent("download");
  await defaultZone.locator(".download-btn").click();
  const textDownload = await textDownloadPromise;
  expect(textDownload.suggestedFilename()).toBe(textName);
  await defaultZone.locator(".file-box").click();
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv")).toHaveAttribute("aria-label", `Preview of ${textName}`);
  await expect(page.locator("#pv-text")).toHaveText("# hello world");
  await expect(page.locator("#pv-copy-image")).toHaveText("Copy Text");
  await expect(page.locator("#pv-download")).toHaveText("Download MD");
  await page.getByRole("button", { name: "Close" }).click();
});

test("les previews restent utilisables sur un écran étroit", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.locator(".zone-select").click();
  await dispatchPaste(page);
  await defaultZone.locator(".thumb-big").click();
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv button")).toHaveCount(6);
  for (const button of await page.locator("#pv button").all()) await expect(button).toBeVisible();
  let widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  await page.locator("#pv-close").click();

  await dispatchTextPaste(page, "narrow text");
  await defaultZone.locator(".file-box").click();
  await expect(page.locator("#pv-text")).toHaveText("narrow text");
  for (const button of await page.locator("#pv button").all()) await expect(button).toBeVisible();
  widths = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
});

test("prefere text plain a html dans un presse-papiers mixte", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchTextPasteFlavors(page, [
    ["text/html", "<p>html value</p>"],
    ["text/plain", "plain value"],
  ]);

  await expect(defaultZone.locator(".fname")).toHaveText(/\.txt$/);
  const filename = await defaultZone.locator(".fname").textContent();
  const response = await page.request.get(
    `/previews/default/${encodeURIComponent(filename)}`,
  );
  expect(await response.text()).toBe("plain value");
});

test("ignore un text plain vide si le html contient du contenu", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchTextPasteFlavors(page, [
    ["text/plain", ""],
    ["text/html", "<p>html value</p>"],
  ]);

  await expect(defaultZone.locator(".fname")).toHaveText(/\.html$/);
  const filename = await defaultZone.locator(".fname").textContent();
  const response = await page.request.get(
    `/previews/default/${encodeURIComponent(filename)}`,
  );
  expect(await response.text()).toBe("<p>html value</p>");
});

test("colle un presse-papiers mixte en un seul document html", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchMixedPaste(page);

  await expect(defaultZone.locator(".latest")).toBeVisible();
  await expect(defaultZone.locator(".fname")).toHaveText(/\.html$/);
  await expect(defaultZone.locator(".index-title")).toHaveText("Content index");
  await defaultZone.locator(".copy-image-btn").click();
  await expect(page.locator("#toast")).toContainText("Text copied");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("hello mixed world");

  const htmlName = await defaultZone.locator(".fname").textContent();
  const response = await page.request.get(`/previews/default/${encodeURIComponent(htmlName)}`);
  expect(response.status()).toBe(200);
  const body = await response.text();
  expect(body).toContain("hello mixed world");
  expect(body).toContain("data:image/png;base64,");
});

test("supprime depuis l'aperçu dans la bonne zone si les noms sont identiques", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  const name = "duplicate.txt";

  await dispatchTextDrop(page, '[data-zone="default"]', name, "default value");
  await dispatchTextDrop(page, '[data-zone="secondary"]', name, "secondary value");
  await expect(defaultZone.locator(".fname")).toHaveText(name);
  await expect(secondary.locator(".fname")).toHaveText(name);

  await secondary.locator(".file-box").click();
  await expect(page.locator("#pv")).toBeVisible();
  page.on("dialog", (dialog) => dialog.accept());
  await page.locator("#pv-delete").click();

  await expect(secondary.locator(".latest")).toHaveCount(0);
  await expect(defaultZone.locator(".fname")).toHaveText(name);
});

test("colle une image avec un texte vide comme image simple", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();

  await dispatchMixedPaste(page, "   ");

  await expect(defaultZone.locator(".latest")).toBeVisible();
  await expect(defaultZone.locator(".fname")).toHaveText(/\.png$/);
  await expect(defaultZone.locator(".copy-image-btn")).toHaveText("Copy Image");
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
});
