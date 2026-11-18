import { describe, expect, it, vi } from "vitest";

import {
  artifactToken,
  displayBoxToSource,
  editableParameters,
  curvatureCurvesForEditing,
  initializeScanTailorWorkspace,
  outputZonesForEditing,
  normalizeBox,
  previewErrorMessage,
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
      <div id="zones-host" data-editable="true"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><span data-live-status></span><h2 data-editor-title></h2><button data-save-stage>Save</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-select-all></button><button data-clear-selection></button><button data-stage-select="output">Output</button><button data-stage-open data-page-id="page" data-stage="output" data-stage-url="/output"></button></div>`;
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
