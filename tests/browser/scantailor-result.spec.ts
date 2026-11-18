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

    const input = page.getByRole("button", { name: "Input", exact: true });
    const result = page.getByRole("button", { name: "Result", exact: true });
    const image = page.locator("[data-editor-image]");
    const save = async () => {
      await page.getByRole("button", { name: "Save and run" }).click();
      await expect(result).toHaveAttribute("aria-pressed", "true", { timeout: 30_000 });
      await expect(image).toHaveAttribute("alt", "Processing-stage result preview");
    };
    for (const stage of ["deskew", "select_content"]) {
      await page.locator(`[data-stage-select="${stage}"]`).click();
      await save();
    }

    // Dewarping runs before Margins (#195); on a blank page it explains its fallback.
    await page.locator('[data-stage-select="dewarping"]').click();
    await save();
    await expect(page.locator("[data-stage-warnings]")).toContainText(
      "the input was kept unchanged",
    );

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
    await expect(page.locator("[data-stage-warnings]")).toContainText(
      "the input was kept unchanged",
    );
    await page.locator('[data-stage-select="margins"]').click();
    await expect(page.locator("[data-stage-warnings]")).toBeHidden();
  });
}
