import interact from "interactjs";

/** Pipeline order (#195). There is no Fix Orientation and no Page Split stage. */
export const STAGE_ORDER = [
  "deskew",
  "select_content",
  "dewarping",
  "margins",
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
  invalidated_reason?: string;
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
  deskew: "Deskew",
  select_content: "Select content",
  margins: "Margins",
  dewarping: "Dewarping",
  output: "Output / binarization",
};

const EDITABLE_KEYS: Record<StageName, readonly string[]> = {
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
  dewarping: ["dewarp_mode", "dewarp_model", "dewarp_points", "dewarp_curves"],
  output: [
    "output_mode",
    "keep_color",
    "invert",
    "zone_mode",
    "zones",
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
    if (
      key in parameters &&
      !(stage === "output" && key === "zones" && parameters.zone_mode !== "manual")
    ) {
      editable[key] = structuredClone(parameters[key]);
    }
  }
  if (stage === "margins") {
    // Older workers stored automatic canvas dimensions under the request keys.
    // Recover that common case without discarding a larger, explicitly sized canvas.
    for (const [axis, before, after] of [
      ["width", "left", "right"],
      ["height", "top", "bottom"],
    ] as const) {
      const size = positiveNumber(parameters[`input_${axis}`] ?? parameters[`source_${axis}`]);
      const padding =
        finiteNumber(parameters[`margin_${before}`], 0) +
        finiteNumber(parameters[`margin_${after}`], 0);
      if (
        !(`resolved_canvas_${axis}` in parameters) &&
        size &&
        parameters[`canvas_${axis}`] === size + padding
      ) {
        delete editable[`canvas_${axis}`];
      }
    }
  }
  return editable;
}

/** Explicitly copy only baseline geometry when an operator starts manual editing. */
export function curvatureCurvesForEditing(parameters: JsonObject): number[][][] {
  const raw =
    parameters.dewarp_mode === "manual" ? parameters.dewarp_curves : parameters.curvature_curves;
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, 32).flatMap((curve): number[][][] => {
    if (
      !Array.isArray(curve) ||
      curve.length < 3 ||
      curve.length > 17 ||
      !curve.every(
        (point) =>
          Array.isArray(point) &&
          point.length === 2 &&
          point.every((value) => typeof value === "number" && Number.isFinite(value)),
      )
    )
      return [];
    return [curve.map((point) => [...point] as number[])];
  });
}

export interface OutputZone {
  kind: "text" | "picture";
  box: number[];
}

/** Copy editable geometry only: workers own detection/provenance fields. */
export function outputZonesForEditing(parameters: JsonObject): OutputZone[] {
  const raw =
    parameters.zone_mode === "manual" && Array.isArray(parameters.zones)
      ? parameters.zones
      : parameters.resolved_zones;
  if (Array.isArray(raw)) {
    return raw.slice(0, 64).flatMap((zone): OutputZone[] => {
      if (
        !isObject(zone) ||
        (zone.kind !== "text" && zone.kind !== "picture") ||
        !Array.isArray(zone.box) ||
        zone.box.length !== 4 ||
        !zone.box.every((v) => typeof v === "number" && Number.isFinite(v))
      )
        return [];
      return [{ kind: zone.kind, box: [...zone.box] as number[] }];
    });
  }
  return (["text", "picture"] as const).flatMap((kind) => {
    const box = parameters[`${kind}_box`];
    if (!(Array.isArray(box) && box.length === 4) && !isObject(box)) return [];
    const normalized = normalizeBox(box, { x1: 0, y1: 0, x2: 1, y2: 1 });
    return [{ kind, box: [normalized.x1, normalized.y1, normalized.x2, normalized.y2] }];
  });
}

export function stageWorkerWarnings(parameters: JsonObject): string {
  return [parameters.dewarp_warning, parameters.output_warning]
    .filter((warning): warning is string => typeof warning === "string" && warning.trim() !== "")
    .map((warning) =>
      warning === "auto_detection_found_no_text_lines_or_page_outline"
        ? "Automatic dewarping found no reliable text lines or page outline; the input was kept unchanged."
        : warning,
    )
    .join(" · ");
}

/** Outcome of re-requesting a preview image that failed to load. */
export interface PreviewFailure {
  /** HTTP status, or null when the request itself failed (network error). */
  status: number | null;
  /** Plain-text response body, if any. */
  body: string;
}

export function previousStageName(stage: StageName): StageName | null {
  const index = STAGE_ORDER.indexOf(stage);
  return index > 0 ? (STAGE_ORDER[index - 1] ?? null) : null;
}

/**
 * Explain why the editor preview for ``stage`` could not be shown, based on the
 * actual response from the preview endpoint rather than the generic image
 * ``error`` event (which carries no status or body).
 */
export function previewErrorMessage(stage: StageName, failure: PreviewFailure): string {
  const { status } = failure;
  const body = failure.body.trim().toLowerCase();
  if (status === null) {
    return "Could not load the preview because of a network error. Check your connection and try again.";
  }
  if (previewIsBeingPrepared(failure)) {
    return "The page image is still being prepared. It will appear automatically.";
  }
  if (status === 404 && body === "input preview not ready") {
    const previous = previousStageName(stage);
    if (previous === null) {
      return "The scanned page image is not available yet. Wait for the upload to finish processing, then reopen this page.";
    }
    return `The input preview is not ready. Run the ${STAGE_LABELS[previous]} stage first.`;
  }
  if (status === 404 && body === "preview not ready") {
    return "The preview for this stage has not been generated yet. It will appear once the worker finishes.";
  }
  if (status === 404 && body === "preview artifact missing") {
    return "The preview file is missing from storage. Save this stage again to regenerate it.";
  }
  if (status === 503) {
    return "Scan storage is temporarily unavailable. Try again shortly.";
  }
  if (status === 401 || status === 403) {
    return "You are not allowed to view this preview. Your session may have expired; sign in again.";
  }
  if (status >= 200 && status < 300) {
    return "The preview was downloaded but could not be displayed as an image.";
  }
  return `Could not load the preview (HTTP ${status}).`;
}

/** Retries of a preview that is being prepared: about one minute in total. */
export const PREVIEW_RETRY_MAX_ATTEMPTS = 10;
const PREVIEW_RETRY_BASE_MS = 1000;
const PREVIEW_RETRY_MAX_MS = 8000;

export const PREVIEW_RETRY_EXHAUSTED_MESSAGE =
  "The page image is taking longer than expected to prepare. Reopen this page later.";

/** Where the editor is in recovering one preview URL. */
export interface PreviewRetryState {
  /** Retries made because the server was still preparing the image. */
  attempts: number;
  /** Whether the image was already reloaded because the diagnostic request succeeded. */
  reloadedAfterOk: boolean;
}

export const INITIAL_PREVIEW_RETRY: PreviewRetryState = { attempts: 0, reloadedAfterOk: false };

/**
 * The server is still generating the page's working image (pages uploaded before
 * the working images existed, or a new upload whose job has not run yet).
 */
export function previewIsBeingPrepared(failure: PreviewFailure): boolean {
  return failure.status === 503 && failure.body.trim().toLowerCase() === "input preview not ready";
}

/**
 * Decide how to recover a preview that failed to load: reload it after ``delay``
 * milliseconds, or give up (``null``).
 *
 * - The diagnostic request succeeded: the image became ready in between, so reload
 *   it at once, but only once, so an image the browser cannot decode never loops.
 * - The image is still being prepared: retry with exponential backoff, bounded.
 * - Anything else is not going to fix itself: give up and explain.
 */
export function nextPreviewRetry(
  failure: PreviewFailure,
  state: PreviewRetryState,
): { delay: number; state: PreviewRetryState } | null {
  if (failure.status !== null && failure.status >= 200 && failure.status < 300) {
    return state.reloadedAfterOk ? null : { delay: 0, state: { ...state, reloadedAfterOk: true } };
  }
  if (!previewIsBeingPrepared(failure) || state.attempts >= PREVIEW_RETRY_MAX_ATTEMPTS) {
    return null;
  }
  return {
    delay: Math.min(PREVIEW_RETRY_BASE_MS * 2 ** state.attempts, PREVIEW_RETRY_MAX_MS),
    state: { ...state, attempts: state.attempts + 1 },
  };
}

/** ``url`` with a cache-busting retry marker, so the browser requests it again. */
export function previewRetryUrl(url: string, attempt: number): string {
  return `${url}${url.includes("?") ? "&" : "?"}retry=${attempt}`;
}

/** Error text for a stage, shown only for genuine failures (not routine invalidation). */
export function stageErrorText(state: Pick<StageState, "status" | "last_error">): string {
  return state.status === "failed" ? state.last_error || "Stage failed" : "";
}

/** Short status shown in the page list for a stage. */
export function stageStatusLabel(state: Pick<StageState, "status">): string {
  return state.status;
}

/** Editor status line for a stage: errors only for failures, invalidation is informational. */
export function stageStatusMessage(
  state: Pick<StageState, "status" | "last_error" | "revision" | "invalidated_reason">,
): { text: string; tone: "info" | "error" } {
  const error = stageErrorText(state);
  if (error) return { text: error, tone: "error" };
  const base = `Revision ${state.revision} · ${state.status}`;
  if (state.status === "invalidated" && state.invalidated_reason) {
    return { text: `${base} (${state.invalidated_reason})`, tone: "info" };
  }
  return { text: base, tone: "info" };
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
  return document.querySelector<HTMLInputElement>("input[name='csrfmiddlewaretoken']")?.value ?? "";
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
  private readonly inputButton: HTMLButtonElement;
  private readonly resultButton: HTMLButtonElement;
  private readonly warnings: HTMLElement;
  private previewMode: "input" | "result" = "input";
  private activePageId: string | null = null;
  private activeStage: StageName = STAGE_ORDER[0];
  private activeCurvatureCurve = 0;
  private draft: JsonObject = {};
  private interactions: InteractableHandle[] = [];
  private eventSource: EventSource | null = null;
  private pollingTimer: number | null = null;
  /** The preview URL being shown, without the cache-busting retry marker. */
  private previewUrl = "";
  private previewRetry: PreviewRetryState = INITIAL_PREVIEW_RETRY;
  private previewRetryTimer: number | null = null;

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
    this.inputButton = element(root, "[data-preview-input]");
    this.resultButton = element(root, "[data-preview-result]");
    this.warnings = element(root, "[data-stage-warnings]");
    for (const page of pages) {
      this.pages.set(page.id, page);
    }
  }

  start(): void {
    this.bindWorkspaceControls();
    const firstPage = this.pages.values().next().value as WorkspacePage | undefined;
    if (firstPage) {
      this.activate(firstPage.id, STAGE_ORDER[0]);
    } else {
      this.disableEditor("Upload a page to begin editing.");
    }
    this.startLiveUpdates();
  }

  private bindWorkspaceControls(): void {
    this.inputButton.addEventListener("click", () => this.showPreview("input"));
    this.resultButton.addEventListener("click", () => this.showPreview("result"));
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
    this.image.addEventListener("load", () => {
      if (
        this.activeStage === "dewarping" &&
        this.draft.dewarp_model === "curvature" &&
        this.draft.dewarp_mode === "manual" &&
        curvatureCurvesForEditing(this.draft).length < 2
      )
        this.renderControls();
      this.renderGeometry();
      if (this.previewRetry !== INITIAL_PREVIEW_RETRY) {
        // Recovered after a retry: replace the "being prepared" note.
        this.previewRetry = INITIAL_PREVIEW_RETRY;
        const state = this.activeContext()?.state;
        if (state) {
          const message = stageStatusMessage(state);
          this.setMessage(message.text, message.tone);
        }
      }
    });
    window.addEventListener("resize", () => this.renderGeometry());
    this.image.addEventListener("error", () => {
      this.clearGeometry();
      void this.explainPreviewFailure(this.image.src, this.activeStage);
    });
    this.updateSelectionCount();
  }

  /**
   * The ``<img>`` error event carries no status or body, so re-request the
   * failed URL to find out why and show a message that matches the cause.
   *
   * When the image turns out to be available after all, or the server is still
   * preparing it, the image is reloaded (with backoff, bounded) so it appears
   * without the member having to navigate away.
   */
  private async explainPreviewFailure(source: string, stage: StageName): Promise<void> {
    if (!source) return;
    if (this.previewRetry === INITIAL_PREVIEW_RETRY) {
      this.setMessage("Loading the preview failed; checking why…", "error");
    }
    let failure: PreviewFailure;
    try {
      const response = await fetch(source, { credentials: "same-origin" });
      if (response.ok) {
        // Only the status matters; the image element downloads it again.
        void response.body?.cancel().catch(() => undefined);
      }
      const body = response.ok ? "" : await response.text();
      failure = { status: response.status, body };
    } catch {
      failure = { status: null, body: "" };
    }
    // Ignore stale answers if the editor moved to another page/stage meanwhile.
    if (this.image.src !== source || this.activeStage !== stage) return;
    const retry = nextPreviewRetry(failure, this.previewRetry);
    if (retry) {
      this.previewRetry = retry.state;
      if (retry.delay > 0) this.setMessage(previewErrorMessage(stage, failure), "info");
      this.schedulePreviewReload(retry.delay);
      return;
    }
    this.setMessage(
      previewIsBeingPrepared(failure)
        ? PREVIEW_RETRY_EXHAUSTED_MESSAGE
        : previewErrorMessage(stage, failure),
      "error",
    );
  }

  private schedulePreviewReload(delay: number): void {
    this.cancelPreviewReload();
    const url = this.previewUrl;
    const attempt = this.previewRetry.attempts + (this.previewRetry.reloadedAfterOk ? 1 : 0);
    this.previewRetryTimer = window.setTimeout(() => {
      this.previewRetryTimer = null;
      if (url && this.previewUrl === url) this.image.src = previewRetryUrl(url, attempt);
    }, delay);
  }

  private cancelPreviewReload(): void {
    if (this.previewRetryTimer !== null) window.clearTimeout(this.previewRetryTimer);
    this.previewRetryTimer = null;
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
    this.showPreview("input");
    this.saveButton.disabled = !this.editable;
    const message = stageStatusMessage(state);
    this.setMessage(message.text, message.tone);
  }

  private showPreview(mode: "input" | "result"): void {
    const context = this.activeContext();
    if (!context) return;
    const state = context.state;
    const hasResult =
      state.status === "success" &&
      Boolean(state.preview_storage_identifier || state.output_storage_identifier);
    this.resultButton.disabled = !hasResult;
    this.previewMode = mode === "result" && hasResult ? "result" : "input";
    this.inputButton.setAttribute("aria-pressed", String(this.previewMode === "input"));
    this.resultButton.setAttribute("aria-pressed", String(this.previewMode === "result"));
    this.image.alt =
      this.previewMode === "input"
        ? "Processing-stage input preview"
        : "Processing-stage result preview";
    const trigger = this.stageTrigger(context.page.id, state.stage);
    const source =
      this.previewMode === "result"
        ? trigger?.dataset.outputPreviewUrl
        : trigger?.dataset.inputPreviewUrl || context.page.thumbnail;
    this.image.hidden = !source;
    const url = source
      ? `${source}${source.includes("?") ? "&" : "?"}revision=${state.revision}-${state.status}`
      : "";
    // A retry in progress for the same URL keeps going; a new URL starts afresh.
    if (url !== this.previewUrl) {
      this.cancelPreviewReload();
      this.previewRetry = INITIAL_PREVIEW_RETRY;
      this.previewUrl = url;
      if (url) this.image.src = url;
    }
    const warning = state.status === "success" ? stageWorkerWarnings(state.parameters) : "";
    this.warnings.textContent = warning ? `Worker warning: ${warning}` : "";
    this.warnings.hidden = !warning;
    this.geometryLayer.hidden = this.previewMode === "result";
    this.renderGeometry();
  }

  private updateActivePreview(state: StageState, previous?: StageState): void {
    if (state.scan_page_id !== this.activePageId || state.stage !== this.activeStage) return;
    const justCompleted =
      state.status === "success" &&
      (previous?.status !== "success" || previous.revision !== state.revision);
    this.showPreview(justCompleted ? "result" : this.previewMode);
    if (justCompleted) this.setMessage("Completed. Result preview is ready.", "success");
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
    const model = this.draft.dewarp_model === "curvature" ? "curvature" : "perspective";
    this.draft.dewarp_model = model;
    this.addSelectValue("Dewarp model", model, ["perspective", "curvature"], (value) => {
      this.draft.dewarp_model = value;
      if (value === "perspective") delete this.draft.dewarp_curves;
      else delete this.draft.dewarp_points;
      this.renderControls();
      this.showPreview("input");
    });
    const mode = String(this.draft.dewarp_mode ?? "auto");
    this.draft.dewarp_mode = mode;
    this.addSelectValue("Mode", mode, ["auto", "manual", "bypass"], (value) => {
      this.draft.dewarp_mode = value;
      if (value !== "manual") {
        delete this.draft.dewarp_curves;
        delete this.draft.dewarp_points;
      }
      this.renderControls();
      this.showPreview("input");
    });
    if (model !== "curvature") return;
    const note = document.createElement("p"); note.className = "muted";
    note.textContent = "Opt-in baseline correction: inspect the Result preview before finishing.";
    this.controls.append(note);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Edit detected baselines";
    button.addEventListener("click", () => {
      this.draft.dewarp_curves = curvatureCurvesForEditing(
        this.activeContext()?.state.parameters ?? {},
      );
      this.draft.dewarp_mode = "manual";
      this.renderControls();
      this.showPreview("input");
    });
    this.controls.append(button);
    if (mode !== "manual") return;
    let curves = curvatureCurvesForEditing(this.draft);
    const size = this.sourceSize();
    if (curves.length < 2 && size.width > 2 && size.height > 10) {
      curves = [0.25, 0.75].map((ratio) => [
        [0, size.height * ratio],
        [size.width / 2, size.height * ratio],
        [size.width - 1, size.height * ratio],
      ]);
    }
    this.draft.dewarp_curves = curves;
    if (curves.length < 2) return;
    this.activeCurvatureCurve = Math.min(this.activeCurvatureCurve, curves.length - 1);
    this.addSelectValue(
      "Baseline",
      String(this.activeCurvatureCurve + 1),
      curves.map((_, index) => String(index + 1)),
      (value) => {
        this.activeCurvatureCurve = Number(value) - 1;
        this.renderControls();
        this.renderGeometry();
      },
    );
    curves[this.activeCurvatureCurve]!.forEach((point, index) => {
      const label = document.createElement("label");
      label.textContent = `Baseline point ${index + 1} y`;
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.max = String(size.height - 1);
      input.step = "0.1";
      input.value = String(point[1]);
      input.addEventListener("change", () => {
        point[1] = clamp(finiteNumber(input.value, point[1] ?? 0), 0, size.height - 1);
        this.renderGeometry();
      });
      label.append(input);
      this.controls.append(label);
    });
    for (const action of ["Add baseline", "Remove baseline"]) {
      const control = document.createElement("button");
      control.type = "button";
      control.textContent = action;
      control.disabled = action === "Add baseline" ? curves.length >= 32 : curves.length <= 2;
      control.addEventListener("click", () => {
        if (action === "Remove baseline") curves.splice(this.activeCurvatureCurve, 1);
        else {
          const ys = curves
            .map((curve) => curve[Math.floor(curve.length / 2)]![1]!)
            .sort((a, b) => a - b);
          let y = size.height / 2;
          let gap = 0;
          for (let i = 1; i < ys.length; i++)
            if (ys[i]! - ys[i - 1]! > gap) {
              gap = ys[i]! - ys[i - 1]!;
              y = (ys[i]! + ys[i - 1]!) / 2;
            }
          curves.push([
            [0, y],
            [size.width / 2, y],
            [size.width - 1, y],
          ]);
          this.activeCurvatureCurve = curves.length - 1;
        }
        this.renderControls();
        this.renderGeometry();
      });
      this.controls.append(control);
    }
  }

  private renderOutputControls(): void {
    const algorithms = ["otsu", "sauvola", "wolf", "fox", "window", "grad", "copy"];
    this.addSelect("Output algorithm", "output_mode", algorithms, "otsu");
    this.addCheckbox("Keep color", "keep_color", false);
    this.addCheckbox("Invert output", "invert", false);
    this.addNumber("Local window size", "window_size", 3, 501, 2, 35);
    this.addNumber("Threshold k", "k", -2, 2, 0.01, 0.33);
    this.addNumber("Threshold r", "r", 1, 512, 1, 128);
    this.addSelect("Text / picture zones", "zone_mode", ["auto", "manual", "off"], "auto", true);
    const detected = outputZonesForEditing(this.activeContext()?.state.parameters ?? {});
    if (this.draft.zone_mode !== "off") {
      this.addSelect("Text algorithm", "text_algorithm", algorithms, "sauvola");
      this.addSelect(
        "Picture algorithm (Keep color uses copy)",
        "picture_algorithm",
        algorithms,
        "copy",
      );
    }
    if (this.draft.zone_mode === "manual") {
      if (!("zones" in this.draft)) this.draft.zones = detected;
      const zones = outputZonesForEditing(this.draft);
      this.draft.zones = zones;
      zones.forEach((zone, index) => {
        this.addSelectValue(`Region ${index + 1} type`, zone.kind, ["text", "picture"], (kind) => {
          zone.kind = kind as OutputZone["kind"];
          this.renderGeometry();
        });
        for (const [coordinate, label] of ["Left", "Top", "Right", "Bottom"].entries()) {
          const wrapper = document.createElement("label");
          wrapper.textContent = `Region ${index + 1} ${label}`;
          const input = document.createElement("input");
          input.type = "number";
          input.min = "0";
          input.value = String(zone.box[coordinate]);
          input.addEventListener("change", () => {
            zone.box[coordinate] = Math.max(
              0,
              finiteNumber(input.value, zone.box[coordinate] ?? 0),
            );
            this.renderGeometry();
          });
          wrapper.append(input);
          this.controls.append(wrapper);
        }
        this.addZoneButton(`Remove region ${index + 1}`, () => {
          zones.splice(index, 1);
          this.renderControls();
          this.renderGeometry();
        });
      });
      for (const kind of ["text", "picture"] as const)
        this.addZoneButton(`Add ${kind} region`, () => {
          if (zones.length >= 64) return;
          const source = this.sourceSize();
          zones.push({
            kind,
            box: [
              Math.round(source.width * 0.1),
              Math.round(source.height * 0.1),
              Math.round(source.width * 0.5),
              Math.round(source.height * 0.5),
            ],
          });
          this.renderControls();
          this.renderGeometry();
        });
    } else if (this.draft.zone_mode === "auto") {
      this.addZoneButton(`Edit detected regions (${detected.length})`, () => {
        this.draft.zone_mode = "manual";
        this.draft.zones = outputZonesForEditing(this.activeContext()?.state.parameters ?? {});
        this.showPreview("input");
        this.renderControls();
        this.renderGeometry();
      });
    }
  }

  private addZoneButton(label: string, action: () => void): void {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", action);
    this.controls.append(button);
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
    if (this.previewMode === "result") return;
    if (!this.image.complete || !this.image.naturalWidth || this.image.hidden) return;
    const source = this.sourceSize();
    const display = this.displaySize();
    this.geometryLayer.style.width = `${display.width}px`;
    this.geometryLayer.style.height = `${display.height}px`;
    switch (this.activeStage) {
      case "select_content":
        if (this.draft.content_mode === "manual") {
          this.renderRectangle("content_box", "Content", "content", source, display, 0.08);
        }
        break;
      case "dewarping":
        if (this.draft.dewarp_mode === "manual") {
          if (this.draft.dewarp_model === "curvature") this.renderCurvatureCurves(source, display);
          else this.renderDewarpPoints(source, display);
        }
        break;
      case "output": {
        const manual = this.draft.zone_mode === "manual";
        const zones = outputZonesForEditing(
          manual ? this.draft : (this.activeContext()?.state.parameters ?? {}),
        );
        zones.forEach((zone, index) => {
          const box = normalizeBox(zone.box, {
            x1: 0,
            y1: 0,
            x2: source.width,
            y2: source.height,
          });
          if (manual) {
            this.renderRectangle(
              "zones",
              `${zone.kind} ${index + 1}`,
              zone.kind,
              source,
              display,
              0,
              1,
              box,
              (next) => {
                const authored = this.draft.zones as OutputZone[];
                authored[index]!.box = [next.x1, next.y1, next.x2, next.y2];
              },
            );
          } else if (this.draft.zone_mode === "auto") {
            const overlay = document.createElement("div");
            overlay.className = `geometry-rectangle geometry-${zone.kind}`;
            overlay.style.pointerEvents = "none";
            overlay.textContent = `Detected ${zone.kind} ${index + 1}`;
            this.positionRectangle(overlay, sourceBoxToDisplay(box, source, display));
            this.geometryLayer.append(overlay);
          }
        });
        break;
      }
      default:
        break;
    }
  }

  private clearGeometry(): void {
    for (const interaction of this.interactions) interaction.unset();
    this.interactions = [];
    this.geometryLayer.replaceChildren();
  }

  private renderRectangle(
    key: string,
    label: string,
    className: string,
    source: Size,
    display: Size,
    startRatio: number,
    endRatio = 0.92,
    explicitBox?: Box,
    onChange?: (box: Box) => void,
  ): void {
    const fallback = {
      x1: source.width * startRatio,
      y1: source.height * 0.1,
      x2: source.width * endRatio,
      y2: source.height * 0.9,
    };
    const sourceBox = explicitBox ?? normalizeBox(this.draft[key], fallback);
    if (!onChange) this.draft[key] = [sourceBox.x1, sourceBox.y1, sourceBox.x2, sourceBox.y2];
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
      if (onChange) onChange(next);
      else this.draft[key] = [next.x1, next.y1, next.x2, next.y2];
    };
    const restriction = interact.modifiers.restrictRect({
      restriction: "parent",
    });
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

  private renderCurvatureCurves(source: Size, display: Size): void {
    const curves = this.draft.dewarp_curves as number[][][] | undefined;
    if (!Array.isArray(curves)) return;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("geometry-lines");
    this.geometryLayer.append(svg);
    curves.forEach((curve, curveIndex) => {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
      line.setAttribute("fill", "none"); line.setAttribute("stroke", curveIndex === this.activeCurvatureCurve ? "#f9c74f" : "#90be6d"); line.setAttribute("stroke-width", "2");
      svg.append(line);
      const redraw = () =>
        line.setAttribute(
          "points",
          curve
            .map(
              (p) =>
                `${(p[0]! / source.width) * display.width},${(p[1]! / source.height) * display.height}`,
            )
            .join(" "),
        );
      redraw();
      if (curveIndex !== this.activeCurvatureCurve) return;
      curve.forEach((point, index) => {
        const handle = document.createElement("div");
        handle.className = "geometry-point";
        handle.setAttribute("aria-label", `Baseline ${curveIndex + 1} control point ${index + 1}`);
        const position = () => {
          handle.style.left = `${(point[0]! / source.width) * display.width}px`;
          handle.style.top = `${(point[1]! / source.height) * display.height}px`;
        };
        position();
        this.geometryLayer.append(handle);
        this.interactions.push(
          interact(handle).draggable({
            modifiers: [interact.modifiers.restrictRect({ restriction: "parent" })],
            listeners: {
              move: (event) => {
                point[0] = clamp(
                  point[0]! + (event.dx / display.width) * source.width,
                  0,
                  source.width - 1,
                );
                point[1] = clamp(
                  point[1]! + (event.dy / display.height) * source.height,
                  0,
                  source.height - 1,
                );
                position();
                redraw();
              },
              end: () => {
                this.renderControls();
                this.renderGeometry();
              },
            },
          }),
        );
      });
    });
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
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
        },
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
      this.setMessage(
        payload.stage.status === "success"
          ? "Saved. Result preview is ready."
          : "Saved. The worker is generating the new preview.",
        "success",
      );
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Stage update failed.", "error");
    } finally {
      this.saveButton.disabled = !this.editable;
    }
  }

  private async applyBatch(): Promise<void> {
    if (!this.editable) return;
    const batchUrl = this.root.querySelector<HTMLElement>(
      `[data-batch-url][data-stage="${this.activeStage}"]`,
    )?.dataset.batchUrl;
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
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
        },
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
    const previous = index >= 0 ? page.stages[index] : undefined;
    if (index >= 0) page.stages[index] = incoming;
    else page.stages.push(incoming);
    this.updateStageDom(incoming);
    this.updateActivePreview(incoming, previous);
  }

  private updateStageDom(state: StageState): void {
    const row = this.root.querySelector<HTMLElement>(
      `[data-stage-row][data-page-id="${state.scan_page_id}"][data-stage="${state.stage}"]`,
    );
    if (!row) return;
    const status = row.querySelector<HTMLElement>("[data-stage-status]");
    if (status) {
      status.textContent = stageStatusLabel(state);
      status.className = `status status-${state.status}`;
      status.title =
        state.status === "invalidated" ? (state.invalidated_reason ?? "") : "";
    }
    const error = row.querySelector<HTMLElement>("[data-stage-error]");
    if (error) {
      const errorText = stageErrorText(state);
      error.textContent = errorText;
      error.hidden = !errorText;
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
      const response = await fetch(workspaceUrl, {
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const payload = (await response.json()) as { pages: WorkspacePage[] };
      for (const page of payload.pages) {
        const previous = this.pages.get(page.id);
        this.pages.set(page.id, page);
        for (const state of page.stages) {
          this.updateStageDom(state);
          this.updateActivePreview(state, previous && stageFor(previous, state.stage));
        }
      }
    } catch {
      this.liveStatus.textContent = "Workspace refresh failed; retrying…";
    }
  }

  private updateSelectionCount(): void {
    this.selectionCount.textContent = `${this.selectedPages.size} page(s) selected`;
    this.batchButton.disabled =
      !this.editable || (this.batchScope.value === "selected" && this.selectedPages.size === 0);
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
