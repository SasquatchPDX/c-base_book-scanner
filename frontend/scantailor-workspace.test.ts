import { describe, expect, it, vi } from "vitest";

import {
  displayBoxToSource,
  editableParameters,
  curvatureCurvesForEditing,
  initializeScanTailorWorkspace,
  isAutoSkippedSplit,
  normalizeBox,
  previewErrorMessage,
  previousStageName,
  sourceBoxToDisplay,
  SPLIT_MODES,
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
  it("copies curvature geometry only after explicit manual editing", () => {
    const curves = [
      [
        [0, 20],
        [50, 25],
        [99, 20],
      ],
      [
        [0, 80],
        [50, 85],
        [99, 80],
      ],
    ];
    const parameters = {
      dewarp_model: "curvature",
      dewarp_mode: "auto",
      curvature_curves: curves,
      curvature_model_version: "derived",
    };
    const authored = curvatureCurvesForEditing(parameters);
    authored[0]![1]![1] = 30;
    expect(curves[0]![1]![1]).toBe(25);
    expect(editableParameters("dewarping", parameters)).toEqual({
      dewarp_model: "curvature",
      dewarp_mode: "auto",
    });
    expect(
      editableParameters("dewarping", { dewarp_mode: "manual", dewarp_curves: authored }),
    ).toEqual({ dewarp_mode: "manual", dewarp_curves: authored });
  });

  it("edits and saves replayable baseline controls without sending detected provenance", async () => {
    const state = {
      id: 1,
      stage: "dewarping",
      stage_label: "Dewarping",
      revision: 1,
      status: "success",
      algorithm: "baseline_curvature",
      parameters: {
        dewarp_model: "curvature",
        dewarp_mode: "auto",
        input_width: 100,
        input_height: 200,
        curvature_curves: [
          [
            [0, 40],
            [50, 50],
            [99, 40],
          ],
          [
            [0, 140],
            [50, 150],
            [99, 140],
          ],
        ],
      },
    };
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify([{ id: "page", logical_sequence_number: 1, side: "single", stages: [state] }])}</script><div id="curvature-host" data-editable="true"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><span data-live-status></span><h2 data-editor-title></h2><button data-save-stage>Save</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-select-all></button><button data-clear-selection></button><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><button data-stage-select="dewarping">Dewarping</button><button data-stage-open data-page-id="page" data-stage="dewarping" data-stage-url="/dewarp"></button></div>`;
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 202, json: async () => ({ stage: state }) });
    vi.stubGlobal("fetch", fetcher);
    const host = document.querySelector<HTMLElement>("#curvature-host")!;
    initializeScanTailorWorkspace(host);
    host.querySelector<HTMLButtonElement>('[data-stage-select="dewarping"]')!.click();
    [...host.querySelectorAll("button")]
      .find((b) => b.textContent === "Edit detected baselines")!
      .click();
    const input = [...host.querySelectorAll("label")]
      .find((label) => label.textContent === "Baseline point 2 y")!
      .querySelector("input")!;
    input.value = "65";
    input.dispatchEvent(new Event("change"));
    host.querySelector<HTMLButtonElement>("[data-save-stage]")!.click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalled());
    const payload = JSON.parse(fetcher.mock.calls[0]![1].body);
    expect(payload.parameters.dewarp_curves[0][1]).toEqual([50, 65]);
    expect(payload.parameters.dewarp_mode).toBe("manual");
    expect(payload.parameters).not.toHaveProperty("curvature_curves");
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
    const message = previewErrorMessage("page_split", {
      status: 404,
      body: "input preview not ready",
    });

    expect(message).not.toMatch(/preceding|Run the/i);
    expect(message).toMatch(/scanned page image/i);
  });

  it("names the actual predecessor when the input preview is missing", () => {
    expect(previewErrorMessage("dewarping", { status: 404, body: "input preview not ready" })).toBe(
      "The input preview is not ready. Run the Margins stage first.",
    );
    expect(previousStageName("page_split")).toBeNull();
    expect(previousStageName("deskew")).toBe("page_split");
  });

  it("distinguishes storage, artifact, network and other failures", () => {
    expect(previewErrorMessage("deskew", { status: 503, body: "storage unavailable" })).toMatch(
      /storage is temporarily unavailable/i,
    );
    expect(
      previewErrorMessage("deskew", { status: 404, body: "preview artifact missing" }),
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
      last_error: "page_split updated via batch",
      invalidated_reason: "page_split updated via batch",
    };

    expect(stageErrorText(invalidated)).toBe("");
    expect(stageStatusMessage(invalidated)).toEqual({
      text: "Revision 3 · invalidated (page_split updated via batch)",
      tone: "info",
    });
  });

  it("shows the error only for failed stages", () => {
    const failed = { status: "failed", revision: 2, last_error: "cannot read image" };

    expect(stageErrorText(failed)).toBe("cannot read image");
    expect(stageStatusMessage(failed)).toEqual({ text: "cannot read image", tone: "error" });
    expect(stageStatusMessage({ status: "success", revision: 1, last_error: "" })).toEqual({
      text: "Revision 1 · success",
      tone: "info",
    });
  });
});

describe("single-page capture page split (#55)", () => {
  it("labels an automatically skipped split and keeps other statuses", () => {
    const skipped = { status: "success", revision: 2, last_error: "", auto_skipped: true };

    expect(isAutoSkippedSplit(skipped)).toBe(true);
    expect(stageStatusLabel(skipped)).toBe("skipped · single page");
    expect(stageStatusMessage(skipped).text).toContain("skipped: single-page capture");
    expect(stageStatusLabel({ status: "success", auto_skipped: false })).toBe("success");
    expect(stageStatusLabel({ status: "queued" })).toBe("queued");
  });

  it("offers the no-split mode alongside the splitting modes", () => {
    expect(SPLIT_MODES).toEqual(["none", "auto", "manual", "single"]);
  });
});
