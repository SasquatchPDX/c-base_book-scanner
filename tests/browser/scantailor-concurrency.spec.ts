import { expect, test, type BrowserContext, type Locator, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

const SCAN_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAACACAIAAAB7vvvtAAAA80lEQVR42u3YsQkEIRRFUZVpxsT+6zCxjUnMrUA0Ez7nxZvs4QuXyXP+yfYrCAABAgQIECBAgAwQIECAAAGKte/mR7W2qP9/jO6CPDFAD5d9k3ZBgADpIB3kiQHSQeaCAAHSQTrIEwOkg8wFAQKkg3SQJwZIB5kLAgRIB+kgTwyQDnJBBgiQDtJBnhggHeSCDBAgHaSDPDFAOsgFGSBAOkgHeWKAdJALAmSAdJAO8sQA6SAXBMgA6SAd5IkB0kEuCJAB0kE6yBMDpINcECBAFqCDjs3igjwxQDrIBQEyQIAAAQIECBAgAwQIECBAgAABAmSbLV6uSh6FE6P0AAAAAElFTkSuQmCC",
  "base64",
);

const RESCAN_PAGE_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAGAAAACACAIAAAB7vvvtAAABY0lEQVR4nO3awQ7BQBRAUSP+/5drYSOkjlaCdO7Z2jQ3j8ybGsuynLLu/OsH+HcFggJBgaBAUCAoEFzWPhhjfPM5fm7tPNgEweoE3cxwzn79XWmCoEBQICgQFAgKBAWCAkGBoEBQICgQFAgKBAWCAkGBADeKzw5wV73pmrQJggJBgWDzb9AM7znuNUFQICgQFAgKBAWCAkGBoEAw4za/SRMEBYICQdt8/1H8TIGgQFAgKBAUCAoEBYICwfG3+Q+P/k0QFAgKBG3z0ARBgaBAUCAoEBQICgQFggLB8bf517gYNEFQICgQtM1DEwQFggJBgaBAUCAoEBQICgQTbfP7doAmCAoEBYK2eWiCoEBQICgQFAgKBAWCAkGBYKJt/sGbK0ETBAWCAkHbPDRBUCAoEBQICgQFggJBgaBAUCAoEBQICgQFggIB7oMOcwO9WxMEY7Ybwq2aICgQFAgKBAWCAkGB4AqkgC4O427g9AAAAABJRU5ErkJggg==",
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

const SCANTAILOR_STAGES = [
  "deskew",
  "select_content",
  "dewarping",
  "margins",
  "output",
] as const;

/** Run every stage of the job's first page in the editor, then return to the overview. */
async function runAllScanTailorStages(page: Page): Promise<void> {
  const overviewUrl = page.url();
  await page.getByRole("link", { name: "Open ScanTailor editor" }).click();
  await expect(page).toHaveURL(/\/editor\/$/);
  for (const stage of SCANTAILOR_STAGES) {
    await page.locator(`[data-stage-select="${stage}"]`).click();
    const queued = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/stages/${stage}/`),
    );
    await page.getByRole("button", { name: "Save and run" }).click();
    await expect((await queued).status()).toBe(202);
    // The page strip's status icon for this stage (#195 step 3).
    await expect(
      page.locator(`[data-strip-page] .st-status-icon[data-stage="${stage}"]`).first(),
    ).toHaveAttribute("data-status", "success", { timeout: 30_000 });
  }
  await page.goto(overviewUrl);
}

async function waitForTextAcrossReloads(
  page: Page,
  locator: Locator,
  expected: string,
): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if ((await locator.textContent())?.includes(expected)) return;
    await page.waitForTimeout(200);
    await page.reload();
  }
  await expect(locator).toContainText(expected);
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

    await firstEditor.getByRole("link", { name: "Open ScanTailor editor" }).click();
    await expect(firstEditor).toHaveURL(/\/editor\/$/);
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
    // Deskew is the first stage (#195) and starts in auto mode for both editors.
    await expect(firstControls.getByRole("radio", { name: "Auto" })).toBeChecked();
    await expect(secondControls.getByRole("radio", { name: "Auto" })).toBeChecked();

    // Switching to manual commits at once, as upstream does (#195 step 4).
    const saved = firstEditor.waitForResponse(
      (response) => response.request().method() === "POST" && /\/stages\/deskew\/$/.test(response.url()),
    );
    await firstControls.getByRole("radio", { name: "Manual" }).check();
    await expect((await saved).status()).toBe(202);
    await expect(firstEditor.locator("[data-editor-message]")).toContainText("Saved.");

    // The second editor still holds the old revision; saving it must not overwrite.
    const conflict = secondEditor.waitForResponse(
      (response) => response.request().method() === "POST" && /\/stages\/deskew\/$/.test(response.url()),
    );
    await secondEditor.getByRole("button", { name: "Save and run" }).click();
    await expect((await conflict).status()).toBe(409);
    await expect(secondEditor.locator("[data-editor-message]")).toContainText(
      "Conflict detected: another editor changed this stage",
    );
    await expect(secondControls.getByRole("radio", { name: "Manual" })).toBeChecked();
  } finally {
    await firstContext.close();
    await secondContext.close();
  }
});

test("a member completes the physical-book pipeline through the browser", async ({ browser }) => {
  test.setTimeout(120_000);
  const context = await browser.newContext({ acceptDownloads: true });
  const title = "Browser Physical Pipeline";

  try {
    const member = await signIn(context, "fry");
    await member.goto("/scan/jobs/?view=html");
    await member.getByLabel("Title hint").fill(title);
    await member.getByLabel("Author hints").fill("Grace Hopper");
    await member.getByRole("button", { name: "Create workspace" }).click();
    await expect(member).toHaveURL(/\/scan\/jobs\/[0-9a-f-]+\/workspace\/$/);
    const workspaceUrl = member.url();

    await member.getByLabel("Page image").setInputFiles({
      name: "physical-page-0001.png",
      mimeType: "image/png",
      buffer: SCAN_PAGE_PNG,
    });
    await member.getByLabel("Sequence").fill("1");
    await member.getByRole("button", { name: "Upload page" }).click();
    await expect(member.locator(".scan-page-card")).toHaveCount(1);
    await runAllScanTailorStages(member);

    await member.getByPlaceholder("Why is a rescan needed?").fill("Blurred lower margin");
    await member.getByRole("button", { name: "Request rescan" }).click();
    await expect(member.locator(".scan-page-card")).toContainText("Rescan requested");

    await member.getByLabel("Page image").setInputFiles({
      name: "physical-page-0001-rescan.png",
      mimeType: "image/png",
      buffer: RESCAN_PAGE_PNG,
    });
    await member.getByLabel("Sequence").fill("1");
    await member.getByRole("button", { name: "Upload page" }).click();
    await expect(member.locator(".scan-page-card")).not.toContainText("Rescan requested");
    await expect(
      member.locator('[data-stage-row][data-stage="output"] [data-stage-status]'),
    ).toHaveText("invalidated");
    await runAllScanTailorStages(member);

    await member.getByRole("button", { name: "Search metadata" }).click();
    await expect(member).toHaveURL(/\/scan-identifications\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 30_000,
    });

    await member.goto("/reviews/?view=html");
    await member.getByRole("link", { name: title }).click();
    await expect(member.locator(".candidate-list")).toContainText("Grace Hopper");
    await member.getByRole("button", { name: "Accept" }).click();

    await member.goto(workspaceUrl);
    await expect(member.getByRole("link", { name: title })).toBeVisible();
    await member.getByLabel("Submit cleaned PDF to OCR").check();
    await member.getByRole("button", { name: "Queue finalization" }).click();
    await expect(member).toHaveURL(/\/scan-finalizations\/[0-9a-f-]+\/$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 30_000,
    });

    await member.goto(`/library/books/?view=html&q=${encodeURIComponent(title)}`);
    await member.getByRole("link", { name: title }).click();
    const variants = member.locator(".variant-list");
    await waitForTextAcrossReloads(member, variants, "browser-physical-pipeline-ocr.txt");
    await expect(variants).toContainText("browser-physical-pipeline-searchable.pdf");

    const downloadStarted = member.waitForEvent("download");
    await variants
      .locator("div", { hasText: "browser-physical-pipeline-ocr.txt" })
      .getByRole("link", { name: "Download" })
      .click();
    const download = await downloadStarted;
    expect(download.suggestedFilename()).toBe("browser-physical-pipeline-ocr.txt");
    expect(await download.failure()).toBeNull();
  } finally {
    await context.close();
  }
});

test("a scanner capture survives a lost acknowledgement and a member re-finishes its rescan", async ({ browser, playwright }) => {
  test.setTimeout(150_000);
  const key = process.env.BOOK_SCANNER_BROWSER_DEVICE_KEY;
  test.skip(!key, "requires the scanner key created by the infrastructure fixture");
  const baseURL = process.env.BOOK_SCANNER_BROWSER_DEVICE_URL!;
  const device = await playwright.request.newContext({ baseURL, extraHTTPHeaders: { Authorization: "Bearer " + key } });
  const memberContext = await browser.newContext({ acceptDownloads: true });

  const date = new Date();
  const started = date.toISOString();
  const jobId = started.slice(0, 19).replace(/[:-]/g, "") + "Z-" + (date.getMilliseconds() + 1);
  const job = "/api/scanner/v1/jobs/" + jobId;
  const title = "Device acceptance " + jobId;
  const digest = (data: Buffer) => createHash("sha256").update(data).digest("hex");
  const opf = Buffer.from('<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>' +
    title + '</dc:title><dc:creator>Ada Scanner</dc:creator><dc:language>en</dc:language></metadata></package>');
  const raw = { sha256: digest(SCAN_PAGE_PNG), camera_role: "left", captured_at: started, recognized_page_number: "1" };
  const asImage = (bytes: Buffer) => ({ name: "page.png", mimeType: "image/png", buffer: bytes });
  const manifest = {
    finished_at: started, sans_last_line_no: 1, pages: [{ sequence_number: 0, sha256: raw.sha256 }],
    covers: [{ position: "front", sha256: raw.sha256 }], metadata_opf: { sha256: digest(opf) },
  };
  try {
    expect((await device.put(job, { data: { scan_started_at: started, capture_layout: "single_page" } })).status()).toBe(201);
    const premature = await device.post(job + "/capture-complete", { data: manifest });
    expect(premature.status()).toBe(409);
    expect((await premature.json()).verified).not.toBe(true);

    // Drop the acknowledgement after the server has committed the page, then retry from the device.
    const form = new FormData();
    form.append("metadata", JSON.stringify(raw));
    form.append("image", new Blob([new Uint8Array(SCAN_PAGE_PNG)], { type: "image/png" }), "page.png");
    const firstReply = await fetch(new URL(job + "/pages/0", baseURL), {
      method: "PUT", headers: { Authorization: "Bearer " + key }, body: form,
    });
    expect(firstReply.status).toBe(201);
    await firstReply.body!.cancel();
    expect((await device.put(job + "/pages/0", { multipart: {
      metadata: JSON.stringify(raw), image: asImage(SCAN_PAGE_PNG),
    } })).status()).toBe(200);
    expect((await device.put(job + "/covers/front", { multipart: {
      metadata: JSON.stringify({ sha256: raw.sha256, captured_at: started }), image: asImage(SCAN_PAGE_PNG),
    } })).status()).toBe(201);
    expect((await device.put(job + "/metadata", { multipart: {
      metadata: JSON.stringify(manifest.metadata_opf), opf: { name: "metadata.opf", mimeType: "application/oebps-package+xml", buffer: opf },
    } })).status()).toBe(201);
    const events = { first_line_no: 1, lines: [JSON.stringify({ type: "job", job_id: jobId })] };
    expect((await (await device.post(job + "/events", { data: events })).json()).sans_last_line_no).toBe(1);
    const complete = await device.post(job + "/capture-complete", { data: manifest });
    expect(complete.status()).toBe(200);
    const verified = await complete.json();
    expect(verified).toMatchObject({ verified: true, state: "captured" });
    expect((await (await device.post(job + "/capture-complete", { data: manifest })).json()).verified).toBe(true);

    const member = await signIn(memberContext, "fry");
    const detailsURL = "/scan/jobs/" + jobId + "/";
    await member.goto("/scan/raw/");
    await member.locator('a[href*="' + jobId + '/workspace/"]').click();
    await expect(member.locator("[data-opf-title]")).toHaveText(title);
    const first = (await (await member.request.get(detailsURL)).json()).pages[0];
    expect(first.available_versions).toHaveLength(1);
    expect(first.active_version.checksum_sha256).toBe(raw.sha256);
    const finish = async () => {
      await runAllScanTailorStages(member);
      await member.getByRole("button", { name: "Queue finalization" }).click();
      await expect(member.locator(".status-card .status")).toHaveText("Success", { timeout: 30_000 });
    };
    await finish();
    await member.goto(verified.portal_url);
    await member.getByPlaceholder("Why is a rescan needed?").fill("Retake the lower margin");
    await member.getByRole("button", { name: "Request rescan" }).click();
    const todos = (await (await device.get(job + "/rescans")).json()).todos;
    expect(todos).toHaveLength(1);
    expect(todos[0].sequence_number).toBe(0);
    const csrf = (await memberContext.cookies()).find(cookie => cookie.name === "csrftoken");
    expect(csrf).toBeDefined();
    const claim = await member.request.post("/scan/todos/" + todos[0].todo_id + "/claim/", {
      headers: { "X-CSRFToken": csrf!.value, Referer: member.url() },
    });
    expect((await claim.json()).state).toBe("claimed");
    const replacement = { multipart: {
      metadata: JSON.stringify({ ...raw, sha256: digest(RESCAN_PAGE_PNG), page_number_verified: true }),
      image: asImage(RESCAN_PAGE_PNG),
    } };
    const replaceURL = job + "/rescans/" + todos[0].todo_id + "/replacement";
    const replaced = await device.post(replaceURL, replacement);
    expect(replaced.status()).toBe(201);
    expect(await replaced.json()).toMatchObject({ todo_state: "completed", version: 2, previous_version: 1, state: "captured" });
    expect((await device.post(replaceURL, replacement)).status()).toBe(200);
    const next = (await (await member.request.get(detailsURL)).json()).pages[0];
    expect(next.id).toBe(first.id);
    expect(next.available_versions.map((v: { checksum_sha256: string }) => v.checksum_sha256)).toEqual([digest(RESCAN_PAGE_PNG), raw.sha256]);
    expect(next.active_version.version).toBe(2);
    await member.goto(verified.portal_url);
    await expect(member.locator("[data-raw-scan]")).toContainText("re-finish needed");
    await finish();
    const finished = await (await member.request.get(detailsURL)).json();
    expect(finished.state).toBe("complete");
    await member.goto("/library/books/" + finished.book_id + "/");
    const downloadable = member.locator(".variant-list > div").filter({ has: member.getByText("PDF", { exact: true }) });
    const pending = member.waitForEvent("download");
    await downloadable.getByRole("link", { name: "Download" }).click();
    const download = await pending;
    const path = await download.path();
    expect((await readFile(path!)).subarray(0, 5).toString()).toBe("%PDF-");
  } finally {

    await memberContext.close();
    await device.dispose();
  }
});
