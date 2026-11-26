import { expect, test, type Locator, type Page } from "@playwright/test";

import { openJobWithPages } from "./scan-helpers";

// Pillow-generated 100×100 white page: automatic dewarping must explain its fallback.
const PAGE = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAA5klEQVR4nO3SsREAIAwDMWD/ncMK+V6qXf35zsxh5y13iNV4ViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYg1tn7z/EDxbGcl84AAAAASUVORK5CYII=",
  "base64",
);

const naturalWidth = (image: Locator) =>
  image.evaluate((node) => (node as HTMLImageElement).naturalWidth);

async function openNewJob(page: Page, title: string): Promise<void> {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("fry");
  await page.getByLabel("Password").fill("fry");
  await page.getByRole("button", { name: "Sign in" }).click();
  await openJobWithPages(page, title, [{ name: "blank.png", buffer: PAGE }]);
}

for (const transport of ["events", "polling"] as const) {
  test(`each stage shows its result beside its input after a run (${transport})`, async ({
    page,
  }) => {
    test.setTimeout(90_000);
    if (transport === "polling") {
      await page.addInitScript(() => {
        Object.defineProperty(window, "EventSource", { value: undefined });
      });
    }
    await openNewJob(page, `Result feedback ${transport}`);

    const input = page.locator("[data-editor-image]");
    const result = page.locator("[data-result-image]");
    const status = page.locator("[data-result-status]");
    const message = page.locator("[data-editor-message]");
    // Two panes side by side (#206): no Input/Result toggle on a wide screen.
    await expect(page.locator("[data-input-pane]")).toBeVisible();
    await expect(page.locator("[data-result-pane]")).toBeVisible();
    await expect(page.getByRole("button", { name: "Result", exact: true })).toBeHidden();

    /** After a run the result pane shows this stage's result; the editor stays on it. */
    const expectResult = async (stage: string): Promise<void> => {
      await expect(result).toBeVisible({ timeout: 30_000 });
      await expect(result).toHaveAttribute("src", new RegExp(`/stages/${stage}/preview/\\?v=`));
      await expect.poll(() => naturalWidth(result)).toBeGreaterThan(0);
      await expect(status).toBeHidden();
      await expect(input).toBeVisible();
      await expect(input).toHaveAttribute("src", new RegExp(`/stages/${stage}/preview/\\?input=true`));
      await expect(page.locator(`[data-stage-select="${stage}"]`)).toHaveClass(/active/);
    };

    // No button: opening the editor runs Deskew (#209). It says when it leaves a page
    // unchanged, and its run queues the later stages, which run with their defaults
    // (#205 chaining).
    await expect(page.getByRole("button", { name: "Save and run" })).toHaveCount(0);
    await page.locator('[data-stage-select="deskew"]').click();
    await expectResult("deskew");
    await expect(page.locator("[data-deskew-result]")).toContainText("Angle 0.00°, no change");

    // Select Content: this blank page has no content box.
    await page.locator('[data-stage-select="select_content"]').click();
    await expectResult("select_content");
    await expect(page.locator("[data-content-result]")).toContainText("No content found");

    // Dewarping runs before Margins (#195); on a blank page it explains its fallback.
    await page.locator('[data-stage-select="dewarping"]').click();
    await expectResult("dewarping");
    await expect(page.locator("[data-dewarp-result]")).toContainText("No text lines");
    await expect(page.locator("[data-stage-warnings]")).toContainText("the page is unchanged");

    // Margins: an edit is saved by itself after a short pause and the stage re-runs
    // (#209). Meanwhile the previous result stays, dimmed, and the panes say that the
    // settings are being saved or run, never "Settings not applied".
    await page.locator('[data-stage-select="margins"]').click();
    await expectResult("margins");
    // Margins are millimetres (#208): 1.7 mm is 20 px at 300 dpi; right follows (linked).
    // The page is blank, so no content is found: the page is just its margins
    // (adaptContentRect), 2 × 20 px wide.
    await page.getByLabel("Left", { exact: true }).fill("1.7");
    await expect(status).toContainText(/Saving|Restarting|Queued|Running/);
    await expect(result).toBeVisible();
    await expect(message).not.toContainText("Settings not applied");
    // The result updates in place: the input stays the 100 px page, the result is the
    // margins' page.
    await expect.poll(() => naturalWidth(result), { timeout: 30_000 }).toBe(40);
    await expectResult("margins");
    await expect.poll(() => naturalWidth(input)).toBe(100);

    // Output re-ran on its own with the new margins.
    await page.locator('[data-stage-select="output"]').click();
    await expectResult("output");
    // Output's own settings: saved, its result pane updates without leaving the stage.
    const outputBefore = await result.getAttribute("src");
    await page.getByLabel("Invert output").check();
    // Autosaved and rerun without a button (#209).
    await expect(status).toContainText(/Saving|Restarting|Queued|Running/);
    await expect(result).not.toHaveAttribute("src", outputBefore!, { timeout: 30_000 });
    await expectResult("output");

    // Reopen the stage, as a user does when adjusting an already completed result.
    await page.reload();
    await page.locator('[data-stage-select="margins"]').click();
    await expect.poll(() => naturalWidth(result), { timeout: 30_000 }).toBe(40);
    // 3.4 mm is 40 px a side.
    await page.getByLabel("Left", { exact: true }).fill("3.4");
    await expect.poll(() => naturalWidth(result), { timeout: 30_000 }).toBe(80);
    await expectResult("margins");

    // Output follows again; its earlier (stale) result is never shown as current.
    await page.locator('[data-stage-select="output"]').click();
    await expectResult("output");

    await page.locator('[data-stage-select="dewarping"]').click();
    await expect(page.locator("[data-stage-warnings]")).toContainText("the page is unchanged");
    await page.locator('[data-stage-select="margins"]').click();
    await expect(page.locator("[data-stage-warnings]")).toBeHidden();
  });
}

test("a narrow screen shows one pane and brings the result forward after a run", async ({
  page,
}) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 820, height: 700 });
  await openNewJob(page, "Result feedback narrow");

  const inputButton = page.getByRole("button", { name: "Input", exact: true });
  const resultButton = page.getByRole("button", { name: "Result", exact: true });
  const inputPane = page.locator("[data-input-pane]");
  const resultPane = page.locator("[data-result-pane]");

  // Opening the editor runs Deskew (#209); its finished run brings the result forward.
  await expect(resultButton).toHaveAttribute("aria-pressed", "true", { timeout: 30_000 });
  await expect(resultPane).toBeVisible();
  await expect(inputPane).toBeHidden();
  await expect(page.locator("[data-result-image]")).toHaveAttribute(
    "src",
    /\/stages\/deskew\/preview\/\?v=/,
  );

  // The choice is remembered when another stage opens; Select Content ran on its own
  // after Deskew (#205 chaining).
  await page.locator('[data-stage-select="select_content"]').click();
  await expect(resultButton).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("[data-result-image]")).toHaveAttribute(
    "src",
    /\/stages\/select_content\/preview\/\?v=/,
    { timeout: 30_000 },
  );

  await inputButton.click();
  await expect(inputPane).toBeVisible();
  await expect(resultPane).toBeHidden();
  await expect(page.locator("[data-editor-image]")).toBeVisible();

  // Editing a setting brings the input forward; once the autosaved settings have run
  // (#209), the result comes forward again.
  await page.locator('[data-stage-select="margins"]').click();
  await resultButton.click();
  await expect(resultPane).toBeVisible();
  // Wait for the chained Margins run, which would bring the result forward again.
  await expect(page.locator("[data-result-image]")).toHaveAttribute(
    "src",
    /\/stages\/margins\/preview\/\?v=/,
    { timeout: 30_000 },
  );
  await page.getByLabel("Left", { exact: true }).fill("1.7");
  await expect(inputButton).toHaveAttribute("aria-pressed", "true");
  await expect(inputPane).toBeVisible();
  await expect(resultPane).toBeHidden();
  await expect(resultButton).toHaveAttribute("aria-pressed", "true", { timeout: 30_000 });
  await expect(resultPane).toBeVisible();
  await expect.poll(() => naturalWidth(page.locator("[data-result-image]")), {
    timeout: 30_000,
  }).toBe(40);
});

test("the input and result panes share one zoom and pan", async ({ page }) => {
  test.setTimeout(60_000);
  await openNewJob(page, "Result feedback zoom");
  const input = page.locator("[data-editor-image]");
  const result = page.locator("[data-result-image]");
  const inputViewport = page.locator("[data-viewport]");
  const resultViewport = page.locator("[data-result-viewport]");
  // Opening the editor runs Deskew (#209); no button.
  await expect(result).toHaveAttribute("src", /\/stages\/deskew\/preview\/\?v=/, {
    timeout: 30_000,
  });
  await expect.poll(() => naturalWidth(result)).toBeGreaterThan(0);
  await expect.poll(() => naturalWidth(input)).toBeGreaterThan(0);

  /** Each image's box relative to its own pane. */
  const placement = async () => {
    const [i, r, iv, rv] = await Promise.all([
      input.boundingBox(),
      result.boundingBox(),
      inputViewport.boundingBox(),
      resultViewport.boundingBox(),
    ]);
    return {
      input: { x: i!.x - iv!.x, y: i!.y - iv!.y, width: i!.width },
      result: { x: r!.x - rv!.x, y: r!.y - rv!.y, width: r!.width },
    };
  };
  const expectAligned = async () => {
    // The blank page is unchanged by Deskew: both panes show it at the same place.
    const now = await placement();
    expect(Math.abs(now.input.width - now.result.width)).toBeLessThanOrEqual(2);
    expect(Math.abs(now.input.x - now.result.x)).toBeLessThanOrEqual(2);
    expect(Math.abs(now.input.y - now.result.y)).toBeLessThanOrEqual(2);
    return now;
  };

  const fitted = await expectAligned();
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect
    .poll(async () => (await placement()).result.width)
    .toBeCloseTo(fitted.result.width * 1.25, 0);
  const zoomed = await expectAligned();
  expect(zoomed.input.width).toBeCloseTo(fitted.input.width * 1.25, 0);

  // Panning the result pane moves the input with it.
  const pane = (await resultViewport.boundingBox())!;
  await page.mouse.move(pane.x + 10, pane.y + pane.height - 10);
  await page.mouse.down();
  await page.mouse.move(pane.x + 50, pane.y + pane.height - 30, { steps: 5 });
  await page.mouse.up();
  const panned = await expectAligned();
  expect(panned.input.x - zoomed.input.x).toBeCloseTo(40, 0);
  expect(panned.input.y - zoomed.input.y).toBeCloseTo(-20, 0);
});
