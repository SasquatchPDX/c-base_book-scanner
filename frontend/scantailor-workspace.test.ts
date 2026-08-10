import { describe, expect, it } from "vitest";

import {
  displayBoxToSource,
  editableParameters,
  normalizeBox,
  sourceBoxToDisplay,
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
