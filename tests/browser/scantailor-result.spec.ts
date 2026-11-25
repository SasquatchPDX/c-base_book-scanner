import { expect, test } from "@playwright/test";

// Pillow-generated 100×100 white page: automatic dewarping must explain its fallback.
const PAGE = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAIAAAD/gAIDAAAA5klEQVR4nO3SsREAIAwDMWD/ncMK+V6qXf35zsxh5y13iNV4ViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYg1tn7z/EDxbGcl84AAAAASUVORK5CYII=",
  "base64",
);

for (const transport of ["events", "polling"] as const) {
  test(`stage results, reduced margins and fallback warnings (${transport})`, async ({ page }) => {
    test.setTimeout(90_000);
    if (transport === "polling") {
      await page.addInitScript(() => {
        Object.defineProperty(window, "EventSource", { value: undefined });
      });
    }
    await page.goto("/accounts/login/");
    await page.getByLabel("LDAP username").fill("fry");
    await page.getByLabel("Password").fill("fry");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.goto("/scan/jobs/?view=html");
    await page.getByLabel("Title hint").fill(`Result feedback ${transport}`);
    await page.getByRole("button", { name: "Create workspace" }).click();
    await page.getByLabel("Page image").setInputFiles({
      name: "blank.png", mimeType: "image/png", buffer: PAGE,
    });
    await page.getByLabel("Sequence").fill("1");
    await page.getByRole("button", { name: "Upload page" }).click();
    await expect(page.locator(".scan-page-card")).toHaveCount(1);
    await page.getByRole("link", { name: "Open ScanTailor editor" }).click();
    await expect(page.locator("[data-scantailor-editor]")).toBeVisible();

    const input = page.getByRole("button", { name: "Input", exact: true });
    const result = page.getByRole("button", { name: "Result", exact: true });
    const image = page.locator("[data-editor-image]");
    const save = async () => {
      await page.getByRole("button", { name: "Save and run" }).click();
      await expect(result).toHaveAttribute("aria-pressed", "true", { timeout: 30_000 });
      await expect(image).toHaveAttribute("alt", "Processing-stage result preview");
    };
    // Deskew keeps the live input view and says when it leaves a page unchanged.
    await page.locator('[data-stage-select="deskew"]').click();
    await page.getByRole("button", { name: "Save and run" }).click();
    await expect(page.locator("[data-editor-message]")).toContainText("Angle 0.00°, no change", {
      timeout: 30_000,
    });
    await expect(input).toHaveAttribute("aria-pressed", "true");

    // Select Content also keeps the input view, with its box; this blank page has none.
    await page.locator('[data-stage-select="select_content"]').click();
    await page.getByRole("button", { name: "Save and run" }).click();
    await expect(page.locator("[data-editor-message]")).toContainText("No content found", {
      timeout: 30_000,
    });
    await expect(input).toHaveAttribute("aria-pressed", "true");

    // Dewarping runs before Margins (#195); on a blank page it explains its fallback
    // and stays on the input view with the model grid.
    await page.locator('[data-stage-select="dewarping"]').click();
    await page.getByRole("button", { name: "Save and run" }).click();
    await expect(page.locator("[data-editor-message]")).toContainText("No text lines", {
      timeout: 30_000,
    });
    await expect(page.locator("[data-stage-warnings]")).toContainText("the page is unchanged");
    await expect(input).toHaveAttribute("aria-pressed", "true");

    await page.locator('[data-stage-select="margins"]').click();
    await page.getByLabel("All margins (pixels)").fill("20");
    await save();
    await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth))
      .toBe(140);
    await input.click();
    await expect(image).toHaveAttribute("src", /input=true/);
    await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth))
      .toBe(100);
    await result.click();
    await expect(image).not.toHaveAttribute("src", /input=true/);

    // Reopen the stage, as a user does when adjusting an already completed result.
    await page.reload();
    await page.locator('[data-stage-select="margins"]').click();
    await page.getByLabel("All margins (pixels)").fill("0");
    await save();
    await expect.poll(() => image.evaluate(node => (node as HTMLImageElement).naturalWidth))
      .toBe(100);

    await page.locator('[data-stage-select="dewarping"]').click();
    await expect(page.locator("[data-stage-warnings]")).toContainText("the page is unchanged");
    await page.locator('[data-stage-select="margins"]').click();
    await expect(page.locator("[data-stage-warnings]")).toBeHidden();
  });
}
