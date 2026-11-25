import { expect, test, type Page } from "@playwright/test";

// Pillow-generated 100×100 white page (same fixture as scantailor-preview-retry.spec.ts).
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

const deskewPost = (method: string, url: string): boolean =>
  method === "POST" && /\/stages\/deskew\/$/.test(url);

test("deskew turns the page live in the browser and saves when the handle is released", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await signIn(page);
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill("Deskew handles");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.getByLabel("Page image").setInputFiles({
    name: "page.png",
    mimeType: "image/png",
    buffer: PAGE,
  });
  await page.getByLabel("Sequence").fill("1");
  await page.getByRole("button", { name: "Upload page" }).click();
  await expect(page.locator(".scan-page-card")).toHaveCount(1);
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();

  const image = page.locator("[data-editor-image]");
  await expect
    .poll(() => image.evaluate((node) => (node as HTMLImageElement).naturalWidth), {
      timeout: 30_000,
    })
    .toBe(100);
  const controls = page.locator("[data-stage-controls]");
  await expect(controls.getByRole("radio", { name: "Auto" })).toBeChecked();
  // The grid is drawn over the page (upstream deskew::ImageView).
  await expect(page.locator(".st-deskew-grid")).toBeVisible();

  const posts: string[] = [];
  page.on("request", (request) => {
    if (deskewPost(request.method(), request.url())) posts.push(request.url());
  });

  const handle = page.locator('[data-deskew-handle="1"]');
  const box = await handle.boundingBox();
  expect(box).not.toBeNull();
  const x = box!.x + box!.width / 2;
  const y = box!.y + box!.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + 12, { steps: 6 });

  // While dragging, the page turns clockwise in the browser and nothing is sent.
  await expect(image).toHaveAttribute("style", /rotate\(\d+(\.\d+)?deg\)/);
  expect(posts).toHaveLength(0);
  const live = Number(await controls.locator("[data-deskew-angle]").inputValue());
  expect(live).toBeGreaterThan(0);

  const saved = page.waitForResponse((response) =>
    deskewPost(response.request().method(), response.url()),
  );
  await page.mouse.up();
  expect((await saved).status()).toBe(202);
  expect(posts).toHaveLength(1);
  await expect(controls.getByRole("radio", { name: "Manual" })).toBeChecked();
  const angle = Number(await controls.locator("[data-deskew-angle]").inputValue());
  await expect(page.locator("[data-editor-message]")).toContainText(
    `Angle ${angle.toFixed(2)}° clockwise (manual)`,
    { timeout: 30_000 },
  );

  // Ctrl+wheel up turns counter-clockwise by 0.1 degree, again without a request
  // until the angle settles.
  await page.locator("[data-viewport]").hover();
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -100);
  await page.keyboard.up("Control");
  await expect(controls.locator("[data-deskew-angle]")).toHaveValue((angle - 0.1).toFixed(2));
  await expect.poll(() => posts.length, { timeout: 5_000 }).toBe(2);
});
