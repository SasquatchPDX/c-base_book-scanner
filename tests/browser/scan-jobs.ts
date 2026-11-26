import { expect, type Page } from "@playwright/test";

// #227: the web UI no longer creates scan jobs or uploads pages (the scanner does).
// Browser tests create a web job and its pages through the session API instead, then
// open the job overview as the old form flow did.

export interface ScanPageUpload {
  name: string;
  buffer: Buffer;
  sequence: number;
}

async function csrfHeaders(page: Page): Promise<Record<string, string>> {
  const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken");
  expect(csrf).toBeDefined();
  return { "X-CSRFToken": csrf!.value, Referer: page.url() };
}

/** Store one page image (a new page, or a replacement for an existing sequence). */
export async function uploadScanPage(
  page: Page,
  jobId: string,
  upload: ScanPageUpload,
): Promise<void> {
  const response = await page.request.post(`/scan/jobs/${jobId}/pages/`, {
    headers: await csrfHeaders(page),
    multipart: {
      file: { name: upload.name, mimeType: "image/png", buffer: upload.buffer },
      logical_sequence_number: String(upload.sequence),
      side: "single",
    },
  });
  expect([200, 201, 202]).toContain(response.status());
}

/** Create a web scan job with ``pages``, open its overview and return the job id. */
export async function createWebScanJob(
  page: Page,
  title: string,
  pages: ScanPageUpload[],
  authors: string[] = [],
): Promise<string> {
  // Also waits for a sign-in that is still navigating, so its CSRF cookie is set.
  await page.goto("/scan/jobs/?view=html");
  const created = await page.request.post("/scan/jobs/", {
    headers: await csrfHeaders(page),
    data: { provisional_title: title, provisional_authors: authors },
  });
  expect(created.status()).toBe(201);
  const jobId = String((await created.json()).id);
  for (const upload of pages) {
    await uploadScanPage(page, jobId, upload);
  }
  await page.goto(`/scan/jobs/${jobId}/workspace/`);
  return jobId;
}
