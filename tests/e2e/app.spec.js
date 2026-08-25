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
  await expect(page.locator("#status-text")).toHaveText("en ligne");
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

test.beforeEach(async ({ request }) => {
  await resetServer(request);
});

test("charge les zones et expose une sélection clavier accessible", async ({ page }) => {
  await openApp(page);

  const defaultZone = page.locator('[data-zone="default"]');
  const secondary = page.locator('[data-zone="secondary"]');
  await expect(defaultZone.getByRole("button", { name: "Sélectionner la zone Default" }))
    .toHaveAttribute("aria-pressed", "false");

  await page.locator("body").press("2");
  await expect(secondary.getByRole("button", { name: "Sélectionner la zone Secondary" }))
    .toHaveAttribute("aria-pressed", "true");
  await expect(secondary).toHaveClass(/active/);
  await expect(defaultZone).not.toHaveClass(/active/);
});

test("refuse un collage sans zone active", async ({ page }) => {
  await openApp(page);
  const before = await page.locator(".latest").count();

  await dispatchPaste(page);

  await expect(page.locator("#toast")).toContainText("Choisissez d'abord une zone");
  await expect.poll(() => page.locator(".latest").count()).toBe(before);
});

test("colle une image et ouvre son aperçu au clavier", async ({ page }) => {
  await openApp(page);
  await page.getByRole("button", { name: "Sélectionner la zone Default" }).click();

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

  await defaultZone.locator(".thumb-big").press("Enter");
  await expect(page.locator("#pv")).toBeVisible();
  await expect(page.locator("#pv-ref")).toHaveText(reference);
  await page.getByRole("button", { name: "Fermer" }).click();
  await expect(page.locator("#pv")).not.toBeVisible();

  await defaultZone.getByRole("button", { name: "Agrandir l'image" }).click();
  await expect(page.locator("#pv")).toBeVisible();
  await page.locator("#pv-clear").click();
  await expect(page.locator("#toast")).toContainText("Presse-papiers effacé");
  await page.getByRole("button", { name: "Fermer" }).click();
});

test("sélectionne une image depuis l'index", async ({ page }) => {
  await openApp(page);
  const defaultZone = page.locator('[data-zone="default"]');
  await defaultZone.getByRole("button", { name: "Sélectionner la zone Default" }).click();

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
  await expect(secondary.locator(".fname")).toHaveText(/\.png$/);
  await expect(secondary.locator(".zone-select")).toHaveAttribute("aria-pressed", "true");
});
