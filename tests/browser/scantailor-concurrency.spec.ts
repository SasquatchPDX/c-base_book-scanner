import { expect, test, type BrowserContext, type Locator, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { createScanJob, openJobWithPages, uploadPage } from "./scan-helpers";

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

/** Run every stage of the job's first page in the editor (the job's only page, #228). */
async function runAllScanTailorStages(page: Page): Promise<void> {
  await expect(page).toHaveURL(/\/editor\/(\?.*)?$/);
  for (const stage of SCANTAILOR_STAGES) {
    // Opening a stage runs it, and each finished stage runs the next one (#209).
    await page.locator(`[data-stage-select="${stage}"]`).click();
    // The page strip's status icon for this stage (#195 step 3).
    await expect(
      page.locator(`[data-strip-page] .st-status-icon[data-stage="${stage}"]`).first(),
    ).toHaveAttribute("data-status", "success", { timeout: 30_000 });
  }
}

/** Request a rescan of the open page from the editor's dialog, with a reason (#228). */
async function requestRescan(page: Page, reason: string): Promise<void> {
  await page.locator("[data-request-rescan]").click();
  const dialog = page.locator("[data-rescan-dialog]");
  await dialog.getByLabel("Reason").fill(reason);
  await dialog.getByRole("button", { name: "Request rescan" }).click();
  await expect(page.locator("[data-strip-page] [data-marker='rescan']")).toBeVisible();
  await expect(page.locator("[data-request-rescan]")).toHaveText("Rescan requested");
}

/** Confirm finish in the editor (#228); it goes on to the identification or finalization. */
async function finishInEditor(page: Page, options: { ocr?: boolean } = {}): Promise<void> {
  await page.getByRole("button", { name: "Finish…" }).click();
  const dialog = page.locator("[data-finish-dialog]");
  await expect(dialog.locator("[data-finish-blockers]")).toBeHidden();
  if (options.ocr) await dialog.getByLabel("Submit cleaned PDF to OCR").check();
  await dialog.getByRole("button", { name: "Finish", exact: true }).click();
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
  // Two sign-ins, an upload and a stage run that waits up to 30 s for its result.
  test.setTimeout(90_000);
  const firstContext = await browser.newContext();
  const secondContext = await browser.newContext();

  try {
    const firstEditor = await signIn(firstContext, "fry");
    const workspaceUrl = await openJobWithPages(firstEditor, "Browser Concurrency Fixture", [
      { name: "page-0001.png", buffer: SCAN_PAGE_PNG },
    ]);
    const secondEditor = await signIn(secondContext, "leela");
    await secondEditor.route("**/workspace/events/", (route) => route.abort());
    const bundleLoaded = secondEditor.waitForResponse(
      (response) => response.url().endsWith("/static/core/dist/scantailor-workspace.js"),
    );
    // Opening the editor asks the server to run the stage (#209); let that answer
    // arrive before the first editor changes the stage.
    const secondEnsured = secondEditor.waitForResponse((response) =>
      response.url().endsWith("/stages/deskew/ensure/"),
    );
    await secondEditor.goto(workspaceUrl);
    await expect((await bundleLoaded).status()).toBe(200);
    await expect((await secondEnsured).status()).toBe(200);

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
    await expect(firstControls.locator("[data-deskew-result]")).toContainText("(manual).", {
      timeout: 30_000,
    });

    // The second editor still holds the old revision; its autosave must not overwrite.
    const conflict = secondEditor.waitForResponse(
      (response) => response.request().method() === "POST" && /\/stages\/deskew\/$/.test(response.url()),
    );
    await secondControls.getByRole("radio", { name: "Manual" }).check();
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
    const workspaceUrl = await createScanJob(member, title, "Grace Hopper");
    await uploadPage(member, workspaceUrl, { name: "physical-page-0001.png", buffer: SCAN_PAGE_PNG });
    await member.goto(workspaceUrl);
    await runAllScanTailorStages(member);

    // Finish waits for requested rescans (#228): the dialog says why.
    await requestRescan(member, "Blurred lower margin");
    await member.getByRole("button", { name: "Finish…" }).click();
    const finishDialog = member.locator("[data-finish-dialog]");
    await expect(finishDialog.locator("[data-finish-blockers]")).toContainText(
      "1 page(s) wait for a requested rescan: 1.",
    );
    await expect(finishDialog.getByRole("button", { name: "Finish", exact: true })).toBeDisabled();
    await finishDialog.getByRole("button", { name: "Cancel" }).click();

    // The replacement reaches the open editor: the marker goes away without a reload.
    await uploadPage(member, workspaceUrl, {
      name: "physical-page-0001-rescan.png",
      buffer: RESCAN_PAGE_PNG,
    });
    await expect(member.locator("[data-strip-page] [data-marker='rescan']")).toHaveCount(0);
    // The replacement changed Deskew's input; every stage runs again (#205).
    await member.reload();
    await runAllScanTailorStages(member);

    // No library book yet: finish starts the identification first.
    await finishInEditor(member);
    await expect(member).toHaveURL(/\/scan-identifications\/[0-9a-f-]+\/(\?view=html)?$/);
    await expect(member.locator(".status-card .status")).toHaveText("Success", {
      timeout: 30_000,
    });

    await member.goto("/reviews/?view=html");
    await member.getByRole("link", { name: title }).click();
    await expect(member.locator(".candidate-list")).toContainText("Grace Hopper");
    await member.getByRole("button", { name: "Accept" }).click();

    await member.goto(workspaceUrl);
    await expect(member.getByRole("link", { name: title })).toBeVisible();
    await finishInEditor(member, { ocr: true });
    await expect(member).toHaveURL(/\/scan-finalizations\/[0-9a-f-]+\/(\?view=html)?$/);
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
    // Every device write is retried once: the repeat answers 200 and stores nothing new.
    const cover = { multipart: {
      metadata: JSON.stringify({ sha256: raw.sha256, captured_at: started }), image: asImage(SCAN_PAGE_PNG),
    } };
    expect((await device.put(job + "/covers/front", cover)).status()).toBe(201);
    expect((await device.put(job + "/covers/front", cover)).status()).toBe(200);
    const metadata = { multipart: {
      metadata: JSON.stringify(manifest.metadata_opf), opf: { name: "metadata.opf", mimeType: "application/oebps-package+xml", buffer: opf },
    } };
    expect((await device.put(job + "/metadata", metadata)).status()).toBe(201);
    expect((await device.put(job + "/metadata", metadata)).status()).toBe(200);
    const events = { first_line_no: 1, lines: [JSON.stringify({ type: "job", job_id: jobId })] };
    expect((await (await device.post(job + "/events", { data: events })).json()).sans_last_line_no).toBe(1);
    expect((await (await device.post(job + "/events", { data: events })).json()).sans_last_line_no).toBe(1);
    expect((await (await device.get(job)).json()).pages).toEqual([{ sequence_number: 0, sha256: raw.sha256, version: 1 }]);

    // A manifest naming bytes serif does not hold never authorizes local deletion.
    const wrong = { ...manifest, pages: [{ sequence_number: 0, sha256: digest(RESCAN_PAGE_PNG) }] };
    const refused = await device.post(job + "/capture-complete", { data: wrong });
    expect(refused.status()).toBe(409);
    const refusal = await refused.json();
    expect(refusal.verified).not.toBe(true);
    expect(refusal.error).toMatchObject({ code: "incomplete", details: { mismatched: ["page:0"] } });
    const complete = await device.post(job + "/capture-complete", { data: manifest });
    expect(complete.status()).toBe(200);
    const verified = await complete.json();
    expect(verified).toMatchObject({ verified: true, state: "captured" });
    expect((await (await device.post(job + "/capture-complete", { data: manifest })).json()).verified).toBe(true);

    const member = await signIn(memberContext, "fry");
    const detailsURL = "/scan/jobs/" + jobId + "/";
    await member.goto("/scan/raw/");
    await member.locator('a[href*="' + jobId + '/editor/"]').click();
    // Finish takes the book from the scanner's OPF (#228: the editor's finish dialog says so).
    await expect(member.locator("[data-finish-dialog]")).toContainText(title);
    const first = (await (await member.request.get(detailsURL)).json()).pages[0];
    expect(first.available_versions).toHaveLength(1);
    expect(first.active_version.checksum_sha256).toBe(raw.sha256);
    const finish = async () => {
      await runAllScanTailorStages(member);
      await finishInEditor(member);
      await expect(member).toHaveURL(/\/scan-finalizations\/[0-9a-f-]+\/(\?view=html)?$/);
      await expect(member.locator(".status-card .status")).toHaveText("Success", { timeout: 30_000 });
      const receipt = await (await member.request.get(new URL(member.url()).pathname)).json();
      return receipt.cleaned_variant_id as number;
    };
    const firstPDF = await finish();
    await member.goto(verified.portal_url);
    await requestRescan(member, "Retake the lower margin");
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
    await expect(member.locator("[data-refinish-notice]")).toBeVisible();
    await expect(member.locator("[data-raw-scan]")).toBeVisible();
    const refinishedPDF = await finish();
    expect(refinishedPDF).toEqual(expect.any(Number));
    expect(refinishedPDF).not.toBe(firstPDF);
    const finished = await (await member.request.get(detailsURL)).json();
    expect(finished.state).toBe("complete");
    await member.goto("/library/books/" + finished.book_id + "/");
    const downloadable = member.locator(".variant-list > div").filter({ has: member.getByText("PDF", { exact: true }) });
    const link = downloadable.getByRole("link", { name: "Download" });
    // The member downloads the re-finished PDF built from the replacement, not the first one.
    const href = new URL((await link.getAttribute("href"))!, member.url());
    expect(href.searchParams.get("variant_id")).toBe(String(refinishedPDF));
    const pending = member.waitForEvent("download");
    await link.click();
    const download = await pending;
    const path = await download.path();
    expect((await readFile(path!)).subarray(0, 5).toString()).toBe("%PDF-");
    const status = await (await device.get(job)).json();
    expect(status).toMatchObject({ state: "complete", open_rescans: 0 });
    expect(status.note).toBeUndefined();
    expect(status.pages).toEqual([{ sequence_number: 0, sha256: digest(RESCAN_PAGE_PNG), version: 2 }]);
  } finally {

    await memberContext.close();
    await device.dispose();
  }
});
