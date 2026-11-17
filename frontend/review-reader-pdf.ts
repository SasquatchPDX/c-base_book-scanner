// The prebuilt minified bundle keeps the committed dist (and its source map) small.
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist/build/pdf.min.mjs";

const MAX_PAGE_WIDTH = 900;
const MAX_PIXEL_RATIO = 2;

export interface PdfHandle {
  pageCount: number;
  destroy: () => void;
}

/** Render a PDF into ``container`` as a scrollable column of lazily drawn pages. */
export async function renderPdf(
  container: HTMLElement,
  url: string,
  workerSrc: string,
): Promise<PdfHandle> {
  GlobalWorkerOptions.workerSrc = workerSrc;
  const loadingTask = getDocument({
    url,
    enableXfa: false,
    disableAutoFetch: true,
    stopAtErrors: false,
  });
  let observer: IntersectionObserver | null = null;
  const destroy = (): void => {
    observer?.disconnect();
    void loadingTask.destroy();
  };

  try {
    const pdf = await loadingTask.promise;
    const firstPage = await pdf.getPage(1);
    const firstViewport = firstPage.getViewport({ scale: 1 });
    const list = document.createElement("div");
    list.className = "review-reader-pages";
    container.append(list);

    const rendered = new Set<number>();
    const renderPage = async (slot: HTMLElement, pageNumber: number): Promise<void> => {
      if (rendered.has(pageNumber)) {
        return;
      }
      rendered.add(pageNumber);
      const page = await pdf.getPage(pageNumber);
      const unscaled = page.getViewport({ scale: 1 });
      const cssWidth = Math.min(Math.max(list.clientWidth - 24, 200), MAX_PAGE_WIDTH);
      const ratio = Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO);
      const viewport = page.getViewport({ scale: (cssWidth / unscaled.width) * ratio });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.style.width = `${cssWidth}px`;
      canvas.setAttribute("aria-label", `Page ${pageNumber}`);
      await page.render({ canvas, viewport }).promise;
      slot.replaceChildren(canvas);
      page.cleanup();
    };

    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) {
            continue;
          }
          const slot = entry.target as HTMLElement;
          observer?.unobserve(slot);
          void renderPage(slot, Number(slot.dataset.page)).catch(() => {
            slot.textContent = `Page ${slot.dataset.page} could not be rendered.`;
          });
        }
      },
      { root: container, rootMargin: "600px 0px" },
    );

    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
      const slot = document.createElement("div");
      slot.className = "review-reader-page";
      slot.dataset.page = String(pageNumber);
      const placeholder = document.createElement("div");
      placeholder.className = "review-reader-page-placeholder";
      placeholder.style.aspectRatio = `${firstViewport.width} / ${firstViewport.height}`;
      slot.append(placeholder);
      list.append(slot);
      observer.observe(slot);
    }
    return { pageCount: pdf.numPages, destroy };
  } catch (error) {
    destroy();
    throw error;
  }
}
