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
  sourceBoxToDisplay,
  stageErrorText,
  stageStatusLabel,
  stageStatusMessage,
  stageWorkerWarnings,
  STAGE_ORDER,
} from "./scantailor-workspace";

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
      <div id="zones-host" data-editable="true" data-stage-url-template="/pages/__PAGE__/stages/__STAGE__/"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><span data-live-status></span><h2 data-editor-title></h2><button data-save-stage>Save</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-stage-select="output">Output</button></div>`;
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
    host.querySelector<HTMLButtonElement>("[data-save-stage]")!.click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalled());
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
  it("does not resubmit a legacy computed margin canvas as a fixed request", () => {
    expect(
      editableParameters("margins", {
        margin: 20,
        input_width: 100,
        input_height: 200,
        margin_left: 20,
        margin_right: 20,
        margin_top: 20,
        margin_bottom: 20,
        canvas_width: 140,
        canvas_height: 240,
      }),
    ).toEqual({ margin: 20 });
    // A larger fixed canvas, and new explicit requests, remain intentional settings.
    expect(
      editableParameters("margins", {
        input_width: 100,
        canvas_width: 300,
      }),
    ).toEqual({ canvas_width: 300 });
    expect(
      editableParameters("margins", {
        input_width: 100,
        canvas_width: 100,
        resolved_canvas_width: 100,
      }),
    ).toEqual({ canvas_width: 100 });
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
      "The input preview is not ready. Run the Dewarping stage first.",
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
  it("treats routine invalidation as information, not an error", () => {
    const invalidated = {
      status: "invalidated",
      revision: 3,
      last_error: "deskew updated via batch",
      invalidated_reason: "deskew updated via batch",
    };

    expect(stageErrorText(invalidated)).toBe("");
    expect(stageStatusMessage(invalidated)).toEqual({
      text: "Revision 3 · invalidated (deskew updated via batch)",
      tone: "info",
    });
  });

  it("shows the error only for failed stages", () => {
    const failed = {
      status: "failed",
      revision: 2,
      last_error: "cannot read image",
    };

    expect(stageErrorText(failed)).toBe("cannot read image");
    expect(stageStatusMessage(failed)).toEqual({
      text: "cannot read image",
      tone: "error",
    });
    expect(stageStatusMessage({ status: "success", revision: 1, last_error: "" })).toEqual({
      text: "Revision 1 · success",
      tone: "info",
    });
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
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify([{ id: "page", logical_sequence_number: 1, side: "single", stages: [state] }])}</script><div id="retry-host" data-editable="true" data-preview-url-template="/__PAGE__/preview/"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><span data-live-status></span><h2 data-editor-title></h2><button data-save-stage>Save</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-select-all></button><button data-clear-selection></button><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><button data-stage-select="deskew">Deskew</button></div>`;
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
    expect(message.textContent).toBe("Revision 1 · unknown");
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
