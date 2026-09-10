import { expect, test } from "@playwright/test";

const EPUB_FIXTURE = process.env.BOOK_SCANNER_BROWSER_EPUB_FIXTURE;

test("a member completes digital intake, review, search, download, and conversion", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  if (!EPUB_FIXTURE) {
    throw new Error("BOOK_SCANNER_BROWSER_EPUB_FIXTURE is required");
  }

  const context = await browser.newContext({ acceptDownloads: true });
  const title = "Browser Digital Library";

  try {
    const member = await context.newPage();
    await member.goto("/accounts/login/");
    await member.getByLabel("LDAP username").fill("leela");
    await member.getByLabel("Password").fill("leela");
    await member.getByRole("button", { name: "Sign in" }).click();
    await expect(member.locator(".identity")).toHaveText("leela");

    await member.goto("/imports/upload/");
    await member.getByLabel("EPUB or PDF").setInputFiles(EPUB_FIXTURE);
    await member.getByRole("button", { name: "Start import" }).click();
    await expect(member).toHaveURL(/\/imports\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Low confidence", {
      timeout: 30_000,
    });

    await member.getByRole("link", { name: "Resolve metadata" }).click();
    await member.getByRole("link", { name: title }).click();
    await expect(member.locator(".candidate-list")).toContainText("Ada Digital");
    await member.getByRole("button", { name: "Accept" }).click();

    await member.goto(`/library/books/?view=html&q=${encodeURIComponent(title)}`);
    await member.getByRole("link", { name: title }).click();
    const variants = member.locator(".variant-list");
    await expect(variants).toContainText("browser-digital-library.epub");

    const originalDownloadStarted = member.waitForEvent("download");
    await variants
      .locator("div", { hasText: "browser-digital-library.epub" })
      .getByRole("link", { name: "Download" })
      .click();
    const originalDownload = await originalDownloadStarted;
    expect(originalDownload.suggestedFilename()).toBe("browser-digital-library.epub");
    expect(await originalDownload.failure()).toBeNull();

    await member.getByRole("button", { name: "EPUB → MOBI" }).click();
    await expect(member).toHaveURL(/\/conversions\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 60_000,
    });
    await member.getByRole("link", { name: "Return to book" }).click();

    const mobiVariant = member.locator(".variant-list > div", {
      has: member.locator("strong", { hasText: /^MOBI$/ }),
    });
    await expect(mobiVariant).toHaveCount(1);
    const convertedDownloadStarted = member.waitForEvent("download");
    await mobiVariant.getByRole("link", { name: "Download" }).click();
    const convertedDownload = await convertedDownloadStarted;
    expect(convertedDownload.suggestedFilename()).toMatch(/\.mobi$/);
    expect(await convertedDownload.failure()).toBeNull();
  } finally {
    await context.close();
  }
});
