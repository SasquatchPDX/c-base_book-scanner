import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  BOTTOM,
  ContentBoxOverlay,
  describeContent,
  LEFT,
  moveEdges,
  RIGHT,
  roundBox,
  sizeInMillimetres,
  TOP,
} from "./content-box-view";
import {
  CylindricalDewarper,
  DewarpOverlay,
  describeDewarp,
  insertionIndex,
  modelIsValid,
  type Pt,
  XSpline,
} from "./dewarp-view";
import {
  angleFromHandle,
  applyScopePages,
  deskewMapPoint,
  deskewResultText,
  describeAngle,
  handlePositions,
  roundAngle,
  wheelAngle,
} from "./deskew-view";

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

describe("deskew (#195 step 4)", () => {
  interface RotationCase {
    size: [number, number];
    angle: number;
    points: [number, number][];
    expected: [number, number][];
  }

  it("previews exactly the rotation the server applies (shared fixture)", () => {
    // tests/test_deskew.py checks scan_transform against the same file.
    const cases = JSON.parse(
      readFileSync(resolve(process.cwd(), "tests/fixtures/deskew_rotation_cases.json"), "utf-8"),
    ) as RotationCase[];
    expect(cases.length).toBeGreaterThanOrEqual(6);
    for (const item of cases) {
      const size = { width: item.size[0], height: item.size[1] };
      item.points.forEach(([x, y], index) => {
        const mapped = deskewMapPoint({ x, y }, size, item.angle);
        const [ex, ey] = item.expected[index]!;
        expect(mapped.x).toBeCloseTo(ex, 6);
        expect(mapped.y).toBeCloseTo(ey, 6);
      });
    }
  });

  it("places the handles like upstream and turns clockwise when the right one goes down", () => {
    const centre = { x: 100, y: 100 };
    const [left, right] = handlePositions(centre, 50, 0);
    expect(left).toEqual({ x: 50, y: 100 });
    expect(right).toEqual({ x: 150, y: 100 });
    const down = angleFromHandle(1, { x: 150, y: 125 }, centre, 50);
    expect(down).toBeCloseTo(30);
    // The left handle moving up is the same clockwise turn.
    expect(angleFromHandle(0, { x: 50, y: 75 }, centre, 50)).toBeCloseTo(30);
    // Clamped to +-45 degrees.
    expect(angleFromHandle(1, { x: 0, y: 1000 }, centre, 50)).toBe(45);
    const [, moved] = handlePositions(centre, 50, down);
    expect(moved.y).toBeCloseTo(125);
  });

  it("rotates by 0.1 per wheel notch with Ctrl and 0.05 with Ctrl+Shift", () => {
    expect(wheelAngle(1, { deltaY: -100, ctrlKey: true, shiftKey: false })).toBeCloseTo(0.9);
    expect(wheelAngle(1, { deltaY: 100, ctrlKey: true, shiftKey: true })).toBeCloseTo(1.05);
    expect(wheelAngle(1, { deltaY: 100, ctrlKey: false, shiftKey: false })).toBeNull();
    expect(wheelAngle(44.98, { deltaY: 100, ctrlKey: true, shiftKey: false })).toBe(45);
    expect(roundAngle(-0.001)).toBe(0);
    expect(Object.is(roundAngle(-0.001), -0)).toBe(false);
  });

  it("says when a page is left unchanged", () => {
    expect(describeAngle(0)).toBe("Angle 0.00°, no change");
    expect(describeAngle(-1.234)).toBe("Angle 1.23° counter-clockwise");
    expect(
      deskewResultText({
        deskew_mode: "auto",
        angle: 0,
        detected_angle: 0.4,
        confidence: 1.25,
        confidence_good: false,
      }),
    ).toBe("Angle 0.00°, no change: detection confidence 1.3 is below 2.0; best guess 0.40°.");
    expect(deskewResultText({ deskew_mode: "auto", angle: 1.3, confidence: 12, confidence_good: true })).toBe(
      "Angle 1.30° clockwise (auto, confidence 12.0).",
    );
    expect(deskewResultText({ deskew_mode: "manual", angle: -2 })).toBe(
      "Angle 2.00° counter-clockwise (manual).",
    );
  });

  it("resolves the Apply to… scopes in display order", () => {
    const order = ["a", "b", "c", "d", "e"];
    const selected = new Set(["d", "b"]);
    expect(applyScopePages("all", order, "c", selected)).toBeNull();
    expect(applyScopePages("this", order, "c", selected)).toEqual(["c"]);
    expect(applyScopePages("following", order, "c", selected)).toEqual(["c", "d", "e"]);
    expect(applyScopePages("selected", order, "c", selected)).toEqual(["b", "d"]);
    expect(applyScopePages("every_other", order, "c", selected)).toEqual(["a", "c", "e"]);
    expect(applyScopePages("every_other", order, "b", selected)).toEqual(["b", "d"]);
    expect(applyScopePages("this", order, null, selected)).toEqual([]);
  });
});

describe("select content (#195 step 5)", () => {
  const image = { width: 1000, height: 1400 };
  const min = { width: 10, height: 10 };
  const start = { x1: 100, y1: 200, x2: 900, y2: 1200 };

  it("moves only the dragged edge, by exactly the pointer movement", () => {
    expect(moveEdges(start, LEFT, 30, 500, image, min)).toEqual({ ...start, x1: 130 });
    expect(moveEdges(start, BOTTOM, 0, -100, image, min)).toEqual({ ...start, y2: 1100 });
    expect(moveEdges(start, TOP | RIGHT, 50, -50, image, min)).toEqual({
      x1: 100,
      y1: 150,
      x2: 950,
      y2: 1200,
    });
  });

  it("keeps the minimum size and stays inside the image", () => {
    expect(moveEdges(start, LEFT, 5000, 0, image, min).x1).toBe(890);
    expect(moveEdges(start, TOP, 0, -5000, image, min).y1).toBe(0);
    expect(moveEdges(start, RIGHT, 5000, 0, image, min).x2).toBe(1000);
    expect(roundBox({ x1: 10.4, y1: 20.6, x2: 9, y2: 30 }, image)).toEqual({
      x1: 9,
      y1: 21,
      x2: 10,
      y2: 30,
    });
  });

  it("shows the physical size like upstream PhysSizeCalc", () => {
    const box = { x1: 0, y1: 0, x2: 1063, y2: 1654 };
    expect(sizeInMillimetres(box, { x: 150, y: 150 }).width).toBeCloseTo(180.0, 0);
    expect(describeContent({ content_mode: "auto" }, box, { x: 150, y: 150 })).toBe(
      "Content 180.0 × 280.1 mm (1063 × 1654 px, auto).",
    );
    expect(describeContent({ content_mode: "auto", content_found: false }, null, null)).toBe(
      "No content found: the whole page is kept.",
    );
    expect(describeContent({ content_mode: "disabled" }, null, null)).toContain("disabled");
  });

  it("drags an edge without jumping, whatever the zoom, and commits on release", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const onPreview = vi.fn();
    const onCommit = vi.fn();
    const overlay = new ContentBoxOverlay(host, start, true, { onPreview, onCommit });
    // Shown at half size: 500 x 700 screen pixels for a 1000 x 1400 page.
    overlay.layout(image, { width: 500, height: 700 });
    expect(overlay.element.style.left).toBe("50px");
    const handle = host.querySelector<HTMLElement>('[data-content-handle="left"]')!;
    expect(handle.dataset.capturesPointer).toBe("true");
    const fire = (type: string, x: number, y: number): void => {
      handle.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true }));
    };
    fire("pointerdown", 300, 300); // grabbed anywhere along the edge: no jump
    fire("pointermove", 310, 999);
    expect(onPreview).toHaveBeenLastCalledWith({ ...start, x1: 120 });
    fire("pointerup", 310, 999);
    expect(onCommit).toHaveBeenCalledWith({ ...start, x1: 120 });
    expect(overlay.element.style.left).toBe("60px");
    host.remove();
  });
});

describe("dewarping (#195 step 6a)", () => {
  interface ModelCase {
    top: Pt[];
    bottom: Pt[];
    depth: number;
    crv: Pt[];
    warped: Pt[];
    arc_length: number;
    straight_line_y: number;
  }

  it("draws the same model as the server (shared fixture)", () => {
    // tests/test_cylindrical.py checks core/cylindrical.py against the same file.
    const cases = JSON.parse(
      readFileSync(resolve(process.cwd(), "tests/fixtures/dewarp_model_cases.json"), "utf-8"),
    ) as ModelCase[];
    expect(cases.length).toBeGreaterThanOrEqual(3);
    for (const item of cases) {
      const top = new XSpline(item.top).toPolyline();
      const bottom = new XSpline(item.bottom).toPolyline();
      const dewarper = new CylindricalDewarper(top, bottom, item.depth);
      expect(dewarper.directrixArcLength).toBeCloseTo(item.arc_length, 9);
      expect(dewarper.straightLineY).toBeCloseTo(item.straight_line_y, 9);
      item.crv.forEach((crv, index) => {
        const [x, y] = dewarper.mapToWarped(crv);
        expect(x).toBeCloseTo(item.warped[index]![0], 6);
        expect(y).toBeCloseTo(item.warped[index]![1], 6);
      });
    }
  });

  it("moves middle points alone and turns the whole curve about the far end", () => {
    const spline = new XSpline([
      [0, 0],
      [50, 0],
      [100, 0],
    ]);
    spline.moveControlPoint(1, [50, 10]);
    expect(spline.points[1]).toEqual([50, 10]);
    spline.moveControlPoint(2, [0, 100]); // rotate 90 degrees about (0, 0)
    expect(spline.points[2]![0]).toBeCloseTo(0);
    expect(spline.points[2]![1]).toBeCloseTo(100);
    expect(spline.points[1]![0]).toBeCloseTo(-10);
    expect(spline.points[1]![1]).toBeCloseTo(50);
    expect(insertionIndex(spline, 0.3)).toBe(1);
    expect(insertionIndex(spline, 0.7)).toBe(2);
  });

  it("checks validity like upstream and describes the result", () => {
    const top: Pt[] = [
      [0, 0],
      [100, 0],
    ];
    const bottom: Pt[] = [
      [0, 100],
      [100, 100],
    ];
    expect(modelIsValid(top, bottom)).toBe(true);
    expect(modelIsValid(top, [...bottom].reverse())).toBe(false);
    expect(describeDewarp({ dewarp_mode: "off" })).toContain("off");
    expect(describeDewarp({ dewarp_mode: "auto", dewarp_applied: true, depth_perception: 2 })).toBe(
      "Dewarped (auto, depth perception 2.0).",
    );
  });

  it("drags a point, inserts one by clicking a curve and deletes it with D", () => {
    const host = document.createElement("div");
    document.body.append(host);
    const onCommit = vi.fn();
    const line = (y: number): Pt[] =>
      [0, 0.25, 0.5, 0.75, 1].map((f) => [f * 400, y] as Pt);
    const overlay = new DewarpOverlay(host, new XSpline(line(50)), new XSpline(line(550)), true, {
      onPreview: () => undefined,
      onCommit,
    });
    overlay.layout({ width: 400, height: 600 }, { width: 400, height: 600 });
    expect(host.querySelectorAll("[data-dewarp-point]")).toHaveLength(10);
    const fire = (target: EventTarget, type: string, x: number, y: number): void => {
      target.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true }));
    };
    const middle = host.querySelector('[data-dewarp-point="0:2"]')!;
    fire(middle, "pointerdown", 200, 50);
    fire(window, "pointermove", 200, 80);
    fire(window, "pointerup", 200, 80);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(overlay.top.points[2]).toEqual([200, 80]);

    // Clicking the bottom curve between two points inserts a point there.
    fire(overlay.element, "pointerdown", 150, 550);
    fire(window, "pointerup", 150, 550);
    expect(overlay.bottom.points).toHaveLength(6);
    const added = host.querySelector('[data-dewarp-point="1:2"]')!;
    added.dispatchEvent(new MouseEvent("pointerenter"));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "d" }));
    expect(overlay.bottom.points).toHaveLength(5);
    overlay.remove();
    host.remove();
  });
});
