import { expect, test, type Page } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("leela");
  await page.getByLabel("Password").fill("leela");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto("/imports/upload/");
});

async function openTextReview(page: Page, title: string) {
  await page.getByLabel("Book file").setInputFiles({
    name: title + ".txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Title: " + title + "\nAuthor: Ada Digital\n\nChapter one.\n"),
  });
  await page.getByRole("button", { name: "Start import" }).click();
  await page.getByRole("link", { name: "Resolve metadata" }).click({ timeout: 30000 });
  await page.getByRole("link", { name: title, exact: true }).click();
}

test("an edition tie creates the shared work without either edition's metadata", async ({ page }) => {
  const fixture = process.env.BOOK_SCANNER_BROWSER_TIED_EPUB;
  if (!fixture) throw new Error("The tied-edition EPUB fixture is required");
  await page.getByLabel("Book file").setInputFiles(fixture);
  await page.getByRole("button", { name: "Start import" }).click();
  await page.getByRole("link", { name: "Open book" }).click({ timeout: 30000 });
  await expect(page.getByRole("heading", { name: "Browser Edition Twins" })).toBeVisible();
  await expect(page.locator(".variant-list")).toContainText("EPUB");
  await expect(page.locator("body")).not.toContainText(/9780140327243|9780439554930|First Browser Press|Second Browser Press/);
});

test("a member inspects and confirms a local manuscript suggestion", async ({ page }) => {
  const title = "Browser Local Manuscript";
  await openTextReview(page, title);
  const form = page.locator("[data-local-suggestion-form]");
  await expect(form.getByLabel("Title")).toHaveValue(title);
  await expect(form.getByLabel("Authors")).toHaveValue("Ada Digital");
  await form.getByRole("button", { name: "Create book from suggestion" }).click();
  await page.goto("/library/books/?view=html&q=" + encodeURIComponent(title));
  await page.getByRole("link", { name: title, exact: true }).click();
  await expect(page.locator(".variant-list")).toContainText("TXT");
});

test("a replacement search invalidates an open candidate and keeps edited terms", async ({ page, context }) => {
  await openTextReview(page, "Browser Review Race");
  const oldTab = await context.newPage();
  await oldTab.goto(page.url());
  const oldCandidate = oldTab.locator(".candidate-list").getByRole("button", { name: "Accept", exact: true });
  await expect(oldCandidate).toBeVisible();
  const refine = page.locator("section.action-panel").filter({ has: page.getByRole("heading", { name: "Refine search" }) });
  await refine.getByLabel("Title").fill("Revised Browser Title");
  await refine.getByRole("button", { name: "Save terms only" }).click();
  await expect(refine.getByLabel("Title")).toHaveValue("Revised Browser Title");
  await refine.getByRole("button", { name: "Search providers again" }).click();
  await expect(page.locator(".candidate-list")).toContainText("Revised Browser Title", { timeout: 30000 });
  await oldCandidate.click();
  await expect(oldTab.locator("body")).toContainText("candidate is from an earlier search");
  await page.getByLabel("ISBN", { exact: true }).fill("9780306406157");
  await page.getByRole("button", { name: "Look up ISBN" }).click();
  await expect(page.locator("body")).toContainText("Accepted automatically", { timeout: 30000 });
  await expect(page.locator("body")).toContainText("Verified Browser Edition");
});
