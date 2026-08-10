import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const SCAN_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAACACAIAAAB7vvvtAAACMklEQVR4nO3dMariQACA4US2EsHK0lbSK0oaSzv1CF5AGz1EwEPoBSztbb2BlRcQUqWW2cLm8dbkh6f7Nuz7v0rJEGZ+JiGkSRxCiFSu8a8nUHcGAgYCBgIGAgYCBgK/yg7Ecfyd86iDp4+E7iBQuoMefshzdsXl4g4CBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAvA+6KlmszkcDh+/Z7PZer3e7Xb7/b4oiu12O5lMno5514y/WyhRcbTdbn/8e7vdxuPx/X6/XC5JkjwdU3MVi33DJZbn+XK5bDQa3W43z/PXT1grX7nEPkmSJEmSKIoOh8N0On39hLUSh5K3zo/XtE+Pfry/ZFmWpmkURdfrdT6fn06nTqdTNqa2Khb7hntQCKEoin6/fz6fK8bUWcVi33APCiEsFovNZjMajV4/W928eomladrr9Var1WAwiKKo1Wodj8c/x2RZ9jdm/y4Vi/1KoP9PxWJ9kgYGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYCBgIGAgYC8GWWH/hR9U/cQaD0sxF6cAcBAwEDAQMBAwEDgd/H9m8chcCElAAAAABJRU5ErkJggg==",
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
