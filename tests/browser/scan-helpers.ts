import { expect, type Page } from "@playwright/test";

/** A page image to upload with the session API. */
export interface PageUpload {
  name: string;
  buffer: Buffer;
  mimeType?: string;
  sequence?: number;
}

async function csrfHeaders(page: Page, referer: string): Promise<Record<string, string>> {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken");
  return { Accept: "application/json", "X-CSRFToken": csrf?.value ?? "", Referer: referer };
}

/**
 * Create a web scan job through the session API (``POST /scan/jobs/``) and open its
 * editor. The web UI creates no jobs (#227: the scanner does; the form is "maybe
 * later") and the job overview is retired (#228). Returns the editor URL.
 */
export async function createScanJob(
  page: Page,
  title: string,
  authors = "",
): Promise<string> {
  // Also waits for a sign-in that is still navigating, so its CSRF cookie is set.
  await page.goto("/scan/jobs/?view=html");
  const created = await page.request.post("/scan/jobs/", {
    headers: await csrfHeaders(page, page.url()),
    data: {
      provisional_title: title,
      provisional_authors: authors
        .split(",")
        .map((name) => name.trim())
        .filter(Boolean),
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const jobId = String((await created.json()).id);
  await page.goto(`/scan/jobs/${jobId}/editor/`);
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
  const response = await page.request.post(`/scan/jobs/${jobIdOf(editorUrl)}/pages/`, {
    headers: await csrfHeaders(page, editorUrl),
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
