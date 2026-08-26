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

async function dispatchBinaryDrop(page, selector) {
  await page.locator(selector).evaluate((element) => {
    const file = new File([new Uint8Array([0, 1, 2, 3])], "archive.zip", {
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
  });
}

test.beforeEach(async ({ request }) => {
  await resetServer(request);
});

test("charge les zones et expose une sélection clavier accessible", async ({ page }) => {
  await openApp(page);

  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await expect(defaultZone.getByRole("button", { name: "Select zone Default" }))
    .toHaveAttribute("aria-pressed", "false");

  await page.locator("body").press("2");
  await expect(secondary.getByRole("button", { name: "Select zone Secondary" }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(secondary).toHaveClass(/active/);
  await expect(defaultZone).not.toHaveClass(/active/);
});

test("refuse un collage sans zone active", async ({ page }) => {
  await openApp(page);
  const before = await page.locator(".latest").count();

  await dispatchPaste(page);

  await expect(page.locator("#toast")).toContainText("Choose a zone first");
  await expect.poll(() => page.locator(".latest").count()).toBe(before);
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
  await expect(defaultZone.locator(".thumb-wrap")).toHaveAttribute("aria-pressed", "true");
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

  await defaultZone.getByRole("button", { name: "Enlarge the image" }).click();
  await expect(page.locator("#pv")).toBeVisible();
  await page.locator("#pv-clear").click();
  await expect(page.locator("#pv-toast")).toContainText("Clipboard cleared");
  await page.getByRole("button", { name: "Close" }).click();
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
  await expect(defaultZone.locator(".thumb-wrap").last()).toHaveAttribute("aria-pressed", "true");
  await expect(defaultZone.locator(".latest .fname")).toHaveText(firstName);
});

test("accepte le glisser-déposer sur une zone", async ({ page }) => {
  await openApp(page);
  await dispatchDrop(page, '[data-zone="secondary"]');

  const secondary = page.locator('[data-zone="secondary"]');
  await expect(secondary.locator(".latest")).toBeVisible();
  await expect(secondary.locator(".fname")).toHaveText("dropped.png");
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-pressed", "true");
});

test("conserve le nom et propose le téléchargement pour un binaire déposé", async ({ page }) => {
  await openApp(page);
  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');

  const defaultZone = page.locator('[data-zone="default"]');
  await expect(defaultZone.locator(".fname")).toHaveText("archive.zip");
  await expect(defaultZone.locator(".download-btn")).toHaveText("Download ZIP");
  await expect(defaultZone.locator(".download-btn")).toHaveAttribute(
    "aria-label",
    "Download ZIP",
  );
  const downloadPromise = page.waitForEvent("download");
  await defaultZone.locator(".download-btn").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("archive.zip");

  await dispatchBinaryDrop(page, '.zone[data-zone="default"]');
  await expect(defaultZone.locator(".thumb-wrap")).toHaveCount(1);
});

test("supprime une image depuis la carte", async ({ page }) => {  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Select zone Default" }).click();
  await dispatchPaste(page);
  await expect(defaultZone.locator(".latest")).toBeVisible();
  const filename = await defaultZone.locator(".fname").textContent();

  page.on("dialog", (dialog) => dialog.accept());
  await defaultZone.getByRole("button", { name: "Delete this image from the disk" }).click();

  await expect(defaultZone.locator(".latest")).toHaveCount(0);
  await expect(defaultZone.locator(".drop-hint")).toBeVisible();
  const response = await page.request.get(`/api/zones/default/images`);
  const payload = await response.json();
  expect(payload.images.some(i => i.filename === filename)).toBe(false);
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
  await expect(page.locator("#pv-text")).toHaveText("# hello world");
  await expect(page.locator("#pv-copy-image")).toHaveText("Copy Text");
  await expect(page.locator("#pv-download")).toHaveText("Download MD");
  await page.getByRole("button", { name: "Close" }).click();
});
