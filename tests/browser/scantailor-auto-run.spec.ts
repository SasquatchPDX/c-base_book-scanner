import { expect, test, type Page } from "@playwright/test";

// #209: the editor runs the stage being shown and reruns it when a setting changes.
// There is no "Save and run" button.

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

interface PostedStage {
  stage: { scan_job_id: string; scan_page_id: string; stage: string; revision: number };
}

test("every stage reruns by itself when a setting changes", async ({ page }) => {
  test.setTimeout(150_000);
  await signIn(page);
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill("Automatic runs");
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
  await expect(page.locator("[data-scantailor-editor]")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save and run" })).toHaveCount(0);

  const controls = page.locator("[data-stage-controls]");
  const icon = (stage: string) =>
    page.locator(`[data-strip-page] .st-status-icon[data-stage="${stage}"]`).first();

  // Opening the editor ran Deskew, and each finished stage ran the next one.
  for (const stage of ["deskew", "select_content", "dewarping", "margins", "output"]) {
    await expect(icon(stage)).toHaveAttribute("data-status", "success", { timeout: 60_000 });
  }

  /** Change a setting with ``act`` and wait for the new revision's result. */
  const changeAndWait = async (stage: string, act: () => Promise<void>): Promise<void> => {
    await page.locator(`[data-stage-select="${stage}"]`).click();
    const saved = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" && response.url().endsWith(`/stages/${stage}/`),
    );
    await act();
    const response = await saved;
    expect(response.status()).toBe(202);
    const posted = ((await response.json()) as PostedStage).stage;
    const stagesUrl = `/scan/jobs/${posted.scan_job_id}/pages/${posted.scan_page_id}/stages/`;
    await expect
      .poll(
        async () => {
          const body = (await (await page.request.get(stagesUrl)).json()) as {
            stages: { stage: string; revision: number; status: string }[];
          };
          const state = body.stages.find((candidate) => candidate.stage === stage)!;
          return `${state.revision}:${state.status}`;
        },
        { timeout: 30_000 },
      )
      .toBe(`${posted.revision}:success`);
    await expect(icon(stage)).toHaveAttribute("data-status", "success", { timeout: 30_000 });
  };

  await changeAndWait("deskew", () => controls.locator("[data-deskew-angle]").fill("1.5"));
  await expect(controls.locator("[data-deskew-result]")).toContainText("(manual).");

  await changeAndWait("select_content", () =>
    controls.getByRole("radio", { name: "Disabled" }).check(),
  );
  await expect(controls.locator("[data-content-result]")).toContainText("detection disabled");

  await changeAndWait("dewarping", () => controls.getByRole("radio", { name: "Off" }).check());
  await expect(controls.locator("[data-dewarp-result]")).toContainText("Dewarping is off");

  // The result pane beside the input (#206) shows each new result.
  const result = page.locator("[data-result-image]");
  const resultFor = async (stage: string): Promise<void> => {
    await expect(result).toBeVisible({ timeout: 30_000 });
    await expect(result).toHaveAttribute("src", new RegExp(`/stages/${stage}/preview/\?v=`));
  };
  // Margins in millimetres (#208) autosave like every other control.
  await changeAndWait("margins", () =>
    controls.getByLabel("Left", { exact: true }).fill("4"),
  );
  await resultFor("margins");

  await changeAndWait("output", async () => {
    await page.getByLabel("Output algorithm").selectOption("sauvola");
  });
  await resultFor("output");

  // Reopening a page whose results are current runs nothing.
  const posts: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/stages\/[a-z_]+\/$/.test(request.url())) {
      posts.push(request.url());
    }
  });
  const ensured = page.waitForResponse((response) => response.url().endsWith("/output/ensure/"));
  await page.reload();
  await page.locator('[data-stage-select="output"]').click();
  expect(((await (await ensured).json()) as { outcome: string }).outcome).toBe("current");
  expect(posts).toHaveLength(0);
});
