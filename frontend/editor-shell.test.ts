import { describe, expect, it, vi } from "vitest";

import {
  applyClick,
  neighbour,
  PageStrip,
  ROW_HEIGHT,
  sortItems,
  statusKind,
  statusTooltip,
  type StripItem,
  visibleRange,
} from "./page-strip";
import { clampZoom, fitView, MAX_ZOOM, MIN_ZOOM, panBy, wheelFactor, zoomAt } from "./pan-zoom";
import { fillUrl, stageSize, stripSourceStage } from "./scantailor-workspace";

describe("pan and zoom (#195 step 3)", () => {
  it("fits the whole image centred in the pane", () => {
    const view = fitView({ width: 1000, height: 800 }, { width: 1200, height: 1600 }, 0);
    expect(view.zoom).toBeCloseTo(0.5);
    expect(view.x).toBeCloseTo(200);
    expect(view.y).toBeCloseTo(0);
  });

  it("keeps the point under the cursor fixed while zooming", () => {
    const view = { zoom: 0.5, x: 100, y: 50 };
    const anchor = { x: 400, y: 300 };
    const imagePoint = { x: (anchor.x - view.x) / view.zoom, y: (anchor.y - view.y) / view.zoom };
    const zoomed = zoomAt(view, 2, anchor);
    expect(zoomed.zoom).toBeCloseTo(1);
    expect(zoomed.x + imagePoint.x * zoomed.zoom).toBeCloseTo(anchor.x);
    expect(zoomed.y + imagePoint.y * zoomed.zoom).toBeCloseTo(anchor.y);
  });

  it("clamps zoom, maps wheel notches and pans", () => {
    expect(clampZoom(100)).toBe(MAX_ZOOM);
    expect(clampZoom(0)).toBe(MIN_ZOOM);
    expect(clampZoom(Number.NaN)).toBe(1);
    expect(wheelFactor(-100)).toBeCloseTo(1.2);
    expect(wheelFactor(100)).toBeCloseTo(1 / 1.2);
    expect(wheelFactor(-3, 1)).toBeGreaterThan(1);
    expect(panBy({ zoom: 2, x: 1, y: 2 }, 3, -4)).toEqual({ zoom: 2, x: 4, y: -2 });
  });
});

function item(id: string, width: number | null, height: number | null): StripItem {
  return { id, label: id, caption: "", thumbnailUrl: `/t/${id}`, stages: [], width, height };
}

describe("page strip (#195 step 3)", () => {
  it("renders only the rows in view plus overscan", () => {
    expect(visibleRange(0, ROW_HEIGHT * 3, 365, ROW_HEIGHT, 2)).toEqual([0, 5]);
    expect(visibleRange(ROW_HEIGHT * 100, ROW_HEIGHT * 3, 365, ROW_HEIGHT, 2)).toEqual([98, 105]);
    expect(visibleRange(ROW_HEIGHT * 400, ROW_HEIGHT * 3, 365, ROW_HEIGHT, 2)).toEqual([365, 365]);
    expect(visibleRange(0, 500, 0)).toEqual([0, 0]);
  });

  it("orders by width or height, stably, with unknown sizes last", () => {
    const items = [item("a", 300, 900), item("b", 100, null), item("c", null, 100), item("d", 100, 50)];
    expect(sortItems(items, "natural")).toEqual(["a", "b", "c", "d"]);
    expect(sortItems(items, "width")).toEqual(["b", "d", "a", "c"]);
    expect(sortItems(items, "height")).toEqual(["d", "c", "a", "b"]);
  });

  it("selects like upstream: click one, Ctrl toggles, Shift selects a range", () => {
    const order = ["a", "b", "c", "d", "e"];
    let state = applyClick({ selected: new Set(), anchor: null }, order, "b", { ctrl: false, shift: false });
    expect([...state.selected]).toEqual(["b"]);
    state = applyClick(state, order, "d", { ctrl: true, shift: false });
    expect([...state.selected].sort()).toEqual(["b", "d"]);
    state = applyClick(state, order, "b", { ctrl: true, shift: false });
    expect([...state.selected]).toEqual(["d"]);
    // The last Ctrl-clicked page ("b") is the anchor of a Shift range.
    state = applyClick(state, order, "a", { ctrl: false, shift: true });
    expect([...state.selected]).toEqual(["a", "b"]);
    state = applyClick(state, order, "e", { ctrl: false, shift: true });
    expect([...state.selected]).toEqual(["b", "c", "d", "e"]);
    // The last selected page cannot be toggled away.
    const single = applyClick({ selected: new Set(["c"]), anchor: "c" }, order, "c", {
      ctrl: true,
      shift: false,
    });
    expect([...single.selected]).toEqual(["c"]);
  });

  it("moves to neighbours within bounds", () => {
    const order = ["a", "b", "c"];
    expect(neighbour(order, "b", 1)).toBe("c");
    expect(neighbour(order, "c", 1)).toBe("c");
    expect(neighbour(order, "a", -1)).toBe("a");
    expect(neighbour(order, null, 1)).toBe("a");
    expect(neighbour([], "a", 1)).toBeNull();
  });

  it("maps stage states to the five icon colours with explanatory tooltips", () => {
    expect(statusKind({ status: "unknown" })).toBe("idle");
    expect(statusKind({ status: "invalidated" })).toBe("idle");
    expect(statusKind({ status: "queued" })).toBe("working");
    expect(statusKind({ status: "running" })).toBe("working");
    expect(statusKind({ status: "success" })).toBe("done");
    expect(statusKind({ status: "success", manual: true })).toBe("manual");
    expect(statusKind({ status: "failed" })).toBe("failed");
    expect(
      statusTooltip({ stage: "deskew", label: "Deskew", status: "failed", last_error: "bad" }),
    ).toBe("Deskew: failed: bad");
    expect(
      statusTooltip({
        stage: "margins",
        label: "Margins",
        status: "invalidated",
        invalidated_reason: "deskew updated",
      }),
    ).toBe("Margins: needs re-run (deskew updated)");
  });

  it("keeps a few hundred pages cheap: only visible rows exist in the DOM", () => {
    const scroller = document.createElement("div");
    document.body.append(scroller);
    const onClick = vi.fn();
    const strip = new PageStrip(scroller, { onClick });
    const items = Array.from({ length: 365 }, (_, index) => item(`p${index}`, index, index));
    strip.setItems(items, items.map((entry) => entry.id));
    const rows = scroller.querySelectorAll("[data-strip-page]");
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(20);
    (rows[1] as HTMLElement).dispatchEvent(new MouseEvent("click", { shiftKey: true }));
    expect(onClick).toHaveBeenCalledWith("p1", { ctrl: false, shift: true });
    strip.setActive("p0", new Set(["p0", "p1"]), false);
    expect(scroller.querySelector('[data-strip-page="p0"]')?.classList.contains("active")).toBe(true);
    expect(scroller.querySelector('[data-strip-page="p1"]')?.getAttribute("aria-selected")).toBe(
      "true",
    );
    scroller.remove();
  });
});

describe("editor helpers", () => {
  const sha = "abcdef0123456789abcdef";
  const stage = (name: string, status: string, extra: Record<string, unknown> = {}) => ({
    stage: name,
    status,
    revision: 2,
    output_sha256: status === "success" ? sha : "",
    parameters: extra,
  });

  it("shows the current stage's output, else the last completed stage before it", () => {
    const page = {
      stages: [stage("deskew", "success"), stage("select_content", "success"), stage("dewarping", "queued")],
    } as never;
    expect(stripSourceStage(page, "select_content")?.stage).toBe("select_content");
    expect(stripSourceStage(page, "margins")?.stage).toBe("select_content");
    expect(stripSourceStage(page, "deskew")?.stage).toBe("deskew");
    expect(stripSourceStage({ stages: [stage("deskew", "failed")] } as never, "deskew")).toBe(
      undefined,
    );
  });

  it("uses the content box for Select Content and the output size elsewhere", () => {
    const page = {
      stages: [
        stage("select_content", "success", { content_area_width: 900, content_area_height: 1200 }),
        stage("margins", "success", { output_width: 1000, output_height: 1300 }),
      ],
    } as never;
    expect(stageSize(page, "select_content")).toEqual({ width: 900, height: 1200 });
    expect(stageSize(page, "margins")).toEqual({ width: 1000, height: 1300 });
    expect(stageSize(page, "output")).toEqual({ width: null, height: null });
  });

  it("fills URL templates", () => {
    expect(fillUrl("/j/pages/__PAGE__/stages/__STAGE__/", "p1", "deskew")).toBe(
      "/j/pages/p1/stages/deskew/",
    );
  });
});
