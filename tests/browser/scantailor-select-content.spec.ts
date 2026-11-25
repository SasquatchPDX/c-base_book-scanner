import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page): Promise<void> {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("fry");
  await page.getByLabel("Password").fill("fry");
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** A 600x800 page with lines of glyph-like marks from (90, 120) to about (510, 630). */
async function textPage(page: Page): Promise<Buffer> {
  const base64 = await page.evaluate(async () => {
    const canvas = new OffscreenCanvas(600, 800);
    const context = canvas.getContext("2d")!;
    context.fillStyle = "rgb(235,235,235)";
    context.fillRect(0, 0, 600, 800);
    context.fillStyle = "rgb(25,25,25)";
    let row = 0;
    for (let y = 120; y < 640; y += 20, row += 1) {
      let x = 90;
      while (x < 510) {
        const word = 30 + ((row * 7 + x) % 50);
        for (let gx = x; gx < Math.min(x + word, 510); gx += 7) {
          context.fillRect(gx, y, Math.min(gx + 4, 510) - gx + 1, 10);
        }
        x += word + 12;
      }
    }
    const blob = await canvas.convertToBlob({ type: "image/png" });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  });
  return Buffer.from(base64, "base64");
}

const stagePost = (stage: string) => (method: string, url: string) =>
  method === "POST" && url.endsWith(`/stages/${stage}/`);

test("auto draws the detected content box and dragging an edge makes it manual", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await signIn(page);
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill("Content box");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await page.getByLabel("Page image").setInputFiles({
    name: "page.png",
    mimeType: "image/png",
    buffer: await textPage(page),
  });
  await page.getByLabel("Sequence").fill("1");
  await page.getByRole("button", { name: "Upload page" }).click();
  await expect(page.locator(".scan-page-card")).toHaveCount(1);
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();

  const message = page.locator("[data-editor-message]");
  // Deskew first: Select Content reads its result.
  await page.getByRole("button", { name: "Save and run" }).click();
  await expect(message).toContainText("Angle", { timeout: 30_000 });

  await page.locator('[data-stage-select="select_content"]').click();
  const controls = page.locator("[data-stage-controls]");
  await expect(controls.getByRole("radio", { name: "Auto" })).toBeChecked();
  await page.getByRole("button", { name: "Save and run" }).click();
  await expect(message).toContainText("mm", { timeout: 30_000 });
  await expect(message).toContainText("auto");
  await expect(
    page.locator('[data-strip-page] .st-status-icon[data-stage="select_content"]').first(),
  ).toHaveAttribute("data-status", "success");

  // The auto result is drawn and stays on the input view.
  const box = page.locator("[data-content-box]");
  await expect(box).toBeVisible();
  await expect(page.getByRole("button", { name: "Input", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  const before = (await box.boundingBox())!;
  // The box sits on the detected text: x 90..512 of the 600 px wide page.
  const shown = (await page.locator("[data-editor-image]").boundingBox())!;
  const scale = shown.width / 600;
  expect(Math.abs(before.x - (shown.x + 90 * scale))).toBeLessThan(4);
  expect(Math.abs(before.y - (shown.y + 120 * scale))).toBeLessThan(4);

  // Drag the right edge 30 px to the left: only that edge moves, no request until release.
  const posts: string[] = [];
  page.on("request", (request) => {
    if (stagePost("select_content")(request.method(), request.url())) posts.push(request.url());
  });
  const edge = (await page.locator('[data-content-handle="right"]').boundingBox())!;
  const x = edge.x + edge.width / 2;
  const y = edge.y + edge.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 30, y + 40, { steps: 5 });
  const during = (await box.boundingBox())!;
  expect(during.x).toBeCloseTo(before.x, 0);
  expect(during.y).toBeCloseTo(before.y, 0);
  expect(during.height).toBeCloseTo(before.height, 0);
  expect(during.width).toBeCloseTo(before.width - 30, 0);
  expect(posts).toHaveLength(0);

  const saved = page.waitForResponse((response) =>
    stagePost("select_content")(response.request().method(), response.url()),
  );
  await page.mouse.up();
  expect((await saved).status()).toBe(202);
  await expect(controls.getByRole("radio", { name: "Manual" })).toBeChecked();
  await expect(message).toContainText("manual", { timeout: 30_000 });
});
