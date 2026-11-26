import { expect, test, type Page } from "@playwright/test";

import { openJobWithPages } from "./scan-helpers";

/** Stages are edited in any order; results follow from input fingerprints (#205). */

async function signIn(page: Page): Promise<void> {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("fry");
  await page.getByLabel("Password").fill("fry");
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** A 600x800 page with lines of glyph-like marks. */
async function textPage(page: Page): Promise<Buffer> {
  const base64 = await page.evaluate(async () => {
    const canvas = new OffscreenCanvas(600, 800);
    const context = canvas.getContext("2d")!;
    context.fillStyle = "rgb(235,235,235)";
    context.fillRect(0, 0, 600, 800);
    context.fillStyle = "rgb(25,25,25)";
    for (let y = 120; y < 640; y += 20) {
      for (let x = 90; x < 510; x += 7) context.fillRect(x, y, 5, 10);
    }
    const blob = await canvas.convertToBlob({ type: "image/png" });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  });
  return Buffer.from(base64, "base64");
}

const stagePost = (page: Page, stage: string) =>
  page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith(`/stages/${stage}/`),
  );

test("Margins edited before Deskew waits, then runs and re-runs on its own", async ({ page }) => {
  test.setTimeout(120_000);
  await signIn(page);
  // Open the editor on Margins: opening a stage runs it (#209), and Deskew must not
  // have run yet.
  await openJobWithPages(
    page,
    "Stage order",
    [{ name: "page.png", buffer: await textPage(page) }],
    "stage=margins",
  );

  const message = page.locator("[data-editor-message]");
  const icon = (stage: string) =>
    page.locator(`[data-strip-page] .st-status-icon[data-stage="${stage}"]`).first();

  // 1. Margins first: opening it queues nothing, it waits for Deskew.
  await expect(page.locator('[data-stage-select="margins"]')).toHaveClass(/active/);
  await expect(message).toContainText("Waiting for Deskew");
  // Millimetres (#208); the right margin follows the left (linked). The edit is saved
  // by itself after a short pause (#209); until then the editor says it is saving.
  const margin = page.locator("[data-stage-controls]").getByLabel("Left", { exact: true });
  const saved = stagePost(page, "margins");
  await margin.fill("1.7");
  await expect(message).toContainText("Saving");
  const result = page.locator("[data-result-image]");
  const resultStatus = page.locator("[data-result-status]");
  await expect(result).toBeHidden();

  // Saving stores the settings; nothing runs and nothing fails.
  const response = await saved;
  expect(response.status()).toBe(202);
  const body = (await response.json()) as { job: unknown; waiting_for: string | null };
  expect(body.job).toBeNull();
  expect(body.waiting_for).toBe("deskew");
  await expect(message).toContainText("Waiting for Deskew");
  await expect(resultStatus).toContainText("Waiting for Deskew");
  await expect(result).toBeHidden();
  await expect(icon("margins")).toHaveAttribute("data-kind", "waiting");
  await expect(page.locator(".st-status-failed")).toHaveCount(0);

  // 2. Opening Deskew runs it (#209); Select Content, Dewarping and then Margins follow
  // without a click (#205).
  await page.locator('[data-stage-select="deskew"]').click();
  for (const stage of ["deskew", "select_content", "dewarping", "margins", "output"]) {
    await expect(icon(stage)).toHaveAttribute("data-status", "success", { timeout: 30_000 });
    await expect(icon(stage)).toHaveAttribute("data-kind", /^(done|manual)$/);
  }
  await expect(page.locator(".st-status-failed")).toHaveCount(0);

  // 3. Deskew edited after Margins succeeded: Margins keeps its settings and re-runs.
  const jobId = /\/scan\/jobs\/([^/]+)\/editor\//.exec(page.url())![1];
  const pageId = await page.locator("[data-strip-page]").first().getAttribute("data-strip-page");
  const marginsRow = async () => {
    const stages = await page.request.get(`/scan/jobs/${jobId}/pages/${pageId}/stages/`);
    const rows = ((await stages.json()) as { stages: Record<string, unknown>[] }).stages;
    return rows.find((row) => row.stage === "margins")!;
  };
  const before = await marginsRow();
  expect(before.current).toBe(true);

  const deskewSaved = stagePost(page, "deskew");
  await page.locator("[data-stage-controls] [data-deskew-angle]").fill("2");
  expect((await deskewSaved).status()).toBe(202);
  await expect(
    page.locator("[data-stage-controls]").getByRole("radio", { name: "Manual" }),
  ).toBeChecked();
  await expect
    .poll(
      async () => {
        const row = await marginsRow();
        const parameters = row.parameters as Record<string, unknown>;
        return row.current === true && row.output_sha256 !== before.output_sha256
          ? parameters.left_mm
          : null;
      },
      { timeout: 30_000 },
    )
    .toBe(1.7);
  for (const stage of ["deskew", "select_content", "dewarping", "margins", "output"]) {
    await expect(icon(stage)).toHaveAttribute("data-kind", /^(done|manual)$/, {
      timeout: 30_000,
    });
  }
  await expect(page.locator(".st-status-failed")).toHaveCount(0);

  await page.locator('[data-stage-select="margins"]').click();
  await expect(margin).toHaveValue("1.7");
  await expect(message).toContainText("success");
  // Only the current result is shown: the re-run one, not the stale one before it.
  await expect(result).toBeVisible();
  await expect(result).toHaveAttribute("src", /\/stages\/margins\/preview\/\?v=/);
  await expect(result).not.toHaveAttribute(
    "src",
    new RegExp(`-${String(before.output_sha256).slice(0, 16)}$`),
  );
  await expect(resultStatus).toBeHidden();
});
