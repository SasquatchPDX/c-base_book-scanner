import { expect, test, type BrowserContext, type Locator, type Page } from "@playwright/test";

const SCAN_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAACACAIAAAB7vvvtAAACMklEQVR4nO3dMariQACA4US2EsHK0lbSK0oaSzv1CF5AGz1EwEPoBSztbb2BlRcQUqWW2cLm8dbkh6f7Nuz7v0rJEGZ+JiGkSRxCiFSu8a8nUHcGAgYCBgIGAgYCBgK/yg7Ecfyd86iDp4+E7iBQuoMefshzdsXl4g4CBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAvA+6KlmszkcDh+/Z7PZer3e7Xb7/b4oiu12O5lMno5514y/WyhRcbTdbn/8e7vdxuPx/X6/XC5JkjwdU3MVi33DJZbn+XK5bDQa3W43z/PXT1grX7nEPkmSJEmSKIoOh8N0On39hLUSh5K3zo/XtE+Pfry/ZFmWpmkURdfrdT6fn06nTqdTNqa2Khb7hntQCKEoin6/fz6fK8bUWcVi33APCiEsFovNZjMajV4/W928eomladrr9Var1WAwiKKo1Wodj8c/x2RZ9jdm/y4Vi/1KoP9PxWJ9kgYGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYC8GWWH/hR9U/cQaD0sxF6cAcBAwEDAQMBAwEDgd/H9m8chcCElAAAAABJRU5ErkJggg==",
  "base64",
);

const RESCAN_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAACACAIAAAB7vvvtAAABY0lEQVR4nO3awQ7BQBRAUSP+/5drYSOkjlaCdO7Z2jQ3j8ybGsuynLLu/OsH+HcFggJBgaBAUCAoEFzWPhhjfPM5fm7tPNgEweoE3cxwzn79XWmCoEBQICgQFAgKBAWCAkGBoEBQICgQFAgKBAWCAkGBADeKzw5wV73pmrQJggJBgWDzb9AM7znuNUFQICgQFAgKBAWCAkGBoEAw4za/SRMEBYICQdt8/1H8TIGgQFAgKBAUCAoEBYICwfG3+Q+P/k0QFAgKBG3z0ARBgaBAUCAoEBQICgQFggLB8bf517gYNEFQICgQtM1DEwQFggJBgaBAUCAoEBQICgQTbfP7doAmCAoEBYK2eWiCoEBQICgQFAgKBAWCAkGBYKJt/sGbK0ETBAWCAkHbPDRBUCAoEBQICgQFggJBgaBAUCAoEBQICgQFggIB7oMOcwO9WxMEY7Ybwq2aICgQFAgKBAWCAkGB4AqkgC4O427g9AAAAABJRU5ErkJggg==",
  "base64",
);

async function signIn(context: BrowserContext, username: string): Promise<Page> {
  const page = await context.newPage();
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill(username);
  await page.getByLabel("Password").fill(username);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".identity")).toHaveText(username);
  return page;
}

const SCANTAILOR_STAGES = [
  "page_split",
  "deskew",
  "select_content",
  "margins",
  "dewarping",
  "output",
] as const;

async function runAllScanTailorStages(page: Page): Promise<void> {
  for (const stage of SCANTAILOR_STAGES) {
    await page.locator(`[data-stage-select="${stage}"]`).click();
    const stageRow = page.locator(`[data-stage-row][data-stage="${stage}"]`);
    const queued = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/stages/${stage}/`),
    );
    await page.getByRole("button", { name: "Save and run" }).click();
    await expect((await queued).status()).toBe(202);
    await expect(stageRow.locator("[data-stage-status]")).toHaveText("success", {
      timeout: 30_000,
    });
  }
}

async function waitForTextAcrossReloads(
  page: Page,
  locator: Locator,
  expected: string,
): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if ((await locator.textContent())?.includes(expected)) return;
    await page.waitForTimeout(200);
    await page.reload();
  }
  await expect(locator).toContainText(expected);
}

test("two LDAP members receive a visible conflict instead of overwriting a stage", async ({
  browser,
}) => {
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();

  try {
    const firstEditor = await signIn(firstContext, "fry");
    await firstEditor.goto("/scan/jobs/?view=html");
    await firstEditor.getByLabel("Title hint").fill("Browser Concurrency Fixture");
    await firstEditor.getByLabel("Author hints").fill("Ada Lovelace");
    await firstEditor.getByRole("button", { name: "Create workspace" }).click();
    await expect(firstEditor).toHaveURL(/\/scan\/jobs\/[0-9a-f-]+\/workspace\/$/);

    await firstEditor.getByLabel("Page image").setInputFiles({
      name: "page-0001.png",
      mimeType: "image/png",
      buffer: SCAN_PAGE_PNG,
    });
    await firstEditor.getByLabel("Sequence").fill("1");
    await firstEditor.getByRole("button", { name: "Upload page" }).click();
    await expect(firstEditor.locator(".scan-page-card")).toHaveCount(1);

    const workspaceUrl = firstEditor.url();
    const secondEditor = await signIn(secondContext, "leela");
    await secondEditor.route("**/workspace/events/", (route) => route.abort());
    const bundleLoaded = secondEditor.waitForResponse(
      (response) => response.url().endsWith("/static/core/dist/scantailor-workspace.js"),
    );
    await secondEditor.goto(workspaceUrl);
    await expect((await bundleLoaded).status()).toBe(200);

    const firstControls = firstEditor.locator("[data-stage-controls]");
    const secondControls = secondEditor.locator("[data-stage-controls]");
    await expect(firstControls.getByLabel("Mode")).toHaveValue("auto");
    await expect(secondControls.getByLabel("Mode")).toHaveValue("auto");

    await firstControls.getByLabel("Mode").selectOption("single");
    const saved = firstEditor.waitForResponse(
      (response) => response.request().method() === "POST" && /\/stages\/page_split\/$/.test(response.url()),
    );
    await firstEditor.getByRole("button", { name: "Save and run" }).click();
    await expect((await saved).status()).toBe(202);
    await expect(firstEditor.locator("[data-editor-message]")).toContainText("Saved.");

    await secondControls.getByLabel("Mode").selectOption("manual");
    const conflict = secondEditor.waitForResponse(
      (response) => response.request().method() === "POST" && /\/stages\/page_split\/$/.test(response.url()),
    );
    await secondEditor.getByRole("button", { name: "Save and run" }).click();
    await expect((await conflict).status()).toBe(409);
    await expect(secondEditor.locator("[data-editor-message]")).toContainText(
      "Conflict detected: another editor changed this stage",
    );
    await expect(secondControls.getByLabel("Mode")).toHaveValue("single");
  } finally {
    await firstContext.close();
    await secondContext.close();
  }
});

test("a member completes the physical-book pipeline through the browser", async ({ browser }) => {
  test.setTimeout(120_000);
  const context = await browser.newContext({ acceptDownloads: true });
  const title = "Browser Physical Pipeline";

  try {
    const member = await signIn(context, "fry");
    await member.goto("/scan/jobs/?view=html");
    await member.getByLabel("Title hint").fill(title);
    await member.getByLabel("Author hints").fill("Grace Hopper");
    await member.getByRole("button", { name: "Create workspace" }).click();
    await expect(member).toHaveURL(/\/scan\/jobs\/[0-9a-f-]+\/workspace\/$/);
    const workspaceUrl = member.url();

    await member.getByLabel("Page image").setInputFiles({
      name: "physical-page-0001.png",
      mimeType: "image/png",
      buffer: SCAN_PAGE_PNG,
    });
    await member.getByLabel("Sequence").fill("1");
    await member.getByRole("button", { name: "Upload page" }).click();
    await expect(member.locator(".scan-page-card")).toHaveCount(1);
    await runAllScanTailorStages(member);

    await member.getByPlaceholder("Why is a rescan needed?").fill("Blurred lower margin");
    await member.getByRole("button", { name: "Request rescan" }).click();
    await expect(member.locator(".scan-page-card")).toContainText("Rescan requested");

    await member.getByLabel("Page image").setInputFiles({
      name: "physical-page-0001-rescan.png",
      mimeType: "image/png",
      buffer: RESCAN_PAGE_PNG,
    });
    await member.getByLabel("Sequence").fill("1");
    await member.getByRole("button", { name: "Upload page" }).click();
    await expect(member.locator(".scan-page-card")).not.toContainText("Rescan requested");
    await expect(
      member.locator('[data-stage-row][data-stage="output"] [data-stage-status]'),
    ).toHaveText("invalidated");
    await runAllScanTailorStages(member);

    await member.getByRole("button", { name: "Search metadata" }).click();
    await expect(member).toHaveURL(/\/scan-identifications\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 30_000,
    });

    await member.goto("/reviews/?view=html");
    await member.getByRole("link", { name: title }).click();
    await expect(member.locator(".candidate-list")).toContainText("Grace Hopper");
    await member.getByRole("button", { name: "Accept" }).click();

    await member.goto(workspaceUrl);
    await expect(member.getByRole("link", { name: title })).toBeVisible();
    await member.getByLabel("Submit cleaned PDF to OCR").check();
    await member.getByRole("button", { name: "Queue finalization" }).click();
    await expect(member).toHaveURL(/\/scan-finalizations\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 30_000,
    });

    await member.goto(`/library/books/?view=html&q=${encodeURIComponent(title)}`);
    await member.getByRole("link", { name: title }).click();
    const variants = member.locator(".variant-list");
    await waitForTextAcrossReloads(member, variants, "browser-physical-pipeline-ocr.txt");
    await expect(variants).toContainText("browser-physical-pipeline-searchable.pdf");

    const downloadStarted = member.waitForEvent("download");
    await variants
      .locator("div", { hasText: "browser-physical-pipeline-ocr.txt" })
      .getByRole("link", { name: "Download" })
      .click();
    const download = await downloadStarted;
    expect(download.suggestedFilename()).toBe("browser-physical-pipeline-ocr.txt");
    expect(await download.failure()).toBeNull();
  } finally {
    await context.close();
  }
});
