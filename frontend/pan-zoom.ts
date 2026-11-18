/**
 * Pan and zoom of the editor canvas (#195 step 3).
 *
 * Zoom sets the image's layout size (``natural * zoom``) and pan is a translation,
 * so layout pixels stay screen pixels: geometry overlays and their drag handlers
 * keep working without knowing about the zoom.
 */

export interface View {
  zoom: number;
  x: number;
  y: number;
}

export interface Extent {
  width: number;
  height: number;
}

export const MIN_ZOOM = 0.05;
export const MAX_ZOOM = 8;
/** One wheel notch (deltaY 100) zooms by this factor. */
export const WHEEL_STEP = 1.2;

export function clampZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return 1;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/** The view that shows the whole image centred in the viewport. */
export function fitView(viewport: Extent, image: Extent, margin = 16): View {
  const width = Math.max(1, image.width);
  const height = Math.max(1, image.height);
  const zoom = clampZoom(
    Math.min(
      Math.max(1, viewport.width - 2 * margin) / width,
      Math.max(1, viewport.height - 2 * margin) / height,
    ),
  );
  return {
    zoom,
    x: (viewport.width - width * zoom) / 2,
    y: (viewport.height - height * zoom) / 2,
  };
}

/** Zoom by ``factor`` keeping the image point under ``anchor`` (viewport px) fixed. */
export function zoomAt(view: View, factor: number, anchor: { x: number; y: number }): View {
  const zoom = clampZoom(view.zoom * factor);
  const ratio = zoom / view.zoom;
  return {
    zoom,
    x: anchor.x - (anchor.x - view.x) * ratio,
    y: anchor.y - (anchor.y - view.y) * ratio,
  };
}

/** Zoom factor for a wheel event's vertical delta (pixels or lines). */
export function wheelFactor(deltaY: number, deltaMode = 0): number {
  const pixels = deltaMode === 1 ? deltaY * 33 : deltaMode === 2 ? deltaY * 400 : deltaY;
  return Math.pow(WHEEL_STEP, -pixels / 100);
}

export function panBy(view: View, dx: number, dy: number): View {
  return { zoom: view.zoom, x: view.x + dx, y: view.y + dy };
}
