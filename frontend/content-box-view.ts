/**
 * Content box editing in the browser (#195 step 5).
 *
 * Derived from Scan Tailor filters/select_content/ImageView.cpp and OptionsWidget.cpp
 * Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
 * Copyright (C) c-base_book-scanner contributors
 *
 * This program is free software: you can redistribute it and/or modify it under the
 * terms of the GNU General Public License as published by the Free Software
 * Foundation, either version 3 of the License, or (at your option) any later version.
 * See the LICENSE file at the repository root.
 *
 * As upstream: the box is drawn in auto and manual mode alike; dragging an edge moves
 * only that edge, dragging a corner moves its two edges, and releasing commits the box
 * as a manual one. The interior does not capture the pointer, so dragging it pans the
 * page. Coordinates are kept in source (stage input) pixels; a drag moves an edge by
 * exactly the pointer's movement, so the box never jumps, whatever the zoom or scroll.
 */

export interface SourceBox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface BoxSize {
  width: number;
  height: number;
}

export const TOP = 1;
export const RIGHT = 2;
export const BOTTOM = 4;
export const LEFT = 8;

/** `select_content::ImageView::m_minBoxSize`: in screen pixels. */
export const MIN_BOX_SCREEN_PX = 10;

/**
 * Upstream `cornerMoveRequest` / `edgeMoveRequest` + `forceInsideImage`, in source
 * pixels: move the edges in `mask` of `start` by (`dx`, `dy`), keeping at least
 * `minSize` and staying inside the image.
 */
export function moveEdges(
  start: SourceBox,
  mask: number,
  dx: number,
  dy: number,
  image: BoxSize,
  minSize: BoxSize,
): SourceBox {
  const box = { ...start };
  if (mask & TOP)
    box.y1 = Math.max(0, Math.min(start.y1 + dy, start.y2 - minSize.height));
  else if (mask & BOTTOM)
    box.y2 = Math.min(
      image.height,
      Math.max(start.y2 + dy, start.y1 + minSize.height),
    );
  if (mask & LEFT)
    box.x1 = Math.max(0, Math.min(start.x1 + dx, start.x2 - minSize.width));
  else if (mask & RIGHT)
    box.x2 = Math.min(
      image.width,
      Math.max(start.x2 + dx, start.x1 + minSize.width),
    );
  return box;
}

/** Whole source pixels, ordered, inside the image. */
export function roundBox(box: SourceBox, image: BoxSize): SourceBox {
  const x1 = Math.max(
    0,
    Math.min(image.width - 1, Math.round(Math.min(box.x1, box.x2))),
  );
  const y1 = Math.max(
    0,
    Math.min(image.height - 1, Math.round(Math.min(box.y1, box.y2))),
  );
  const x2 = Math.max(
    x1 + 1,
    Math.min(image.width, Math.round(Math.max(box.x1, box.x2))),
  );
  const y2 = Math.max(
    y1 + 1,
    Math.min(image.height, Math.round(Math.max(box.y1, box.y2))),
  );
  return { x1, y1, x2, y2 };
}

/**
 * `PhysSizeCalc::sizeMM`: stage coordinates are original pixels (rotation keeps
 * lengths), so millimetres are pixels / dpi * 25.4.
 */
export function sizeInMillimetres(
  box: SourceBox,
  dpi: { x: number; y: number },
): BoxSize {
  return {
    width: ((box.x2 - box.x1) * 25.4) / Math.max(1, dpi.x),
    height: ((box.y2 - box.y1) * 25.4) / Math.max(1, dpi.y),
  };
}

export function describeContent(
  parameters: Record<string, unknown>,
  box: SourceBox | null,
  dpi: { x: number; y: number } | null,
): string {
  const mode = String(parameters.content_mode ?? "auto");
  if (mode === "disabled")
    return "Content detection disabled: the whole page is kept.";
  if (mode !== "manual" && parameters.content_found === false)
    return "No content found: the whole page is kept.";
  if (!box) return "";
  const pixels = `${Math.round(box.x2 - box.x1)} × ${Math.round(box.y2 - box.y1)} px`;
  if (!dpi) return `Content ${pixels} (${mode}).`;
  const size = sizeInMillimetres(box, dpi);
  return `Content ${size.width.toFixed(1)} × ${size.height.toFixed(1)} mm (${pixels}, ${mode}).`;
}

export interface ContentBoxCallbacks {
  onPreview(box: SourceBox): void;
  onCommit(box: SourceBox): void;
}

const HANDLES: readonly [string, number][] = [
  ["top", TOP],
  ["right", RIGHT],
  ["bottom", BOTTOM],
  ["left", LEFT],
  ["top-left", TOP | LEFT],
  ["top-right", TOP | RIGHT],
  ["bottom-right", BOTTOM | RIGHT],
  ["bottom-left", BOTTOM | LEFT],
];

/** The blue box with eight drag handles, inside the editor's geometry layer. */
export class ContentBoxOverlay {
  readonly element: HTMLElement;
  private box: SourceBox;
  private source: BoxSize = { width: 1, height: 1 };
  private display: BoxSize = { width: 1, height: 1 };
  private drag: {
    mask: number;
    pointer: number;
    x: number;
    y: number;
    start: SourceBox;
  } | null = null;

  constructor(
    host: HTMLElement,
    box: SourceBox,
    private readonly editable: boolean,
    private readonly callbacks: ContentBoxCallbacks,
  ) {
    this.box = box;
    this.element = document.createElement("div");
    this.element.className = "st-content-box";
    this.element.dataset.contentBox = "";
    if (editable) {
      for (const [name, mask] of HANDLES) {
        const handle = document.createElement("div");
        handle.className = `st-content-handle st-content-${name}`;
        handle.dataset.contentHandle = name;
        handle.dataset.capturesPointer = "true";
        handle.title = "Drag lines or corners to resize the content box.";
        handle.addEventListener("pointerdown", (event) =>
          this.start(mask, event),
        );
        this.element.append(handle);
      }
    }
    host.append(this.element);
  }

  isDragging(): boolean {
    return this.drag !== null;
  }

  current(): SourceBox {
    return { ...this.box };
  }

  setBox(box: SourceBox): void {
    if (this.drag) return;
    this.box = box;
    this.place();
  }

  layout(source: BoxSize, display: BoxSize): void {
    this.source = source;
    this.display = display;
    this.place();
  }

  private scale(): { x: number; y: number } {
    return {
      x: this.display.width / Math.max(1, this.source.width),
      y: this.display.height / Math.max(1, this.source.height),
    };
  }

  private place(): void {
    const { x, y } = this.scale();
    this.element.style.left = `${this.box.x1 * x}px`;
    this.element.style.top = `${this.box.y1 * y}px`;
    this.element.style.width = `${Math.max(1, (this.box.x2 - this.box.x1) * x)}px`;
    this.element.style.height = `${Math.max(1, (this.box.y2 - this.box.y1) * y)}px`;
  }

  private start(mask: number, event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    this.drag = {
      mask,
      pointer: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      start: this.current(),
    };
    this.element.classList.add("dragging");
    // Follow the pointer on the window: a drag keeps working even if the handle is
    // re-rendered or loses pointer capture meanwhile.
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onEnd);
    window.addEventListener("pointercancel", this.onEnd);
    try {
      (event.currentTarget as HTMLElement | null)?.setPointerCapture?.(event.pointerId);
    } catch {
      // Capture is a convenience only.
    }
  }

  private readonly onMove = (event: PointerEvent): void => this.move(event);
  private readonly onEnd = (event: PointerEvent): void => this.end(event);

  private move(event: PointerEvent): void {
    const drag = this.drag;
    if (!drag || drag.pointer !== event.pointerId) return;
    event.preventDefault();
    const { x, y } = this.scale();
    const dx = (event.clientX - drag.x) / x;
    const dy = (event.clientY - drag.y) / y;
    this.box = moveEdges(drag.start, drag.mask, dx, dy, this.source, {
      width: MIN_BOX_SCREEN_PX / x,
      height: MIN_BOX_SCREEN_PX / y,
    });
    this.place();
    this.callbacks.onPreview(this.current());
  }

  private end(event: PointerEvent): void {
    const drag = this.drag;
    if (!drag || drag.pointer !== event.pointerId) return;
    this.drag = null;
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onEnd);
    window.removeEventListener("pointercancel", this.onEnd);
    this.element.classList.remove("dragging");
    this.box = roundBox(this.box, this.source);
    this.place();
    this.callbacks.onCommit(this.current());
  }

  remove(): void {
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onEnd);
    window.removeEventListener("pointercancel", this.onEnd);
    this.element.remove();
  }
}
