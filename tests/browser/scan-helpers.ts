import { expect, type Page } from "@playwright/test";

/** A page image to upload with the session API. */
export interface PageUpload {
  name: string;
  buffer: Buffer;
  mimeType?: string;
  sequence?: number;
}

/**
 * Create a web scan job from the job list; the browser lands in its editor (#228: the
 * job overview with its upload form is retired). Returns the editor URL.
 */
export async function createScanJob(
  page: Page,
  title: string,
  authors = "",
): Promise<string> {
  await page.goto("/scan/jobs/?view=html");
  await page.getByLabel("Title hint").fill(title);
  if (authors) await page.getByLabel("Author hints").fill(authors);
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page).toHaveURL(/\/scan\/jobs\/[0-9a-f-]+\/editor\/$/);
  return page.url();
}

export function jobIdOf(editorUrl: string): string {
  return /\/scan\/jobs\/([^/]+)\/editor\//.exec(editorUrl)![1]!;
}

/**
 * Upload a page the way a generic scanner client does (``POST /scan/jobs/{id}/pages/``,
 * docs/scanner-protocol.md): the scanner uploads pages, the web UI does not.
 */
export async function uploadPage(page: Page, editorUrl: string, upload: PageUpload): Promise<void> {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken");
  const response = await page.request.post(`/scan/jobs/${jobIdOf(editorUrl)}/pages/`, {
    headers: { Accept: "application/json", "X-CSRFToken": csrf?.value ?? "", Referer: editorUrl },
    multipart: {
      file: { name: upload.name, mimeType: upload.mimeType ?? "image/png", buffer: upload.buffer },
      logical_sequence_number: String(upload.sequence ?? 1),
      side: "single",
    },
  });
  expect(response.status(), await response.text()).toBeLessThan(300);
}

/** Create a job, upload its pages and open the editor with them (``query`` e.g. "stage=margins"). */
export async function openJobWithPages(
  page: Page,
  title: string,
  uploads: PageUpload[],
  query = "",
): Promise<string> {
  const editorUrl = await createScanJob(page, title);
  for (const [index, upload] of uploads.entries()) {
    await uploadPage(page, editorUrl, { sequence: index + 1, ...upload });
  }
  await page.goto(query ? `${editorUrl}?${query}` : editorUrl);
  await expect(page.locator("[data-scantailor-editor]")).toBeVisible();
  await expect(page.locator("[data-strip-page]")).toHaveCount(uploads.length);
  return editorUrl;
}
