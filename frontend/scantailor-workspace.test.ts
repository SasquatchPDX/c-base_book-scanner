import { describe, expect, it } from "vitest";

import {
  displayBoxToSource,
  editableParameters,
  normalizeBox,
  previewErrorMessage,
  previousStageName,
  sourceBoxToDisplay,
  stageErrorText,
  stageStatusMessage,
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
    expect(
      previewErrorMessage("dewarping", { status: 404, body: "input preview not ready" }),
    ).toBe("The input preview is not ready. Run the Margins stage first.");
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
