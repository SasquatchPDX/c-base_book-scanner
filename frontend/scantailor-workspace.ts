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
import {
  type AlignX,
  type AlignY,
  describeMargins,
  linkedSide,
  MARGIN_SIDES,
  type MarginSide,
  marginSettings,
  MAX_MARGIN_MM,
} from "./margins-view";
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
  /** Input fingerprint of the result (#205): what it was computed from. */
  applied_input_sha256?: string;
  applied_revision?: number | null;
  /** Whether the result was computed with the stage's current settings. */
  settings_applied?: boolean;
  /**
   * The input fingerprint the server computes now: the page version's checksum for
   * the first stage; for an aligned page's Margins result its predecessor's output
   * combined with the job's common page size (#208), which only the server knows.
   */
  input_sha256?: string;
  /** Margins (#208): the predecessor output ``input_sha256`` was built from. */
  input_basis_sha256?: string;
  /** Margins (#208): the result differs from its input only by the common page size. */
  common_size_changed?: boolean;
  manual?: boolean;
  /** Set by the server on every write of the row: orders updates of one revision (#209). */
  updated_at?: string;
}

/** A stage draft to save for the page and stage it was edited on (#209). */
interface DraftSnapshot {
  pageId: string;
  stage: StageName;
  draft: JsonObject;
  /** ``stableJson(draft)``: what becomes the applied draft once it is saved. */
  sent: string;
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
    "left_mm",
    "top_mm",
    "right_mm",
    "bottom_mm",
    "align_x",
    "align_y",
    "align_with_others",
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
    // Millimetres and alignment only (#208): pixel settings and explicit canvases
    // written before are converted (or dropped) instead of being sent back.
    const settings = marginSettings(parameters);
    return {
      ...(typeof parameters.margins_mode === "string"
        ? { margins_mode: parameters.margins_mode }
        : {}),
      ...settings,
    };
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
    return `Waiting for ${STAGE_LABELS[previous]}: its result is not ready yet.`;
  }
  if (status === 404 && body === "preview not ready") {
    return "The preview for this stage has not been generated yet. It will appear once the worker finishes.";
  }
  if (status === 404 && body === "preview artifact missing") {
    return "The preview file is missing from storage. Change a setting of this stage to run it again.";
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
 * a current result, else the last current stage before it (unbroken from the first).
 */
export function stripSourceStage(
  page: Pick<WorkspacePage, "stages">,
  stage: StageName,
): StageState | undefined {
  let latest: StageState | undefined;
  for (const name of STAGE_ORDER) {
    const state = page.stages.find((candidate) => candidate.stage === name);
    if (!state || !stageReadiness(page, name).current || !artifactToken(state)) break;
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

/** Derived state of a stage (#205), computed from its page's rows like ``scantailor.py``. */
export interface StageReadiness {
  /** A successful result that matches the stage's settings and current input. */
  current: boolean;
  /** The first earlier stage without a current result; the stage cannot run before it. */
  waitingFor: StageName | null;
  /** Why a successful result is not current ("input changed", "settings changed, not applied"). */
  staleReason: string;
}

type ReadinessFields = Pick<
  StageState,
  | "stage"
  | "status"
  | "output_sha256"
  | "applied_input_sha256"
  | "settings_applied"
  | "input_sha256"
  | "input_basis_sha256"
  | "common_size_changed"
>;

function ownResultCurrent(state: ReadinessFields, previous: ReadinessFields | undefined): boolean {
  if (state.status !== "success" || state.settings_applied !== true) return false;
  // The input fingerprint: the page version's checksum for the first stage, else the
  // predecessor's last output.
  let input =
    state.stage === STAGE_ORDER[0]
      ? (state.input_sha256 ?? "")
      : previous?.status === "success"
        ? (previous.output_sha256 ?? "")
        : "";
  if (state.input_basis_sha256) {
    // An aligned page's Margins (#208): the server's fingerprint includes the common
    // page size. It holds only while the predecessor output it was built from is
    // still the one on this page; a newer predecessor output means "input changed".
    input =
      previous?.status === "success" && previous.output_sha256 === state.input_basis_sha256
        ? (state.input_sha256 ?? "")
        : "";
  }
  return input === (state.applied_input_sha256 ?? "");
}

/**
 * Whether ``stage`` of ``page`` is current, which earlier stage it waits for, and why
 * its result is stale. Rows arrive one by one over live updates, so this is derived
 * from all of the page's rows every time instead of being stored per row.
 */
export function stageReadiness(
  page: { stages: readonly ReadinessFields[] },
  stage: StageName,
): StageReadiness {
  let upstreamCurrent = true;
  let waitingFor: StageName | null = null;
  let previous: ReadinessFields | undefined;
  for (const name of STAGE_ORDER) {
    const state = page.stages.find((candidate) => candidate.stage === name);
    const current: boolean =
      upstreamCurrent && state !== undefined && ownResultCurrent(state, previous);
    if (name === stage) {
      let staleReason = "";
      if (state?.status === "success" && !current) {
        staleReason =
          state.settings_applied !== true
            ? "settings changed, not applied"
            : upstreamCurrent &&
                state.common_size_changed === true &&
                previous?.output_sha256 === state.input_basis_sha256
              ? "common page size changed"
              : "input changed";
      }
      return { current, waitingFor, staleReason };
    }
    if (!current && waitingFor === null) waitingFor = name;
    upstreamCurrent = current;
    previous = state;
  }
  return { current: false, waitingFor, staleReason: "" };
}

/**
 * Editor status line for a stage, derived from its state (#205): failures are errors;
 * unsaved edits, waiting for an earlier stage and stale results are information.
 */
export function stageStatusMessage(
  state: Pick<StageState, "status" | "last_error" | "revision">,
  readiness?: StageReadiness,
  settingsNotApplied = false,
  restarting = false,
): { text: string; tone: "info" | "error" } {
  const error = stageErrorText(state);
  if (error) return { text: `Failed: ${error}`, tone: "error" };
  if (settingsNotApplied) return { text: SETTINGS_NOT_APPLIED, tone: "info" };
  const base = `Revision ${state.revision}`;
  if (state.status === "queued" || state.status === "running") {
    // The stage runs by itself (#209); a run of older settings restarts with the new ones.
    return { text: restarting ? RESTARTING_MESSAGE : RUNNING_MESSAGE, tone: "info" };
  }
  if (readiness?.waitingFor) {
    return { text: `Waiting for ${STAGE_LABELS[readiness.waitingFor]}.`, tone: "info" };
  }
  if (state.status === "success") {
    if (readiness?.staleReason) {
      return { text: `${base} · stale: ${readiness.staleReason}`, tone: "info" };
    }
    return { text: `${base} · success`, tone: "info" };
  }
  return { text: `${base} · not run yet`, tone: "info" };
}

/** Edits the editor holds that are neither saved nor being saved, e.g. after a failed save. */
export const SETTINGS_NOT_APPLIED = "Settings not applied yet.";
export const RUNNING_MESSAGE = "Running…";
export const RESTARTING_MESSAGE = "Restarting with new settings…";

/**
 * Status line and result pane while an edit waits for its autosave or is being saved
 * (#209). This replaces "Settings not applied" for the short debounce, so an edit does
 * not flash a warning; ``restarting`` when a run of the older settings is active.
 */
export function savingMessage(restarting: boolean): string {
  return restarting ? RESTARTING_MESSAGE : "Saving…";
}

/** Pause after the last change before the draft is saved and the stage runs (#209). */
export const AUTOSAVE_DELAY_MS = 600;

/** JSON with sorted object keys, to compare drafts regardless of key order. */
export function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    isObject(item)
      ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
      : item,
  );
}

/** ``updated_at`` (ISO 8601 from the server) in microseconds, or null if unreadable. */
function timestampMicros(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = /^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(.*)$/.exec(value);
  if (!match) return null;
  const seconds = Date.parse(`${match[1]}${match[3] ?? ""}`);
  if (Number.isNaN(seconds)) return null;
  return seconds * 1000 + Number((match[2] ?? "").padEnd(6, "0"));
}

/** How far a run got, for two updates of the same revision with the same timestamp. */
const STATUS_PROGRESS: Record<string, number> = {
  unknown: 0,
  queued: 1,
  running: 2,
  success: 3,
  failed: 3,
};

/**
 * Whether ``incoming`` is an older view of a stage row than ``existing`` (#209).
 *
 * Replies and live updates can arrive out of order: the live "success" of a run may
 * come before the save request's own reply, which still says "queued". Merging must
 * never replace a newer state with an older one. Order: the revision, then the row's
 * ``updated_at`` (the server sets it on every write), then how far the run got.
 */
export function isOlderStageState(
  incoming: Pick<StageState, "revision" | "status" | "updated_at">,
  existing: Pick<StageState, "revision" | "status" | "updated_at">,
): boolean {
  if (incoming.revision !== existing.revision) return incoming.revision < existing.revision;
  const incomingTime = timestampMicros(incoming.updated_at);
  const existingTime = timestampMicros(existing.updated_at);
  if (incomingTime !== null && existingTime !== null && incomingTime !== existingTime) {
    return incomingTime < existingTime;
  }
  return (STATUS_PROGRESS[incoming.status] ?? 0) < (STATUS_PROGRESS[existing.status] ?? 0);
}

/**
 * Why the result pane shows no current result for a stage (#206); empty when it has one.
 *
 * The same derived state as the status line (#205, ``stageStatusMessage``): a failure,
 * unsaved edits, a queued or running job, an earlier stage to wait for, a stale result.
 * A stale result is never shown as current; the pane gives the reason instead.
 */
export function resultPaneMessage(
  state: Pick<
    StageState,
    "status" | "last_error" | "preview_storage_identifier" | "output_storage_identifier"
  >,
  readiness: StageReadiness,
  settingsNotApplied = false,
): string {
  const error = stageErrorText(state);
  if (error) return `Failed: ${error}`;
  if (settingsNotApplied) {
    return `${SETTINGS_NOT_APPLIED} The result appears once they are saved.`;
  }
  if (state.status === "queued") {
    return "Queued: the result appears here once the worker has run this stage.";
  }
  if (state.status === "running") return "Running: the result appears here when it finishes.";
  if (readiness.waitingFor) {
    return `Waiting for ${STAGE_LABELS[readiness.waitingFor]}. This stage runs when it is done.`;
  }
  if (state.status === "success") {
    if (!readiness.current) {
      return `Stale: ${readiness.staleReason || "input changed"}. It runs again by itself.`;
    }
    return state.preview_storage_identifier || state.output_storage_identifier
      ? ""
      : "This run produced no result preview.";
  }
  return "Not run yet. The result appears here once this stage has run.";
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
  private readonly revertButton: HTMLButtonElement | null;
  private readonly batchButton: HTMLButtonElement;
  private readonly batchScope: HTMLSelectElement;
  private readonly preserveExceptions: HTMLInputElement;
  private readonly selectionCount: HTMLElement;
  private readonly inputButton: HTMLButtonElement;
  private readonly resultButton: HTMLButtonElement;
  private readonly warnings: HTMLElement;
  /** Pane shown on narrow screens, where only one fits (#206); both show otherwise. */
  private previewMode: "input" | "result" = "input";
  private readonly panes: HTMLElement | null;
  private readonly resultPane: HTMLElement | null;
  private readonly resultViewport: HTMLElement | null;
  private readonly resultSurface: HTMLElement | null;
  private readonly resultImage: HTMLImageElement | null;
  private readonly resultStatus: HTMLElement | null;
  /** The result URL shown and the page/stage it belongs to. */
  private resultUrl = "";
  private resultKey = "";
  private activePageId: string | null = null;
  private activeStage: StageName = STAGE_ORDER[0];
  private draft: JsonObject = {};
  /**
   * The draft as last loaded or saved (``stableJson``): a different draft is not applied
   * yet (#205), and an unchanged one is never saved again (#209).
   */
  private appliedDraft = "";
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
  /** Pending autosave of the draft (#209). */
  private commitTimer: number | null = null;
  /** Margins link toggles; null until the member changes them (then equal sides link). */
  private readonly marginLinks: { horizontal: boolean | null; vertical: boolean | null } = {
    horizontal: null,
    vertical: null,
  };
  private saving = false;
  private saveQueued = false;
  /**
   * Drafts of pages or stages the editor left while a save was in flight (#209): they
   * are saved, for the page and stage they belong to, before the active draft again.
   */
  private leftDrafts: DraftSnapshot[] = [];
  /** "Revert to saved" was pressed while a save was in flight: reload after it. */
  private revertAfterSave = false;
  /** Revision saved while an older run was active: it restarts with the new settings. */
  private restartingRevision: number | null = null;
  /** The ensure request of the stage last opened (#209). */
  private ensuring: Promise<void> = Promise.resolve();

  constructor(root: HTMLElement, pages: WorkspacePage[]) {
    this.root = root;
    this.editable = root.dataset.editable === "true";
    this.controls = element(root, "[data-stage-controls]");
    this.image = element(root, "[data-editor-image]");
    this.geometryLayer = element(root, "[data-geometry-layer]");
    this.message = element(root, "[data-editor-message]");
    this.liveStatus = element(root, "[data-live-status]");
    this.title = element(root, "[data-editor-title]");
    this.revertButton = root.querySelector<HTMLButtonElement>("[data-revert-stage]");
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
    this.panes = root.querySelector<HTMLElement>("[data-panes]");
    this.resultPane = root.querySelector<HTMLElement>("[data-result-pane]");
    this.resultViewport = root.querySelector<HTMLElement>("[data-result-viewport]");
    this.resultSurface = root.querySelector<HTMLElement>("[data-result-surface]");
    this.resultImage = root.querySelector<HTMLImageElement>("[data-result-image]");
    this.resultStatus = root.querySelector<HTMLElement>("[data-result-status]");
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
    this.revertButton?.addEventListener("click", () => this.revertDraft());
    this.controls.addEventListener("input", () => this.draftEdited());
    this.controls.addEventListener("change", () => this.draftEdited());
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
        this.showStatus();
      }
    });
    window.addEventListener("resize", () => {
      this.fitToPane();
    });
    this.image.addEventListener("error", () => {
      this.clearGeometry();
      void this.explainPreviewFailure(this.image.src, this.activeStage);
    });
    this.resultImage?.addEventListener("load", () => {
      if (this.fitPending && !this.image.naturalWidth) {
        this.fitPending = false;
        this.fitToPane();
      } else {
        this.applyResultView();
      }
    });
    this.resultImage?.addEventListener("error", () => {
      if (!this.resultImage?.getAttribute("src")) return;
      this.resultImage.hidden = true;
      this.setResultStatus("The result preview could not be loaded.", false);
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
    if (this.previewRetry === INITIAL_PREVIEW_RETRY && !this.settingsNotApplied()) {
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
      if (retry.delay > 0) {
        this.showInputFailure(stage, failure, previewErrorMessage(stage, failure), "info");
      }
      this.schedulePreviewReload(retry.delay);
      return;
    }
    this.showInputFailure(
      stage,
      failure,
      previewIsBeingPrepared(failure)
        ? PREVIEW_RETRY_EXHAUSTED_MESSAGE
        : previewErrorMessage(stage, failure),
      "error",
    );
  }

  /**
   * Show why the input pane is empty. A missing input only means an earlier stage
   * has no current result: name the stage the page actually waits for (#205), and
   * leave "Settings not applied" in place while the member has unsaved edits.
   */
  private showInputFailure(
    stage: StageName,
    failure: PreviewFailure,
    text: string,
    kind: "info" | "error",
  ): void {
    const inputNotReady =
      failure.status === 404 && failure.body.trim().toLowerCase() === "input preview not ready";
    if (inputNotReady) {
      // The status line says "Saving…" or "Settings not applied yet" then (#209).
      if (this.savePending() || this.settingsNotApplied()) return;
      const page = this.activeContext()?.page;
      const waiting = page ? stageReadiness(page, stage).waitingFor : null;
      if (waiting) {
        this.setMessage(`Waiting for ${STAGE_LABELS[waiting]}: its result is not ready yet.`, kind);
        return;
      }
    }
    this.setMessage(text, kind);
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
    // A change still waiting for its autosave is saved before the editor moves on, for
    // the page and stage it was made on (#209).
    if (this.commitTimer !== null || this.saveQueued) this.leaveDraft();
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
    this.restartingRevision = null;
    this.renderControls();
    // Rendering fills in control defaults; the result is what "unchanged" means.
    this.appliedDraft = stableJson(this.draft);
    this.showPreview(this.previewMode);
    if (this.revertButton) this.revertButton.disabled = !this.editable;
    this.showStatus();
    this.ensuring = this.ensureActiveStage();
  }

  /**
   * Run the stage being opened unless its result is current (#209). The server queues
   * it at most once, and only when every earlier stage has a current result (#205).
   */
  private async ensureActiveStage(): Promise<void> {
    const context = this.activeContext();
    if (!context || !this.editable) return;
    const url = this.url("ensureUrlTemplate", context.page.id, context.state.stage);
    if (!url) return;
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken() },
      });
      if (response.status === 507) {
        // Nearly full scan volume (#204): nothing was queued; say why.
        this.setMessage(await response.text(), "error");
        return;
      }
      if (!response.ok) return;
      const payload = (await response.json()) as { stage: StageState };
      this.mergeStage(payload.stage);
    } catch {
      // Live updates or the next visit try again.
    }
  }

  /** "Revert to saved": drop changes that were not saved yet and show the stored settings. */
  private revertDraft(): void {
    if (this.commitTimer !== null) window.clearTimeout(this.commitTimer);
    this.commitTimer = null;
    this.saveQueued = false;
    if (this.saving) {
      // The settings being saved are the saved ones once the reply is in: show them then.
      this.revertAfterSave = true;
      return;
    }
    if (this.activePageId) this.activate(this.activePageId, this.activeStage);
  }

  /** The active draft, to save it for the page and stage it was made on. */
  private activeSnapshot(): DraftSnapshot | null {
    if (!this.activePageId) return null;
    return {
      pageId: this.activePageId,
      stage: this.activeStage,
      draft: structuredClone(this.draft),
      sent: stableJson(this.draft),
    };
  }

  /** The editor moves on with an edit not saved yet: keep it and save it (#209). */
  private leaveDraft(): void {
    if (this.commitTimer !== null) window.clearTimeout(this.commitTimer);
    this.commitTimer = null;
    this.saveQueued = false;
    const snapshot = this.settingsNotApplied() ? this.activeSnapshot() : null;
    if (!snapshot || !this.editable) return;
    this.leftDrafts.push(snapshot);
    // A save in flight saves it next (commitDraft); otherwise save it now.
    if (!this.saving) void this.commitDraft();
  }

  /** Whether the editor holds edits that were not saved (applied) yet (#205). */
  private settingsNotApplied(): boolean {
    return stableJson(this.draft) !== this.appliedDraft;
  }

  /** An edit waits for its autosave or is being saved (#209). */
  private savePending(): boolean {
    return this.commitTimer !== null || this.saving;
  }

  /** A run of older settings is active while newer ones are saved (#209). */
  private restarting(state: StageState): boolean {
    const active = state.status === "queued" || state.status === "running";
    return active && (this.savePending() || this.restartingRevision === state.revision);
  }

  /** The status line derived from the active stage's state (#205, #209). */
  private showStatus(): void {
    const context = this.activeContext();
    if (!context) return;
    if (this.savePending()) {
      this.setMessage(savingMessage(this.restarting(context.state)), "info");
      return;
    }
    const message = stageStatusMessage(
      context.state,
      stageReadiness(context.page, context.state.stage),
      this.settingsNotApplied(),
      this.restarting(context.state),
    );
    this.setMessage(message.text, message.tone);
  }

  /**
   * A control changed the draft: it is saved after a short pause (#209), and no result
   * is shown for settings that are not applied.
   */
  private draftEdited(): void {
    // Only a real change is saved: view toggles (grid, links) are not settings.
    if (this.settingsNotApplied()) this.scheduleCommit();
    if (this.saving) return;
    // Where only one pane fits, the edit is what to see; the result pane says why it
    // has no result either way.
    if (this.settingsNotApplied() && this.previewMode === "result") this.showPreview("input");
    else this.refreshResultPane();
    this.showStatus();
  }

  /** The active stage has a current result for the settings in the editor. */
  private resultAvailable(): boolean {
    const context = this.activeContext();
    if (!context) return false;
    const state = context.state;
    return (
      stageReadiness(context.page, state.stage).current &&
      !this.settingsNotApplied() &&
      Boolean(state.preview_storage_identifier || state.output_storage_identifier)
    );
  }

  /**
   * Show the stage: its input (with the editable overlay) and its result side by side
   * (#206). ``mode`` picks the pane shown where only one fits (narrow screens).
   */
  private showPreview(mode: "input" | "result"): void {
    const context = this.activeContext();
    if (!context) return;
    const state = context.state;
    // Worker warnings describe a current result only (#205).
    const hasResult = this.resultAvailable();
    this.previewMode = mode;
    this.inputButton.setAttribute("aria-pressed", String(this.previewMode === "input"));
    this.resultButton.setAttribute("aria-pressed", String(this.previewMode === "result"));
    if (this.panes) this.panes.dataset.pane = this.previewMode;
    const preview = this.url("previewUrlTemplate", context.page.id, state.stage);
    const source = preview ? `${preview}?input=true` : "";
    this.image.hidden = !source;
    let url = "";
    if (source) {
      const index = STAGE_ORDER.indexOf(state.stage);
      const previousName = index > 0 ? STAGE_ORDER[index - 1] : undefined;
      let token: string;
      if (previousName) {
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
    const warning = hasResult ? stageWorkerWarnings(state.parameters) : "";
    this.warnings.textContent = warning ? `Worker warning: ${warning}` : "";
    this.warnings.hidden = !warning;
    this.updateResultPane(context.page, state);
    this.applyDeskewPreview();
    this.renderGeometry();
  }

  /**
   * The result pane: this stage's current result. While the stage is queued or running
   * the previous result stays, dimmed, under a spinner; without a result it says why.
   */
  private updateResultPane(page: WorkspacePage, state: StageState): void {
    const image = this.resultImage;
    if (!image) return;
    const key = `${page.id}:${state.stage}`;
    // While an edit waits for its autosave or is being saved, its settings count as
    // applied (#209): the previous result stays, dimmed, until the new one is ready.
    const active = page.id === this.activePageId && state.stage === this.activeStage;
    const pending = active && this.savePending();
    const notApplied = active && !pending && this.settingsNotApplied();
    const message = pending
      ? savingMessage(this.restarting(state))
      : resultPaneMessage(state, stageReadiness(page, state.stage), notApplied);
    const busy =
      pending || (!notApplied && (state.status === "queued" || state.status === "running"));
    const preview = this.url("previewUrlTemplate", page.id, state.stage);
    let url = "";
    if (preview && !message) {
      const token = artifactToken(state);
      const query = token ? `v=${token}` : `revision=${state.revision}-${state.status}`;
      url = `${preview}?${query}`;
    }
    if (url) {
      this.resultKey = key;
      if (url !== this.resultUrl) {
        this.resultUrl = url;
        image.src = url;
      }
      image.hidden = false;
    } else if (busy && this.resultKey === key && this.resultUrl) {
      // Being recomputed: the previous result of this page and stage stays, dimmed.
      image.hidden = false;
    } else if (notApplied && this.resultKey === key && this.resultUrl) {
      // Unsaved edits: the result no longer matches them and is hidden, but kept so
      // that saving can dim it under the spinner while the stage re-runs.
      image.hidden = true;
    } else {
      // Nothing to keep: no earlier result of this page and stage, or it is stale.
      this.resultUrl = "";
      this.resultKey = "";
      image.removeAttribute("src");
      image.hidden = true;
    }
    this.resultPane?.classList.toggle("busy", busy);
    this.setResultStatus(message, busy);
  }

  /** Re-derive the active stage's result pane, e.g. after another stage changed. */
  private refreshResultPane(): void {
    const context = this.activeContext();
    if (context) this.updateResultPane(context.page, context.state);
  }

  private setResultStatus(message: string, busy: boolean): void {
    const status = this.resultStatus;
    if (!status) return;
    status.hidden = !message;
    const spinner = status.querySelector<HTMLElement>("[data-result-spinner]");
    if (spinner) spinner.hidden = !busy;
    const text = status.querySelector<HTMLElement>("[data-result-message]");
    if (text) text.textContent = message;
  }

  private updateActivePreview(state: StageState, previous?: StageState): void {
    if (state.scan_page_id !== this.activePageId || state.stage !== this.activeStage) return;
    const justCompleted =
      state.status === "success" &&
      (previous?.status !== "success" || previous.revision !== state.revision);
    const page = this.pages.get(state.scan_page_id);
    // The result pane always follows the stage; the input pane and its overlay are
    // left alone while the member drags on them.
    if (
      this.dewarpOverlay?.isDragging() === true ||
      this.contentOverlay?.isDragging() === true ||
      this.deskewOverlay?.isDragging() === true
    ) {
      if (page) this.updateResultPane(page, state);
      return;
    }
    // On narrow screens a finished run brings the result pane forward.
    this.showPreview(justCompleted ? "result" : this.previewMode);
    if (state.stage === "deskew") {
      this.syncDeskewControls();
      if (justCompleted) this.setMessage(deskewResultText(state.parameters), "success");
    } else if (state.stage === "dewarping") {
      const result = this.controls.querySelector<HTMLElement>("[data-dewarp-result]");
      if (result && state.status === "success") result.textContent = describeDewarp(state.parameters);
      if (justCompleted) this.setMessage(describeDewarp(state.parameters), "success");
    } else if (state.stage === "select_content") {
      this.updateContentText();
      if (justCompleted) {
        const box = this.contentBox();
        this.setMessage(describeContent(state.parameters, box, this.contentDpi()), "success");
      }
    } else if (state.stage === "margins") {
      // A re-run caused by another page (#208) updates the sizes shown here too.
      const result = this.controls.querySelector<HTMLElement>("[data-margins-result]");
      if (result) result.textContent = state.status === "success" ? describeMargins(state.parameters) : "";
      if (justCompleted) this.setMessage(`Completed. ${describeMargins(state.parameters)}`, "success");
    } else if (justCompleted) {
      this.setMessage("Completed. Result preview is ready.", "success");
    }
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
    return this.editable && this.activeStage === "deskew" && !this.image.hidden;
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
    // Where only one pane fits, the page being turned is the one to see.
    if (this.previewMode === "result") this.showPreview("input");
    this.applyDeskewPreview();
    if (commit) this.draftEdited();
  }

  /**
   * Save the draft after a short pause (#209): every stage saves by itself, and the
   * server runs it with the new settings (a run of older settings is cancelled).
   */
  private scheduleCommit(): void {
    if (!this.editable) return;
    if (this.commitTimer !== null) window.clearTimeout(this.commitTimer);
    this.commitTimer = window.setTimeout(() => {
      this.commitTimer = null;
      void this.commitDraft();
    }, AUTOSAVE_DELAY_MS);
  }

  /** Save the stage draft; a change made while a save is running is saved after it. */
  private async commitDraft(): Promise<void> {
    if (!this.editable) return;
    if (this.commitTimer !== null) {
      window.clearTimeout(this.commitTimer);
      this.commitTimer = null;
    }
    if (this.saving) {
      this.saveQueued = true;
      return;
    }
    const left = this.leftDrafts.shift();
    if (left) await this.saveDraft(left);
    else await this.saveActiveStage();
    if (this.leftDrafts.length > 0 || this.saveQueued) {
      this.saveQueued = false;
      await this.commitDraft();
      return;
    }
    if (this.revertAfterSave) {
      this.revertAfterSave = false;
      if (this.activePageId) this.activate(this.activePageId, this.activeStage);
    }
  }

  /** Field and result text after a run (unless the member is changing the angle). */
  private syncDeskewControls(): void {
    if (this.activeStage !== "deskew") return;
    const state = this.activeContext()?.state;
    if (!state) return;
    const field = this.controls.querySelector<HTMLInputElement>("[data-deskew-angle]");
    const editing =
      this.commitTimer !== null || this.saving || this.deskewOverlay?.isDragging() === true;
    if (editing) return;
    // A focused field keeps what the member typed; the result line still follows the
    // run, so a saved angle stops reading "not saved yet" while the field has focus.
    const typing = field !== null && document.activeElement === field;
    if (!typing && !("manual_angle" in this.draft) && field) {
      field.value = this.deskewAngle().toFixed(2);
    }
    const result = this.controls.querySelector<HTMLElement>("[data-deskew-result]");
    if (result && state.status === "success" && !this.settingsNotApplied()) {
      result.textContent = deskewResultText(state.parameters);
    }
    this.applyDeskewPreview();
  }

  /** Turn the input preview by the current angle and place the grid and handles. */
  private applyDeskewPreview(): void {
    const active = this.activeStage === "deskew" && !this.image.hidden;
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

  /**
   * Upstream `page_layout::OptionsWidget` (#208): hard margins in mm with linked
   * pairs, "Align with other pages" and the 3 × 3 alignment grid. Saved by the
   * autosave like every control (#209) or with "Apply to…".
   */
  private renderMarginControls(): void {
    const settings = marginSettings(this.draft);
    Object.assign(this.draft, settings);
    const links = {
      horizontal: this.marginLinks.horizontal ?? settings.left_mm === settings.right_mm,
      vertical: this.marginLinks.vertical ?? settings.top_mm === settings.bottom_mm,
    };
    const fields = new Map<MarginSide, HTMLInputElement>();
    const margins = document.createElement("fieldset");
    margins.className = "st-margins";
    const legend = document.createElement("legend");
    legend.textContent = "Margins (mm)";
    margins.append(legend);
    for (const side of MARGIN_SIDES) {
      const label = document.createElement("label");
      label.textContent = side[0]!.toUpperCase() + side.slice(1);
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.max = String(MAX_MARGIN_MM);
      input.step = "0.1";
      input.value = String(settings[`${side}_mm`]);
      input.disabled = !this.editable;
      input.dataset.margin = side;
      input.addEventListener("input", () => {
        if (!input.validity.valid || input.value === "") return;
        const value = Number(input.value);
        this.draft[`${side}_mm`] = value;
        const pair = side === "left" || side === "right" ? "horizontal" : "vertical";
        if (links[pair]) {
          const other = linkedSide(side);
          this.draft[`${other}_mm`] = value;
          const otherField = fields.get(other);
          if (otherField) otherField.value = input.value;
        }
      });
      fields.set(side, input);
      label.append(input);
      margins.append(label);
    }
    for (const [pair, text] of [
      ["vertical", "Link top and bottom"],
      ["horizontal", "Link left and right"],
    ] as const) {
      const label = document.createElement("label");
      label.className = "checkbox";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = links[pair];
      box.disabled = !this.editable;
      box.dataset.marginLink = pair;
      box.addEventListener("change", () => {
        links[pair] = box.checked;
        this.marginLinks[pair] = box.checked;
      });
      label.append(box, document.createTextNode(text));
      margins.append(label);
    }
    this.controls.append(margins);

    const alignLabel = document.createElement("label");
    alignLabel.className = "checkbox";
    const align = document.createElement("input");
    align.type = "checkbox";
    align.checked = settings.align_with_others;
    align.disabled = !this.editable;
    align.dataset.alignWithOthers = "";
    alignLabel.append(align, document.createTextNode("Align with other pages"));
    this.controls.append(alignLabel);

    const grid = document.createElement("fieldset");
    grid.className = "st-align-grid";
    const gridLegend = document.createElement("legend");
    gridLegend.textContent = "Alignment";
    grid.append(gridLegend);
    const cells: HTMLInputElement[] = [];
    for (const vertical of ["top", "center", "bottom"] as const satisfies readonly AlignY[]) {
      for (const horizontal of ["left", "center", "right"] as const satisfies readonly AlignX[]) {
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "margins-alignment";
        radio.value = `${vertical} ${horizontal}`;
        radio.title = vertical === horizontal ? "centre" : `${vertical} ${horizontal}`;
        radio.setAttribute("aria-label", radio.title);
        radio.checked = settings.align_y === vertical && settings.align_x === horizontal;
        radio.dataset.align = `${vertical}-${horizontal}`;
        radio.addEventListener("change", () => {
          if (!radio.checked) return;
          this.draft.align_x = horizontal;
          this.draft.align_y = vertical;
        });
        cells.push(radio);
        grid.append(radio);
      }
    }
    const syncGrid = (): void => {
      for (const cell of cells) cell.disabled = !this.editable || !align.checked;
    };
    align.addEventListener("change", () => {
      this.draft.align_with_others = align.checked;
      syncGrid();
    });
    syncGrid();
    this.controls.append(grid);

    const result = document.createElement("p");
    result.className = "st-stage-result";
    result.dataset.marginsResult = "";
    const state = this.activeContext()?.state;
    result.textContent =
      state?.status === "success" ? describeMargins(state.parameters ?? {}) : "";
    this.controls.append(result);
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
    button.addEventListener("click", () => {
      action();
      this.draftEdited();
    });
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
      if (input.validity.valid && input.value !== "") {
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
    const keepContent = this.activeStage === "select_content" ? this.contentOverlay : null;
    const keepDewarp = this.activeStage === "dewarping" ? this.dewarpOverlay : null;
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
      this.draftEdited();
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

  /**
   * Save the draft (#209: called by the autosave, never by a button). The server stores
   * the settings and runs the stage when every earlier stage is current (#205); a run of
   * older settings is cancelled. An unchanged draft is not sent.
   */
  private async saveActiveStage(): Promise<void> {
    const snapshot = this.activeSnapshot();
    if (!snapshot || !this.activeContext() || !this.editable) return;
    if (snapshot.sent === this.appliedDraft) {
      // Nothing changed (or changed back): nothing to save or run. A run that finished
      // while the previous save was in flight was not shown yet: show it now.
      this.syncDeskewControls();
      this.showStatus();
      this.refreshResultPane();
      return;
    }
    await this.saveDraft(snapshot);
  }

  /**
   * Save ``target``'s draft for its page and stage, which may no longer be the ones
   * shown (#209: the editor moved on while the autosave was pending).
   */
  private async saveDraft(target: DraftSnapshot): Promise<void> {
    const page = this.pages.get(target.pageId);
    const current = page ? stageFor(page, target.stage) : undefined;
    const url = this.url("stageUrlTemplate", target.pageId, target.stage);
    if (!page || !current || !url || !this.editable) return;
    const { pageId, stage, sent } = target;
    const isActive = (): boolean => this.activePageId === pageId && this.activeStage === stage;
    const restarting = current.status === "queued" || current.status === "running";
    this.saving = true;
    if (isActive()) this.setMessage(savingMessage(restarting), "info");
    try {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken(),
        },
        body: JSON.stringify({
          expected_revision: current.revision,
          algorithm: current.algorithm,
          parameters: target.draft,
        }),
      });
      if (response.status === 409 && !isActive()) {
        // Left before its autosave and changed by someone else meanwhile: the other
        // editor's values stay; load them without leaving the stage shown now.
        await this.reloadPageData(url);
        this.setMessage(
          `Conflict detected: another editor changed page ${page.logical_sequence_number} · ${STAGE_LABELS[stage]}. Your last change there was not saved.`,
          "conflict",
        );
        return;
      }
      if (response.status === 409) {
        // The latest values replace the edits: none of them is saved after all.
        if (this.commitTimer !== null) window.clearTimeout(this.commitTimer);
        this.commitTimer = null;
        this.saveQueued = false;
        await this.refreshPage(url);
        // Reloading re-opens the stage; its ensure answer must not hide the conflict.
        await this.ensuring;
        this.setMessage(
          "Conflict detected: another editor changed this stage. Latest values loaded; review before changing them again.",
          "conflict",
        );
        return;
      }
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as {
        stage: StageState;
        job: unknown;
        waiting_for?: string | null;
      };
      // The editor may have moved on to another page or stage while this was saved.
      const stillActive = isActive();
      if (stillActive) {
        // The saved settings are the applied ones now (#205).
        this.appliedDraft = sent;
        this.restartingRevision =
          restarting && payload.stage.status !== "success" ? payload.stage.revision : null;
      }
      // A live update may already have brought a newer state; merging keeps it (#209).
      this.mergeStage(payload.stage);
      if (!stillActive) return;
      const waitingFor = payload.waiting_for;
      if (waitingFor && isStageName(waitingFor)) {
        // Saving only stored the settings; the stage runs once that stage is done.
        this.setMessage(
          `Saved. Waiting for ${STAGE_LABELS[waitingFor]}; this stage runs when it is done.`,
          "success",
        );
        return;
      }
      const state = this.activeContext()?.state ?? payload.stage;
      if (state.status !== "success") {
        const page = this.pages.get(state.scan_page_id);
        const message = stageStatusMessage(
          state,
          page ? stageReadiness(page, state.stage) : undefined,
          false,
          this.restartingRevision === state.revision,
        );
        this.setMessage(message.text, message.tone);
        return;
      }
      // Without a job the result was already current for these settings: describe it.
      this.setMessage(
        state.stage === "deskew"
          ? `Saved. ${deskewResultText(state.parameters)}`
          : state.stage === "dewarping"
            ? `Saved. ${describeDewarp(state.parameters)}`
          : state.stage === "select_content"
            ? `Saved. ${describeContent(state.parameters, this.contentBox(), this.contentDpi())}`
            : "Saved. Result preview is ready.",
        "success",
      );
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Stage update failed.", "error");
    } finally {
      this.saving = false;
      if (!this.saveQueued) {
        this.syncDeskewControls();
        this.refreshResultPane();
        // Saved for a page or stage the editor has left: the one shown said "Saving…".
        if (this.activePageId !== pageId || this.activeStage !== stage) this.showStatus();
      }
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
        applied: { page_id: string; stage: StageState; waiting_for?: string | null }[];
        jobs: unknown[];
        applied_count: number;
        skipped_count: number;
      };
      for (const item of payload.applied) this.mergeStage(item.stage);
      const waiting = payload.applied.filter((item) => item.waiting_for).length;
      this.setMessage(
        `Saved for ${payload.applied_count} page(s): ${payload.jobs.length} queued, ` +
          `${waiting} waiting for an earlier stage; preserved ${payload.skipped_count} manual exception(s).`,
        "success",
      );
    } catch (error) {
      this.setMessage(error instanceof Error ? error.message : "Batch update failed.", "error");
    } finally {
      this.batchButton.disabled = !this.editable;
    }
  }

  private async refreshPage(stageUrl: string): Promise<void> {
    const page = await this.reloadPageData(stageUrl);
    this.activate(page.id, this.activeStage);
  }

  /** Load a page's latest stage rows into the editor's model, without opening it. */
  private async reloadPageData(stageUrl: string): Promise<WorkspacePage> {
    const response = await fetch(stageUrl, { credentials: "same-origin" });
    if (!response.ok) throw new Error("Could not load the latest stage revision.");
    const page = (await response.json()) as WorkspacePage;
    this.pages.set(page.id, page);
    return page;
  }

  private mergeStage(incoming: StageState): void {
    const page = this.pages.get(incoming.scan_page_id);
    if (!page) return;
    const index = page.stages.findIndex((state) => state.stage === incoming.stage);
    const previous = index >= 0 ? page.stages[index] : undefined;
    // Never replace a newer state with an older one (#209): a save's reply can arrive
    // after the live update of the run it queued.
    if (previous && isOlderStageState(incoming, previous)) return;
    if (index >= 0) page.stages[index] = incoming;
    else page.stages.push(incoming);
    this.updateStageDom(incoming);
    this.updateActivePreview(incoming, previous);
    if (incoming.scan_page_id !== this.activePageId || this.saving) return;
    // Another stage of the open page changed: what this stage waits for, whether its
    // result is current and its input preview all derive from that (#205).
    const justCompleted =
      incoming.status === "success" &&
      (previous?.status !== "success" || previous.revision !== incoming.revision);
    if (incoming.stage !== this.activeStage) {
      if (incoming.stage === previousStageName(this.activeStage) && !this.isDragging())
        this.showPreview(this.previewMode);
      else this.refreshResultPane();
      this.showStatus();
    } else if (!justCompleted) {
      this.showStatus();
    }
  }

  private isDragging(): boolean {
    return (
      this.deskewOverlay?.isDragging() === true ||
      this.contentOverlay?.isDragging() === true ||
      this.dewarpOverlay?.isDragging() === true
    );
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
        const readiness = stageReadiness(page, name);
        return {
          stage: name,
          label: STAGE_LABELS[name],
          status: state?.status ?? "unknown",
          manual: state?.manual ?? false,
          last_error: state?.last_error ?? "",
          waiting_for: readiness.waitingFor ? STAGE_LABELS[readiness.waitingFor] : "",
          stale_reason: readiness.staleReason,
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
    if (this.resultImage) this.resultImage.draggable = false;
    // Both panes share one view (#206): zooming or panning either moves both.
    for (const pane of [viewport, this.resultViewport]) {
      if (pane) this.bindPanZoom(pane, pane === viewport);
    }
    const zoomButton = (selector: string, action: () => void): void => {
      this.root.querySelector<HTMLButtonElement>(selector)?.addEventListener("click", action);
    };
    const centre = (): { x: number; y: number } => {
      const extent = this.paneExtent();
      return { x: extent.width / 2, y: extent.height / 2 };
    };
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

  private bindPanZoom(viewport: HTMLElement, input: boolean): void {
    viewport.addEventListener(
      "wheel",
      (event) => {
        if (event.ctrlKey) {
          // Upstream deskew: Ctrl+wheel rotates, Ctrl+Shift+wheel rotates finely.
          if (input && this.deskewEditing()) {
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
  }

  /** Size of a pane; both have the same size, but one may be hidden (narrow screens). */
  private paneExtent(): { width: number; height: number } {
    for (const pane of [this.viewport, this.resultViewport]) {
      if (pane && pane.clientWidth > 0) {
        return { width: pane.clientWidth, height: pane.clientHeight };
      }
    }
    return { width: 0, height: 0 };
  }

  private fitToPane(): void {
    // Fit the input; the result alone when the input preview is not available.
    const image = this.image.naturalWidth ? this.image : this.resultImage;
    if (!this.viewport || !image?.naturalWidth) {
      this.renderGeometry();
      return;
    }
    this.view = fitView(this.paneExtent(), {
      width: image.naturalWidth,
      height: image.naturalHeight,
    });
    this.applyView();
  }

  /** Lay the image out at ``natural * zoom`` and translate it; redraw overlays if resized. */
  private applyView(resized = true): void {
    this.applyResultView();
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

  /** The result at the input's zoom, centred where the input is, for comparison. */
  private applyResultView(): void {
    const image = this.resultImage;
    if (!image || !this.resultSurface || !image.naturalWidth) return;
    const zoom = this.view.zoom;
    const width = image.naturalWidth * zoom;
    const height = image.naturalHeight * zoom;
    const inputWidth = this.image.naturalWidth ? this.image.naturalWidth * zoom : width;
    const inputHeight = this.image.naturalHeight ? this.image.naturalHeight * zoom : height;
    const x = this.view.x + (inputWidth - width) / 2;
    const y = this.view.y + (inputHeight - height) / 2;
    image.style.width = `${Math.round(width)}px`;
    image.style.height = `${Math.round(height)}px`;
    this.resultSurface.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
    if (this.zoomLevel) this.zoomLevel.textContent = `${Math.round(zoom * 100)} %`;
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
        applied: { page_id: string; stage: StageState; waiting_for?: string | null }[];
        jobs: unknown[];
        applied_count: number;
      };
      for (const item of payload.applied) this.mergeStage(item.stage);
      // Pages with a current result or a stage still to finish before it get no job (#205).
      const waiting = payload.applied.filter((item) => item.waiting_for).length;
      this.setMessage(
        `Queued ${STAGE_LABELS[stage]} for ${payload.jobs.length} of ${payload.applied_count} ` +
          `page(s); ${waiting} waiting for an earlier stage.`,
        "success",
      );
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
    if (this.resultImage) this.resultImage.hidden = true;
    this.controls.replaceChildren();
    if (this.revertButton) this.revertButton.disabled = true;
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
