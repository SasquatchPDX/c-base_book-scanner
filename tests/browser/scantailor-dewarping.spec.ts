import { expect, test, type Page } from "@playwright/test";
import { createWebScanJob } from "./scan-jobs";

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

test("dewarping shows the model grid and a dragged point saves a manual model", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await signIn(page);
  await createWebScanJob(page, "Dewarping grid", [
    { name: "page.png", buffer: await textPage(page), sequence: 1 },
  ]);
  await expect(page.locator(".scan-page-card")).toHaveCount(1);
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();

  // Opening a stage runs it (#209), and each finished stage runs the next one (#205).
  // Each stage's own result line: the status line moves on to the chained stages.
  const runStage = async (stage: string, resultLine: string, expected: string): Promise<void> => {
    await page.locator(`[data-stage-select="${stage}"]`).click();
    await expect(page.locator(`[data-stage-controls] ${resultLine}`)).toContainText(expected, {
      timeout: 30_000,
    });
    await expect(
      page.locator(`[data-strip-page] .st-status-icon[data-stage="${stage}"]`).first(),
    ).toHaveAttribute("data-status", "success", { timeout: 30_000 });
  };
  await runStage("deskew", "[data-deskew-result]", "Angle");
  await runStage("select_content", "[data-content-result]", "mm");
  await runStage("dewarping", "[data-dewarp-result]", "");
  // The result pane beside the input shows the dewarped page (#206).
  const result = page.locator("[data-result-image]");
  await expect(result).toHaveAttribute("src", /\/stages\/dewarping\/preview\/\?v=/);
  const autoResult = await result.getAttribute("src");
  const inputSource = await page.locator("[data-editor-image]").getAttribute("src");

  // Upstream's view: the model grid with red control points on the page.
  const overlay = page.locator("[data-dewarp-overlay]");
  await expect(overlay).toBeVisible();
  await expect(page.locator("[data-dewarp-point]")).toHaveCount(10);
  const shown = (await page.locator("[data-editor-image]").boundingBox())!;
  const point = page.locator('[data-dewarp-point="0:2"]');
  const at = (await point.boundingBox())!;
  const x = at.x + at.width / 2;
  const y = at.y + at.height / 2;
  expect(x).toBeGreaterThanOrEqual(shown.x - 2);
  expect(x).toBeLessThanOrEqual(shown.x + shown.width + 2);
  expect(y).toBeGreaterThanOrEqual(shown.y - 2);
  expect(y).toBeLessThanOrEqual(shown.y + shown.height + 2);

  // Dragging the top curve's middle point bends the grid without a request...
  const posts: string[] = [];
  page.on("request", (request) => {
    if (stagePost("dewarping")(request.method(), request.url())) posts.push(request.url());
  });
  const gridBefore = await page.locator(".st-dewarp-grid").getAttribute("d");
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + 15, { steps: 5 });
  expect(await page.locator(".st-dewarp-grid").getAttribute("d")).not.toBe(gridBefore);
  expect(posts).toHaveLength(0);
  // ...and releasing it saves the model as manual.
  const saved = page.waitForResponse((response) =>
    stagePost("dewarping")(response.request().method(), response.url()),
  );
  await page.mouse.up();
  expect((await saved).status()).toBe(202);
  await expect(
    page.locator("[data-stage-controls]").getByRole("radio", { name: "Manual" }),
  ).toBeChecked();
  await expect(page.locator("[data-dewarp-result]")).toContainText("manual", {
    timeout: 30_000,
  });
  // The manual model's result replaces the auto one; the input was never reloaded.
  await expect(result).not.toHaveAttribute("src", autoResult!);
  await expect(result).toHaveAttribute("src", /\/stages\/dewarping\/preview\/\?v=/);
  await expect(page.locator("[data-editor-image]")).toHaveAttribute("src", inputSource!);
});
