import { afterEach, describe, expect, it, vi } from "vitest";

import {
  describeHistogram,
  OUTPUT_ALGORITHM_NAMES,
  OUTPUT_ALGORITHMS,
  OUTPUT_PARAMETERS,
  outputParametersFor,
  outputParameterValue,
  parseParameterInput,
} from "./output-algorithms";
import { AUTOSAVE_DELAY_MS, initializeScanTailorWorkspace } from "./scantailor-workspace";

describe("output algorithm catalogue (#225)", () => {
  it("shows only the settings each algorithm reads", () => {
    const keys = (name: string) => outputParametersFor([name]).map((spec) => spec.key);
    expect(keys("otsu")).toEqual([]);
    expect(keys("grad")).toEqual([]);
    expect(keys("copy")).toEqual([]);
    expect(keys("sauvola")).toEqual(["window_size", "sauvola_k", "r"]);
    expect(keys("wolf")).toEqual(["window_size", "wolf_k", "wolf_lower_bound", "wolf_upper_bound"]);
    expect(keys("fox")).toEqual(["window_size", "fox_k"]);
    expect(keys("window")).toEqual(["window_size", "window_k"]);
    expect(keys("su")).toEqual(["window_size", "su_min_edge_ratio"]);
    // Zoned pages: the union, each setting once.
    expect(outputParametersFor(["sauvola", "wolf"]).map((spec) => spec.key)).toEqual([
      "window_size",
      "sauvola_k",
      "r",
      "wolf_k",
      "wolf_lower_bound",
      "wolf_upper_bound",
    ]);
  });

  it("uses ScanTailor Advanced defaults and documents every algorithm", () => {
    expect(OUTPUT_PARAMETERS.sauvola_k!.defaultValue).toBe(0.34);
    expect(OUTPUT_PARAMETERS.wolf_k!.defaultValue).toBe(0.3);
    expect(OUTPUT_PARAMETERS.wolf_lower_bound!.defaultValue).toBe(1);
    expect(OUTPUT_PARAMETERS.wolf_upper_bound!.defaultValue).toBe(254);
    expect(OUTPUT_ALGORITHM_NAMES).toContain("su");
    for (const algorithm of OUTPUT_ALGORITHMS) {
      expect(algorithm.summary.length).toBeGreaterThan(10);
      expect(algorithm.provenance.length).toBeGreaterThan(0);
    }
    for (const name of ["fox", "window", "grad"]) {
      const algorithm = OUTPUT_ALGORITHMS.find((entry) => entry.name === name)!;
      expect(algorithm.provenance).toMatch(/Not from ScanTailor/);
    }
    for (const spec of Object.values(OUTPUT_PARAMETERS)) expect(spec.help.length).toBeGreaterThan(20);
  });

  it("reads a pre-#225 shared k when an algorithm has no own coefficient", () => {
    const spec = OUTPUT_PARAMETERS.wolf_k!;
    expect(outputParameterValue({ k: 0.5 }, spec)).toBe(0.5);
    expect(outputParameterValue({ k: 0.5, wolf_k: 0.2 }, spec)).toBe(0.2);
    expect(outputParameterValue({}, spec)).toBe(0.3);
    expect(outputParameterValue({ k: 0.5 }, OUTPUT_PARAMETERS.r!)).toBe(128);
  });

  it("applies values outside the recommended range and clamps only at technical limits", () => {
    const fox = OUTPUT_PARAMETERS.fox_k!;
    expect(parseParameterInput("0.4", fox)).toEqual({ value: 0.4, note: "", tone: "" });
    expect(parseParameterInput("10", fox)).toMatchObject({ value: 10, tone: "info" });
    expect(parseParameterInput("-10", fox)).toMatchObject({ value: -10, tone: "info" });
    expect(parseParameterInput("0,5", fox).value).toBe(0.5);
    expect(parseParameterInput("", fox)).toMatchObject({ value: null, tone: "warning" });
    const window = OUTPUT_PARAMETERS.window_size!;
    expect(parseParameterInput("301", window)).toMatchObject({ value: 301, tone: "info" });
    expect(parseParameterInput("2000", window)).toMatchObject({ value: 501, tone: "warning" });
    expect(parseParameterInput("40.4", window).value).toBe(40);
  });

  it("describes a global T and a per-pixel T distribution", () => {
    expect(
      describeHistogram({ mode: "otsu", threshold: { kind: "global", median: 141 }, black_fraction: 0.12 }),
    ).toBe("Otsu (global) · T 141 · 12 % black");
    const local = describeHistogram({
      mode: "su",
      threshold: { kind: "local", low: 90.2, median: 120.6, high: 150, defined_fraction: 0.3 },
      black_fraction: 0.05,
      preview_window: 11,
    });
    expect(local).toContain("T per pixel: median 121, 10–90 % 90–150");
    expect(local).toContain("near text only (30 % of pixels)");
    expect(local).toContain("window 11 px on the preview");
    expect(describeHistogram({ mode: "su", threshold: { kind: "local", defined_fraction: 0 } })).toContain(
      "no text edges",
    );
  });
});

describe("output controls in the editor (#225)", () => {
  const flush = async (): Promise<void> => {
    for (let index = 0; index < 20; index += 1) await Promise.resolve();
  };
  const done = (stage: string, input: string) => ({
    stage,
    status: "success",
    revision: 1,
    parameters: {},
    output_sha256: `${stage}-out`,
    applied_input_sha256: input,
    settings_applied: true,
    input_sha256: input,
    updated_at: "2026-11-24T10:00:00+00:00",
  });
  const output = {
    id: 5,
    scan_page_id: "page",
    stage: "output",
    stage_label: "Output",
    revision: 1,
    status: "unknown",
    algorithm: "binarization_wolf",
    // Saved before #225: one shared k.
    parameters: { output_mode: "wolf", zone_mode: "off", k: 0.5 },
    last_error: "",
    updated_at: "2026-11-24T10:00:00+00:00",
  };
  const pages = [
    {
      id: "page",
      logical_sequence_number: 1,
      side: "single",
      stages: [
        done("deskew", "v1"),
        done("select_content", "deskew-out"),
        done("dewarping", "select_content-out"),
        done("margins", "dewarping-out"),
        output,
      ],
    },
  ];

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    document.body.replaceChildren();
  });

  it("shows the selected algorithm's fields and saves a value outside the guidance", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => ({
      ok: true,
      status: 202,
      json: async () => ({
        stage: url.endsWith("/ensure/")
          ? output
          : {
              ...output,
              revision: 2,
              status: "queued",
              parameters: JSON.parse(String(init?.body)).parameters,
              updated_at: "2026-11-24T10:00:02+00:00",
            },
      }),
    }));
    vi.stubGlobal("fetch", fetcher);
    document.body.innerHTML = `<script id="scantailor-workspace-data" type="application/json">${JSON.stringify(pages)}</script>
      <div id="host" data-editable="true" data-stage-url-template="/pages/__PAGE__/stages/__STAGE__/" data-ensure-url-template="/pages/__PAGE__/stages/__STAGE__/ensure/"><div data-stage-controls></div><img data-editor-image><div data-geometry-layer></div><span data-editor-message></span><button data-preview-input>Input</button><button data-preview-result>Result</button><p data-stage-warnings></p><span data-live-status></span><h2 data-editor-title></h2><button data-revert-stage>Revert to saved</button><button data-batch-stage></button><select data-batch-scope></select><input data-preserve-exceptions type="checkbox"><span data-selection-count></span><button data-stage-select="output">Output</button></div>`;
    const root = document.querySelector<HTMLElement>("#host")!;
    initializeScanTailorWorkspace(root);
    root.querySelector<HTMLButtonElement>('[data-stage-select="output"]')!.click();
    await flush();
    const fields = () =>
      [...root.querySelectorAll<HTMLInputElement>("[data-output-parameter]")].map(
        (input) => input.dataset.outputParameter,
      );
    expect(fields()).toEqual(["window_size", "wolf_k", "wolf_lower_bound", "wolf_upper_bound"]);
    const k = root.querySelector<HTMLInputElement>('[data-output-parameter="wolf_k"]')!;
    expect(k.value).toBe("0.5");
    // No min/max: the browser never marks a typed value invalid.
    expect(k.min).toBe("");
    expect(k.max).toBe("");
    expect(root.querySelector('[data-algorithm-note="wolf"]')?.textContent).toContain("binarizeWolf");

    k.value = "10";
    k.dispatchEvent(new Event("input", { bubbles: true }));
    expect(root.querySelector('[data-parameter-note="wolf_k"]')?.textContent).toContain(
      "applied as typed",
    );
    await vi.advanceTimersByTimeAsync(AUTOSAVE_DELAY_MS);
    await flush();
    const posts = fetcher.mock.calls.filter(([url]) => url === "/pages/page/stages/output/");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0]![1]!.body)).parameters.wolf_k).toBe(10);

    const select = root.querySelector<HTMLSelectElement>("[data-stage-controls] select")!;
    select.value = "otsu";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    expect(fields()).toEqual([]);
    select.value = "su";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    expect(fields()).toEqual(["window_size", "su_min_edge_ratio"]);
  });
});
