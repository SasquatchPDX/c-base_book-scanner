/**
 * Deskew editing in the browser (#195 step 4).
 *
 * Derived from Scan Tailor filters/deskew/ImageView.cpp and OptionsWidget.cpp
 * Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
 * Copyright (C) c-base_book-scanner contributors
 *
 * This program is free software: you can redistribute it and/or modify it under the
 * terms of the GNU General Public License as published by the Free Software
 * Foundation, either version 3 of the License, or (at your option) any later version.
 * See the LICENSE file at the repository root.
 *
 * The page rotates live with a CSS transform while the angle changes; nothing is
 * requested from the server until the angle is committed. Angles are degrees,
 * positive = clockwise on screen, which is CSS `rotate()` and upstream's internal
 * convention. The server applies the same rotation (`scan_transform.rotate(-angle)`),
 * which tests/test_deskew.py and editor-shell.test.ts check against one fixture.
 */

export interface DeskewPoint {
  x: number;
  y: number;
}

/** `deskew::ImageView::m_maxRotationDeg` and `OptionsWidget::MAX_ANGLE`. */
export const MAX_ROTATION_DEG = 45;
/** `deskew::ImageView::m_cellSize`: grid spacing in screen pixels. */
export const GRID_CELL = 20;
/** Ctrl+wheel and Ctrl+Shift+wheel steps (upstream `onWheelEvent`). */
export const WHEEL_STEP = 0.1;
export const FINE_WHEEL_STEP = 0.05;

const DEG = Math.PI / 180;

export function clampAngle(angle: number): number {
  if (!Number.isFinite(angle)) return 0;
  return Math.max(-MAX_ROTATION_DEG, Math.min(MAX_ROTATION_DEG, angle));
}

/** Round to the 0.01 degree the angle field shows (and avoid "-0"). */
export function roundAngle(angle: number): number {
  return Math.round(clampAngle(angle) * 100) / 100 + 0;
}

/**
 * Where a point of the unrotated page lands after the editor's rotation, on the
 * canvas that holds the whole rotated page (as the server renders it).
 */
export function deskewMapPoint(
  point: DeskewPoint,
  size: { width: number; height: number },
  angle: number,
): DeskewPoint {
  const cos = Math.cos(angle * DEG);
  const sin = Math.sin(angle * DEG);
  const canvasWidth = Math.abs(size.width * cos) + Math.abs(size.height * sin);
  const canvasHeight = Math.abs(size.width * sin) + Math.abs(size.height * cos);
  const dx = point.x - size.width / 2;
  const dy = point.y - size.height / 2;
  return {
    x: canvasWidth / 2 + dx * cos - dy * sin,
    y: canvasHeight / 2 + dx * sin + dy * cos,
  };
}

/** Upstream `getRotationHandles`: the left and right handle on a circle of `radius`. */
export function handlePositions(
  centre: DeskewPoint,
  radius: number,
  angle: number,
): [DeskewPoint, DeskewPoint] {
  const cos = Math.cos(angle * DEG);
  const sin = Math.sin(angle * DEG);
  return [
    { x: centre.x - cos * radius, y: centre.y - sin * radius },
    { x: centre.x + cos * radius, y: centre.y + sin * radius },
  ];
}

/**
 * Upstream `handleMoveRequest`: the angle for a handle dragged to `pointer`. Only the
 * vertical position counts; the left handle turns the other way.
 */
export function angleFromHandle(
  index: 0 | 1,
  pointer: DeskewPoint,
  centre: DeskewPoint,
  radius: number,
): number {
  if (radius <= 0) return 0;
  const relative = Math.max(-radius, Math.min(radius, pointer.y - centre.y));
  let angle = (Math.asin(relative / radius) * 180) / Math.PI;
  if (index === 0) angle = -angle;
  return clampAngle(angle) + 0;
}

/**
 * Upstream `onWheelEvent`: Ctrl+wheel turns by 0.1 degree per notch, Ctrl+Shift+wheel
 * by 0.05; wheel up turns counter-clockwise. Returns null when the wheel is not for us.
 */
export function wheelAngle(
  angle: number,
  event: { deltaY: number; deltaX?: number; ctrlKey: boolean; shiftKey: boolean },
): number | null {
  if (!event.ctrlKey) return null;
  const delta = event.deltaY || event.deltaX || 0;
  if (delta === 0) return null;
  const step = event.shiftKey ? FINE_WHEEL_STEP : WHEEL_STEP;
  return roundAngle(angle + (delta < 0 ? -step : step));
}

export type ApplyScope = "this" | "all" | "following" | "selected" | "every_other";

/**
 * Pages an "Apply to…" choice covers, in display order (upstream ApplyDialog:
 * this page, all pages, this page and the following ones, selected pages, and every
 * other page counted from this one). `null` means every page of the job.
 */
export function applyScopePages(
  scope: ApplyScope,
  order: readonly string[],
  current: string | null,
  selected: ReadonlySet<string>,
): string[] | null {
  if (scope === "all") return null;
  if (scope === "selected") return order.filter((id) => selected.has(id));
  if (!current) return [];
  const index = order.indexOf(current);
  if (scope === "this" || index < 0) return [current];
  if (scope === "following") return order.slice(index);
  return order.filter((_, position) => (position - index) % 2 === 0);
}

function formatDegrees(angle: number): string {
  return `${Math.abs(angle).toFixed(2)}°`;
}

/** "Angle 1.30° clockwise", or "angle 0.00°, no change" (#195 §6 rule 3). */
export function describeAngle(angle: number): string {
  const rounded = roundAngle(angle);
  if (rounded === 0) return "Angle 0.00°, no change";
  return `Angle ${formatDegrees(rounded)} ${rounded > 0 ? "clockwise" : "counter-clockwise"}`;
}

/** What the stage found, from its recorded parameters (shown below the angle field). */
export function deskewResultText(parameters: Record<string, unknown>): string {
  const angle = Number(parameters.angle);
  if (!Number.isFinite(angle)) return "";
  const mode = parameters.deskew_mode === "manual" ? "manual" : "auto";
  const text = describeAngle(angle);
  if (mode === "manual") return `${text} (manual).`;
  const confidence = Number(parameters.confidence);
  const detected = Number(parameters.detected_angle);
  if (parameters.confidence_good === false && Number.isFinite(confidence)) {
    const found = Number.isFinite(detected) ? `; best guess ${formatDegrees(detected)}` : "";
    return `${text}: detection confidence ${confidence.toFixed(1)} is below 2.0${found}.`;
  }
  return Number.isFinite(confidence)
    ? `${text} (auto, confidence ${confidence.toFixed(1)}).`
    : `${text} (auto).`;
}

const SVG = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(
  name: K,
  attributes: Record<string, string | number> = {},
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

export interface DeskewOverlayCallbacks {
  /** The angle changed during a drag (preview only). */
  onPreview(angle: number): void;
  /** A drag finished: commit the angle. */
  onCommit(angle: number): void;
}

/**
 * Grid, rotation arcs and the two round handles (upstream `deskew::ImageView`), drawn
 * in viewport coordinates around the page centre. The grid does not rotate: the page
 * turns underneath it, so straight text lines can be compared with the grid lines.
 */
export class DeskewOverlay {
  readonly element: SVGSVGElement;
  private readonly grid: SVGRectElement;
  private readonly pattern: SVGPatternElement;
  private readonly cross: SVGPathElement;
  private readonly arcs: SVGPathElement;
  private readonly handles: [SVGCircleElement, SVGCircleElement];
  private centre: DeskewPoint = { x: 0, y: 0 };
  private radius = 0;
  private angle = 0;
  private dragging: { index: 0 | 1; pointer: number } | null = null;
  private keepGrid = true;

  constructor(
    host: HTMLElement,
    private readonly callbacks: DeskewOverlayCallbacks,
  ) {
    const id = `deskew-grid-${Math.random().toString(36).slice(2)}`;
    this.element = svg("svg", { class: "st-deskew-overlay", "aria-hidden": "true" });
    const defs = svg("defs");
    this.pattern = svg("pattern", {
      id,
      width: GRID_CELL,
      height: GRID_CELL,
      patternUnits: "userSpaceOnUse",
    });
    this.pattern.append(
      svg("path", {
        d: `M ${GRID_CELL} 0 L 0 0 0 ${GRID_CELL}`,
        class: "st-deskew-grid-line",
      }),
    );
    defs.append(this.pattern);
    this.grid = svg("rect", { width: "100%", height: "100%", fill: `url(#${id})` });
    this.grid.classList.add("st-deskew-grid");
    this.cross = svg("path", { class: "st-deskew-cross" });
    this.arcs = svg("path", { class: "st-deskew-arcs" });
    const handle = (index: 0 | 1): SVGCircleElement => {
      const node = svg("circle", { r: 9, class: "st-deskew-handle" });
      node.dataset.capturesPointer = "true";
      node.dataset.deskewHandle = String(index);
      const title = svg("title");
      title.textContent = "Drag this handle to rotate the image.";
      node.append(title);
      node.addEventListener("pointerdown", (event) => this.startDrag(index, event));
      node.addEventListener("pointermove", (event) => this.moveDrag(event));
      node.addEventListener("pointerup", (event) => this.endDrag(event));
      node.addEventListener("pointercancel", (event) => this.endDrag(event));
      return node;
    };
    this.handles = [handle(0), handle(1)];
    this.element.append(defs, this.grid, this.cross, this.arcs, ...this.handles);
    host.append(this.element);
  }

  /** Show the grid all the time (true) or only while rotating (false). */
  setKeepGrid(keep: boolean): void {
    this.keepGrid = keep;
    this.updateGridVisibility();
  }

  isDragging(): boolean {
    return this.dragging !== null;
  }

  /** Place the overlay around the page centre (viewport coordinates). */
  layout(centre: DeskewPoint, radius: number, angle: number): void {
    this.centre = centre;
    this.radius = Math.max(0, radius);
    this.angle = angle;
    const width = this.element.clientWidth || this.element.parentElement?.clientWidth || 0;
    const height = this.element.clientHeight || this.element.parentElement?.clientHeight || 0;
    // Grid lines pass through the rotation centre, every GRID_CELL pixels.
    const offsetX = ((centre.x % GRID_CELL) + GRID_CELL) % GRID_CELL;
    const offsetY = ((centre.y % GRID_CELL) + GRID_CELL) % GRID_CELL;
    this.pattern.setAttribute("x", String(offsetX));
    this.pattern.setAttribute("y", String(offsetY));
    this.cross.setAttribute(
      "d",
      `M 0 ${centre.y} H ${width} M ${centre.x} 0 V ${height}`,
    );
    const r = this.radius;
    const s = Math.sin(MAX_ROTATION_DEG * DEG) * r;
    const c = Math.cos(MAX_ROTATION_DEG * DEG) * r;
    // The two arcs "(  )" along which the handles move, +-45 degrees each.
    this.arcs.setAttribute(
      "d",
      [
        `M ${centre.x + c} ${centre.y - s} A ${r} ${r} 0 0 1 ${centre.x + c} ${centre.y + s}`,
        `M ${centre.x - c} ${centre.y + s} A ${r} ${r} 0 0 1 ${centre.x - c} ${centre.y - s}`,
      ].join(" "),
    );
    this.placeHandles();
    this.updateGridVisibility();
  }

  setAngle(angle: number): void {
    this.angle = angle;
    this.placeHandles();
  }

  /** Show the grid briefly while the angle changes by wheel or field. */
  flashGrid(): void {
    this.element.classList.add("rotating");
    window.clearTimeout(this.flashTimer);
    this.flashTimer = window.setTimeout(() => {
      this.element.classList.remove("rotating");
      this.updateGridVisibility();
    }, 900);
    this.updateGridVisibility();
  }

  private flashTimer = 0;

  private updateGridVisibility(): void {
    const rotating = this.dragging !== null || this.element.classList.contains("rotating");
    const visible = this.keepGrid || rotating;
    this.grid.style.display = visible ? "" : "none";
    this.cross.style.display = visible ? "" : "none";
  }

  private placeHandles(): void {
    const positions = handlePositions(this.centre, this.radius, this.angle);
    positions.forEach((position, index) => {
      this.handles[index]?.setAttribute("cx", String(position.x));
      this.handles[index]?.setAttribute("cy", String(position.y));
    });
  }

  private pointerPoint(event: PointerEvent): DeskewPoint {
    const bounds = this.element.getBoundingClientRect();
    return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
  }

  private startDrag(index: 0 | 1, event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    this.dragging = { index, pointer: event.pointerId };
    this.handles[index].setPointerCapture?.(event.pointerId);
    this.element.classList.add("dragging");
    this.updateGridVisibility();
  }

  private moveDrag(event: PointerEvent): void {
    if (!this.dragging || this.dragging.pointer !== event.pointerId) return;
    event.preventDefault();
    const angle = angleFromHandle(
      this.dragging.index,
      this.pointerPoint(event),
      this.centre,
      this.radius,
    );
    if (angle === this.angle) return;
    this.setAngle(angle);
    this.callbacks.onPreview(angle);
  }

  private endDrag(event: PointerEvent): void {
    if (!this.dragging || this.dragging.pointer !== event.pointerId) return;
    this.dragging = null;
    this.element.classList.remove("dragging");
    this.updateGridVisibility();
    this.callbacks.onCommit(roundAngle(this.angle));
  }

  remove(): void {
    window.clearTimeout(this.flashTimer);
    this.element.remove();
  }
}
