/**
 * Margins options (#208), after upstream Scan Tailor's `page_layout::OptionsWidget`:
 * hard margins in millimetres with linked left/right and top/bottom pairs, a 3 × 3
 * alignment grid and "Align with other pages". The server computes the common page
 * size (the largest content plus hard margins) and each page's soft margins.
 */

type JsonObject = Record<string, unknown>;

export type AlignX = "left" | "center" | "right";
export type AlignY = "top" | "center" | "bottom";

export interface MarginSettings {
  left_mm: number;
  top_mm: number;
  right_mm: number;
  bottom_mm: number;
  align_x: AlignX;
  align_y: AlignY;
  align_with_others: boolean;
}

export const MARGIN_SIDES = ["top", "bottom", "left", "right"] as const;
export type MarginSide = (typeof MARGIN_SIDES)[number];

/** Upstream `Settings::defaultHardMarginsMM()` and default alignment (top, centre). */
export const DEFAULT_MARGINS: Readonly<MarginSettings> = {
  left_mm: 10,
  top_mm: 5,
  right_mm: 10,
  bottom_mm: 5,
  align_x: "center",
  align_y: "top",
  align_with_others: true,
};

export const MAX_MARGIN_MM = 500;
const MM_PER_INCH = 25.4;

function finite(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(number) ? number : null;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clampMargin(value: number): number {
  return Math.min(MAX_MARGIN_MM, Math.max(0, value));
}

function alignX(value: unknown): AlignX {
  return value === "left" || value === "right" ? value : value === "center" ? "center" : DEFAULT_MARGINS.align_x;
}

function alignY(value: unknown): AlignY {
  return value === "top" || value === "bottom" ? value : value === "center" ? "center" : DEFAULT_MARGINS.align_y;
}

function recordedDpi(parameters: JsonObject): { x: number; y: number } {
  const dpi = parameters.dpi;
  if (Array.isArray(dpi) && dpi.length >= 2) {
    const x = finite(dpi[0]);
    const y = finite(dpi[1]);
    if (x && y && x > 0 && y > 0) return { x, y };
  }
  return { x: 300, y: 300 };
}

/**
 * The editable settings of a Margins stage: the millimetre keys when present, else
 * what the last run resolved (`hard_margins_mm`), else pixel settings written before
 * #208 converted with the recorded dpi, else upstream's defaults.
 */
export function marginSettings(parameters: JsonObject): MarginSettings {
  const sides = { left: "left_mm", top: "top_mm", right: "right_mm", bottom: "bottom_mm" } as const;
  const values: Record<keyof typeof sides, number> = {
    left: DEFAULT_MARGINS.left_mm,
    top: DEFAULT_MARGINS.top_mm,
    right: DEFAULT_MARGINS.right_mm,
    bottom: DEFAULT_MARGINS.bottom_mm,
  };
  const hasMm = Object.values(sides).some((key) => key in parameters);
  const resolved = isObject(parameters.hard_margins_mm) ? parameters.hard_margins_mm : null;
  const legacy = ["margin", "left", "top", "right", "bottom"].some((key) => key in parameters);
  for (const side of Object.keys(sides) as (keyof typeof sides)[]) {
    if (hasMm) {
      const value = finite(parameters[sides[side]]);
      if (value !== null) values[side] = clampMargin(value);
    } else if (resolved) {
      const value = finite(resolved[side]);
      if (value !== null) values[side] = clampMargin(value);
    } else if (legacy) {
      const dpi = recordedDpi(parameters);
      const pixels = finite(parameters[side]) ?? finite(parameters.margin) ?? 0;
      const axis = side === "left" || side === "right" ? dpi.x : dpi.y;
      values[side] = clampMargin(Math.round(((Math.max(0, pixels) * MM_PER_INCH) / axis) * 10) / 10);
    }
  }
  return {
    left_mm: values.left,
    top_mm: values.top,
    right_mm: values.right,
    bottom_mm: values.bottom,
    align_x: alignX(parameters.align_x),
    align_y: alignY(parameters.align_y),
    align_with_others: parameters.align_with_others !== false,
  };
}

function mmSize(value: unknown): { width: number; height: number } | null {
  if (!isObject(value)) return null;
  const width = finite(value.width);
  const height = finite(value.height);
  return width !== null && height !== null ? { width, height } : null;
}

const one = (value: number): string => value.toFixed(1);

/** What the last run produced: the page size, the common size and the soft margins. */
export function describeMargins(parameters: JsonObject): string {
  const page = mmSize(parameters.page_size_mm);
  if (!page) return "";
  const size = `${one(page.width)} × ${one(page.height)} mm`;
  if (parameters.align_with_others === false)
    return `Page ${size}: not aligned with other pages, it keeps its own size.`;
  const common = mmSize(parameters.aggregate_size_mm);
  const soft = isObject(parameters.soft_margins_mm) ? parameters.soft_margins_mm : {};
  const softText = (["left", "top", "right", "bottom"] as const)
    .map((side) => `${side} ${one(finite(soft[side]) ?? 0)}`)
    .join(", ");
  const empty = parameters.content_found === false ? " No content: the page is centred." : "";
  return (
    `Page ${size}` +
    (common ? ` (common size ${one(common.width)} × ${one(common.height)} mm)` : "") +
    `. Soft margins ${softText} mm.${empty}`
  );
}

/** The other side of a linked pair (upstream's chain buttons). */
export function linkedSide(side: MarginSide): MarginSide {
  return side === "top" ? "bottom" : side === "bottom" ? "top" : side === "left" ? "right" : "left";
}
