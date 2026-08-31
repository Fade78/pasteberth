const { test, expect } = require("@playwright/test");

const ONE_PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNwaDgAAAKEAYEml6crAAAAAElFTkSuQmCC";

async function resetServer(request) {
  const response = await request.post("./__e2e/reset");
  expect(response.status()).toBe(204);
}

test.beforeEach(async ({ request }) => {
  await resetServer(request);
});

test("sert l'application entièrement sous le préfixe", async ({ page }) => {
  const applicationRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === "http://127.0.0.1:8876") {
      applicationRequests.push(url.pathname);
    }
  });

  await page.goto("./", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".zone")).toHaveCount(2);
  await expect(page.locator("#status-text")).toHaveText("online");
  await expect(page.locator(".brand-icon")).toHaveAttribute(
    "src",
    "/paste/static/favicon.svg",
  );
  await page.getByRole("button", { name: "Select zone Default" }).click();

  await page.evaluate((base64) => {
    const bytes = Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
    const file = new File([bytes], "clipboard.png", { type: "image/png" });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: dataTransfer });
    window.dispatchEvent(event);
  }, ONE_PIXEL_PNG);
  await expect(page.locator('[data-zone="default"] .latest')).toBeVisible();

  const index = await page.request.get("./api/zones/default/images");
  expect(index.status()).toBe(200);
  const payload = await index.json();
  expect(payload.images[0].preview_url).toMatch(/^\/paste\/previews\//);

  for (const pathname of applicationRequests) {
    expect(pathname === "/paste/" || pathname.startsWith("/paste/")).toBe(true);
  }
});
