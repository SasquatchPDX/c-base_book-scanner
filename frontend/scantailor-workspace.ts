import interact from "interactjs";

import {
  applyClick,
  neighbour,
  PageStrip,
  sortItems,
  type SortMode,
  type StripItem,
} from "./page-strip";
import {
  ContentBoxOverlay,
  describeContent,
  type SourceBox,
} from "./content-box-view";
import { DewarpOverlay, describeDewarp, XSpline, type Pt } from "./dewarp-view";
import {
  type ApplyScope,
  applyScopePages,
  clampAngle,
  DeskewOverlay,
  deskewResultText,
  describeAngle,
  roundAngle,
  wheelAngle,
} from "./deskew-view";
import { fitView, panBy, type View, wheelFactor, zoomAt } from "./pan-zoom";

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
  output_sha256?: string;
  last_error: string;
  invalidated_reason?: string;
  manual?: boolean;
}

interface WorkspacePage {
  id: string;
  active_version?: { id?: string; original_filename?: string };
  logical_sequence_number: number;
  side: string;
  recognized_page_number?: string | null;
  thumbnail?: string;
  /** Token of the first stage's input (the working image); empty while it is generated. */
  work_image_token?: string;
  stages: StageState[];
}

interface WorkspaceEvent {
  job_id: string;
  state: string;
  updated_at: string;
  stages: StageState[];
}

interface WorkspaceStateDelta {
  cursor: string;
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
  deskew: ["manual_angle"],
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
  dewarping: ["dewarp_mode", "depth_perception", "top_curve", "bottom_curve"],
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

/**
 * Content token of a stage's current artifacts, as the server computes it (#195).
 * Preview URLs carry it as ``?v=``; the server caches such URLs for good.
 */
export function artifactToken(
  state: Pick<StageState, "status" | "revision" | "output_sha256"> | undefined,
): string {
  if (!state || state.status !== "success" || !state.output_sha256) return "";
  return `${state.revision}-${state.output_sha256.slice(0, 16)}`;
}

/** Fill ``__PAGE__``/``__STAGE__`` placeholders of a URL template from the page. */
export function fillUrl(template: string, pageId: string, stage = ""): string {
  return template.replace("__PAGE__", pageId).replace("__STAGE__", stage);
}

/**
 * The stage whose output the page strip shows for ``stage``: that stage when it has
 * a result, else the last completed stage before it (unbroken from the first).
 */
export function stripSourceStage(
  page: Pick<WorkspacePage, "stages">,
  stage: StageName,
): StageState | undefined {
  let latest: StageState | undefined;
  for (const name of STAGE_ORDER) {
    const state = page.stages.find((candidate) => candidate.stage === name);
    if (!state || state.status !== "success" || !artifactToken(state)) break;
    latest = state;
    if (name === stage) break;
  }
  return latest;
}

/** Full-resolution size of a page at ``stage`` for the width/height orders. */
export function stageSize(
  page: Pick<WorkspacePage, "stages">,
  stage: StageName,
): { width: number | null; height: number | null } {
  const state = page.stages.find((candidate) => candidate.stage === stage);
  const parameters = state?.parameters ?? {};
  const width =
    stage === "select_content"
      ? positiveNumber(parameters.content_area_width)
      : positiveNumber(parameters.output_width);
  const height =
    stage === "select_content"
      ? positiveNumber(parameters.content_area_height)
      : positiveNumber(parameters.output_height);
  return { width, height };
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

const KEEP_GRID_KEY = "scantailor.deskew.keepGrid";

/** Per-viewer preference; storage may be unavailable (private windows). */
function readKeepGrid(): boolean {
  try {
    return window.localStorage.getItem(KEEP_GRID_KEY) !== "false";
  } catch {
    return true;
  }
}

function writeKeepGrid(keep: boolean): void {
  try {
    window.localStorage.setItem(KEEP_GRID_KEY, String(keep));
  } catch {
    // The preference is a convenience only.
  }
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
  private draft: JsonObject = {};
  private interactions: InteractableHandle[] = [];
  private eventSource: EventSource | null = null;
  private pollingTimer: number | null = null;
  private pollCursor: string | null = null;
  private readonly viewport: HTMLElement | null;
  private readonly surface: HTMLElement | null;
  private readonly zoomLevel: HTMLElement | null;
  private readonly statusHint: HTMLElement | null;
  private readonly sortSelect: HTMLSelectElement | null;
  private readonly keepInView: HTMLInputElement | null;
  private readonly strip: PageStrip | null;
  private readonly naturalOrder: string[];
  private view: View = { zoom: 1, x: 0, y: 0 };
  private fitPending = true;
  private selectionAnchor: string | null = null;
  /** The preview URL being shown, without the cache-busting retry marker. */
  private previewUrl = "";
  private previewRetry: PreviewRetryState = INITIAL_PREVIEW_RETRY;
  private previewRetryTimer: number | null = null;
  private deskewOverlay: DeskewOverlay | null = null;
  private contentOverlay: ContentBoxOverlay | null = null;
  private dewarpOverlay: DewarpOverlay | null = null;
  private deskewSaveTimer: number | null = null;
  private saving = false;
  private saveQueued = false;

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
    this.naturalOrder = pages.map((page) => page.id);
    this.viewport = root.querySelector<HTMLElement>("[data-viewport]");
    this.surface = root.querySelector<HTMLElement>("[data-surface]");
    this.zoomLevel = root.querySelector<HTMLElement>("[data-zoom-level]");
    this.statusHint = root.querySelector<HTMLElement>("[data-status-hint]");
    this.sortSelect = root.querySelector<HTMLSelectElement>("[data-strip-sort]");
    this.keepInView = root.querySelector<HTMLInputElement>("[data-keep-in-view]");
    const scroller = root.querySelector<HTMLElement>("[data-strip-scroll]");
    this.strip = scroller
      ? new PageStrip(scroller, {
          onClick: (pageId, modifiers) => this.clickPage(pageId, modifiers),
        })
      : null;
  }

  private url(name: string, pageId = "", stage = ""): string {
    return fillUrl(this.root.dataset[name] ?? "", pageId, stage);
  }

  start(): void {
    this.bindWorkspaceControls();
    // The job overview links "Edit" as ?page=<id>&stage=<stage>.
    const query = new URLSearchParams(window.location.search);
    const requestedPage = query.get("page") ?? "";
    const requestedStage = query.get("stage") ?? "";
    const firstPage = this.pages.get(requestedPage) ?? this.pages.values().next().value;
    if (firstPage) {
      this.activate(
        firstPage.id,
        isStageName(requestedStage) ? requestedStage : STAGE_ORDER[0],
      );
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
    this.root.querySelectorAll<HTMLButtonElement>("[data-stage-run-all]").forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.stageRunAll ?? "";
        if (isStageName(value)) void this.runStageForAllPages(value, button);
      });
    });
    this.saveButton.addEventListener("click", () => void this.saveActiveStage());
    this.batchButton.addEventListener("click", () => void this.applyBatch());
    this.bindCanvas();
    this.bindStrip();
    this.batchScope.addEventListener("change", () => this.updateSelectionCount());
    this.image.addEventListener("load", () => {
      if (this.fitPending) {
        this.fitPending = false;
        this.fitToPane();
      } else {
        this.applyView();
      }
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
    window.addEventListener("resize", () => {
      this.fitToPane();
    });
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
    const stageChanged = stage !== this.activeStage;
    if (pageId !== this.activePageId || stageChanged) this.fitPending = true;
    this.activePageId = pageId;
    this.activeStage = stage;
    if (!this.selectedPages.has(pageId)) {
      this.selectedPages.clear();
      this.selectedPages.add(pageId);
      this.selectionAnchor = pageId;
    }
    this.draft = editableParameters(stage, state.parameters ?? {});
    const hint = this.root.querySelector<HTMLElement>(`[data-stage-select="${stage}"]`)?.dataset
      .hint;
    if (this.statusHint) this.statusHint.textContent = hint ?? "";
    if (stageChanged) this.refreshStrip();
    this.strip?.setActive(pageId, new Set(this.selectedPages), this.keepInView?.checked ?? true);
    this.updateSelectionCount();
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
    const preview = this.url("previewUrlTemplate", context.page.id, state.stage);
    const source = preview
      ? this.previewMode === "result"
        ? preview
        : `${preview}?input=true`
      : "";
    this.image.hidden = !source;
    let url = "";
    if (source) {
      const index = STAGE_ORDER.indexOf(state.stage);
      const previousName = index > 0 ? STAGE_ORDER[index - 1] : undefined;
      let token: string;
      if (this.previewMode === "result") {
        token = artifactToken(state);
      } else if (previousName) {
        token = artifactToken(stageFor(context.page, previousName));
      } else {
        token = context.page.work_image_token ?? "";
      }
      // An untokenised URL still needs a per-run cache buster.
      const query = token ? `v=${token}` : `revision=${state.revision}-${state.status}`;
      url = `${source}${source.includes("?") ? "&" : "?"}${query}`;
    }
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
    this.applyDeskewPreview();
    this.renderGeometry();
  }

  private updateActivePreview(state: StageState, previous?: StageState): void {
    if (state.scan_page_id !== this.activePageId || state.stage !== this.activeStage) return;
    const justCompleted =
      state.status === "success" &&
      (previous?.status !== "success" || previous.revision !== state.revision);
    if (state.stage === "deskew") {
      // The input view already shows the rotation live; stay there and show the result.
      this.showPreview(this.previewMode);
      this.syncDeskewControls();
      if (justCompleted) this.setMessage(deskewResultText(state.parameters), "success");
      return;
    }
    if (state.stage === "dewarping") {
      if (this.dewarpOverlay?.isDragging()) return;
      this.showPreview(this.previewMode);
      const result = this.controls.querySelector<HTMLElement>("[data-dewarp-result]");
      if (result && state.status === "success") result.textContent = describeDewarp(state.parameters);
      if (justCompleted) this.setMessage(describeDewarp(state.parameters), "success");
      return;
    }
    if (state.stage === "select_content") {
      // The input view shows the content box; keep it unless a drag is in progress.
      if (this.contentOverlay?.isDragging()) return;
      this.showPreview(this.previewMode);
      this.updateContentText();
      if (justCompleted) {
        const box = this.contentBox();
        this.setMessage(describeContent(state.parameters, box, this.contentDpi()), "success");
      }
      return;
    }
    this.showPreview(justCompleted ? "result" : this.previewMode);
    if (justCompleted) this.setMessage("Completed. Result preview is ready.", "success");
  }

  private markActiveControls(): void {
    this.root.querySelectorAll<HTMLElement>("[data-stage-select]").forEach((item) => {
      item.classList.toggle("active", item.dataset.stageSelect === this.activeStage);
    });
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
    const state = this.activeContext()?.state;
    const manual = "manual_angle" in this.draft;
    const modes = document.createElement("fieldset");
    modes.className = "st-radio-group";
    const legend = document.createElement("legend");
    legend.textContent = "Mode";
    modes.append(legend);
    for (const value of ["auto", "manual"] as const) {
      const label = document.createElement("label");
      label.className = "checkbox";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "deskew-mode";
      radio.value = value;
      radio.checked = (value === "manual") === manual;
      radio.disabled = !this.editable;
      radio.dataset.deskewMode = value;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        if (value === "manual") {
          this.setDeskewAngle(this.deskewAngle(), false);
        } else {
          // Upstream clears the page's parameters and detects again.
          delete this.draft.manual_angle;
          this.applyDeskewPreview();
        }
        void this.commitDraft();
      });
      label.append(radio, document.createTextNode(value === "auto" ? "Auto" : "Manual"));
      modes.append(label);
    }
    this.controls.append(modes);

    const label = document.createElement("label");
    label.textContent = "Angle (degrees, + = clockwise)";
    const input = document.createElement("input");
    input.type = "number";
    input.step = "0.01";
    input.min = "-45";
    input.max = "45";
    input.dataset.deskewAngle = "";
    input.disabled = !this.editable;
    input.value = this.deskewAngle().toFixed(2);
    input.title =
      "Mouse wheel: 0.1° per notch (Shift: 0.05°). Ctrl+wheel over the page also rotates.";
    input.addEventListener("input", () => {
      const value = Number(input.value);
      if (input.value === "" || !Number.isFinite(value)) return;
      this.deskewOverlay?.flashGrid();
      this.setDeskewAngle(clampAngle(value), true, false);
    });
    input.addEventListener(
      "wheel",
      (event) => {
        if (input.disabled) return;
        event.preventDefault();
        // A number field goes up when the wheel goes up.
        const angle = wheelAngle(this.deskewAngle(), {
          deltaY: -(event.deltaY || event.deltaX),
          ctrlKey: true,
          shiftKey: event.shiftKey,
        });
        if (angle === null) return;
        this.deskewOverlay?.flashGrid();
        this.setDeskewAngle(angle, true);
      },
      { passive: false },
    );
    label.append(input);
    this.controls.append(label);

    const grid = document.createElement("label");
    grid.className = "checkbox";
    const keep = document.createElement("input");
    keep.type = "checkbox";
    keep.dataset.deskewGrid = "";
    keep.checked = readKeepGrid();
    keep.addEventListener("change", () => {
      writeKeepGrid(keep.checked);
      this.deskewOverlay?.setKeepGrid(keep.checked);
    });
    grid.append(keep, document.createTextNode("Keep the grid visible"));
    this.controls.append(grid);

    const result = document.createElement("p");
    result.className = "st-stage-result";
    result.dataset.deskewResult = "";
    result.textContent = state?.status === "success" ? deskewResultText(state.parameters) : "";
    this.controls.append(result);
  }

  /** The angle shown: the manual draft, else what auto mode applied last. */
  private deskewAngle(): number {
    if ("manual_angle" in this.draft) return roundAngle(finiteNumber(this.draft.manual_angle, 0));
    const state = this.activeContext()?.state;
    return state ? roundAngle(finiteNumber(state.parameters.angle, 0)) : 0;
  }

  private deskewEditing(): boolean {
    return (
      this.editable &&
      this.activeStage === "deskew" &&
      this.previewMode === "input" &&
      !this.image.hidden
    );
  }

  /**
   * Change the angle: the page turns at once in the browser. ``commit`` saves it after
   * a short pause (upstream commits when a drag ends or the spin box changes).
   */
  private setDeskewAngle(angle: number, commit: boolean, updateField = true): void {
    const rounded = roundAngle(angle);
    const wasManual = "manual_angle" in this.draft;
    this.draft.manual_angle = rounded;
    if (!wasManual) {
      const radio = this.controls.querySelector<HTMLInputElement>('[data-deskew-mode="manual"]');
      if (radio) radio.checked = true;
    }
    if (updateField) {
      const field = this.controls.querySelector<HTMLInputElement>("[data-deskew-angle]");
      if (field) field.value = rounded.toFixed(2);
    }
    const result = this.controls.querySelector<HTMLElement>("[data-deskew-result]");
    if (result) result.textContent = `${describeAngle(rounded)} (manual, not saved yet).`;
    this.applyDeskewPreview();
    if (commit) this.scheduleDeskewCommit();
  }

  private scheduleDeskewCommit(): void {
    if (this.deskewSaveTimer !== null) window.clearTimeout(this.deskewSaveTimer);
    this.deskewSaveTimer = window.setTimeout(() => {
      this.deskewSaveTimer = null;
      void this.commitDraft();
    }, 600);
  }

  /** Save the stage draft; a change made while a save is running is saved after it. */
  private async commitDraft(): Promise<void> {
    if (!this.editable) return;
    if (this.deskewSaveTimer !== null) {
      window.clearTimeout(this.deskewSaveTimer);
      this.deskewSaveTimer = null;
    }
    if (this.saving) {
      this.saveQueued = true;
      return;
    }
    await this.saveActiveStage();
    if (this.saveQueued) {
      this.saveQueued = false;
      await this.commitDraft();
    }
  }

  /** Field and result text after a run (unless the member is changing the angle). */
  private syncDeskewControls(): void {
    if (this.activeStage !== "deskew") return;
    const state = this.activeContext()?.state;
    if (!state) return;
    const field = this.controls.querySelector<HTMLInputElement>("[data-deskew-angle]");
    const editing =
      this.deskewSaveTimer !== null ||
      this.saving ||
      this.deskewOverlay?.isDragging() === true ||
      (field !== null && document.activeElement === field);
    if (editing) return;
    if (!("manual_angle" in this.draft) && field) field.value = this.deskewAngle().toFixed(2);
    const result = this.controls.querySelector<HTMLElement>("[data-deskew-result]");
    if (result && state.status === "success") result.textContent = deskewResultText(state.parameters);
    this.applyDeskewPreview();
  }

  /** Turn the input preview by the current angle and place the grid and handles. */
  private applyDeskewPreview(): void {
    const active =
      this.activeStage === "deskew" && this.previewMode === "input" && !this.image.hidden;
    if (!active || !this.viewport || !this.image.naturalWidth) {
      this.image.style.transform = "";
      if (this.deskewOverlay) this.deskewOverlay.element.style.display = "none";
      return;
    }
    const angle = this.deskewAngle();
    this.image.style.transformOrigin = "50% 50%";
    this.image.style.transform = angle ? `rotate(${angle}deg)` : "";
    if (!this.deskewOverlay) {
      this.deskewOverlay = new DeskewOverlay(this.viewport, {
        onPreview: (value) => this.setDeskewAngle(value, false),
        onCommit: (value) => {
          this.setDeskewAngle(value, false);
          void this.commitDraft();
        },
      });
      this.deskewOverlay.setKeepGrid(readKeepGrid());
    }
    const overlay = this.deskewOverlay;
    overlay.element.style.display = "";
    overlay.element.classList.toggle("read-only", !this.editable);
    const width = this.image.naturalWidth * this.view.zoom;
    const height = this.image.naturalHeight * this.view.zoom;
    const centre = { x: this.view.x + width / 2, y: this.view.y + height / 2 };
    // Handles sit at the page edges, but stay inside the pane.
    const radius = Math.max(
      24,
      Math.min(
        width / 2,
        this.viewport.clientWidth / 2 - 16,
        this.viewport.clientHeight / 2 - 16,
      ),
    );
    overlay.layout(centre, radius, angle);
  }

  private renderContentControls(): void {
    const state = this.activeContext()?.state;
    const mode = String(this.draft.content_mode ?? state?.parameters.content_mode ?? "auto");
    const modes = document.createElement("fieldset");
    modes.className = "st-radio-group";
    const legend = document.createElement("legend");
    legend.textContent = "Mode";
    modes.append(legend);
    for (const [value, text] of [
      ["auto", "Auto"],
      ["manual", "Manual"],
      ["disabled", "Disabled"],
    ] as const) {
      const label = document.createElement("label");
      label.className = "checkbox";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "content-mode";
      radio.value = value;
      radio.checked = mode === value;
      radio.disabled = !this.editable;
      radio.dataset.contentMode = value;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        this.draft.content_mode = value;
        if (value === "manual") {
          // Start from the box on screen (the auto result), as upstream does.
          const box = this.contentBox() ?? this.defaultContentBox();
          this.draft.content_box = [box.x1, box.y1, box.x2, box.y2];
        } else {
          delete this.draft.content_box;
        }
        this.renderGeometry();
        void this.commitDraft();
      });
      label.append(radio, document.createTextNode(text));
      modes.append(label);
    }
    this.controls.append(modes);
    const result = document.createElement("p");
    result.className = "st-stage-result";
    result.dataset.contentResult = "";
    this.controls.append(result);
    this.updateContentText();
  }

  /** The box to draw: the manual draft, else what auto mode found last. */
  private contentBox(): SourceBox | null {
    const state = this.activeContext()?.state;
    const mode = String(this.draft.content_mode ?? state?.parameters.content_mode ?? "auto");
    if (mode === "disabled") return null;
    const fallback = { x1: 0, y1: 0, x2: 0, y2: 0 };
    if (mode === "manual" && this.draft.content_box !== undefined) {
      const box = normalizeBox(this.draft.content_box, fallback);
      return box.x2 > box.x1 && box.y2 > box.y1 ? box : null;
    }
    if (!state || state.status !== "success" || state.parameters.content_found === false)
      return null;
    const box = normalizeBox(state.parameters.content_box, fallback);
    return box.x2 > box.x1 && box.y2 > box.y1 ? box : null;
  }

  /** Upstream `createContentBox`: 70 % of the page, centred. */
  private defaultContentBox(): SourceBox {
    const source = this.sourceSize();
    return {
      x1: Math.round(source.width * 0.15),
      y1: Math.round(source.height * 0.15),
      x2: Math.round(source.width * 0.85),
      y2: Math.round(source.height * 0.85),
    };
  }

  private contentDpi(): { x: number; y: number } | null {
    const dpi = this.activeContext()?.state.parameters.dpi;
    if (Array.isArray(dpi) && dpi.length >= 2) {
      const x = positiveNumber(dpi[0]);
      const y = positiveNumber(dpi[1]);
      if (x && y) return { x, y };
    }
    return null;
  }

  private updateContentText(box: SourceBox | null = this.contentBox()): void {
    const result = this.controls.querySelector<HTMLElement>("[data-content-result]");
    if (!result) return;
    const state = this.activeContext()?.state;
    const parameters = { ...(state?.parameters ?? {}), ...this.draft };
    const known = state?.status === "success" || this.draft.content_mode === "manual";
    result.textContent = known ? describeContent(parameters, box, this.contentDpi()) : "";
  }

  /**
   * Draw the content box. The overlay persists across re-renders (live updates,
   * zoom, image reloads), like the Deskew overlay, so a drag in progress survives them.
   */
  private renderContentBox(source: Size, display: Size): void {
    const box = this.contentBox();
    const existing = this.contentOverlay;
    if (existing && (box || existing.isDragging())) {
      if (box) existing.setBox(box); // ignored while dragging
      if (existing.element.parentElement !== this.geometryLayer)
        this.geometryLayer.append(existing.element);
      existing.layout(source, display);
      return;
    }
    this.removeContentOverlay();
    if (!box) return;
    this.contentOverlay = new ContentBoxOverlay(this.geometryLayer, box, this.editable, {
      onPreview: (next) => this.updateContentText(next),
      onCommit: (next) => {
        // Upstream manualContentRectSet: a drag makes the box manual and commits it.
        this.draft.content_mode = "manual";
        this.draft.content_box = [next.x1, next.y1, next.x2, next.y2];
        const radio = this.controls.querySelector<HTMLInputElement>(
          '[data-content-mode="manual"]',
        );
        if (radio) radio.checked = true;
        this.updateContentText(next);
        void this.commitDraft();
      },
    });
    this.contentOverlay.layout(source, display);
  }

  private renderMarginControls(): void {
    this.addNumber("All margins (pixels)", "margin", 0, 5000, 1, 0);
    this.addSelect("Horizontal alignment", "align_x", ["left", "center", "right"], "center");
    this.addSelect("Vertical alignment", "align_y", ["top", "center", "bottom"], "center");
  }

  private renderDewarpControls(): void {
    const state = this.activeContext()?.state;
    const mode = String(this.draft.dewarp_mode ?? state?.parameters.dewarp_mode ?? "auto");
    const modes = document.createElement("fieldset");
    modes.className = "st-radio-group";
    const legend = document.createElement("legend");
    legend.textContent = "Mode";
    modes.append(legend);
    for (const [value, text] of [
      ["off", "Off"],
      ["auto", "Auto"],
      ["manual", "Manual"],
    ] as const) {
      const label = document.createElement("label");
      label.className = "checkbox";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "dewarp-mode";
      radio.value = value;
      radio.checked = mode === value || (value === "off" && mode === "bypass");
      radio.disabled = !this.editable;
      radio.dataset.dewarpMode = value;
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        this.draft.dewarp_mode = value;
        if (value === "manual") {
          // Start from the curves on screen (the auto result), as upstream does.
          const curves = this.dewarpCurves();
          this.draft.top_curve = curves.top.points.map(([x, y]) => [x, y]);
          this.draft.bottom_curve = curves.bottom.points.map(([x, y]) => [x, y]);
        } else {
          delete this.draft.top_curve;
          delete this.draft.bottom_curve;
        }
        this.renderGeometry();
        void this.commitDraft();
      });
      label.append(radio, document.createTextNode(text));
      modes.append(label);
    }
    this.controls.append(modes);

    // Upstream's depth perception slider (1 to 3, default 2).
    const label = document.createElement("label");
    label.textContent = "Depth perception";
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = "3";
    slider.step = "0.1";
    slider.dataset.depthPerception = "";
    slider.disabled = !this.editable;
    slider.value = String(finiteNumber(this.draft.depth_perception ?? state?.parameters.depth_perception, 2));
    slider.addEventListener("input", () => {
      this.draft.depth_perception = Number(slider.value);
      if (this.dewarpOverlay) {
        this.dewarpOverlay.depth = Number(slider.value);
        this.dewarpOverlay.draw();
      }
    });
    slider.addEventListener("change", () => void this.commitDraft());
    label.append(slider);
    this.controls.append(label);

    const result = document.createElement("p");
    result.className = "st-stage-result";
    result.dataset.dewarpResult = "";
    result.textContent = state?.status === "success" ? describeDewarp(state.parameters) : "";
    this.controls.append(result);
  }

  /** The curves to show: the manual draft, else the last run's, else a flat model. */
  private dewarpCurves(): { top: XSpline; bottom: XSpline } {
    const state = this.activeContext()?.state;
    const parameters = state?.parameters ?? {};
    const points = (value: unknown): Pt[] | null => {
      if (!Array.isArray(value) || value.length < 2) return null;
      const out: Pt[] = [];
      for (const item of value) {
        if (!Array.isArray(item) || item.length < 2) return null;
        const x = Number(item[0]);
        const y = Number(item[1]);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
        out.push([x, y]);
      }
      return out;
    };
    const manual = String(this.draft.dewarp_mode ?? "") === "manual";
    const top = points(manual ? this.draft.top_curve : parameters.top_curve) ?? points(parameters.top_curve);
    const bottom =
      points(manual ? this.draft.bottom_curve : parameters.bottom_curve) ?? points(parameters.bottom_curve);
    if (top && bottom) return { top: new XSpline(top), bottom: new XSpline(bottom) };
    // Upstream initNewSpline on the content box: five points along each edge.
    const source = this.sourceSize();
    const line = (y: number): Pt[] =>
      [0, 0.25, 0.5, 0.75, 1].map((f) => [f * source.width, y] as Pt);
    return { top: new XSpline(line(0)), bottom: new XSpline(line(source.height)) };
  }

  private renderDewarpOverlay(source: Size, display: Size): void {
    const state = this.activeContext()?.state;
    const mode = String(this.draft.dewarp_mode ?? state?.parameters.dewarp_mode ?? "auto");
    if (mode === "off" || mode === "bypass") {
      this.removeDewarpOverlay();
      return;
    }
    const depth = finiteNumber(this.draft.depth_perception ?? state?.parameters.depth_perception, 2);
    const curves = this.dewarpCurves();
    const existing = this.dewarpOverlay;
    if (existing) {
      existing.setCurves(curves.top, curves.bottom, depth); // ignored while dragging
      if (existing.element.parentElement !== this.geometryLayer)
        this.geometryLayer.append(existing.element);
      existing.layout(source, display);
      return;
    }
    const overlay = new DewarpOverlay(this.geometryLayer, curves.top, curves.bottom, this.editable, {
      onPreview: () => undefined,
      onCommit: (top, bottom) => {
        // Upstream dragFinished: an edited auto model becomes manual.
        this.draft.dewarp_mode = "manual";
        this.draft.top_curve = top.points.map(([x, y]) => [x, y]);
        this.draft.bottom_curve = bottom.points.map(([x, y]) => [x, y]);
        const radio = this.controls.querySelector<HTMLInputElement>('[data-dewarp-mode="manual"]');
        if (radio) radio.checked = true;
        void this.commitDraft();
      },
    });
    overlay.depth = depth;
    this.dewarpOverlay = overlay;
    overlay.layout(source, display);
  }

  private removeDewarpOverlay(): void {
    this.dewarpOverlay?.remove();
    this.dewarpOverlay = null;
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
        this.renderContentBox(source, display);
        break;
      case "dewarping":
        this.renderDewarpOverlay(source, display);
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
    // Keep the persistent overlays (and any drag on them); renderGeometry updates them.
    const input = this.previewMode === "input";
    const keepContent = input && this.activeStage === "select_content" ? this.contentOverlay : null;
    const keepDewarp = input && this.activeStage === "dewarping" ? this.dewarpOverlay : null;
    if (!keepContent) this.removeContentOverlay();
    if (!keepDewarp) this.removeDewarpOverlay();
    for (const child of [...this.geometryLayer.children]) {
      if (child !== keepContent?.element && child !== keepDewarp?.element) child.remove();
    }
  }

  private removeContentOverlay(): void {
    this.contentOverlay?.remove();
    this.contentOverlay = null;
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

  private async saveActiveStage(): Promise<void> {
    const context = this.activeContext();
    if (!context || !this.activePageId || !this.editable) return;
    const url = this.url("stageUrlTemplate", this.activePageId, this.activeStage);
    if (!url) return;
    this.saving = true;
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
      const done = payload.stage.status === "success";
      this.setMessage(
        done && payload.stage.stage === "deskew"
          ? `Saved. ${deskewResultText(payload.stage.parameters)}`
          : done && payload.stage.stage === "dewarping"
            ? `Saved. ${describeDewarp(payload.stage.parameters)}`
          : done && payload.stage.stage === "select_content"
            ? `Saved. ${describeContent(payload.stage.parameters, this.contentBox(), this.contentDpi())}`
            : done
            ? "Saved. Result preview is ready."
            : "Saved. The worker is generating the new preview.",
        "success",
      );
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Stage update failed.", "error");
    } finally {
      this.saving = false;
      this.saveButton.disabled = !this.editable;
      if (!this.saveQueued) this.syncDeskewControls();
    }
  }

  private async applyBatch(): Promise<void> {
    if (!this.editable) return;
    const batchUrl = this.url("batchUrlTemplate", "", this.activeStage);
    if (!batchUrl) return;
    const scope = (this.batchScope.value || "selected") as ApplyScope;
    const order = this.strip?.displayOrder() ?? this.naturalOrder;
    const pageIds = applyScopePages(scope, order, this.activePageId, this.selectedPages);
    if (pageIds !== null && pageIds.length === 0) {
      this.setMessage("Select at least one page before applying a batch.", "error");
      return;
    }
    this.batchButton.disabled = true;
    this.setMessage("Applying stage settings to the batch…", "info");
    const body: JsonObject = {
      parameters: this.draft,
      preserve_exceptions: this.preserveExceptions.checked,
    };
    if (pageIds !== null) body.page_ids = pageIds;
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
    const page = this.pages.get(state.scan_page_id);
    if (page && this.strip) this.strip.update(this.stripItem(page));
  }

  // --- page strip ------------------------------------------------------------------

  private stripItem(page: WorkspacePage): StripItem {
    const source = stripSourceStage(page, this.activeStage);
    const versionId = page.active_version?.id;
    let thumbnailUrl = "";
    if (source) {
      const template = this.url("stageThumbnailTemplate", page.id, source.stage);
      if (template) thumbnailUrl = `${template}?v=${artifactToken(source)}`;
    } else if (versionId && page.work_image_token) {
      const template = this.url("pageThumbnailTemplate", page.id);
      if (template) thumbnailUrl = `${template}?v=${page.work_image_token}`;
    } else {
      // Working image still being generated: the full page thumbnail renders the scan.
      thumbnailUrl = page.thumbnail ?? "";
    }
    const side = page.side && page.side !== "single" ? ` ${page.side}` : "";
    const printed = page.recognized_page_number ? `p. ${page.recognized_page_number}` : "";
    const size = stageSize(page, this.activeStage);
    return {
      id: page.id,
      label: `${page.logical_sequence_number}${side}`,
      caption: page.active_version?.original_filename || printed,
      thumbnailUrl,
      width: size.width,
      height: size.height,
      stages: STAGE_ORDER.map((name) => {
        const state = stageFor(page, name);
        return {
          stage: name,
          label: STAGE_LABELS[name],
          status: state?.status ?? "unknown",
          manual: state?.manual ?? false,
          last_error: state?.last_error ?? "",
          invalidated_reason: state?.invalidated_reason ?? "",
        };
      }),
    };
  }

  private refreshStrip(): void {
    if (!this.strip) return;
    const items = this.naturalOrder
      .map((id) => this.pages.get(id))
      .filter((page): page is WorkspacePage => page !== undefined)
      .map((page) => this.stripItem(page));
    const mode = (this.sortSelect?.value ?? "natural") as SortMode;
    this.strip.setItems(items, sortItems(items, mode));
  }

  private bindStrip(): void {
    this.sortSelect?.addEventListener("change", () => {
      this.refreshStrip();
      this.strip?.setActive(this.activePageId, new Set(this.selectedPages), true);
    });
    this.refreshStrip();
    document.addEventListener("keydown", (event) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, select, textarea, [contenteditable='true']")) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const steps: Record<string, number> = {
        ArrowDown: 1,
        PageDown: 1,
        w: 1,
        W: 1,
        ArrowUp: -1,
        PageUp: -1,
        q: -1,
        Q: -1,
        Home: -Infinity,
        End: Infinity,
      };
      const step = steps[event.key];
      if (step === undefined || !this.strip) return;
      const order = this.strip.displayOrder();
      const next = Number.isFinite(step)
        ? neighbour(order, this.activePageId, step)
        : step < 0
          ? (order[0] ?? null)
          : (order[order.length - 1] ?? null);
      if (!next) return;
      event.preventDefault();
      this.selectedPages.clear();
      this.activate(next, this.activeStage);
    });
  }

  private clickPage(pageId: string, modifiers: { ctrl: boolean; shift: boolean }): void {
    const order = this.strip?.displayOrder() ?? this.naturalOrder;
    const next = applyClick(
      { selected: this.selectedPages, anchor: this.selectionAnchor ?? this.activePageId },
      order,
      pageId,
      modifiers,
    );
    this.selectedPages.clear();
    for (const id of next.selected) this.selectedPages.add(id);
    this.selectionAnchor = next.anchor;
    if (pageId !== this.activePageId) this.activate(pageId, this.activeStage);
    else this.strip?.setActive(pageId, new Set(this.selectedPages), false);
    this.updateSelectionCount();
  }

  // --- canvas: pan and zoom ----------------------------------------------------------

  private bindCanvas(): void {
    const viewport = this.viewport;
    if (!viewport) return;
    this.image.draggable = false;
    viewport.addEventListener(
      "wheel",
      (event) => {
        if (event.ctrlKey) {
          // Upstream deskew: Ctrl+wheel rotates, Ctrl+Shift+wheel rotates finely.
          if (this.deskewEditing()) {
            event.preventDefault();
            const angle = wheelAngle(this.deskewAngle(), event);
            if (angle !== null) {
              this.deskewOverlay?.flashGrid();
              this.setDeskewAngle(angle, true);
            }
          }
          return;
        }
        event.preventDefault();
        const bounds = viewport.getBoundingClientRect();
        this.view = zoomAt(this.view, wheelFactor(event.deltaY, event.deltaMode), {
          x: event.clientX - bounds.left,
          y: event.clientY - bounds.top,
        });
        this.applyView();
      },
      { passive: false },
    );
    let drag: { id: number; x: number; y: number } | null = null;
    viewport.addEventListener("pointerdown", (event) => {
      const target = event.target as HTMLElement | null;
      // Handles and boxes own their pointer events; only the background pans.
      if (
        event.button !== 0 ||
        target?.closest("[data-geometry-layer] > *, [data-captures-pointer]")
      )
        return;
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("panning");
    });
    viewport.addEventListener("pointermove", (event) => {
      if (!drag || drag.id !== event.pointerId) return;
      this.view = panBy(this.view, event.clientX - drag.x, event.clientY - drag.y);
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
      this.applyView(false);
    });
    const end = (event: PointerEvent): void => {
      if (!drag || drag.id !== event.pointerId) return;
      drag = null;
      viewport.classList.remove("panning");
    };
    viewport.addEventListener("pointerup", end);
    viewport.addEventListener("pointercancel", end);
    const zoomButton = (selector: string, action: () => void): void => {
      this.root.querySelector<HTMLButtonElement>(selector)?.addEventListener("click", action);
    };
    const centre = (): { x: number; y: number } => ({
      x: viewport.clientWidth / 2,
      y: viewport.clientHeight / 2,
    });
    zoomButton("[data-zoom-in]", () => {
      this.view = zoomAt(this.view, 1.25, centre());
      this.applyView();
    });
    zoomButton("[data-zoom-out]", () => {
      this.view = zoomAt(this.view, 0.8, centre());
      this.applyView();
    });
    zoomButton("[data-zoom-fit]", () => this.fitToPane());
    zoomButton("[data-zoom-actual]", () => {
      this.view = zoomAt(this.view, 1 / this.view.zoom, centre());
      this.applyView();
    });
  }

  private fitToPane(): void {
    if (!this.viewport || !this.image.naturalWidth) {
      this.renderGeometry();
      return;
    }
    this.view = fitView(
      { width: this.viewport.clientWidth, height: this.viewport.clientHeight },
      { width: this.image.naturalWidth, height: this.image.naturalHeight },
    );
    this.applyView();
  }

  /** Lay the image out at ``natural * zoom`` and translate it; redraw overlays if resized. */
  private applyView(resized = true): void {
    if (!this.surface || !this.image.naturalWidth) {
      if (resized) this.renderGeometry();
      return;
    }
    this.image.style.width = `${Math.round(this.image.naturalWidth * this.view.zoom)}px`;
    this.image.style.height = `${Math.round(this.image.naturalHeight * this.view.zoom)}px`;
    this.surface.style.transform = `translate(${Math.round(this.view.x)}px, ${Math.round(this.view.y)}px)`;
    if (this.zoomLevel) this.zoomLevel.textContent = `${Math.round(this.view.zoom * 100)} %`;
    this.applyDeskewPreview();
    if (resized) this.renderGeometry();
  }

  // --- batch: "▶" runs a stage for every page with its own settings ---------------

  private async runStageForAllPages(stage: StageName, button: HTMLButtonElement): Promise<void> {
    if (!this.editable) return;
    const batchUrl = this.url("batchUrlTemplate", "", stage);
    if (!batchUrl) return;
    button.disabled = true;
    this.setMessage(`Running ${STAGE_LABELS[stage]} for all pages…`, "info");
    try {
      // No parameters: every page keeps its own settings (upstream batch processing).
      const response = await fetch(batchUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify({ preserve_exceptions: false }),
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as {
        applied: { page_id: string; stage: StageState }[];
        applied_count: number;
      };
      for (const item of payload.applied) this.mergeStage(item.stage);
      this.setMessage(`Queued ${STAGE_LABELS[stage]} for ${payload.applied_count} page(s).`, "success");
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Batch run failed.", "error");
    } finally {
      button.disabled = !this.editable;
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
    const stateUrl = this.root.dataset.workspaceStateUrl;
    if (stateUrl) {
      this.liveStatus.textContent = "Live updates unavailable; using compatibility polling";
      this.pollingTimer = window.setInterval(() => void this.pollWorkspace(stateUrl), 5000);
    }
  }

  /** Compatibility polling: fetch only the stage rows changed since the last cursor. */
  private async pollWorkspace(stateUrl: string): Promise<void> {
    try {
      const url = this.pollCursor
        ? `${stateUrl}?since=${encodeURIComponent(this.pollCursor)}`
        : stateUrl;
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      const payload = (await response.json()) as WorkspaceStateDelta;
      for (const state of payload.stages) this.mergeStage(state);
      this.pollCursor = payload.cursor;
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

const workspace = document.querySelector<HTMLElement>("[data-scantailor-editor]");
if (workspace) initializeScanTailorWorkspace(workspace);
