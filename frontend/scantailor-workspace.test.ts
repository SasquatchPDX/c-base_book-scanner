import { afterEach, describe, expect, it, vi } from "vitest";

import {
  artifactToken,
  displayBoxToSource,
  editableParameters,
  INITIAL_PREVIEW_RETRY,
  initializeScanTailorWorkspace,
  nextPreviewRetry,
  outputZonesForEditing,
  normalizeBox,
  PREVIEW_RETRY_EXHAUSTED_MESSAGE,
  PREVIEW_RETRY_MAX_ATTEMPTS,
  previewErrorMessage,
  previewIsBeingPrepared,
  previewRetryUrl,
  previousStageName,
  resultPaneMessage,
  sourceBoxToDisplay,
  stageErrorText,
  stageReadiness,
  stageStatusLabel,
  AUTOSAVE_DELAY_MS,
  isOlderStageState,
  savingMessage,
  stableJson,
  stageStatusMessage,
  stageWorkerWarnings,
  STAGE_ORDER,
} from "./scantailor-workspace";
import { describeMargins } from "./margins-view";

describe("ScanTailor geometry coordinates", () => {
  it("round-trips a source box through a reduced preview", () => {
    const source = { width: 2400, height: 3200 };
    const display = { width: 600, height: 800 };
    const box = { x1: 120, y1: 240, x2: 2040, y2: 2800 };

    expect(displayBoxToSource(sourceBoxToDisplay(box, source, display), source, display)).toEqual(
      box,
    );
  });

  it("normalizes list and object boxes into ordered coordinates", () => {
    const fallback = { x1: 0, y1: 0, x2: 10, y2: 10 };

    expect(normalizeBox([90, 80, 10, 20], fallback)).toEqual({
      x1: 10,
      y1: 20,
      x2: 90,
      y2: 80,
    });
    expect(normalizeBox({ x1: 5, y1: 7, width: 20, height: 30 }, fallback)).toEqual({
      x1: 5,
      y1: 7,
      x2: 25,
      y2: 37,
    });
  });
});

describe("editable stage parameters", () => {
  it("turns detected geometry into independent editable lists, excluding provenance", () => {
    const parameters = {
      zone_mode: "auto",
      resolved_zones: [
        { kind: "picture", box: [1, 2, 30, 40], source: "auto" },
        { kind: "text", box: [50, 2, 90, 40], source: "auto" },
      ],
    };
    const zones = outputZonesForEditing(parameters);
    zones[0]!.box[0] = 8;
    expect(parameters.resolved_zones[0]!.box[0]).toBe(1);
    expect(zones[0]).not.toHaveProperty("source");
    expect(editableParameters("output", { ...parameters, zones: [] })).not.toHaveProperty(
      "resolved_zones",
    );
    expect(editableParameters("output", { ...parameters, zones: [] })).not.toHaveProperty("zones");
    expect(
      outputZonesForEditing({
        zone_mode: "manual",
        picture_box: [1, 2, 30, 40],
      }),
    ).toEqual([{ kind: "picture", box: [1, 2, 30, 40] }]);
    expect(
      outputZonesForEditing({
        zone_mode: "manual",
        picture_box: { x1: 1, y1: 2, width: 29, height: 38 },
      }),
    ).toEqual([{ kind: "picture", box: [1, 2, 30, 40] }]);
  });

  it("edits, adds and removes multiple detected regions through real controls", async () => {
    const state = {
      id: 1,
      stage: "output",
      stage_label: "Output",
      revision: 1,
      status: "success",
      algorithm: "binarization_otsu",
      parameters: {
        zone_mode: "auto",
        resolved_zones: [
          { kind: "picture", box: [1, 2, 30, 40] },
          { kind: "text", box: [50, 2, 90, 40] },
        ],
      },
    };
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify([{ id: "page", logical_sequence_number: 1, side: "single", stages: [state] }])}</script>
      <div id="zones-host" data-editable="true" data-stage-url-template="/pages/__PAGE__/stages/__STAGE__/"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><span data-live-status></span><h2 data-editor-title></h2><button data-revert-stage>Revert to saved</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-stage-select="output">Output</button></div>`;
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ stage: state }),
    });
    vi.stubGlobal("fetch", fetcher);
    const host = document.querySelector<HTMLElement>("#zones-host")!;
    initializeScanTailorWorkspace(host);
    host.querySelector<HTMLButtonElement>('[data-stage-select="output"]')!.click();
    const click = (label: string) =>
      [...host.querySelectorAll("button")].find((b) => b.textContent === label)!.click();
    click("Edit detected regions (2)");
    click("Add picture region");
    expect(host.textContent).toContain("Remove region 3");
    click("Remove region 2");
    // No button: the edits are saved together after a short pause (#209).
    expect(fetcher).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalled(), { timeout: 3000 });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const posted = JSON.parse(fetcher.mock.calls[0]![1].body);
    expect(posted.parameters.zone_mode).toBe("manual");
    expect(posted.parameters.zones.map((z: { kind: string }) => z.kind)).toEqual([
      "picture",
      "picture",
    ]);
    expect(posted.parameters).not.toHaveProperty("resolved_zones");
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });
  it("sends margins in millimetres, never legacy pixels or canvases (#208)", () => {
    // Pixel settings written before #208 are converted with the recorded dpi.
    expect(
      editableParameters("margins", {
        margin: 59,
        dpi: [300, 300],
        canvas_width: 140,
        canvas_height: 240,
        margin_left: 20,
      }),
    ).toEqual({
      left_mm: 5,
      top_mm: 5,
      right_mm: 5,
      bottom_mm: 5,
      align_x: "center",
      align_y: "top",
      align_with_others: true,
    });
    // A page never edited gets upstream's defaults; explicit values are kept.
    expect(editableParameters("margins", {})).toMatchObject({
      left_mm: 10,
      top_mm: 5,
      right_mm: 10,
      bottom_mm: 5,
    });
    expect(
      editableParameters("margins", {
        margins_mode: "manual",
        left_mm: 12.5,
        align_with_others: false,
        align_x: "right",
        align_y: "bottom",
      }),
    ).toEqual({
      margins_mode: "manual",
      left_mm: 12.5,
      top_mm: 5,
      right_mm: 10,
      bottom_mm: 5,
      align_x: "right",
      align_y: "bottom",
      align_with_others: false,
    });
  });
  it("describes the common page size and the soft margins (#208)", () => {
    expect(
      describeMargins({
        page_size_mm: { width: 148, height: 210 },
        aggregate_size_mm: { width: 148, height: 210 },
        soft_margins_mm: { left: 3, top: 0, right: 3, bottom: 12.5 },
      }),
    ).toBe(
      "Page 148.0 × 210.0 mm (common size 148.0 × 210.0 mm). Soft margins left 3.0, top 0.0, right 3.0, bottom 12.5 mm.",
    );
    expect(
      describeMargins({ page_size_mm: { width: 100, height: 50 }, align_with_others: false }),
    ).toBe("Page 100.0 × 50.0 mm: not aligned with other pages, it keeps its own size.");
    expect(describeMargins({})).toBe("");
  });
  it("keeps manual geometry while excluding worker-derived provenance", () => {
    expect(
      editableParameters("select_content", {
        content_mode: "manual",
        content_box: [20, 30, 400, 700],
        resolved_box: [18, 29, 402, 701],
        input_width: 600,
        output_sha256: "derived",
      }),
    ).toEqual({
      content_mode: "manual",
      content_box: [20, 30, 400, 700],
    });
  });

  it("retains output zone and algorithm controls", () => {
    const parameters = editableParameters("output", {
      output_mode: "sauvola",
      zone_mode: "manual",
      text_box: [10, 10, 100, 200],
      picture_box: [110, 10, 300, 200],
      text_algorithm: "wolf",
      picture_algorithm: "window",
      worker_job_id: "ignored",
    });

    expect(parameters).toMatchObject({
      output_mode: "sauvola",
      zone_mode: "manual",
      text_algorithm: "wolf",
      picture_algorithm: "window",
    });
    expect(parameters).not.toHaveProperty("worker_job_id");
  });
});

describe("worker warnings", () => {
  it("shows recorded fallback explanations and ignores absent or malformed values", () => {
    expect(stageWorkerWarnings({ dewarp_warning: "No reliable page geometry found" })).toBe(
      "No reliable page geometry found",
    );
    expect(stageWorkerWarnings({ output_warning: "Fell back to Otsu", dewarp_warning: null })).toBe(
      "Fell back to Otsu",
    );
    expect(stageWorkerWarnings({ output_warning: [], dewarp_warning: " " })).toBe("");
  });
});

describe("preview error messages", () => {
  it("never tells the first stage to run a preceding stage", () => {
    const message = previewErrorMessage("deskew", {
      status: 404,
      body: "input preview not ready",
    });

    expect(message).not.toMatch(/preceding|Run the/i);
    expect(message).toMatch(/scanned page image/i);
  });

  it("names the actual predecessor when the input preview is missing", () => {
    expect(previewErrorMessage("margins", { status: 404, body: "input preview not ready" })).toBe(
      "Waiting for Dewarping: its result is not ready yet.",
    );
    expect(previousStageName("deskew")).toBeNull();
    expect(previousStageName("dewarping")).toBe("select_content");
  });

  it("distinguishes storage, artifact, network and other failures", () => {
    expect(
      previewErrorMessage("deskew", {
        status: 503,
        body: "storage unavailable",
      }),
    ).toMatch(/storage is temporarily unavailable/i);
    expect(
      previewErrorMessage("deskew", {
        status: 404,
        body: "preview artifact missing",
      }),
    ).toMatch(/missing from storage/i);
    expect(previewErrorMessage("deskew", { status: 404, body: "preview not ready" })).toMatch(
      /not been generated yet/i,
    );
    expect(previewErrorMessage("deskew", { status: null, body: "" })).toMatch(/network error/i);
    expect(previewErrorMessage("deskew", { status: 500, body: "" })).toBe(
      "Could not load the preview (HTTP 500).",
    );
    for (const stage of STAGE_ORDER) {
      for (const status of [null, 200, 403, 404, 500, 503]) {
        expect(previewErrorMessage(stage, { status, body: "" })).not.toMatch(/preceding/i);
      }
    }
  });
});

describe("stage status presentation", () => {
  it("derives waiting and stale messages from the state, not a stored flag (#205)", () => {
    const success = { status: "success", revision: 3, last_error: "" };
    const unknown = { status: "unknown", revision: 2, last_error: "" };

    expect(
      stageStatusMessage(unknown, { current: false, waitingFor: "deskew", staleReason: "" }),
    ).toEqual({ text: "Waiting for Deskew.", tone: "info" });
    expect(
      stageStatusMessage(success, {
        current: false,
        waitingFor: null,
        staleReason: "settings changed, not applied",
      }),
    ).toEqual({ text: "Revision 3 · stale: settings changed, not applied", tone: "info" });
    expect(
      stageStatusMessage(success, { current: true, waitingFor: null, staleReason: "" }, true),
    ).toEqual({ text: "Settings not applied yet.", tone: "info" });
    expect(stageStatusMessage(unknown)).toEqual({ text: "Revision 2 · not run yet", tone: "info" });
  });

  it("shows the error only for failed stages", () => {
    const failed = {
      status: "failed",
      revision: 2,
      last_error: "cannot read image",
    };

    expect(stageErrorText(failed)).toBe("cannot read image");
    expect(stageStatusMessage(failed)).toEqual({
      text: "Failed: cannot read image",
      tone: "error",
    });
    expect(stageStatusMessage({ status: "success", revision: 1, last_error: "" })).toEqual({
      text: "Revision 1 · success",
      tone: "info",
    });
  });
});

describe("automatic runs (#209)", () => {
  const flush = async (): Promise<void> => {
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
  };
  const done = (stage: string) => ({
    stage,
    status: "success",
    revision: 1,
    parameters: {},
    output_sha256: `${stage}-out`,
    applied_input_sha256: stage === "deskew" ? "v1" : "",
    settings_applied: true,
    input_sha256: stage === "deskew" ? "v1" : "",
    updated_at: "2026-11-24T10:00:00+00:00",
  });
  /** Deskew to Dewarping current, so ``stage`` can run. */
  const readyPage = (state: Record<string, unknown>) => {
    const earlier = ["deskew", "select_content", "dewarping"].map(done);
    earlier[1]!.applied_input_sha256 = "deskew-out";
    earlier[2]!.applied_input_sha256 = "select_content-out";
    return [{ id: "page", logical_sequence_number: 1, side: "single", stages: [...earlier, state] }];
  };
  const host = (pages: unknown, extra = ""): HTMLElement => {
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify(pages)}</script>
      <div id="auto-host" data-editable="true" data-stage-url-template="/pages/__PAGE__/stages/__STAGE__/" data-ensure-url-template="/pages/__PAGE__/stages/__STAGE__/ensure/" ${extra}><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><span data-live-status></span><h2 data-editor-title></h2><button data-revert-stage>Revert to saved</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-stage-select="margins">Margins</button></div>`;
    return document.querySelector<HTMLElement>("#auto-host")!;
  };
  const margins = {
    id: 4,
    scan_page_id: "page",
    stage: "margins",
    stage_label: "Margins",
    revision: 1,
    status: "unknown",
    algorithm: "margin_pad",
    parameters: {},
    last_error: "",
    updated_at: "2026-11-24T10:00:00+00:00",
  };

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });

  it("says that a stage runs, or that it restarts with new settings", () => {
    const state = { status: "running", revision: 2, last_error: "" };
    const ready = { current: false, waitingFor: null, staleReason: "" };
    expect(stageStatusMessage(state, ready).text).toBe("Running…");
    expect(stageStatusMessage({ ...state, status: "queued" }, ready).text).toBe("Running…");
    expect(stageStatusMessage(state, ready, false, true).text).toBe(
      "Restarting with new settings…",
    );
    expect(savingMessage(false)).toBe("Saving…");
    expect(savingMessage(true)).toBe("Restarting with new settings…");
  });

  it("runs the opened stage and saves a changed setting without a button", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => ({
      ok: true,
      status: 202,
      json: async () => ({
        stage: url.endsWith("/ensure/")
          ? { ...margins, status: "queued", updated_at: "2026-11-24T10:00:01+00:00" }
          : {
              ...margins,
              revision: 2,
              status: "queued",
              parameters: JSON.parse(String(init?.body)).parameters,
              updated_at: "2026-11-24T10:00:02+00:00",
            },
      }),
    }));
    vi.stubGlobal("fetch", fetcher);
    const root = host(readyPage(margins));
    initializeScanTailorWorkspace(root);
    root.querySelector<HTMLButtonElement>('[data-stage-select="margins"]')!.click();
    await flush();
    // Opening the stage asks the server to run it (once; the server deduplicates).
    expect(fetcher.mock.calls.some(([url]) => url === "/pages/page/stages/margins/ensure/")).toBe(
      true,
    );
    const posts = () => fetcher.mock.calls.filter(([url]) => url === "/pages/page/stages/margins/");
    const top = root.querySelector<HTMLInputElement>('[data-margin="top"]')!;
    const message = root.querySelector("[data-editor-message]")!;
    top.value = "20";
    top.dispatchEvent(new Event("input", { bubbles: true }));
    top.value = "24";
    top.dispatchEvent(new Event("input", { bubbles: true }));
    // While the autosave waits, the status line says so instead of "Settings not applied".
    expect(message.textContent).toBe("Restarting with new settings…");
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS - 1);
    expect(posts()).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    await flush();
    expect(posts()).toHaveLength(1);
    const sent = JSON.parse(String(posts()[0]![1]!.body)).parameters;
    // The millimetre fields (#208) autosave like every other control; top and bottom
    // are linked while they are equal.
    expect([sent.top_mm, sent.bottom_mm]).toEqual([24, 24]);
    // Saved while queued: the old run restarts with the new settings.
    expect(message.textContent).toBe("Restarting with new settings…");
    // The same value again is not saved again.
    top.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    expect(posts()).toHaveLength(1);
  });

  it("keeps a live result that arrived before the save's own reply", async () => {
    vi.useFakeTimers();
    const listeners = new Map<string, (event: MessageEvent<string>) => void>();
    class FakeEventSource {
      constructor(readonly url: string) {}
      addEventListener(name: string, listener: (event: MessageEvent<string>) => void): void {
        listeners.set(name, listener);
      }
    }
    vi.stubGlobal("EventSource", FakeEventSource);
    let replySave: () => void = () => undefined;
    const queued = { ...margins, revision: 2, status: "queued", updated_at: "2026-11-24T10:00:02+00:00" };
    const succeeded = {
      ...queued,
      status: "success",
      output_sha256: "m2",
      applied_input_sha256: "dewarping-out",
      settings_applied: true,
      preview_storage_identifier: "m2.jpg",
      updated_at: "2026-11-24T10:00:03.250000+00:00",
    };
    const fetcher = vi.fn((url: string) => {
      const reply = (stage: unknown) => ({ ok: true, status: 202, json: async () => ({ stage }) });
      if (url.endsWith("/ensure/")) return Promise.resolve(reply({ ...margins }));
      // The save's reply is held back until the run's live update has arrived.
      return new Promise((resolve) => {
        replySave = () => resolve(reply(queued));
      });
    });
    vi.stubGlobal("fetch", fetcher);
    const root = host(readyPage(margins), 'data-events-url="/events/"');
    initializeScanTailorWorkspace(root);
    root.querySelector<HTMLButtonElement>('[data-stage-select="margins"]')!.click();
    await flush();
    const top = root.querySelector<HTMLInputElement>('[data-margin="top"]')!;
    top.value = "30";
    top.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    listeners.get("workspace")!(
      new MessageEvent("workspace", { data: JSON.stringify({ stages: [succeeded] }) }),
    );
    replySave();
    await flush();
    const message = root.querySelector("[data-editor-message]")!;
    // Not "stuck on queued": the older reply did not replace the newer success.
    expect(message.textContent).not.toMatch(/Running|Restarting|queued/);
    expect(root.querySelector("[data-result-status]")?.textContent ?? "").not.toMatch(/Queued/);
  });

  it("saves an edit for the stage it was made on when the editor moves on mid-save", async () => {
    vi.useFakeTimers();
    const replies: (() => void)[] = [];
    let revision = 1;
    const fetcher = vi.fn((url: string, init?: RequestInit) => {
      const reply = (stage: unknown) => ({ ok: true, status: 202, json: async () => ({ stage }) });
      if (url.endsWith("/ensure/")) return Promise.resolve(reply({ ...margins, revision }));
      revision += 1;
      const saved = {
        ...margins,
        revision,
        status: "queued",
        parameters: JSON.parse(String(init?.body)).parameters,
        updated_at: `2026-11-24T10:00:0${revision}+00:00`,
      };
      // Each save's reply is held until the test releases it.
      return new Promise((resolve) => replies.push(() => resolve(reply(saved))));
    });
    vi.stubGlobal("fetch", fetcher);
    const root = host(readyPage(margins));
    const deskew = document.createElement("button");
    deskew.dataset.stageSelect = "deskew";
    root.append(deskew);
    initializeScanTailorWorkspace(root);
    root.querySelector<HTMLButtonElement>('[data-stage-select="margins"]')!.click();
    await flush();
    const posts = () =>
      fetcher.mock.calls.filter(([url]) => !String(url).endsWith("/ensure/")) as unknown as [
        string,
        RequestInit,
      ][];
    const top = root.querySelector<HTMLInputElement>('[data-margin="top"]')!;
    top.value = "20";
    top.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    expect(posts()).toHaveLength(1); // in flight
    // A second change, then the member opens Deskew before its autosave ran.
    top.value = "40";
    top.dispatchEvent(new Event("input", { bubbles: true }));
    deskew.click();
    await flush();
    replies.shift()!();
    await flush();
    // The second change is saved for Margins, not dropped and not sent to Deskew.
    expect(posts()).toHaveLength(2);
    const [url, init] = posts()[1]!;
    expect(url).toBe("/pages/page/stages/margins/");
    const body = JSON.parse(String(init.body));
    expect(body.parameters.top_mm).toBe(40);
    expect(body.expected_revision).toBe(2); // the revision the first save produced
    replies.shift()!();
    await flush();
    expect(posts()).toHaveLength(2);
    expect(root.querySelector("[data-editor-title]")?.textContent).toContain("Deskew");
  });

  it("reverts to the settings a save in flight stores, not to older ones", async () => {
    vi.useFakeTimers();
    const replies: (() => void)[] = [];
    const fetcher = vi.fn((url: string, init?: RequestInit) => {
      const reply = (stage: unknown) => ({ ok: true, status: 202, json: async () => ({ stage }) });
      if (url.endsWith("/ensure/")) return Promise.resolve(reply({ ...margins }));
      const saved = {
        ...margins,
        revision: 2,
        status: "queued",
        parameters: JSON.parse(String(init?.body)).parameters,
        updated_at: "2026-11-24T10:00:02+00:00",
      };
      return new Promise((resolve) => replies.push(() => resolve(reply(saved))));
    });
    vi.stubGlobal("fetch", fetcher);
    const root = host(readyPage(margins));
    initializeScanTailorWorkspace(root);
    root.querySelector<HTMLButtonElement>('[data-stage-select="margins"]')!.click();
    await flush();
    const posts = () => fetcher.mock.calls.filter(([url]) => !String(url).endsWith("/ensure/"));
    const field = () => root.querySelector<HTMLInputElement>('[data-margin="top"]')!;
    field().value = "20";
    field().dispatchEvent(new Event("input", { bubbles: true }));
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    field().value = "40";
    field().dispatchEvent(new Event("input", { bubbles: true }));
    root.querySelector<HTMLButtonElement>("[data-revert-stage]")!.click();
    replies.shift()!();
    await flush();
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    expect(posts()).toHaveLength(1); // the reverted change is not saved
    expect(field().value).toBe("20"); // the stored settings are shown
  });
});

describe("merge order of stage updates (#209)", () => {
  const at = (revision: number, status: string, updated_at: string) => ({
    revision,
    status,
    updated_at,
  });

  it("never lets an older revision replace a newer one", () => {
    expect(isOlderStageState(at(1, "success", "b"), at(2, "queued", "a"))).toBe(true);
    expect(isOlderStageState(at(3, "queued", "a"), at(2, "success", "b"))).toBe(false);
  });

  it("orders updates of the same revision by updated_at, to the microsecond", () => {
    const queued = at(2, "queued", "2026-11-24T10:00:02.000100+00:00");
    const success = at(2, "success", "2026-11-24T10:00:02.000200+00:00");
    expect(isOlderStageState(queued, success)).toBe(true);
    expect(isOlderStageState(success, queued)).toBe(false);
    // Python omits a zero fraction.
    expect(
      isOlderStageState(
        at(2, "queued", "2026-11-24T10:00:02+00:00"),
        at(2, "success", "2026-11-24T10:00:02.5+00:00"),
      ),
    ).toBe(true);
    // A newer job for the same revision (#205) is a newer state, though less advanced.
    expect(
      isOlderStageState(
        at(2, "queued", "2026-11-24T10:00:05+00:00"),
        at(2, "success", "2026-11-24T10:00:03+00:00"),
      ),
    ).toBe(false);
  });

  it("falls back to how far the run got when the timestamps are equal or missing", () => {
    const same = "2026-11-24T10:00:02+00:00";
    expect(isOlderStageState(at(2, "queued", same), at(2, "running", same))).toBe(true);
    expect(isOlderStageState(at(2, "success", same), at(2, "running", same))).toBe(false);
    expect(isOlderStageState(at(2, "running", ""), at(2, "success", ""))).toBe(true);
  });
});

describe("drafts (#209)", () => {
  it("compares drafts regardless of key order", () => {
    expect(stableJson({ b: 1, a: { d: [1, 2], c: true } })).toBe(
      stableJson({ a: { c: true, d: [1, 2] }, b: 1 }),
    );
    expect(stableJson({ a: 1 })).not.toBe(stableJson({ a: 2 }));
  });
});

describe("stage readiness from input fingerprints (#205)", () => {
  const row = (
    stage: string,
    status: string,
    output: string,
    appliedInput: string,
    extra: Record<string, unknown> = {},
  ) => ({
    stage,
    status,
    output_sha256: output,
    applied_input_sha256: appliedInput,
    settings_applied: true,
    input_sha256: stage === "deskew" ? "v1" : "",
    ...extra,
  });

  it("is current when every result matches its input and settings", () => {
    const page = {
      stages: [
        row("deskew", "success", "d1", "v1"),
        row("select_content", "success", "s1", "d1"),
        row("dewarping", "success", "w1", "s1"),
        row("margins", "success", "m1", "w1"),
        row("output", "success", "o1", "m1"),
      ],
    } as never;
    for (const stage of STAGE_ORDER) {
      expect(stageReadiness(page, stage)).toEqual({
        current: true,
        waitingFor: null,
        staleReason: "",
      });
    }
  });

  it("waits for the first earlier stage without a current result", () => {
    const page = {
      stages: [
        row("deskew", "unknown", "", ""),
        row("margins", "unknown", "", "", { settings_applied: false }),
      ],
    } as never;
    expect(stageReadiness(page, "margins")).toEqual({
      current: false,
      waitingFor: "deskew",
      staleReason: "",
    });
    expect(stageReadiness(page, "deskew").waitingFor).toBeNull();
  });

  it("marks a result stale when its input or its settings changed", () => {
    const page = {
      stages: [
        row("deskew", "success", "d2", "v1"),
        row("select_content", "success", "s1", "d1"),
        row("dewarping", "success", "w1", "s1", { settings_applied: false }),
      ],
    } as never;
    expect(stageReadiness(page, "deskew")).toEqual({
      current: true,
      waitingFor: null,
      staleReason: "",
    });
    expect(stageReadiness(page, "select_content")).toEqual({
      current: false,
      waitingFor: null,
      staleReason: "input changed",
    });
    expect(stageReadiness(page, "dewarping")).toEqual({
      current: false,
      waitingFor: "select_content",
      staleReason: "settings changed, not applied",
    });
    // A replacement scan changes the first stage's input.
    const rescanned = {
      stages: [{ ...row("deskew", "success", "d1", "v1"), input_sha256: "v2" }],
    } as never;
    expect(stageReadiness(rescanned, "deskew").staleReason).toBe("input changed");
  });

  it("uses the server's Margins fingerprint, which includes the common page size (#208)", () => {
    const upstream = [
      row("deskew", "success", "d1", "v1"),
      row("select_content", "success", "s1", "d1"),
      row("dewarping", "success", "w1", "s1"),
    ];
    const margins = (extra: Record<string, unknown>) =>
      ({ stages: [...upstream, row("margins", "success", "m1", "w1+a1", extra)] }) as never;
    // Current: the fingerprint for the predecessor output on this page matches.
    expect(
      stageReadiness(margins({ input_sha256: "w1+a1", input_basis_sha256: "w1" }), "margins"),
    ).toEqual({ current: true, waitingFor: null, staleReason: "" });
    // Another page changed the common size.
    expect(
      stageReadiness(
        margins({ input_sha256: "w1+a2", input_basis_sha256: "w1", common_size_changed: true }),
        "margins",
      ),
    ).toEqual({ current: false, waitingFor: null, staleReason: "common page size changed" });
    // Dewarping produced a newer output than the one the server's fingerprint used.
    expect(
      stageReadiness(margins({ input_sha256: "w1+a1", input_basis_sha256: "w0" }), "margins"),
    ).toEqual({ current: false, waitingFor: null, staleReason: "input changed" });
  });
});

describe("result pane message (#206, from #205's derived state)", () => {
  const state = (status: string, extra: Record<string, string> = {}) => ({
    status,
    last_error: "",
    preview_storage_identifier: "",
    output_storage_identifier: "",
    ...extra,
  });
  const current = { current: true, waitingFor: null, staleReason: "" };
  const notCurrent = { current: false, waitingFor: null, staleReason: "" };

  it("is empty when the stage has a current result", () => {
    expect(
      resultPaneMessage(state("success", { preview_storage_identifier: "p.jpg" }), current),
    ).toBe("");
    expect(resultPaneMessage(state("success"), current)).toMatch(/no result preview/);
  });

  it("gives the status line's reason when there is no current result", () => {
    expect(resultPaneMessage(state("unknown"), notCurrent)).toMatch(/^Not run yet/);
    expect(resultPaneMessage(state("queued"), notCurrent)).toMatch(/^Queued/);
    expect(resultPaneMessage(state("running"), notCurrent)).toMatch(/^Running/);
    expect(
      resultPaneMessage(state("failed", { last_error: "cannot read image" }), notCurrent),
    ).toBe("Failed: cannot read image");
    expect(
      resultPaneMessage(state("unknown"), { ...notCurrent, waitingFor: "deskew" }),
    ).toBe("Waiting for Deskew. This stage runs when it is done.");
    expect(
      resultPaneMessage(state("success", { preview_storage_identifier: "p.jpg" }), {
        ...notCurrent,
        staleReason: "input changed",
      }),
    ).toBe("Stale: input changed. It runs again by itself.");
  });

  it("never shows a result for settings that are not applied", () => {
    const done = state("success", { preview_storage_identifier: "p.jpg" });
    expect(resultPaneMessage(done, current, true)).toBe(
      "Settings not applied yet. The result appears once they are saved.",
    );
    // A failure is still reported first, as on the status line.
    expect(resultPaneMessage(state("failed", { last_error: "boom" }), notCurrent, true)).toBe(
      "Failed: boom",
    );
  });

  it("puts a running job before waiting and stale", () => {
    const stale = { current: false, waitingFor: "deskew" as const, staleReason: "input changed" };
    expect(resultPaneMessage(state("queued"), stale)).toMatch(/^Queued/);
    expect(resultPaneMessage(state("success"), stale)).toMatch(/^Waiting for Deskew/);
  });
});

describe("pipeline stages (#195)", () => {
  it("runs the five stages in order with no Page Split or Fix Orientation", () => {
    expect(STAGE_ORDER).toEqual(["deskew", "select_content", "dewarping", "margins", "output"]);
  });

  it("shows the plain status in the page list", () => {
    expect(stageStatusLabel({ status: "success" })).toBe("success");
    expect(stageStatusLabel({ status: "queued" })).toBe("queued");
  });
});

describe("artifact tokens (#195)", () => {
  it("match the server's revision-and-checksum token and skip unfinished stages", () => {
    const sha = "0123456789abcdef0123456789abcdef";
    expect(artifactToken({ status: "success", revision: 4, output_sha256: sha })).toBe(
      "4-0123456789abcdef",
    );
    expect(artifactToken({ status: "queued", revision: 4, output_sha256: sha })).toBe("");
    expect(artifactToken({ status: "success", revision: 4, output_sha256: "" })).toBe("");
    expect(artifactToken(undefined)).toBe("");
  });
});

describe("preview retries while the page image is prepared (#195)", () => {
  const pending = { status: 503, body: "input preview not ready" };

  it("recognises only the server's being-prepared answer as retryable", () => {
    expect(previewIsBeingPrepared(pending)).toBe(true);
    expect(previewIsBeingPrepared({ status: 503, body: "storage unavailable" })).toBe(false);
    expect(previewIsBeingPrepared({ status: 404, body: "input preview not ready" })).toBe(false);
    expect(previewErrorMessage("deskew", pending)).toMatch(/appear automatically/);
  });

  it("backs off exponentially and gives up after about a minute", () => {
    let state = INITIAL_PREVIEW_RETRY;
    const delays: number[] = [];
    for (;;) {
      const retry = nextPreviewRetry(pending, state);
      if (!retry) break;
      delays.push(retry.delay);
      state = retry.state;
    }
    expect(delays).toHaveLength(PREVIEW_RETRY_MAX_ATTEMPTS);
    expect(delays.slice(0, 5)).toEqual([1000, 2000, 4000, 8000, 8000]);
    const total = delays.reduce((sum, delay) => sum + delay, 0);
    expect(total).toBeGreaterThanOrEqual(55_000);
    expect(total).toBeLessThanOrEqual(70_000);
  });

  it("reloads at once, but only once, when the diagnostic request succeeds", () => {
    const first = nextPreviewRetry({ status: 200, body: "" }, INITIAL_PREVIEW_RETRY);
    expect(first).toEqual({ delay: 0, state: { attempts: 0, reloadedAfterOk: true } });
    expect(nextPreviewRetry({ status: 200, body: "" }, first!.state)).toBeNull();
  });

  it("does not retry failures that will not fix themselves", () => {
    for (const failure of [
      { status: 404, body: "input preview not ready" },
      { status: 404, body: "preview artifact missing" },
      { status: 403, body: "" },
      { status: null, body: "" },
    ]) {
      expect(nextPreviewRetry(failure, INITIAL_PREVIEW_RETRY)).toBeNull();
    }
  });

  it("adds a cache-busting retry marker", () => {
    expect(previewRetryUrl("/p/?input=true&revision=1-unknown", 2)).toBe(
      "/p/?input=true&revision=1-unknown&retry=2",
    );
    expect(previewRetryUrl("/p/", 1)).toBe("/p/?retry=1");
  });
});

describe("editor preview recovery (#195)", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });

  const response = (status: number, body = "") => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
  });

  async function flush(): Promise<void> {
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
  }

  function mountDeskew(): { host: HTMLElement; image: HTMLImageElement; message: HTMLElement } {
    const state = {
      id: 1,
      stage: "deskew",
      stage_label: "Deskew",
      revision: 1,
      status: "unknown",
      parameters: {},
    };
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify([{ id: "page", logical_sequence_number: 1, side: "single", stages: [state] }])}</script><div id="retry-host" data-editable="true" data-preview-url-template="/__PAGE__/preview/"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><span data-live-status></span><h2 data-editor-title></h2><button data-revert-stage>Revert to saved</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-select-all></button><button data-clear-selection></button><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><button data-stage-select="deskew">Deskew</button></div>`;
    const host = document.querySelector<HTMLElement>("#retry-host")!;
    initializeScanTailorWorkspace(host);
    return {
      host,
      image: host.querySelector<HTMLImageElement>("[data-editor-image]")!,
      message: host.querySelector<HTMLElement>("[data-editor-message]")!,
    };
  }

  it("keeps retrying a page image that is still queued and shows it when ready", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(response(503, "input preview not ready"))
      .mockResolvedValueOnce(response(503, "input preview not ready"))
      .mockResolvedValue(response(200));
    vi.stubGlobal("fetch", fetcher);
    const { image, message } = mountDeskew();
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown");

    image.dispatchEvent(new Event("error"));
    await flush();
    expect(message.textContent).toMatch(/still being prepared/);
    await vi.advanceTimersByTimeAsync(999);
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown");
    await vi.advanceTimersByTimeAsync(1);
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown&retry=1");

    image.dispatchEvent(new Event("error"));
    await flush();
    await vi.advanceTimersByTimeAsync(2000);
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown&retry=2");

    // Generation finished: this load succeeds and the stage status returns.
    image.dispatchEvent(new Event("load"));
    expect(message.textContent).toBe("Revision 1 · not run yet");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("reloads the image when the diagnostic request already succeeds", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200)));
    const { image } = mountDeskew();

    // The first GET raced the generation job; by the diagnostic GET it was done.
    image.dispatchEvent(new Event("error"));
    await flush();
    await vi.advanceTimersByTimeAsync(0);
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown&retry=1");

    // A response that is fine but undecodable is not reloaded forever.
    image.dispatchEvent(new Event("error"));
    await flush();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(image.getAttribute("src")).toBe("/page/preview/?input=true&revision=1-unknown&retry=1");
  });

  it("gives up after the retry budget with an explanation", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(response(503, "input preview not ready"));
    vi.stubGlobal("fetch", fetcher);
    const { image, message } = mountDeskew();

    for (let attempt = 0; attempt <= PREVIEW_RETRY_MAX_ATTEMPTS; attempt += 1) {
      image.dispatchEvent(new Event("error"));
      await flush();
      await vi.advanceTimersByTimeAsync(8000);
    }
    expect(message.textContent).toBe(PREVIEW_RETRY_EXHAUSTED_MESSAGE);
    expect(fetcher).toHaveBeenCalledTimes(PREVIEW_RETRY_MAX_ATTEMPTS + 1);
    expect(image.getAttribute("src")).toContain(`retry=${PREVIEW_RETRY_MAX_ATTEMPTS}`);
  });

  it("stops retrying when the member opens another stage", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(503, "input preview not ready")));
    const { host, image } = mountDeskew();
    image.dispatchEvent(new Event("error"));
    await flush();
    // Showing the result mode (no result yet) keeps the same URL; switching the
    // source clears the pending retry.
    host.dataset.previewUrlTemplate = "/other/__PAGE__/preview/";
    host.querySelector<HTMLButtonElement>("[data-preview-input]")!.click();
    expect(image.getAttribute("src")).toBe("/other/page/preview/?input=true&revision=1-unknown");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(image.getAttribute("src")).toBe("/other/page/preview/?input=true&revision=1-unknown");
  });
});
