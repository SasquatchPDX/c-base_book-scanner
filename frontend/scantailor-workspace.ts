import interact from "interactjs";

export const STAGE_ORDER = [
  "page_split",
  "deskew",
  "select_content",
  "margins",
  "dewarping",
  "output",
] as const;

export type StageName = (typeof STAGE_ORDER)[number];

type JsonObject = Record<string, unknown>;

interface StageState {
  id: number;
  scan_job_id: string;
  scan_page_id: string;
  stage: StageName;
  stage_label: string;
  revision: number;
  status: string;
  algorithm: string;
  algorithm_version: string;
  parameters: JsonObject;
  output_storage_identifier: string;
  preview_storage_identifier: string;
  last_error: string;
}

interface WorkspacePage {
  id: string;
  logical_sequence_number: number;
  side: string;
  thumbnail?: string;
  stages: StageState[];
}

interface WorkspaceEvent {
  job_id: string;
  state: string;
  updated_at: string;
  stages: StageState[];
}

export interface Size {
  width: number;
  height: number;
}

export interface Box {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface Point {
  x: number;
  y: number;
}

interface InteractableHandle {
  unset(): unknown;
}

const STAGE_LABELS: Record<StageName, string> = {
  page_split: "Page split",
  deskew: "Deskew",
  select_content: "Select content",
  margins: "Margins",
  dewarping: "Dewarping",
  output: "Output / binarization",
};

const EDITABLE_KEYS: Record<StageName, readonly string[]> = {
  page_split: ["split_mode", "split_x"],
  deskew: ["manual_angle", "angle_min", "angle_max", "angle_step"],
  select_content: ["content_mode", "content_box"],
  margins: [
    "margins_mode",
    "margin",
    "top",
    "right",
    "bottom",
    "left",
    "canvas_width",
    "canvas_height",
    "align_x",
    "align_y",
  ],
  dewarping: ["dewarp_mode", "dewarp_points"],
  output: [
    "output_mode",
    "keep_color",
    "invert",
    "zone_mode",
    "text_box",
    "picture_box",
    "text_algorithm",
    "picture_algorithm",
    "window_size",
    "k",
    "r",
  ],
};

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function sourceBoxToDisplay(box: Box, source: Size, display: Size): Box {
  const scaleX = display.width / Math.max(1, source.width);
  const scaleY = display.height / Math.max(1, source.height);
  return {
    x1: box.x1 * scaleX,
    y1: box.y1 * scaleY,
    x2: box.x2 * scaleX,
    y2: box.y2 * scaleY,
  };
}

export function displayBoxToSource(box: Box, source: Size, display: Size): Box {
  const scaleX = source.width / Math.max(1, display.width);
  const scaleY = source.height / Math.max(1, display.height);
  return {
    x1: Math.round(clamp(box.x1 * scaleX, 0, source.width)),
    y1: Math.round(clamp(box.y1 * scaleY, 0, source.height)),
    x2: Math.round(clamp(box.x2 * scaleX, 0, source.width)),
    y2: Math.round(clamp(box.y2 * scaleY, 0, source.height)),
  };
}

export function editableParameters(stage: StageName, parameters: JsonObject): JsonObject {
  const editable: JsonObject = {};
  for (const key of EDITABLE_KEYS[stage]) {
    if (key in parameters) {
      editable[key] = structuredClone(parameters[key]);
    }
  }
  return editable;
}

export function normalizeBox(value: unknown, fallback: Box): Box {
  if (Array.isArray(value) && value.length >= 4) {
    return orderedBox({
      x1: finiteNumber(value[0], fallback.x1),
      y1: finiteNumber(value[1], fallback.y1),
      x2: finiteNumber(value[2], fallback.x2),
      y2: finiteNumber(value[3], fallback.y2),
    });
  }
  if (isObject(value)) {
    const x1 = finiteNumber(value.x1, fallback.x1);
    const y1 = finiteNumber(value.y1, fallback.y1);
    const width = finiteNumber(value.width, fallback.x2 - fallback.x1);
    const height = finiteNumber(value.height, fallback.y2 - fallback.y1);
    return orderedBox({
      x1,
      y1,
      x2: finiteNumber(value.x2, x1 + width),
      y2: finiteNumber(value.y2, y1 + height),
    });
  }
  return fallback;
}

function orderedBox(box: Box): Box {
  return {
    x1: Math.min(box.x1, box.x2),
    y1: Math.min(box.y1, box.y2),
    x2: Math.max(box.x1, box.x2),
    y2: Math.max(box.y1, box.y2),
  };
}

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positiveNumber(value: unknown): number | null {
  const parsed = finiteNumber(value, 0);
  return parsed > 0 ? parsed : null;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStageName(value: string): value is StageName {
  return (STAGE_ORDER as readonly string[]).includes(value);
}

function stageFor(page: WorkspacePage, stage: StageName): StageState | undefined {
  return page.stages.find((state) => state.stage === stage);
}

function csrfToken(): string {
  return (
    document.querySelector<HTMLInputElement>("input[name='csrfmiddlewaretoken']")?.value ?? ""
  );
}

function element<T extends HTMLElement>(parent: ParentNode, selector: string): T {
  const found = parent.querySelector<T>(selector);
  if (!found) {
    throw new Error(`ScanTailor workspace element missing: ${selector}`);
  }
  return found;
}

class WorkspaceController {
  private readonly root: HTMLElement;
  private readonly pages = new Map<string, WorkspacePage>();
  private readonly selectedPages = new Set<string>();
  private readonly editable: boolean;
  private readonly controls: HTMLElement;
  private readonly image: HTMLImageElement;
  private readonly geometryLayer: HTMLElement;
  private readonly message: HTMLElement;
  private readonly liveStatus: HTMLElement;
  private readonly title: HTMLElement;
  private readonly saveButton: HTMLButtonElement;
  private readonly batchButton: HTMLButtonElement;
  private readonly batchScope: HTMLSelectElement;
  private readonly preserveExceptions: HTMLInputElement;
  private readonly selectionCount: HTMLElement;
  private activePageId: string | null = null;
  private activeStage: StageName = "page_split";
  private draft: JsonObject = {};
  private interactions: InteractableHandle[] = [];
  private eventSource: EventSource | null = null;
  private pollingTimer: number | null = null;

  constructor(root: HTMLElement, pages: WorkspacePage[]) {
    this.root = root;
    this.editable = root.dataset.editable === "true";
    this.controls = element(root, "[data-stage-controls]");
    this.image = element(root, "[data-editor-image]");
    this.geometryLayer = element(root, "[data-geometry-layer]");
    this.message = element(root, "[data-editor-message]");
    this.liveStatus = element(root, "[data-live-status]");
    this.title = element(root, "[data-editor-title]");
    this.saveButton = element(root, "[data-save-stage]");
    this.batchButton = element(root, "[data-batch-stage]");
    this.batchScope = element(root, "[data-batch-scope]");
    this.preserveExceptions = element(root, "[data-preserve-exceptions]");
    this.selectionCount = element(root, "[data-selection-count]");
    for (const page of pages) {
      this.pages.set(page.id, page);
    }
  }

  start(): void {
    this.bindWorkspaceControls();
    const firstPage = this.pages.values().next().value as WorkspacePage | undefined;
    if (firstPage) {
      this.activate(firstPage.id, "page_split");
    } else {
      this.disableEditor("Upload a page to begin editing.");
    }
    this.startLiveUpdates();
  }

  private bindWorkspaceControls(): void {
    this.root.querySelectorAll<HTMLButtonElement>("[data-stage-select]").forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.stageSelect ?? "";
        if (!isStageName(value)) return;
        const pageId = this.activePageId ?? this.pages.keys().next().value;
        if (typeof pageId === "string") this.activate(pageId, value);
      });
    });
    this.root.querySelectorAll<HTMLButtonElement>("[data-stage-open]").forEach((button) => {
      button.addEventListener("click", () => {
        const pageId = button.dataset.pageId;
        const value = button.dataset.stage;
        if (pageId && value && isStageName(value)) this.activate(pageId, value);
      });
    });
    this.root.querySelectorAll<HTMLInputElement>("[data-page-select]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => {
        const pageId = checkbox.dataset.pageId;
        if (!pageId) return;
        if (checkbox.checked) this.selectedPages.add(pageId);
        else this.selectedPages.delete(pageId);
        this.updateSelectionCount();
      });
    });
    element<HTMLButtonElement>(this.root, "[data-select-all]").addEventListener("click", () => {
      this.root.querySelectorAll<HTMLInputElement>("[data-page-select]").forEach((checkbox) => {
        checkbox.checked = true;
        const pageId = checkbox.dataset.pageId;
        if (pageId) this.selectedPages.add(pageId);
      });
      this.updateSelectionCount();
    });
    element<HTMLButtonElement>(this.root, "[data-clear-selection]").addEventListener(
      "click",
      () => {
        this.root.querySelectorAll<HTMLInputElement>("[data-page-select]").forEach((checkbox) => {
          checkbox.checked = false;
        });
        this.selectedPages.clear();
        this.updateSelectionCount();
      },
    );
    this.saveButton.addEventListener("click", () => void this.saveActiveStage());
    this.batchButton.addEventListener("click", () => void this.applyBatch());
    this.batchScope.addEventListener("change", () => this.updateSelectionCount());
    this.image.addEventListener("load", () => this.renderGeometry());
    window.addEventListener("resize", () => this.renderGeometry());
    this.image.addEventListener("error", () => {
      this.setMessage("The input preview is not ready. Run the preceding stage first.", "error");
      this.clearGeometry();
    });
    this.updateSelectionCount();
  }

  private activate(pageId: string, stage: StageName): void {
    const page = this.pages.get(pageId);
    const state = page ? stageFor(page, stage) : undefined;
    if (!page || !state) return;
    this.activePageId = pageId;
    this.activeStage = stage;
    this.draft = editableParameters(stage, state.parameters ?? {});
    this.title.textContent = `Page ${page.logical_sequence_number} · ${STAGE_LABELS[stage]}`;
    this.markActiveControls();
    this.renderControls();
    const trigger = this.stageTrigger(pageId, stage);
    const source = trigger?.dataset.inputPreviewUrl || page.thumbnail || "";
    if (source) {
      this.image.hidden = false;
      this.image.src = `${source}${source.includes("?") ? "&" : "?"}revision=${state.revision}`;
    } else {
      this.image.hidden = true;
      this.clearGeometry();
    }
    this.saveButton.disabled = !this.editable;
    this.setMessage(
      state.last_error ? state.last_error : `Revision ${state.revision} · ${state.status}`,
      state.last_error ? "error" : "info",
    );
  }

  private markActiveControls(): void {
    this.root.querySelectorAll<HTMLElement>("[data-stage-select]").forEach((item) => {
      item.classList.toggle("active", item.dataset.stageSelect === this.activeStage);
    });
    this.root.querySelectorAll<HTMLElement>("[data-stage-open]").forEach((item) => {
      item.classList.toggle(
        "active",
        item.dataset.stage === this.activeStage && item.dataset.pageId === this.activePageId,
      );
    });
  }

  private stageTrigger(pageId: string, stage: StageName): HTMLElement | null {
    return this.root.querySelector<HTMLElement>(
      `[data-stage-open][data-page-id="${pageId}"][data-stage="${stage}"]`,
    );
  }

  private activeContext(): { page: WorkspacePage; state: StageState } | null {
    if (!this.activePageId) return null;
    const page = this.pages.get(this.activePageId);
    const state = page ? stageFor(page, this.activeStage) : undefined;
    return page && state ? { page, state } : null;
  }

  private renderControls(): void {
    this.controls.replaceChildren();
    switch (this.activeStage) {
      case "page_split":
        this.renderPageSplitControls();
        break;
      case "deskew":
        this.renderDeskewControls();
        break;
      case "select_content":
        this.renderContentControls();
        break;
      case "margins":
        this.renderMarginControls();
        break;
      case "dewarping":
        this.renderDewarpControls();
        break;
      case "output":
        this.renderOutputControls();
        break;
    }
  }

  private renderPageSplitControls(): void {
    this.addSelect("Mode", "split_mode", ["auto", "manual", "single"], "auto", true);
    if (this.draft.split_mode === "manual") {
      this.addNumber("Split position", "split_x", 1, undefined, 1);
    }
  }

  private renderDeskewControls(): void {
    const mode = "manual_angle" in this.draft ? "manual" : "auto";
    this.addSelectValue("Mode", mode, ["auto", "manual"], (value) => {
      if (value === "manual") {
        const state = this.activeContext()?.state;
        this.draft.manual_angle = finiteNumber(state?.parameters.angle, 0);
      } else {
        delete this.draft.manual_angle;
      }
      this.renderControls();
      this.renderGeometry();
    });
    if ("manual_angle" in this.draft) {
      this.addNumber("Angle (degrees)", "manual_angle", -15, 15, 0.05);
    } else {
      this.addNumber("Auto minimum", "angle_min", -15, 0, 0.25, -7);
      this.addNumber("Auto maximum", "angle_max", 0, 15, 0.25, 7);
      this.addNumber("Search step", "angle_step", 0.05, 5, 0.05, 1);
    }
  }

  private renderContentControls(): void {
    this.addSelect("Mode", "content_mode", ["auto", "manual"], "auto", true);
  }

  private renderMarginControls(): void {
    this.addNumber("All margins (pixels)", "margin", 0, 5000, 1, 0);
    this.addSelect("Horizontal alignment", "align_x", ["left", "center", "right"], "center");
    this.addSelect("Vertical alignment", "align_y", ["top", "center", "bottom"], "center");
  }

  private renderDewarpControls(): void {
    this.addSelect("Mode", "dewarp_mode", ["auto", "manual", "bypass"], "auto", true);
  }

  private renderOutputControls(): void {
    const algorithms = ["otsu", "sauvola", "wolf", "fox", "window", "grad", "copy"];
    this.addSelect("Output algorithm", "output_mode", algorithms, "otsu");
    this.addCheckbox("Keep color", "keep_color", false);
    this.addCheckbox("Invert output", "invert", false);
    this.addNumber("Local window size", "window_size", 3, 501, 2, 35);
    this.addNumber("Threshold k", "k", -2, 2, 0.01, 0.33);
    this.addNumber("Threshold r", "r", 1, 512, 1, 128);
    this.addSelect("Text / picture zones", "zone_mode", ["auto", "manual"], "auto", true);
    if (this.draft.zone_mode === "manual") {
      this.addSelect("Text algorithm", "text_algorithm", algorithms, "sauvola");
      this.addSelect("Picture algorithm", "picture_algorithm", algorithms, "window");
    }
  }

  private addSelect(
    label: string,
    key: string,
    options: readonly string[],
    fallback: string,
    rerender = false,
  ): void {
    const current = typeof this.draft[key] === "string" ? String(this.draft[key]) : fallback;
    if (!(key in this.draft)) this.draft[key] = current;
    this.addSelectValue(label, current, options, (value) => {
      this.draft[key] = value;
      if (rerender) {
        this.renderControls();
        this.renderGeometry();
      }
    });
  }

  private addSelectValue(
    labelText: string,
    current: string,
    options: readonly string[],
    change: (value: string) => void,
  ): void {
    const label = document.createElement("label");
    label.textContent = labelText;
    const select = document.createElement("select");
    for (const value of options) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value.replaceAll("_", " ");
      option.selected = value === current;
      select.append(option);
    }
    select.addEventListener("change", () => change(select.value));
    label.append(select);
    this.controls.append(label);
  }

  private addNumber(
    labelText: string,
    key: string,
    minimum?: number,
    maximum?: number,
    step = 1,
    fallback = 0,
  ): void {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number";
    input.value = String(finiteNumber(this.draft[key], fallback));
    input.step = String(step);
    if (minimum !== undefined) input.min = String(minimum);
    if (maximum !== undefined) input.max = String(maximum);
    this.draft[key] = Number(input.value);
    input.addEventListener("input", () => {
      if (input.validity.valid) {
        this.draft[key] = Number(input.value);
        this.renderGeometry();
      }
    });
    label.append(input);
    this.controls.append(label);
  }

  private addCheckbox(labelText: string, key: string, fallback: boolean): void {
    const label = document.createElement("label");
    label.className = "checkbox";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = typeof this.draft[key] === "boolean" ? Boolean(this.draft[key]) : fallback;
    this.draft[key] = input.checked;
    input.addEventListener("change", () => {
      this.draft[key] = input.checked;
    });
    label.append(input, document.createTextNode(labelText));
    this.controls.append(label);
  }

  private sourceSize(): Size {
    const context = this.activeContext();
    const natural = {
      width: Math.max(1, this.image.naturalWidth),
      height: Math.max(1, this.image.naturalHeight),
    };
    if (!context) return natural;
    const own = context.state.parameters;
    const inputWidth = positiveNumber(own.input_width ?? own.source_width);
    const inputHeight = positiveNumber(own.input_height ?? own.source_height);
    if (inputWidth && inputHeight) return { width: inputWidth, height: inputHeight };
    const index = STAGE_ORDER.indexOf(this.activeStage);
    if (index > 0) {
      const previousName = STAGE_ORDER[index - 1];
      if (previousName) {
        const previous = stageFor(context.page, previousName)?.parameters ?? {};
        const width = positiveNumber(
          previous.output_width ?? previous.resolved_width ?? previous.canvas_width,
        );
        const height = positiveNumber(
          previous.output_height ?? previous.content_area_height ?? previous.canvas_height,
        );
        if (width && height) return { width, height };
      }
    }
    return natural;
  }

  private displaySize(): Size {
    return {
      width: Math.max(1, this.image.clientWidth),
      height: Math.max(1, this.image.clientHeight),
    };
  }

  private renderGeometry(): void {
    this.clearGeometry();
    if (!this.image.complete || !this.image.naturalWidth || this.image.hidden) return;
    const source = this.sourceSize();
    const display = this.displaySize();
    this.geometryLayer.style.width = `${display.width}px`;
    this.geometryLayer.style.height = `${display.height}px`;
    switch (this.activeStage) {
      case "page_split":
        if (this.draft.split_mode === "manual") this.renderSplitLine(source, display);
        break;
      case "select_content":
        if (this.draft.content_mode === "manual") {
          this.renderRectangle("content_box", "Content", "content", source, display, 0.08);
        }
        break;
      case "dewarping":
        if (this.draft.dewarp_mode === "manual") this.renderDewarpPoints(source, display);
        break;
      case "output":
        if (this.draft.zone_mode === "manual") {
          this.renderRectangle("text_box", "Text", "text", source, display, 0.08, 0.52);
          this.renderRectangle("picture_box", "Picture", "picture", source, display, 0.55, 0.92);
        }
        break;
      default:
        break;
    }
  }

  private clearGeometry(): void {
    for (const interaction of this.interactions) interaction.unset();
    this.interactions = [];
    this.geometryLayer.replaceChildren();
  }

  private renderSplitLine(source: Size, display: Size): void {
    const x = finiteNumber(this.draft.split_x, source.width / 2);
    this.draft.split_x = Math.round(x);
    const line = document.createElement("div");
    line.className = "geometry-split-line";
    line.setAttribute("role", "slider");
    line.setAttribute("aria-label", "Manual page split position");
    line.style.left = `${(x / source.width) * display.width}px`;
    this.geometryLayer.append(line);
    const interaction = interact(line).draggable({
      modifiers: [interact.modifiers.restrictRect({ restriction: "parent" })],
      listeners: {
        move: (event) => {
          const current = finiteNumber(line.style.left.replace("px", ""), 0);
          const next = clamp(current + event.dx, 0, display.width);
          line.style.left = `${next}px`;
          this.draft.split_x = Math.round((next / display.width) * source.width);
          line.setAttribute("aria-valuenow", String(this.draft.split_x));
        },
      },
    });
    this.interactions.push(interaction);
  }

  private renderRectangle(
    key: "content_box" | "text_box" | "picture_box",
    label: string,
    className: string,
    source: Size,
    display: Size,
    startRatio: number,
    endRatio = 0.92,
  ): void {
    const fallback = {
      x1: source.width * startRatio,
      y1: source.height * 0.1,
      x2: source.width * endRatio,
      y2: source.height * 0.9,
    };
    const sourceBox = normalizeBox(this.draft[key], fallback);
    this.draft[key] = [sourceBox.x1, sourceBox.y1, sourceBox.x2, sourceBox.y2];
    const box = sourceBoxToDisplay(sourceBox, source, display);
    const overlay = document.createElement("div");
    overlay.className = `geometry-rectangle geometry-${className}`;
    overlay.setAttribute("aria-label", `${label} region; drag or resize`);
    const caption = document.createElement("span");
    caption.textContent = label;
    overlay.append(caption);
    this.positionRectangle(overlay, box);
    this.geometryLayer.append(overlay);

    const commit = (): void => {
      const displayed = this.elementBox(overlay);
      const next = displayBoxToSource(displayed, source, display);
      this.draft[key] = [next.x1, next.y1, next.x2, next.y2];
    };
    const restriction = interact.modifiers.restrictRect({ restriction: "parent" });
    const interaction = interact(overlay)
      .draggable({
        modifiers: [restriction],
        listeners: {
          move: (event) => {
            const current = this.elementBox(overlay);
            const width = current.x2 - current.x1;
            const height = current.y2 - current.y1;
            const left = clamp(current.x1 + event.dx, 0, display.width - width);
            const top = clamp(current.y1 + event.dy, 0, display.height - height);
            this.positionRectangle(overlay, {
              x1: left,
              y1: top,
              x2: left + width,
              y2: top + height,
            });
            commit();
          },
        },
      })
      .resizable({
        edges: { left: true, right: true, bottom: true, top: true },
        modifiers: [
          interact.modifiers.restrictEdges({ outer: "parent" }),
          interact.modifiers.restrictSize({ min: { width: 24, height: 24 } }),
        ],
        listeners: {
          move: (event) => {
            const parentRect = this.geometryLayer.getBoundingClientRect();
            this.positionRectangle(overlay, {
              x1: event.rect.left - parentRect.left,
              y1: event.rect.top - parentRect.top,
              x2: event.rect.right - parentRect.left,
              y2: event.rect.bottom - parentRect.top,
            });
            commit();
          },
        },
      });
    this.interactions.push(interaction);
  }

  private positionRectangle(elementToMove: HTMLElement, box: Box): void {
    elementToMove.style.left = `${box.x1}px`;
    elementToMove.style.top = `${box.y1}px`;
    elementToMove.style.width = `${Math.max(1, box.x2 - box.x1)}px`;
    elementToMove.style.height = `${Math.max(1, box.y2 - box.y1)}px`;
  }

  private elementBox(target: HTMLElement): Box {
    const left = finiteNumber(target.style.left.replace("px", ""), 0);
    const top = finiteNumber(target.style.top.replace("px", ""), 0);
    return {
      x1: left,
      y1: top,
      x2: left + finiteNumber(target.style.width.replace("px", ""), target.offsetWidth),
      y2: top + finiteNumber(target.style.height.replace("px", ""), target.offsetHeight),
    };
  }

  private renderDewarpPoints(source: Size, display: Size): void {
    const fallback: Point[] = [
      { x: source.width * 0.05, y: source.height * 0.05 },
      { x: source.width * 0.95, y: source.height * 0.05 },
      { x: source.width * 0.95, y: source.height * 0.95 },
      { x: source.width * 0.05, y: source.height * 0.95 },
    ];
    const points = this.normalizedPoints(this.draft.dewarp_points, fallback);
    this.draft.dewarp_points = points.map((point) => [Math.round(point.x), Math.round(point.y)]);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("geometry-lines");
    const polygon = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    svg.append(polygon);
    this.geometryLayer.append(svg);
    const displayed = points.map((point) => ({
      x: (point.x / source.width) * display.width,
      y: (point.y / source.height) * display.height,
    }));
    const redraw = (): void => {
      polygon.setAttribute("points", displayed.map((point) => `${point.x},${point.y}`).join(" "));
      this.draft.dewarp_points = displayed.map((point) => [
        Math.round((point.x / display.width) * source.width),
        Math.round((point.y / display.height) * source.height),
      ]);
    };
    displayed.forEach((point, index) => {
      const handle = document.createElement("div");
      handle.className = "geometry-point";
      handle.style.left = `${point.x}px`;
      handle.style.top = `${point.y}px`;
      handle.setAttribute("aria-label", `Dewarp control point ${index + 1}`);
      this.geometryLayer.append(handle);
      const interaction = interact(handle).draggable({
        modifiers: [interact.modifiers.restrictRect({ restriction: "parent" })],
        listeners: {
          move: (event) => {
            point.x = clamp(point.x + event.dx, 0, display.width);
            point.y = clamp(point.y + event.dy, 0, display.height);
            handle.style.left = `${point.x}px`;
            handle.style.top = `${point.y}px`;
            redraw();
          },
        },
      });
      this.interactions.push(interaction);
    });
    redraw();
  }

  private normalizedPoints(value: unknown, fallback: Point[]): Point[] {
    if (!Array.isArray(value) || value.length !== 4) return fallback;
    return value.map((item, index) => {
      const defaultPoint = fallback[index] ?? { x: 0, y: 0 };
      if (Array.isArray(item) && item.length >= 2) {
        return {
          x: finiteNumber(item[0], defaultPoint.x),
          y: finiteNumber(item[1], defaultPoint.y),
        };
      }
      if (isObject(item)) {
        return {
          x: finiteNumber(item.x, defaultPoint.x),
          y: finiteNumber(item.y, defaultPoint.y),
        };
      }
      return defaultPoint;
    });
  }

  private async saveActiveStage(): Promise<void> {
    const context = this.activeContext();
    if (!context || !this.activePageId || !this.editable) return;
    const trigger = this.stageTrigger(this.activePageId, this.activeStage);
    const url = trigger?.dataset.stageUrl;
    if (!url) return;
    this.saveButton.disabled = true;
    this.setMessage("Saving revision and queueing a preview…", "info");
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({
          expected_revision: context.state.revision,
          algorithm: context.state.algorithm,
          parameters: this.draft,
        }),
      });
      if (response.status === 409) {
        await this.refreshPage(url);
        this.setMessage(
          "Conflict detected: another editor changed this stage. Latest values loaded; review before retrying.",
          "conflict",
        );
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as { stage: StageState };
      this.mergeStage(payload.stage);
      this.setMessage("Saved. The worker is generating the new preview.", "success");
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Stage update failed.", "error");
    } finally {
      this.saveButton.disabled = !this.editable;
    }
  }

  private async applyBatch(): Promise<void> {
    if (!this.editable) return;
    const batchUrl = this.root
      .querySelector<HTMLElement>(`[data-batch-url][data-stage="${this.activeStage}"]`)
      ?.dataset.batchUrl;
    if (!batchUrl) return;
    const selected = [...this.selectedPages];
    if (this.batchScope.value === "selected" && selected.length === 0) {
      this.setMessage("Select at least one page before applying a batch.", "error");
      return;
    }
    this.batchButton.disabled = true;
    this.setMessage("Applying stage settings to the batch…", "info");
    const body: JsonObject = {
      parameters: this.draft,
      preserve_exceptions: this.preserveExceptions.checked,
    };
    if (this.batchScope.value === "selected") body.page_ids = selected;
    try {
      const response = await fetch(batchUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as {
        applied: { page_id: string; stage: StageState }[];
        applied_count: number;
        skipped_count: number;
      };
      for (const item of payload.applied) this.mergeStage(item.stage);
      this.setMessage(
        `Queued ${payload.applied_count} page(s); preserved ${payload.skipped_count} manual exception(s).`,
        "success",
      );
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Batch update failed.", "error");
    } finally {
      this.batchButton.disabled = !this.editable;
    }
  }

  private async refreshPage(stageUrl: string): Promise<void> {
    const response = await fetch(stageUrl, { credentials: "same-origin" });
    if (!response.ok) throw new Error("Could not load the latest stage revision.");
    const page = (await response.json()) as WorkspacePage;
    this.pages.set(page.id, page);
    this.activate(page.id, this.activeStage);
  }

  private mergeStage(incoming: StageState): void {
    const page = this.pages.get(incoming.scan_page_id);
    if (!page) return;
    const index = page.stages.findIndex((state) => state.stage === incoming.stage);
    if (index >= 0) page.stages[index] = incoming;
    else page.stages.push(incoming);
    this.updateStageDom(incoming);
  }

  private updateStageDom(state: StageState): void {
    const row = this.root.querySelector<HTMLElement>(
      `[data-stage-row][data-page-id="${state.scan_page_id}"][data-stage="${state.stage}"]`,
    );
    if (!row) return;
    const status = row.querySelector<HTMLElement>("[data-stage-status]");
    if (status) {
      status.textContent = state.status;
      status.className = `status status-${state.status}`;
    }
    const error = row.querySelector<HTMLElement>("[data-stage-error]");
    if (error) {
      error.textContent = state.last_error;
      error.hidden = !state.last_error;
    }
    const revision = row.querySelector<HTMLInputElement>("input[name='expected_revision']");
    if (revision) revision.value = String(state.revision);
    const preview = row.querySelector<HTMLElement>("[data-stage-preview-link]");
    if (preview && (state.preview_storage_identifier || state.output_storage_identifier)) {
      preview.hidden = false;
    }
  }

  private startLiveUpdates(): void {
    const eventUrl = this.root.dataset.eventsUrl;
    if (eventUrl && typeof EventSource !== "undefined") {
      this.eventSource = new EventSource(eventUrl);
      this.eventSource.addEventListener("open", () => {
        this.liveStatus.textContent = "Live updates connected";
        this.liveStatus.className = "live-status connected";
      });
      this.eventSource.addEventListener("workspace", (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as WorkspaceEvent;
        for (const state of payload.stages) this.mergeStage(state);
        const context = this.activeContext();
        if (context && ["success", "failed"].includes(context.state.status)) {
          const trigger = this.stageTrigger(context.page.id, context.state.stage);
          const source = trigger?.dataset.inputPreviewUrl;
          if (source && this.image.src) {
            this.image.src = `${source}${source.includes("?") ? "&" : "?"}revision=${context.state.revision}-${context.state.status}`;
          }
        }
      });
      this.eventSource.addEventListener("error", () => {
        this.liveStatus.textContent = "Live updates reconnecting…";
        this.liveStatus.className = "live-status reconnecting";
      });
      return;
    }
    const workspaceUrl = this.root.dataset.workspaceUrl;
    if (workspaceUrl) {
      this.liveStatus.textContent = "Live updates unavailable; using compatibility polling";
      this.pollingTimer = window.setInterval(() => void this.pollWorkspace(workspaceUrl), 5000);
    }
  }

  private async pollWorkspace(workspaceUrl: string): Promise<void> {
    try {
      const response = await fetch(workspaceUrl, { credentials: "same-origin" });
      if (!response.ok) return;
      const payload = (await response.json()) as { pages: WorkspacePage[] };
      for (const page of payload.pages) {
        this.pages.set(page.id, page);
        for (const state of page.stages) this.updateStageDom(state);
      }
    } catch {
      this.liveStatus.textContent = "Workspace refresh failed; retrying…";
    }
  }

  private updateSelectionCount(): void {
    this.selectionCount.textContent = `${this.selectedPages.size} page(s) selected`;
    this.batchButton.disabled =
      !this.editable ||
      (this.batchScope.value === "selected" && this.selectedPages.size === 0);
  }

  private setMessage(message: string, kind: "info" | "success" | "error" | "conflict"): void {
    this.message.textContent = message;
    this.message.className = `editor-message editor-message-${kind}`;
  }

  private disableEditor(message: string): void {
    this.image.hidden = true;
    this.controls.replaceChildren();
    this.saveButton.disabled = true;
    this.batchButton.disabled = true;
    this.setMessage(message, "info");
  }
}

function parseWorkspacePages(): WorkspacePage[] {
  const script = document.querySelector<HTMLScriptElement>("#scantailor-workspace-data");
  if (!script?.textContent) return [];
  const value = JSON.parse(script.textContent) as unknown;
  return Array.isArray(value) ? (value as WorkspacePage[]) : [];
}

export function initializeScanTailorWorkspace(root: HTMLElement): void {
  new WorkspaceController(root, parseWorkspacePages()).start();
}

const workspace = document.querySelector<HTMLElement>("[data-scantailor-workspace]");
if (workspace) initializeScanTailorWorkspace(workspace);
