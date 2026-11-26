/**
 * Output stage algorithms and their settings (#225).
 *
 * Mirrors ``OUTPUT_PARAMETERS`` / ``OUTPUT_MODE_PARAMETERS`` in
 * ``core/scantailor.py``. Each algorithm shows only the settings it reads, each
 * setting has a short explanation, and the recommended range is guidance only: a
 * typed value outside it is applied as typed. Only ``hard`` limits (memory, the
 * 8-bit range) clamp, and the editor says when they do.
 */

export type OutputAlgorithmName =
  | "otsu"
  | "sauvola"
  | "wolf"
  | "fox"
  | "window"
  | "su"
  | "grad"
  | "copy";

export interface OutputParameterSpec {
  key: string;
  label: string;
  help: string;
  defaultValue: number;
  /** Recommended range: guidance, never enforced. */
  recommended: readonly [number, number];
  step: number;
  /** Technical limit the worker clamps to. */
  hard?: readonly [number, number];
  integer?: boolean;
  /** Settings saved before #225 had one ``k`` shared by every algorithm. */
  legacyKey?: string;
}

export interface OutputAlgorithmSpec {
  name: OutputAlgorithmName;
  label: string;
  /** What it does, in one or two sentences. */
  summary: string;
  /** Where it comes from. */
  provenance: string;
  parameters: readonly string[];
}

const WINDOW_HELP =
  "Side of the square around each pixel whose mean and contrast set its threshold, in " +
  "pixels of the full-resolution image. Use about 1–3 text line heights: smaller follows " +
  "uneven light more closely but can hollow out thick strokes; larger behaves more like " +
  "one global threshold. Rough guide: 25–75 at 300 dpi, 50–150 at 600 dpi.";

export const OUTPUT_PARAMETERS: Readonly<Record<string, OutputParameterSpec>> = {
  window_size: {
    key: "window_size",
    label: "Local window size (px)",
    help: WINDOW_HELP,
    defaultValue: 35,
    recommended: [15, 151],
    step: 2,
    hard: [3, 501],
    integer: true,
  },
  sauvola_k: {
    key: "sauvola_k",
    label: "Sauvola k",
    help:
      "How far below the local mean the threshold sits where contrast is low. Larger k: " +
      "thinner text, cleaner paper; smaller k: bolder text, more noise.",
    defaultValue: 0.34,
    recommended: [0.2, 0.5],
    step: 0.01,
    legacyKey: "k",
  },
  r: {
    key: "r",
    label: "Sauvola r (contrast range)",
    help:
      "The local standard deviation counted as full contrast. 128 suits 8-bit scans; " +
      "lower it for faint, low-contrast pages.",
    defaultValue: 128,
    recommended: [64, 128],
    step: 1,
    hard: [1, 1e6],
  },
  wolf_k: {
    key: "wolf_k",
    label: "Wolf k",
    help:
      "Weight of the page's darkest value and of local contrast. Larger k: lower " +
      "threshold, less black; 0 means the local mean alone.",
    defaultValue: 0.3,
    recommended: [0.1, 0.5],
    step: 0.01,
    legacyKey: "k",
  },
  wolf_lower_bound: {
    key: "wolf_lower_bound",
    label: "Wolf lower bound",
    help: "Gray values below this are always black.",
    defaultValue: 1,
    recommended: [0, 50],
    step: 1,
    hard: [0, 255],
    integer: true,
  },
  wolf_upper_bound: {
    key: "wolf_upper_bound",
    label: "Wolf upper bound",
    help: "Gray values above this are always white, whatever the threshold.",
    defaultValue: 254,
    recommended: [200, 254],
    step: 1,
    hard: [0, 255],
    integer: true,
  },
  fox_k: {
    key: "fox_k",
    label: "Fox k",
    help:
      "Multiples of the local standard deviation subtracted below a fixed 12 % offset " +
      "under the local mean. Larger k: less black; negative k: bolder text.",
    defaultValue: 0.2,
    recommended: [0, 1],
    step: 0.05,
    legacyKey: "k",
  },
  window_k: {
    key: "window_k",
    label: "Window k",
    help:
      "Lowers the threshold to (1 − k/2) × local mean. 0 = the local mean itself (paper " +
      "noise turns black); 1 = half of it (faint text may vanish).",
    defaultValue: 0.33,
    recommended: [0.1, 0.8],
    step: 0.01,
    legacyKey: "k",
  },
  su_min_edge_ratio: {
    key: "su_min_edge_ratio",
    label: "Su minimum edge pixels (× window)",
    help:
      "A pixel can only be text if its window holds at least this many high-contrast " +
      "(stroke edge) pixels, as a multiple of the window size. Larger: fewer specks, but " +
      "isolated small marks can vanish.",
    defaultValue: 1,
    recommended: [0.5, 2],
    step: 0.1,
    hard: [0, 1e6],
  },
};

export const OUTPUT_ALGORITHMS: readonly OutputAlgorithmSpec[] = [
  {
    name: "otsu",
    label: "Otsu (global)",
    summary: "One threshold T for the whole page, chosen from the histogram. No settings.",
    provenance: "Upstream ScanTailor binarizeOtsu; Otsu (1979).",
    parameters: [],
  },
  {
    name: "sauvola",
    label: "Sauvola (local)",
    summary: "T = m · (1 + k · (s / r − 1)) per pixel, from the local mean m and deviation s.",
    provenance: "Upstream binarizeSauvola, as offered by ScanTailor Advanced; Sauvola & Pietikäinen (2000).",
    parameters: ["window_size", "sauvola_k", "r"],
  },
  {
    name: "wolf",
    label: "Wolf (local)",
    summary:
      "T = m − k · (m − Min) · (1 − s / Smax) per pixel; Min and Smax are measured once over the page.",
    provenance: "Upstream binarizeWolf, as offered by ScanTailor Advanced; Wolf & Jolion (2003).",
    parameters: ["window_size", "wolf_k", "wolf_lower_bound", "wolf_upper_bound"],
  },
  {
    name: "fox",
    label: "Fox (local, project-specific)",
    summary: "T = 0.88 · m − k · s per pixel: Niblack's contrast term below a fixed noise offset.",
    provenance:
      "Not from ScanTailor or ScanTailor Advanced and no published source is known; a " +
      "Niblack (1986) variant written for this project. The name has no meaning.",
    parameters: ["window_size", "fox_k"],
  },
  {
    name: "window",
    label: "Window (local mean, project-specific)",
    summary: "T = m · (1 − k / 2) per pixel: a fraction of the local mean.",
    provenance:
      "Not from ScanTailor or ScanTailor Advanced; a local-mean threshold written for " +
      "this project (similar in spirit to Bradley & Roth 2007).",
    parameters: ["window_size", "window_k"],
  },
  {
    name: "su",
    label: "Su 2010 (local max/min contrast)",
    summary:
      "Finds stroke edges from local contrast, then marks a pixel black when enough edges are " +
      "nearby and it is no lighter than their mean + ½ deviation. Robust on uneven light and stains.",
    provenance:
      "Su, Lu & Tan, “Binarization of historical document images using the local maximum " +
      "and minimum”, DAS 2010, doi:10.1145/1815330.1815351. Added by #225.",
    parameters: ["window_size", "su_min_edge_ratio"],
  },
  {
    name: "grad",
    label: "Grad (Otsu × gradient, project-specific)",
    summary: "Otsu of the image XOR Otsu of the edge strength. No settings.",
    provenance: "Not from ScanTailor or ScanTailor Advanced; experimental, written for this project.",
    parameters: [],
  },
  {
    name: "copy",
    label: "Copy (no binarization)",
    summary: "Keeps the input pixels.",
    provenance: "—",
    parameters: [],
  },
];

export const OUTPUT_ALGORITHM_NAMES: readonly OutputAlgorithmName[] = OUTPUT_ALGORITHMS.map(
  (algorithm) => algorithm.name,
);

export function outputAlgorithm(name: unknown): OutputAlgorithmSpec | undefined {
  return OUTPUT_ALGORITHMS.find((algorithm) => algorithm.name === name);
}

/** The settings the given algorithms read, in a stable order, each once. */
export function outputParametersFor(names: Iterable<unknown>): OutputParameterSpec[] {
  const wanted = new Set<string>();
  for (const name of names) for (const key of outputAlgorithm(name)?.parameters ?? []) wanted.add(key);
  return Object.values(OUTPUT_PARAMETERS).filter((spec) => wanted.has(spec.key));
}

/** The value to show: own key, else the pre-#225 shared ``k``, else the default. */
export function outputParameterValue(
  parameters: Readonly<Record<string, unknown>>,
  spec: OutputParameterSpec,
): number {
  for (const key of [spec.key, spec.legacyKey]) {
    if (!key) continue;
    const value = parameters[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return spec.defaultValue;
}

export interface ParsedParameter {
  /** The value to store, or null when the text is not a number (keep the old one). */
  value: number | null;
  /** What the editor says about the typed value; empty when it is in range. */
  note: string;
  tone: "" | "info" | "warning";
}

function formatNumber(value: number): string {
  return String(Number(value.toPrecision(6)));
}

export function guidanceText(spec: OutputParameterSpec): string {
  const [low, high] = spec.recommended;
  return `Recommended ${formatNumber(low)}–${formatNumber(high)}, default ${formatNumber(spec.defaultValue)}.`;
}

/**
 * Typed text to a stored value (#225). Values outside the recommended range are
 * applied as typed with a note; only the hard technical limit clamps.
 */
export function parseParameterInput(text: string, spec: OutputParameterSpec): ParsedParameter {
  const trimmed = text.trim().replace(",", ".");
  const parsed = trimmed === "" ? Number.NaN : Number(trimmed);
  if (!Number.isFinite(parsed)) {
    return { value: null, note: "Not a number; the previous value stays.", tone: "warning" };
  }
  let value = spec.integer ? Math.round(parsed) : parsed;
  if (spec.hard) {
    const [minimum, maximum] = spec.hard;
    if (value < minimum || value > maximum) {
      value = Math.min(maximum, Math.max(minimum, value));
      return {
        value,
        note: `Clamped to ${formatNumber(value)}: the technical limit is ${formatNumber(minimum)}–${formatNumber(maximum)}.`,
        tone: "warning",
      };
    }
  }
  const [low, high] = spec.recommended;
  if (value < low || value > high) {
    return {
      value,
      note: `Outside the recommended range; applied as typed.`,
      tone: "info",
    };
  }
  return { value, note: "", tone: "" };
}

export interface HistogramThreshold {
  kind?: unknown;
  median?: unknown;
  low?: unknown;
  high?: unknown;
  defined_fraction?: unknown;
}

const percent = (value: number) => `${(value * 100).toFixed(value < 0.1 ? 1 : 0)} %`;

/** The caption under the histogram: which T the algorithm chose and how much turns black. */
export function describeHistogram(histogram: Readonly<Record<string, unknown>>): string {
  const parts: string[] = [];
  const algorithm = outputAlgorithm(histogram.mode);
  if (algorithm) parts.push(algorithm.label);
  const threshold = histogram.threshold as HistogramThreshold | undefined;
  if (threshold && typeof threshold === "object") {
    const { median, low, high } = threshold;
    if (typeof median === "number" && typeof low === "number" && typeof high === "number") {
      parts.push(
        `T per pixel: median ${Math.round(median)}, 10–90 % ${Math.round(low)}–${Math.round(high)}`,
      );
      const defined = threshold.defined_fraction;
      if (typeof defined === "number" && defined < 0.995) {
        parts.push(`T defined near text only (${percent(defined)} of pixels)`);
      }
    } else if (typeof median === "number") {
      parts.push(
        threshold.kind === "intensity"
          ? `intensity part T ${Math.round(median)}`
          : `T ${Math.round(median)}`,
      );
    } else {
      parts.push("no threshold: no text edges found");
    }
  } else {
    parts.push("no threshold");
  }
  const black = histogram.black_fraction;
  if (typeof black === "number") parts.push(`${percent(black)} black`);
  const window = histogram.preview_window;
  if (typeof window === "number") parts.push(`window ${window} px on the preview`);
  return parts.join(" · ");
}
