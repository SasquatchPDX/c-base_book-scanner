import { expect, test, type Page } from "@playwright/test";

// tests/browser_fixtures.py: seed_migrated_scan_page (a 120×90 page without a working image).
const MIGRATED_JOB_ID = "6f1d2b3c-0195-4a50-8b7e-000000000050";

// Pillow-generated 100×100 white page.
const PAGE = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAA5klEQVR4nO3SsREAIAwDMWD/ncMK+V6qXf35zsxh5y13iNV4ViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYg1tn7z/EDxbGcl84AAAAASUVORK5CYII=",
  "base64",
);

async function signIn(page: Page): Promise<void> {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("fry");
  await page.getByLabel("Password").fill("fry");
  await page.getByRole("button", { name: "Sign in" }).click();
}

const naturalWidth = (page: Page) =>
  page.locator("[data-editor-image]").evaluate((node) => (node as HTMLImageElement).naturalWidth);

test("first open of a page migrated without a working image shows its Deskew input", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const statuses: number[] = [];
  page.on("response", (response) => {
    if (/\/stages\/deskew\/preview\/\?input=true/.test(response.url())) {
      statuses.push(response.status());
    }
  });
  await signIn(page);
  // The editor (#195 step 3) opens on the first page's Deskew input.
  await page.goto(`/scan/jobs/${MIGRATED_JOB_ID}/editor/`);
  await expect(page.locator("[data-strip-page]")).toHaveCount(1);
  const image = page.locator("[data-editor-image]");
  await expect(image).toHaveAttribute("alt", "Processing-stage input preview");

  // The first request queues generation; the editor shows the image once it is ready,
  // without navigating away or reloading.
  await expect.poll(() => naturalWidth(page), { timeout: 45_000 }).toBe(120);
  expect(statuses.at(-1)).toBe(200);
  expect(statuses.every((status) => status === 200 || status === 503)).toBe(true);
  await expect(page.locator("[data-editor-message]")).not.toHaveClass(/editor-message-error/);
});

test("an upload whose working image is still queued appears once it is ready", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await signIn(page);
  // Hold back the first input previews as the server does while the generation job
  // waits in the queue: the image request and the editor's diagnostic request both
  // see the retryable answer, then the real image.
  let held = 0;
  await page.route(/\/stages\/deskew\/preview\/\?input=true/, async (route) => {
    if (held < 3) {
      held += 1;
      await route.fulfill({
        status: 503,
        contentType: "text/plain",
        headers: { "Retry-After": "2", "Cache-Control": "no-store" },
        body: "input preview not ready",
      });
      return;
    }
    await route.continue();
  });
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill("Delayed working image");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.getByLabel("Page image").setInputFiles({
    name: "blank.png",
    mimeType: "image/png",
    buffer: PAGE,
  });
  await page.getByLabel("Sequence").fill("1");
  await page.getByRole("button", { name: "Upload page" }).click();
  await expect(page.locator(".scan-page-card")).toHaveCount(1);
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();

  const message = page.locator("[data-editor-message]");
  await expect(message).toContainText("still being prepared");
  await expect.poll(() => naturalWidth(page), { timeout: 30_000 }).toBe(100);
  expect(held).toBe(3);
  await expect(page.locator("[data-editor-image]")).toHaveAttribute("src", /retry=\d+/);
  await expect(message).not.toContainText("still being prepared");
});
