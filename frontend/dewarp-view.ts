/**
 * Dewarping in the browser (#195 step 6a).
 *
 * Derived from Scan Tailor filters/output/DewarpingView.cpp,
 * interaction/InteractiveXSpline.cpp, math/XSpline.cpp, math/ArcLengthMapper.cpp,
 * math/PolylineIntersector.cpp and dewarping/CylindricalSurfaceDewarper.cpp
 * Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
 * Copyright (C) c-base_book-scanner contributors
 *
 * This program is free software: you can redistribute it and/or modify it under the
 * terms of the GNU General Public License as published by the Free Software
 * Foundation, either version 3 of the License, or (at your option) any later version.
 * See the LICENSE file at the repository root.
 *
 * The page shows the distortion model as upstream draws it: a 30 x 30 blue grid that
 * follows the cylinder, translucent white margins outside its left and right edges,
 * and the red control points of the top and bottom X-splines. Dragging a middle point
 * moves it; dragging an end point rotates and scales its whole curve about the other
 * end; clicking a curve adds a point there and starts dragging it; Del or D removes the
 * point under the pointer. The grid follows every movement in the browser; releasing
 * commits the curves (and makes an auto model manual), as upstream does.
 *
 * The model is a port of core/cylindrical.py; tests/fixtures/dewarp_model_cases.json
 * keeps both in agreement.
 */

export type Pt = [number, number];

// --- XSpline ---------------------------------------------------------------------------

function gBlend(q: number, p: number, u: number): number {
  return (
    q * u +
    2 * q * u ** 2 +
    (10 - 12 * q - p) * u ** 3 +
    (2 * p + 14 * q - 15) * u ** 4 +
    (6 - 5 * q - p) * u ** 5
  );
}

function hBlend(q: number, u: number): number {
  return q * u + 2 * q * u ** 2 - 2 * q * u ** 4 - q * u ** 5;
}

export class XSpline {
  points: Pt[];
  tensions: number[];

  constructor(points: Pt[], tensions?: number[]) {
    this.points = points.map(([x, y]) => [x, y]);
    this.tensions =
      tensions ?? this.points.map((_, i) => (i === 0 || i === this.points.length - 1 ? 0 : 1));
  }

  get numSegments(): number {
    return Math.max(0, this.points.length - 1);
  }

  private segmentPoint(segment: number, t: number): Pt {
    const last = this.points.length - 1;
    const idxs = [Math.max(0, segment - 1), segment, segment + 1, Math.min(segment + 2, last)];
    const ts1 = this.tensions[segment] ?? 0;
    const ts2 = this.tensions[segment + 1] ?? 0;
    const s1 = Math.max(ts1, 0);
    const s2 = Math.max(ts2, 0);
    const [t0, t1, t2, t3] = [-1, 0, 1, 2];
    const T0p = t1 + s1;
    const T1p = t2 + s2;
    const T2m = t1 - s1;
    const T3m = t2 - s2;
    const q1 = -0.5 * Math.min(ts1, 0);
    const q2 = -0.5 * Math.min(ts2, 0);
    const p = [2 * (t0 - T0p) ** 2, 2 * (t1 - T1p) ** 2, 2 * (t2 - T2m) ** 2, 2 * (t3 - T3m) ** 2];
    const u0 = (t - T0p) / (t0 - T0p);
    const u3 = (t - T3m) / (t3 - T3m);
    const a = [
      t <= T0p ? gBlend(q1, p[0]!, u0) : hBlend(q1, u0),
      gBlend(q2, p[1]!, (t - T1p) / (t1 - T1p)),
      gBlend(q1, p[2]!, (t - T2m) / (t2 - T2m)),
      t >= T3m ? gBlend(q2, p[3]!, u3) : hBlend(q2, u3),
    ];
    const total = a[0]! + a[1]! + a[2]! + a[3]!;
    let x = 0;
    let y = 0;
    idxs.forEach((index, k) => {
      const w = a[k]! / total;
      x += w * this.points[index]![0];
      y += w * this.points[index]![1];
    });
    return [x, y];
  }

  pointAt(t: number): Pt {
    const n = this.numSegments;
    if (n <= 0) return this.points[0] ?? [0, 0];
    if (t >= 1) return this.segmentPoint(n - 1, 1);
    const scaled = t * n;
    const segment = Math.floor(scaled);
    return this.segmentPoint(segment, scaled - segment);
  }

  /** `toPolyline` with upstream's default sampling (0.2 px from the spline). */
  toPolyline(maxDist = 0.2): Pt[] {
    const n = this.numSegments;
    const start = this.pointAt(0);
    if (n === 0) return [start];
    const end = this.pointAt(1);
    const out: Pt[] = [start];
    const maxSq = maxDist * maxDist;
    const refine = (pt: number, pp: Pt, nt: number, np: Pt, depth: number): void => {
      const dx = np[0] - pp[0];
      const dy = np[1] - pp[1];
      if (dx * dx + dy * dy < 1e-6 || depth > 40) return;
      let mt = 0.5 * (pt + nt);
      const junction = Math.floor(mt * n + 0.5) / n;
      const isJunction = (junction - pt) * (nt - pt) > 0 && (junction - nt) * (pt - nt) > 0;
      if (isJunction) mt = junction;
      const mp = this.pointAt(mt);
      if (!isJunction) {
        const proj = projectPoint([pp, np], mp);
        if ((mp[0] - proj[0]) ** 2 + (mp[1] - proj[1]) ** 2 <= maxSq) return;
      }
      refine(pt, pp, mt, mp, depth + 1);
      out.push(mp);
      refine(mt, mp, nt, np, depth + 1);
    };
    refine(0, start, 1, end, 0);
    out.push(end);
    return out;
  }

  /** `InteractiveXSpline::controlPointMoveRequest`. */
  moveControlPoint(index: number, to: Pt): void {
    const last = this.points.length - 1;
    if (index > 0 && index < last) {
      this.points[index] = [to[0], to[1]];
      return;
    }
    // An end point: rotate and scale every point about the opposite end.
    const origin = this.points[index === 0 ? last : 0]!;
    const old = this.points[index]!;
    const ox = old[0] - origin[0];
    const oy = old[1] - origin[1];
    if (ox * ox + oy * oy > 1) {
      const nx = to[0] - origin[0];
      const ny = to[1] - origin[1];
      const norm = ox * ox + oy * oy;
      // Complex division: (n / o) as a rotation-and-scale matrix.
      const a = (nx * ox + ny * oy) / norm;
      const b = (ny * ox - nx * oy) / norm;
      this.points = this.points.map(([x, y]) => {
        const px = x - origin[0];
        const py = y - origin[1];
        return [origin[0] + a * px - b * py, origin[1] + b * px + a * py];
      });
    } else {
      // Move the end point and spread the others evenly on the line.
      this.points = this.points.map((_, i) => {
        const f = i / last;
        return [origin[0] + (to[0] - origin[0]) * f, origin[1] + (to[1] - origin[1]) * f];
      });
      if (index === 0) this.points.reverse();
    }
  }
}

// --- geometry ----------------------------------------------------------------------------

function projectionScalar(line: [Pt, Pt], p: Pt): number {
  const [o, e] = line;
  const vx = e[0] - o[0];
  const vy = e[1] - o[1];
  const norm = vx * vx + vy * vy;
  if (Math.abs(norm) <= Number.EPSILON) return 0;
  return ((p[0] - o[0]) * vx + (p[1] - o[1]) * vy) / norm;
}

function projectPoint(line: [Pt, Pt], p: Pt): Pt {
  const s = projectionScalar(line, p);
  const [o, e] = line;
  return [o[0] + (e[0] - o[0]) * s, o[1] + (e[1] - o[1]) * s];
}

function lineIntersection(a: [Pt, Pt], b: [Pt, Pt]): Pt | null {
  const [[x1, y1], [x2, y2]] = a;
  const [[x3, y3], [x4, y4]] = b;
  const denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4);
  if (denom === 0) return null;
  const da = x1 * y2 - y1 * x2;
  const db = x3 * y4 - y3 * x4;
  return [
    (da * (x3 - x4) - (x1 - x2) * db) / denom,
    (da * (y3 - y4) - (y1 - y2) * db) / denom,
  ];
}

type Mat3 = number[];

function homography(pairs: [Pt, Pt][]): Mat3 {
  const a: number[][] = [];
  const b: number[] = [];
  for (const [[fx, fy], [tx, ty]] of pairs) {
    a.push([-fx, -fy, -1, 0, 0, 0, tx * fx, tx * fy]);
    b.push(-tx);
    a.push([0, 0, 0, -fx, -fy, -1, ty * fx, ty * fy]);
    b.push(-ty);
  }
  return [...solve(a, b), 1];
}

function solve(a: number[][], b: number[]): number[] {
  const n = b.length;
  const m = a.map((row, i) => [...row, b[i]!]);
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1)
      if (Math.abs(m[row]![col]!) > Math.abs(m[pivot]![col]!)) pivot = row;
    if (Math.abs(m[pivot]![col]!) < 1e-14) throw new Error("degenerate model");
    [m[col], m[pivot]] = [m[pivot]!, m[col]!];
    for (let row = 0; row < n; row += 1) {
      if (row === col) continue;
      const f = m[row]![col]! / m[col]![col]!;
      for (let k = col; k <= n; k += 1) m[row]![k]! -= f * m[col]![k]!;
    }
  }
  return m.map((row, i) => row[n]! / row[i]!);
}

function applyH(h: Mat3, [x, y]: Pt): Pt {
  const w = h[6]! * x + h[7]! * y + h[8]!;
  return [(h[0]! * x + h[1]! * y + h[2]!) / w, (h[3]! * x + h[4]! * y + h[5]!) / w];
}

function invert(h: Mat3): Mat3 {
  const [a, b, c, d, e, f, g, hh, i] = h as [number, number, number, number, number, number, number, number, number];
  const A = e * i - f * hh;
  const B = -(d * i - f * g);
  const C = d * hh - e * g;
  const det = a * A + b * B + c * C;
  return [
    A / det,
    -(b * i - c * hh) / det,
    (b * f - c * e) / det,
    B / det,
    (a * i - c * g) / det,
    -(a * f - c * d) / det,
    C / det,
    -(a * hh - b * g) / det,
    (a * e - b * d) / det,
  ];
}

function threePoint1D(pairs: [number, number][]): [number, number, number] {
  const solved = solve(
    pairs.map(([f, t]) => [-f, -1, f * t]),
    pairs.map(([, t]) => -t),
  );
  return [solved[0]!, solved[1]!, solved[2]!];
}

function intersectPolyline(points: Pt[], line: [Pt, Pt]): Pt {
  const [o, e] = line;
  const nx = -(e[1] - o[1]);
  const ny = e[0] - o[0];
  const dots = points.map(([x, y]) => (x - o[0]) * nx + (y - o[1]) * ny);
  const front = dots[0]!;
  const back = dots[dots.length - 1]!;
  if (front * back > 0) {
    const end = Math.abs(front) < Math.abs(back) ? points[0]! : points[points.length - 1]!;
    return projectPoint(line, end);
  }
  let left = 0;
  let right = points.length - 1;
  let leftDot = dots[left]!;
  while (left + 1 < right) {
    const mid = (left + right) >> 1;
    if (dots[mid]! * leftDot <= 0) right = mid;
    else {
      left = mid;
      leftDot = dots[mid]!;
    }
  }
  const seg: [Pt, Pt] = [points[left]!, points[left + 1]!];
  return (
    lineIntersection(line, seg) ?? [0.5 * (seg[0][0] + seg[1][0]), 0.5 * (seg[0][1] + seg[1][1])]
  );
}

function interp(value: number, src: number[], dst: number[]): number {
  if (src.length === 0) return 0;
  if (src.length === 1) return dst[0]!;
  let segment: number;
  if (value < src[0]!) segment = 0;
  else if (value > src[src.length - 1]!) segment = src.length - 2;
  else {
    let left = 0;
    let right = src.length - 1;
    while (left + 1 < right) {
      const mid = (left + right) >> 1;
      if ((value - src[mid]!) * (value - src[left]!) <= 0) right = mid;
      else left = mid;
    }
    segment = left;
  }
  const s0 = src[segment]!;
  const s1 = src[segment + 1]!;
  return dst[segment]! + ((value - s0) * (dst[segment + 1]! - dst[segment]!)) / (s1 - s0);
}

// --- the dewarper --------------------------------------------------------------------------

export class CylindricalDewarper {
  private readonly pln2img: Mat3;
  private readonly img2pln: Mat3;
  readonly straightLineY: number;
  private readonly xs: number[] = [];
  private arcs: number[] = [];
  readonly directrixArcLength: number;

  constructor(
    private readonly top: Pt[],
    private readonly bottom: Pt[],
    private readonly depth: number,
  ) {
    if (top.length < 2 || bottom.length < 2) throw new Error("each curve needs two points");
    this.pln2img = homography([
      [[0, 0], top[0]!],
      [[1, 0], top[top.length - 1]!],
      [[0, 1], bottom[0]!],
      [[1, 1], bottom[bottom.length - 1]!],
    ]);
    this.img2pln = invert(this.pln2img);
    const coupled = this.coupled();
    this.straightLineY = this.calcStraightLineY(coupled);
    let prevX = -Infinity;
    let prevFx = 0;
    for (const [img1, img2, plnX] of coupled) {
      if (plnX <= prevX) continue;
      const g: [Pt, Pt] = [img1, img2];
      const y1 = projectionScalar(g, applyH(this.pln2img, [plnX, 0]));
      const y2 = projectionScalar(g, applyH(this.pln2img, [plnX, 1]));
      const elevation = Math.min(0.5, Math.max(-0.5, this.depth * (1 - (y2 - y1))));
      const arc =
        this.xs.length === 0
          ? 0
          : this.arcs[this.arcs.length - 1]! + Math.hypot(plnX - this.xs[this.xs.length - 1]!, elevation - prevFx);
      this.xs.push(plnX);
      this.arcs.push(arc);
      prevFx = elevation;
      prevX = plnX;
    }
    this.directrixArcLength = this.arcs.length >= 2 ? this.arcs[this.arcs.length - 1]! : 0;
    if (this.arcs.length > 1 && this.directrixArcLength !== 0) {
      const scale = 1 / this.directrixArcLength;
      this.arcs = this.arcs.map((a) => a * scale);
    }
  }

  private coupled(): [Pt, Pt, number][] {
    const s1 = this.top;
    const s2 = this.bottom;
    let i1 = 0;
    let i2 = 0;
    let prev1 = s1[0]!;
    let next1 = s1[0]!;
    let prev2 = s2[0]!;
    let next2 = s2[0]!;
    let nx1 = 0;
    let nx2 = 0;
    const out: [Pt, Pt, number][] = [];
    const advance1 = (): void => {
      i1 += 1;
      if (i1 === s1.length) return;
      prev1 = next1;
      next1 = s1[i1]!;
      nx1 = applyH(this.img2pln, next1)[0];
    };
    const advance2 = (): void => {
      i2 += 1;
      if (i2 === s2.length) return;
      prev2 = next2;
      next2 = s2[i2]!;
      nx2 = applyH(this.img2pln, next2)[0];
    };
    for (;;) {
      if (nx1 < nx2 && i1 !== s1.length) {
        const pln = applyH(this.img2pln, next1);
        const img1 = next1;
        const other = applyH(this.pln2img, [pln[0], pln[1] + 1]);
        const img2 = lineIntersection([img1, other], [next2, prev2]) ?? next2;
        out.push([img1, img2, pln[0]]);
        advance1();
        if (i2 !== s2.length && (next2[0] - img2[0]) ** 2 + (next2[1] - img2[1]) ** 2 < 1) advance2();
      } else if (i2 !== s2.length) {
        const pln = applyH(this.img2pln, next2);
        const img2 = next2;
        const other = applyH(this.pln2img, [pln[0], pln[1] + 1]);
        const img1 = lineIntersection([img2, other], [next1, prev1]) ?? next1;
        out.push([img1, img2, pln[0]]);
        advance2();
        if (i1 !== s1.length && (next1[0] - img1[0]) ** 2 + (next1[1] - img1[1]) ** 2 < 1) advance1();
      } else return out;
    }
  }

  private calcStraightLineY(coupled: [Pt, Pt, number][]): number {
    let accum = 0;
    let weights = 0;
    for (const [img1, img2, plnX] of coupled) {
      const g: [Pt, Pt] = [img1, img2];
      const p2 = projectionScalar(g, applyH(this.pln2img, [plnX, 0]));
      const p3 = projectionScalar(g, applyH(this.pln2img, [plnX, 1]));
      const dp1 = p2;
      const dp2 = 1 - p3;
      const weight = Math.abs(dp1 + dp2);
      if (weight < 0.01) continue;
      const p0 = (p3 * dp1 + p2 * dp2) / (dp1 + dp2);
      const pt: Pt = [img1[0] + (img2[0] - img1[0]) * p0, img1[1] + (img2[1] - img1[1]) * p0];
      accum += applyH(this.img2pln, pt)[1] * weight;
      weights += weight;
    }
    return weights === 0 ? 0.5 : accum / weights;
  }

  /** `mapGeneratrix`: the image line at unrolled x and its 1-D homography. */
  generatrix(crvX: number): { p1: Pt; p2: Pt; h: [number, number, number] } {
    const plnX = interp(crvX, this.arcs, this.xs);
    const line: [Pt, Pt] = [applyH(this.pln2img, [plnX, 0]), applyH(this.pln2img, [plnX, 1])];
    const topProj = projectionScalar(line, intersectPolyline(this.top, line));
    const bottomProj = projectionScalar(line, intersectPolyline(this.bottom, line));
    const straight = projectionScalar(line, applyH(this.pln2img, [plnX, this.straightLineY]));
    const y = this.straightLineY;
    const pairs: [number, number][] =
      Math.abs(y) < 0.05 || Math.abs(y - 1) < 0.05
        ? [
            [0, topProj],
            [1, bottomProj],
            [0.5, 0.5 * (topProj + bottomProj)],
          ]
        : [
            [0, topProj],
            [1, bottomProj],
            [y, straight],
          ];
    return { p1: line[0], p2: line[1], h: threePoint1D(pairs) };
  }

  mapToWarped([crvX, crvY]: Pt): Pt {
    const g = this.generatrix(crvX);
    const [a, b, c] = g.h;
    const t = (crvY * a + b) / (crvY * c + 1);
    return [g.p1[0] + (g.p2[0] - g.p1[0]) * t, g.p1[1] + (g.p2[1] - g.p1[1]) * t];
  }
}

/** `DistortionModel::isValid`. */
export function modelIsValid(top: Pt[], bottom: Pt[]): boolean {
  for (const curve of [top, bottom]) {
    if (curve.length < 2) return false;
    const a = curve[0]!;
    const b = curve[curve.length - 1]!;
    if (a[0] === b[0] && a[1] === b[1]) return false;
  }
  const poly = [top[0]!, top[top.length - 1]!, bottom[bottom.length - 1]!, bottom[0]!];
  const dots = poly.map((cur, i) => {
    const prev = poly[(i + 3) & 3]!;
    const next = poly[(i + 1) & 3]!;
    const nx = -(cur[1] - prev[1]);
    const ny = cur[0] - prev[0];
    return nx * (next[0] - cur[0]) + ny * (next[1] - cur[1]);
  });
  const low = Math.min(...dots);
  const high = Math.max(...dots);
  if (low * high <= 0) return false;
  return !(Math.abs(low) < 0.01 || Math.abs(high) < 0.01);
}

/** Grid lines of the model (upstream draws 30 by 30), in image coordinates. */
export function modelGrid(dewarper: CylindricalDewarper, lines = 30): { vertical: Pt[][]; horizontal: Pt[][] } {
  const vertical: Pt[][] = [];
  const horizontal: Pt[][] = Array.from({ length: lines }, () => []);
  for (let j = 0; j < lines; j += 1) {
    const x = j / (lines - 1);
    const g = dewarper.generatrix(x);
    const [a, b, c] = g.h;
    const at = (y: number): Pt => {
      const t = (y * a + b) / (y * c + 1);
      return [g.p1[0] + (g.p2[0] - g.p1[0]) * t, g.p1[1] + (g.p2[1] - g.p1[1]) * t];
    };
    vertical.push([at(0), at(1)]);
    for (let i = 0; i < lines; i += 1) horizontal[i]!.push(at(i / (lines - 1)));
  }
  return { vertical, horizontal };
}

/** Index to insert a control point at spline parameter `t` (upstream: segment + 1). */
export function insertionIndex(spline: XSpline, t: number): number {
  return Math.min(spline.numSegments, Math.floor(t * spline.numSegments)) + 1;
}

/** The spline parameter of the polyline point nearest to `p`, and its distance. */
export function nearestOnSpline(spline: XSpline, p: Pt, samples = 200): { t: number; dist: number } {
  let best = { t: 0, dist: Infinity };
  for (let i = 0; i <= samples; i += 1) {
    const t = i / samples;
    const q = spline.pointAt(t);
    const dist = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (dist < best.dist) best = { t, dist };
  }
  return best;
}

export function describeDewarp(parameters: Record<string, unknown>): string {
  const mode = String(parameters.dewarp_mode ?? "auto");
  const depth = Number(parameters.depth_perception ?? 2);
  const warning = typeof parameters.dewarp_warning === "string" ? parameters.dewarp_warning : "";
  if (mode === "off") return "Dewarping is off: the page is unchanged.";
  if (warning) return warning;
  const applied = parameters.dewarp_applied === true;
  return applied
    ? `Dewarped (${mode}, depth perception ${depth.toFixed(1)}).`
    : `No change (${mode}).`;
}

// --- the interactive overlay ----------------------------------------------------------------

export interface DewarpCallbacks {
  /** Curves changed during a drag (preview only). */
  onPreview(top: XSpline, bottom: XSpline): void;
  /** A drag or an edit finished: commit the curves. */
  onCommit(top: XSpline, bottom: XSpline): void;
}

const SVG = "http://www.w3.org/2000/svg";

function svg<K extends keyof SVGElementTagNameMap>(name: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function pathOf(points: Pt[], sx: number, sy: number): string {
  return points.map(([x, y], i) => `${i ? "L" : "M"} ${(x * sx).toFixed(2)} ${(y * sy).toFixed(2)}`).join(" ");
}

export class DewarpOverlay {
  readonly element: SVGSVGElement;
  private readonly grid: SVGPathElement;
  private readonly margins: SVGPathElement;
  private readonly points: SVGGElement;
  /** Invisible wide strokes along both curves: clicking them inserts a point. */
  private readonly hit: SVGPathElement;
  private source: { width: number; height: number } = { width: 1, height: 1 };
  private display: { width: number; height: number } = { width: 1, height: 1 };
  private drag: { curve: 0 | 1; index: number; pointer: number } | null = null;
  private hovered: { curve: 0 | 1; index: number } | null = null;
  depth = 2;

  constructor(
    host: HTMLElement,
    public top: XSpline,
    public bottom: XSpline,
    private readonly editable: boolean,
    private readonly callbacks: DewarpCallbacks,
  ) {
    this.element = svg("svg", { class: "st-dewarp-overlay" });
    this.element.dataset.dewarpOverlay = "";
    this.margins = svg("path", { class: "st-dewarp-margins" });
    this.grid = svg("path", { class: "st-dewarp-grid" });
    this.points = svg("g", { class: "st-dewarp-points" });
    this.hit = svg("path", { class: "st-dewarp-hit" });
    this.element.append(this.margins, this.grid, this.hit, this.points);
    if (editable) {
      this.hit.dataset.capturesPointer = "true";
      this.hit.dataset.dewarpCurve = "";
      this.element.addEventListener("pointerdown", (event) => this.onCurvePointerDown(event));
      document.addEventListener("keydown", this.onKey);
    }
    host.append(this.element);
  }

  isDragging(): boolean {
    return this.drag !== null;
  }

  setCurves(top: XSpline, bottom: XSpline, depth: number): void {
    if (this.drag) return;
    this.top = top;
    this.bottom = bottom;
    this.depth = depth;
    this.draw();
  }

  layout(source: { width: number; height: number }, display: { width: number; height: number }): void {
    this.source = source;
    this.display = display;
    this.element.setAttribute("width", String(display.width));
    this.element.setAttribute("height", String(display.height));
    this.draw();
  }

  private scale(): [number, number] {
    return [this.display.width / Math.max(1, this.source.width), this.display.height / Math.max(1, this.source.height)];
  }

  draw(): void {
    const [sx, sy] = this.scale();
    const top = this.top.toPolyline();
    const bottom = this.bottom.toPolyline();
    let gridPath = "";
    if (modelIsValid(top, bottom)) {
      try {
        const grid = modelGrid(new CylindricalDewarper(top, bottom, this.depth));
        gridPath = [...grid.vertical, ...grid.horizontal].map((line) => pathOf(line, sx, sy)).join(" ");
      } catch {
        gridPath = "";
      }
    }
    if (!gridPath) {
      // Just the frame, as upstream draws an invalid model.
      gridPath = [top, bottom, [top[0]!, bottom[0]!], [top[top.length - 1]!, bottom[bottom.length - 1]!]]
        .map((line) => pathOf(line, sx, sy))
        .join(" ");
    }
    this.grid.setAttribute("d", gridPath);
    this.margins.setAttribute("d", this.marginPath(top, bottom, sx, sy));
    this.hit.setAttribute("d", `${pathOf(top, sx, sy)} ${pathOf(bottom, sx, sy)}`);
    this.points.replaceChildren();
    ([this.top, this.bottom] as const).forEach((spline, curve) => {
      spline.points.forEach(([x, y], index) => {
        const dot = svg("circle", { cx: x * sx, cy: y * sy, r: 5, class: "st-dewarp-point" });
        if (this.editable) {
          dot.dataset.capturesPointer = "true";
          dot.dataset.dewarpPoint = `${curve}:${index}`;
          const title = svg("title");
          const end = index === 0 || index === spline.points.length - 1;
          title.textContent = end ? "This point can be dragged." : "Drag this point or delete it by pressing Del or D.";
          dot.append(title);
          dot.addEventListener("pointerdown", (event) => this.startDrag(curve as 0 | 1, index, event));
          dot.addEventListener("pointerenter", () => (this.hovered = { curve: curve as 0 | 1, index }));
          dot.addEventListener("pointerleave", () => (this.hovered = null));
        }
        this.points.append(dot);
      });
    });
  }

  /** `virtMarginArea`: translucent white beyond the model's left and right edges. */
  private marginPath(top: Pt[], bottom: Pt[], sx: number, sy: number): string {
    const h = this.source.height;
    const w = this.source.width;
    const extend = (a: Pt, b: Pt, y: number): number => (b[1] === a[1] ? a[0] : a[0] + ((y - a[1]) * (b[0] - a[0])) / (b[1] - a[1]));
    const lt = top[0]!;
    const lb = bottom[0]!;
    const rt = top[top.length - 1]!;
    const rb = bottom[bottom.length - 1]!;
    const left: Pt[] = [[0, 0], [extend(lt, lb, 0), 0], [extend(lt, lb, h), h], [0, h]];
    const right: Pt[] = [[w, 0], [extend(rt, rb, 0), 0], [extend(rt, rb, h), h], [w, h]];
    return `${pathOf(left, sx, sy)} Z ${pathOf(right, sx, sy)} Z`;
  }

  private toSource(event: PointerEvent): Pt {
    const bounds = this.element.getBoundingClientRect();
    const [sx, sy] = this.scale();
    return [(event.clientX - bounds.left) / sx, (event.clientY - bounds.top) / sy];
  }

  private startDrag(curve: 0 | 1, index: number, event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    this.drag = { curve, index, pointer: event.pointerId };
    window.addEventListener("pointermove", this.onMove);
    window.addEventListener("pointerup", this.onEnd);
    window.addEventListener("pointercancel", this.onEnd);
  }

  /** Upstream: a click on a curve inserts a control point there and drags it. */
  private onCurvePointerDown(event: PointerEvent): void {
    if (event.button !== 0 || this.drag) return;
    const target = event.target as Element | null;
    if (target?.closest("[data-dewarp-point]")) return;
    const p = this.toSource(event);
    const [sx] = this.scale();
    const tolerance = 8 / Math.max(sx, 1e-6);
    const candidates = ([this.top, this.bottom] as const).map((spline, curve) => ({
      curve: curve as 0 | 1,
      spline,
      ...nearestOnSpline(spline, p),
    }));
    const best = candidates.sort((a, b) => a.dist - b.dist)[0]!;
    if (best.dist > tolerance) return;
    const index = insertionIndex(best.spline, best.t);
    best.spline.points.splice(index, 0, best.spline.pointAt(best.t));
    best.spline.tensions.splice(index, 0, 1);
    this.draw();
    this.startDrag(best.curve, index, event);
  }

  private readonly onMove = (event: PointerEvent): void => {
    const drag = this.drag;
    if (!drag || drag.pointer !== event.pointerId) return;
    event.preventDefault();
    const spline = drag.curve === 0 ? this.top : this.bottom;
    spline.moveControlPoint(drag.index, this.toSource(event));
    this.draw();
    this.callbacks.onPreview(this.top, this.bottom);
  };

  private readonly onEnd = (event: PointerEvent): void => {
    const drag = this.drag;
    if (!drag || drag.pointer !== event.pointerId) return;
    this.drag = null;
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onEnd);
    window.removeEventListener("pointercancel", this.onEnd);
    this.callbacks.onCommit(this.top, this.bottom);
  };

  /** Del or D removes the hovered middle point (not the end points). */
  private readonly onKey = (event: KeyboardEvent): void => {
    if (event.key !== "Delete" && event.key !== "d" && event.key !== "D") return;
    const target = event.target;
    if (target instanceof Element && target.closest("input, select, textarea")) return;
    const hovered = this.hovered;
    if (!hovered || this.drag) return;
    const spline = hovered.curve === 0 ? this.top : this.bottom;
    if (hovered.index <= 0 || hovered.index >= spline.points.length - 1) return;
    spline.points.splice(hovered.index, 1);
    spline.tensions.splice(hovered.index, 1);
    this.hovered = null;
    event.preventDefault();
    this.draw();
    this.callbacks.onCommit(this.top, this.bottom);
  };

  remove(): void {
    window.removeEventListener("pointermove", this.onMove);
    window.removeEventListener("pointerup", this.onEnd);
    window.removeEventListener("pointercancel", this.onEnd);
    document.removeEventListener("keydown", this.onKey);
    this.element.remove();
  }
}
