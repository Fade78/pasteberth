const { test, expect } = require("@playwright/test");

async function resetServer(request) {
  const response = await request.post("/__e2e/reset");
  expect(response.status()).toBe(204);
}

test.beforeEach(async ({ request }) => {
  await resetServer(request);
});

test("smoke natif: le sélecteur de fichiers publie un fichier nommé", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".zone")).toHaveCount(2);
  await expect(page.locator("#status-text")).toHaveText("online");

  const responsePromise = page.waitForResponse(response => (
    response.request().method() === "POST"
      && response.url().includes("/api/zones/default/images")
      && [200, 201].includes(response.status())
  ));
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Add files to Default" }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: "native-smoke.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("native smoke\n"),
  });

  const item = await (await responsePromise).json();
  expect(item.filename).toBe("native-smoke.txt");
  expect(item.creation_method).toBe("web_mouse_drop");
  await expect(page.locator('[data-zone="default"] .fname')).toHaveText("native-smoke.txt");
});
