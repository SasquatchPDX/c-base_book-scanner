/**
 * The page strip of the editor (#195 step 3): upstream Scan Tailor's thumbnail
 * sequence. Virtualised (only visible rows exist in the DOM), lazily loaded
 * thumbnails, one small status icon per stage, sortable, multi-selectable.
 */

export type StatusKind = "idle" | "waiting" | "working" | "done" | "stale" | "manual" | "failed";

export interface StripStage {
  stage: string;
  label: string;
  status: string;
  manual?: boolean;
  last_error?: string;
  /** Label of the first earlier stage without a current result (#205). */
  waiting_for?: string;
  /** Why a successful result is not current: "input changed", "settings changed, not applied". */
  stale_reason?: string;
}

export interface StripItem {
  id: string;
  label: string;
  caption: string;
  thumbnailUrl: string;
  declaredBlank?: boolean;
  stages: StripStage[];
  /** Size used by the width/height orders (current stage, full resolution). */
  width: number | null;
  height: number | null;
}

export type SortMode = "natural" | "width" | "height";

export const ROW_HEIGHT = 196;
export const OVERSCAN = 4;

/** Colour class of a stage's status icon. */
export function statusKind(
  stage: Pick<StripStage, "status" | "manual" | "waiting_for" | "stale_reason">,
): StatusKind {
  switch (stage.status) {
    case "queued":
    case "running":
      return "working";
    case "failed":
      return "failed";
    case "skipped":
      return "done";
    default:
      // Derived from input fingerprints (#205): waiting beats an old result.
      if (stage.waiting_for) return "waiting";
      if (stage.status !== "success") return "idle";
      if (stage.stale_reason) return "stale";
      return stage.manual ? "manual" : "done";
  }
}

export function statusTooltip(stage: StripStage): string {
  if (stage.status === "skipped") return `${stage.label}: skipped (declared blank)`;
  const kind = statusKind(stage);
  const text: Record<StatusKind, string> = {
    idle: "not started",
    waiting: `waiting for ${stage.waiting_for ?? ""}`,
    working: stage.status === "running" ? "working" : "queued",
    done: "done",
    stale: "stale",
    manual: "done (manual settings)",
    failed: "failed",
  };
  let detail = "";
  if (kind === "failed" && stage.last_error) detail = `: ${stage.last_error}`;
  else if (kind === "stale" && stage.stale_reason) detail = ` (${stage.stale_reason})`;
  return `${stage.label}: ${text[kind]}${detail}`;
}

/** Rows to render for a scroll position: [first, last) with overscan. */
export function visibleRange(
  scrollTop: number,
  viewportHeight: number,
  count: number,
  rowHeight = ROW_HEIGHT,
  overscan = OVERSCAN,
): [number, number] {
  if (count <= 0) return [0, 0];
  const first = Math.min(count, Math.max(0, Math.floor(scrollTop / rowHeight) - overscan));
  const last = Math.min(count, Math.ceil((scrollTop + viewportHeight) / rowHeight) + overscan);
  return [first, Math.max(first, last)];
}

/** Page ids in display order. Width/height orders are stable and put unknown sizes last. */
export function sortItems(items: StripItem[], mode: SortMode): string[] {
  const natural = items.map((item) => item.id);
  if (mode === "natural") return natural;
  const key = (item: StripItem): number => {
    const value = mode === "width" ? item.width : item.height;
    return value === null ? Number.POSITIVE_INFINITY : value;
  };
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => key(a.item) - key(b.item) || a.index - b.index)
    .map(({ item }) => item.id);
}

export interface SelectionState {
  selected: Set<string>;
  anchor: string | null;
}

/**
 * Upstream ThumbnailSequence selection: a plain click selects one page, Ctrl/Cmd
 * toggles one, Shift selects the range from the anchor in display order.
 */
export function applyClick(
  state: SelectionState,
  order: string[],
  clicked: string,
  modifiers: { ctrl: boolean; shift: boolean },
): SelectionState {
  if (modifiers.shift && state.anchor && order.includes(state.anchor)) {
    const from = order.indexOf(state.anchor);
    const to = order.indexOf(clicked);
    const [low, high] = from <= to ? [from, to] : [to, from];
    return { selected: new Set(order.slice(low, high + 1)), anchor: state.anchor };
  }
  if (modifiers.ctrl) {
    const selected = new Set(state.selected);
    if (selected.has(clicked) && selected.size > 1) selected.delete(clicked);
    else selected.add(clicked);
    return { selected, anchor: clicked };
  }
  return { selected: new Set([clicked]), anchor: clicked };
}

/** The neighbour of ``current`` ``step`` rows away in display order (clamped). */
export function neighbour(order: string[], current: string | null, step: number): string | null {
  if (order.length === 0) return null;
  const index = current ? order.indexOf(current) : -1;
  if (index < 0) return order[0] ?? null;
  return order[Math.min(order.length - 1, Math.max(0, index + step))] ?? null;
}

export interface StripCallbacks {
  onClick(pageId: string, modifiers: { ctrl: boolean; shift: boolean }): void;
}

export class PageStrip {
  private items = new Map<string, StripItem>();
  private order: string[] = [];
  private active: string | null = null;
  private selected = new Set<string>();
  private readonly spacer: HTMLElement;
  private frame = 0;

  constructor(
    private readonly scroller: HTMLElement,
    private readonly callbacks: StripCallbacks,
  ) {
    this.spacer = document.createElement("div");
    this.spacer.className = "st-strip-spacer";
    this.scroller.replaceChildren(this.spacer);
    this.scroller.addEventListener("scroll", () => this.schedule(), { passive: true });
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => this.schedule()).observe(this.scroller);
    }
  }

  setItems(items: StripItem[], order: string[]): void {
    this.items = new Map(items.map((item) => [item.id, item]));
    this.order = order.filter((id) => this.items.has(id));
    this.spacer.style.height = `${this.order.length * ROW_HEIGHT}px`;
    this.render();
  }

  setOrder(order: string[]): void {
    this.order = order.filter((id) => this.items.has(id));
    this.render();
  }

  update(item: StripItem): void {
    this.items.set(item.id, item);
    const node = this.spacer.querySelector<HTMLElement>(`[data-strip-page="${item.id}"]`);
    if (node) this.fill(node, item);
  }

  setActive(pageId: string | null, selected: Set<string>, keepInView: boolean): void {
    this.active = pageId;
    this.selected = selected;
    if (keepInView && pageId) this.scrollTo(pageId);
    this.render();
  }

  displayOrder(): string[] {
    return [...this.order];
  }

  private scrollTo(pageId: string): void {
    const index = this.order.indexOf(pageId);
    if (index < 0) return;
    const top = index * ROW_HEIGHT;
    const bottom = top + ROW_HEIGHT;
    if (top < this.scroller.scrollTop) this.scroller.scrollTop = top;
    else if (bottom > this.scroller.scrollTop + this.scroller.clientHeight)
      this.scroller.scrollTop = bottom - this.scroller.clientHeight;
  }

  private schedule(): void {
    if (this.frame) return;
    const run = (): void => {
      this.frame = 0;
      this.render();
    };
    this.frame =
      typeof requestAnimationFrame === "function" ? requestAnimationFrame(run) : (run(), 0);
  }

  /** Render only the visible rows; reuse nodes that stay visible. */
  render(): void {
    const [first, last] = visibleRange(
      this.scroller.scrollTop,
      this.scroller.clientHeight || ROW_HEIGHT * 4,
      this.order.length,
    );
    const wanted = new Set(this.order.slice(first, last));
    for (const node of [...this.spacer.children] as HTMLElement[]) {
      if (!wanted.has(node.dataset.stripPage ?? "")) node.remove();
    }
    for (let index = first; index < last; index += 1) {
      const id = this.order[index];
      const item = id ? this.items.get(id) : undefined;
      if (!id || !item) continue;
      let node = this.spacer.querySelector<HTMLElement>(`[data-strip-page="${id}"]`);
      if (!node) {
        node = this.createRow(id);
        this.spacer.append(node);
      }
      node.style.transform = `translateY(${index * ROW_HEIGHT}px)`;
      this.fill(node, item);
    }
  }

  private createRow(id: string): HTMLElement {
    const node = document.createElement("div");
    node.className = "st-strip-item";
    node.dataset.stripPage = id;
    node.setAttribute("role", "option");
    const frame = document.createElement("div");
    frame.className = "st-strip-thumb";
    const image = document.createElement("img");
    image.loading = "lazy";
    image.decoding = "async";
    image.draggable = false;
    image.alt = "";
    frame.append(image);
    const caption = document.createElement("div");
    caption.className = "st-strip-caption";
    const icons = document.createElement("span");
    icons.className = "st-strip-icons";
    node.append(frame, caption, icons);
    node.addEventListener("click", (event) => {
      this.callbacks.onClick(id, {
        ctrl: event.ctrlKey || event.metaKey,
        shift: event.shiftKey,
      });
    });
    return node;
  }

  private fill(node: HTMLElement, item: StripItem): void {
    node.classList.toggle("active", item.id === this.active);
    node.classList.toggle("selected", this.selected.has(item.id));
    node.setAttribute("aria-selected", String(this.selected.has(item.id)));
    node.title = item.caption ? `${item.label} · ${item.caption}` : item.label;
    const image = node.querySelector("img");
    if (image && image.getAttribute("src") !== item.thumbnailUrl) {
      if (item.thumbnailUrl) image.src = item.thumbnailUrl;
      else image.removeAttribute("src");
      image.alt = item.label;
    }
    if (image) image.hidden = item.declaredBlank === true && !item.thumbnailUrl;
    const caption = node.querySelector<HTMLElement>(".st-strip-caption");
    if (caption) caption.textContent = item.caption ? `${item.label} · ${item.caption}` : item.label;
    const icons = node.querySelector<HTMLElement>(".st-strip-icons");
    if (icons) {
      icons.replaceChildren(
        ...item.stages.map((stage, index) => {
          const icon = document.createElement("span");
          const kind = statusKind(stage);
          icon.className = `st-status-icon st-status-${kind}`;
          icon.dataset.stage = stage.stage;
          icon.dataset.status = stage.status;
          icon.dataset.kind = kind;
          icon.textContent = String(index + 1);
          icon.title = statusTooltip(stage);
          return icon;
        }),
      );
    }
  }
}
