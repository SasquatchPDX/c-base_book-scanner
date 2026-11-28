import { expect, test } from "@playwright/test";

const EPUB_FIXTURE = process.env.BOOK_SCANNER_BROWSER_EPUB_FIXTURE;

test("the upload picker offers TXT and the server rejects unsupported files", async ({ page }) => {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("leela");
  await page.getByLabel("Password").fill("leela");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto("/imports/upload/");

  const picker = page.getByLabel("Book file", { exact: true });
  await expect(picker).toHaveAttribute("accept", /(?:^|,)\.txt(?:,|$)/);
  await expect(page.locator("#book-file-help")).toContainText("TXT");
  const choosing = page.waitForEvent("filechooser");
  await picker.click();
  await (await choosing).setFiles({
    name: "Browser Plain Story.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Title: Browser Plain Story\nAuthor: Ada Lovelace\n\nA plain text story.\n"),
  });
  await page.getByRole("button", { name: "Start import" }).click();
  await expect(page).toHaveURL(/\/imports\/[0-9a-f-]+\/$/);
  await expect(page.locator(".status-card .status")).toHaveText("Low confidence", {
    timeout: 30_000,
  });
  await page.getByRole("link", { name: "Resolve metadata" }).click();
  await page.getByRole("link", { name: "Browser Plain Story", exact: true }).click();
  // This candidate comes from the same external-provider fixture as EPUB intake.
  await expect(page.locator(".candidate-list")).toContainText("Ada Lovelace");
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await page.goto("/library/books/?view=html&q=Browser%20Plain%20Story");
  await page.getByRole("link", { name: "Browser Plain Story", exact: true }).click();
  await expect(page.locator(".variant-list")).toContainText("TXT");

  await page.goto("/imports/upload/");
  // Browser filters are advisory; the server must still reject a bypassed picker.
  await page.getByLabel("Book file", { exact: true }).setInputFiles({
    name: "unsupported.docx",
    mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("unsupported input"),
  });
  const rejected = page.waitForResponse(
    response => response.url().endsWith("/imports/upload/") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Start import" }).click();
  expect((await rejected).status()).toBe(400);
  await expect(page.locator("body")).toContainText("unsupported file format: unsupported.docx");
});

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
    await member.getByLabel("Book file", { exact: true }).setInputFiles(EPUB_FIXTURE);
    await member.getByRole("button", { name: "Start import" }).click();
    await expect(member).toHaveURL(/\/imports\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Low confidence", {
      timeout: 30_000,
    });

    await member.getByRole("link", { name: "Resolve metadata" }).click();
    await member.getByRole("link", { name: title }).click();
    // Title, author and publisher match, but the provider's edition is in another
    // language than the file, so the import was held for review instead of auto-accepted.
    await expect(member.locator('[data-block-reason="edition_conflict"]')).toContainText(
      "describes another edition than the file",
    );
    await expect(member.locator(".candidate-list")).toContainText("Ada Digital");

    // The reviewer can read the actual file in the collapsed-by-default reader panel.
    const reader = member.locator("[data-review-reader]");
    await expect(reader.locator("[data-reader-body]")).toBeHidden();
    await reader.getByRole("button", { name: "Read the book" }).click();
    await expect(reader.getByRole("tab", { name: /EPUB/ })).toHaveAttribute("aria-selected", "true");
    await expect(reader.locator("iframe")).toHaveAttribute("sandbox", "");
    await expect(reader.frameLocator("iframe").locator("body")).toContainText(
      "Browser Digital Library",
    );
    await reader.getByRole("button", { name: "Close" }).click();
    await expect(reader.locator("[data-reader-body]")).toBeHidden();

    await member.getByRole("button", { name: "Accept" }).click();

    // Search-first landing page (#28): shelf, facet link and author index, no JavaScript needed.
    await member.goto("/library/?view=html");
    await expect(member.getByRole("search")).toBeVisible();
    const recentlyAdded = member.getByRole("region", { name: "Recently added" });
    await expect(recentlyAdded.getByRole("link", { name: new RegExp(title) })).toBeVisible();
    const facets = member.getByRole("navigation", { name: "Browse by facet" });
    await facets.getByRole("link", { name: "EPUB", exact: true }).click();
    await expect(member).toHaveURL(/[?&]format=epub(&|$)/);
    await expect(member.getByRole("link", { name: title })).toBeVisible();
    for (const image of await member.locator("img.cover-thumb").all()) {
      await expect(image).toHaveAttribute("loading", "lazy");
      await expect(image).toHaveAttribute("width", /\d+/);
    }
    await member.goto("/library/authors/?view=html&letter=A");
    await member.getByRole("link", { name: "Ada Digital" }).click();
    await expect(member).toHaveURL(/[?&]author_id=\d+/);
    await expect(member.getByRole("link", { name: title })).toBeVisible();

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
