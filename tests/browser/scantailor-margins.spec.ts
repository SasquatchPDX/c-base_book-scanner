import { expect, test, type Page } from "@playwright/test";

/** Margins gives aligned pages one common size, like Scan Tailor's page layout (#208). */

async function signIn(page: Page): Promise<void> {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("fry");
  await page.getByLabel("Password").fill("fry");
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** A 600x800 page with a block of glyph-like marks inside ``[x0, y0, x1, y1]``. */
async function textPage(page: Page, block: [number, number, number, number]): Promise<Buffer> {
  const base64 = await page.evaluate(async ([x0, y0, x1, y1]) => {
    const canvas = new OffscreenCanvas(600, 800);
    const context = canvas.getContext("2d")!;
    context.fillStyle = "rgb(235,235,235)";
    context.fillRect(0, 0, 600, 800);
    context.fillStyle = "rgb(25,25,25)";
    for (let y = y0; y < y1; y += 20) {
      for (let x = x0; x < x1; x += 7) context.fillRect(x, y, 5, 10);
    }
    const blob = await canvas.convertToBlob({ type: "image/png" });
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }, block);
  return Buffer.from(base64, "base64");
}

type Size = { width: number; height: number };

test("aligned pages share the largest page's size; an unaligned page keeps its own", async ({
  page,
}) => {
  test.setTimeout(150_000);
  await signIn(page);
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill("Common page size");
  await page.getByRole("button", { name: "Create workspace" }).click();
  // A full text page and a small title block.
  const blocks: [number, number, number, number][] = [
    [90, 120, 510, 640],
    [250, 200, 350, 240],
  ];
  for (const [index, block] of blocks.entries()) {
    await page.getByLabel("Page image").setInputFiles({
      name: `page${index + 1}.png`,
      mimeType: "image/png",
      buffer: await textPage(page, block),
    });
    await page.getByLabel("Sequence").fill(String(index + 1));
    await page.getByRole("button", { name: "Upload page" }).click();
    await expect(page.locator(".scan-page-card")).toHaveCount(index + 1);
  }
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();
  await expect(page.locator("[data-strip-page]")).toHaveCount(2);

  const jobId = /\/scan\/jobs\/([^/]+)\/editor\//.exec(page.url())![1];
  const pageIds = await page
    .locator("[data-strip-page]")
    .evaluateAll((nodes) => nodes.map((node) => (node as HTMLElement).dataset.stripPage!));
  const marginsRow = async (pageId: string) => {
    const response = await page.request.get(`/scan/jobs/${jobId}/pages/${pageId}/stages/`);
    const rows = ((await response.json()) as { stages: Record<string, unknown>[] }).stages;
    return rows.find((row) => row.stage === "margins")!;
  };
  /** The page size of a current Margins result, else null. */
  const pageSize = async (pageId: string): Promise<Size | null> => {
    const row = await marginsRow(pageId);
    const parameters = row.parameters as Record<string, unknown>;
    return row.current === true && row.status === "success"
      ? ((parameters.page_size_mm as Size | undefined) ?? null)
      : null;
  };
  const strip = (index: number) => page.locator("[data-strip-page]").nth(index);
  const controls = page.locator("[data-stage-controls]");

  // Deskew on each page; the later stages follow with their defaults (#205 chaining),
  // Margins with upstream's 10/5/10/5 mm, aligned with the other pages.
  for (const index of [0, 1]) {
    await strip(index).click();
    // Opening the page runs Deskew (#209 ensure-on-open); the rest chain from it.
    await page.locator('[data-stage-select="deskew"]').click();
    await expect(
      strip(index).locator('.st-status-icon[data-stage="output"]'),
    ).toHaveAttribute("data-status", "success", { timeout: 45_000 });
  }

  // Both pages come out at the full page's size (the title page is re-run if it
  // finished before the full page's content size was known).
  let common: Size | null = null;
  await expect
    .poll(
      async () => {
        const [first = null, second = null] = await Promise.all(pageIds.map(pageSize));
        common = first;
        return first !== null && JSON.stringify(first) === JSON.stringify(second);
      },
      { timeout: 45_000 },
    )
    .toBe(true);
  await expect(page.locator(".st-status-failed")).toHaveCount(0);

  // The editor shows the common size, the soft margins and the upstream controls.
  await strip(1).click();
  await page.locator('[data-stage-select="margins"]').click();
  const resultLine = controls.locator("[data-margins-result]");
  await expect(resultLine).toContainText(
    `Page ${common!.width.toFixed(1)} × ${common!.height.toFixed(1)} mm (common size`,
  );
  await expect(resultLine).toContainText("Soft margins");
  await expect(controls.getByLabel("Top", { exact: true })).toHaveValue("5");
  await expect(controls.getByLabel("Left", { exact: true })).toHaveValue("10");
  const align = controls.getByLabel("Align with other pages");
  await expect(align).toBeChecked();
  const cells = controls.locator('[data-align]');
  await expect(cells).toHaveCount(9);
  await expect(controls.locator('[data-align="top-center"]')).toBeChecked();
  await expect(cells.first()).toBeEnabled();

  // Linked sides: the right margin follows the left.
  await controls.getByLabel("Left", { exact: true }).fill("12");
  await expect(controls.getByLabel("Right", { exact: true })).toHaveValue("12");
  await controls.getByLabel("Left", { exact: true }).fill("10");

  // Not aligned: the grid is disabled and the page keeps its own (smaller) size.
  await align.uncheck();
  await expect(cells.first()).toBeDisabled();
  // The change autosaves and reruns (#209).
  await expect(resultLine).toContainText("not aligned with other pages", { timeout: 30_000 });
  await expect
    .poll(async () => {
      const own = await pageSize(pageIds[1]!);
      return own !== null && own.width < common!.width && own.height < common!.height;
    }, { timeout: 30_000 })
    .toBe(true);
  // The full page is unaffected: it alone sets the common size.
  expect(await pageSize(pageIds[0]!)).toEqual(common);
  await expect(page.locator(".st-status-failed")).toHaveCount(0);
});
