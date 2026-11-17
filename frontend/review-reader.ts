/**
 * Fold-out reader panel for the metadata review page (issue #6).
 *
 * The panel is collapsed by default. Each attached file is a tab; the viewer picks a
 * renderer by preview kind: pdf.js for PDF, server-decoded plain text, server-sanitized
 * HTML in a script-less sandboxed frame for EPUB/HTML/FB2, and page images for CBZ.
 */

export type PreviewKind = "pdf" | "text" | "document" | "images" | "image" | "none";

const PREVIEW_KINDS: readonly PreviewKind[] = ["pdf", "text", "document", "images", "image"];

export interface ReaderSource {
  key: string;
  kind: PreviewKind;
  format: string;
  filename: string;
  size: string;
  baseUrl: string;
  downloadUrl: string;
  bookUrl: string;
  hint: string;
}

export interface TextPagePayload {
  text: string;
  page: number;
  page_count: number;
  truncated: boolean;
  encoding: string;
}

export interface PartsPayload {
  parts: { index: number; label: string }[];
}

export interface PdfHandle {
  pageCount: number;
  destroy: () => void;
}

export interface ReaderDeps {
  fetchJson: (url: string) => Promise<unknown>;
  renderPdf: (container: HTMLElement, url: string, workerSrc: string) => Promise<PdfHandle>;
}

export function parsePreviewKind(value: string | undefined): PreviewKind {
  return PREVIEW_KINDS.find((kind) => kind === value) ?? "none";
}

export function sourceFromElement(element: HTMLElement): ReaderSource {
  const data = element.dataset;
  return {
    key: data.readerSource ?? "",
    kind: parsePreviewKind(data.kind),
    format: data.format ?? "",
    filename: data.filename ?? "",
    size: data.size ?? "",
    baseUrl: data.baseUrl ?? "",
    downloadUrl: data.downloadUrl ?? "",
    bookUrl: data.bookUrl ?? "",
    hint: data.hint ?? "",
  };
}

export function textPageUrl(source: ReaderSource, page: number): string {
  return `${source.baseUrl}text/?page=${page}`;
}

export function documentPartUrl(source: ReaderSource, part: number): string {
  return `${source.baseUrl}document/?part=${part}`;
}

export function pageImageUrl(source: ReaderSource, page: number): string {
  return `${source.baseUrl}pages/${page}/`;
}

export function fileUrl(source: ReaderSource): string {
  return `${source.baseUrl}file/`;
}

export function unsupportedMessage(source: ReaderSource): string {
  const format = source.format ? source.format.toUpperCase() : "this file";
  const reason = source.hint || `There is no in-browser preview for ${format}.`;
  return `${reason} Download the file to check it locally${
    source.bookUrl ? ", or request a conversion from the book page" : ""
  }.`;
}

async function defaultFetchJson(url: string): Promise<unknown> {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && "error" in payload
        ? String((payload as { error: unknown }).error)
        : `The preview could not be loaded (HTTP ${response.status}).`;
    throw new Error(message);
  }
  if (payload === null) {
    throw new Error("The preview response was not readable.");
  }
  return payload;
}

async function defaultRenderPdf(
  container: HTMLElement,
  url: string,
  workerSrc: string,
): Promise<PdfHandle> {
  const module = await import("./review-reader-pdf");
  return module.renderPdf(container, url, workerSrc);
}

function isTextPage(value: unknown): value is TextPagePayload {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as TextPagePayload).text === "string" &&
    typeof (value as TextPagePayload).page === "number" &&
    typeof (value as TextPagePayload).page_count === "number"
  );
}

function isParts(value: unknown): value is PartsPayload {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as PartsPayload).parts)
  );
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

export class ReviewReader {
  readonly root: HTMLElement;
  private readonly toggleButton: HTMLButtonElement | null;
  private readonly body: HTMLElement | null;
  private readonly viewer: HTMLElement | null;
  private readonly meta: HTMLElement | null;
  private readonly tabs: HTMLElement[];
  private readonly deps: ReaderDeps;
  private generation = 0;
  private pdfHandle: PdfHandle | null = null;
  activeKey: string | null = null;

  constructor(root: HTMLElement, deps: Partial<ReaderDeps> = {}) {
    this.root = root;
    this.deps = {
      fetchJson: deps.fetchJson ?? defaultFetchJson,
      renderPdf: deps.renderPdf ?? defaultRenderPdf,
    };
    this.toggleButton = root.querySelector<HTMLButtonElement>("[data-reader-toggle]");
    this.body = root.querySelector<HTMLElement>("[data-reader-body]");
    this.viewer = root.querySelector<HTMLElement>("[data-reader-viewer]");
    this.meta = root.querySelector<HTMLElement>("[data-reader-meta]");
    this.tabs = Array.from(root.querySelectorAll<HTMLElement>("[data-reader-source]"));

    this.toggleButton?.addEventListener("click", () => this.toggle());
    root
      .querySelector<HTMLButtonElement>("[data-reader-close]")
      ?.addEventListener("click", () => this.close());
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.isOpen) {
        this.close();
      }
    });
    for (const tab of this.tabs) {
      tab.addEventListener("click", () => {
        void this.select(tab.dataset.readerSource ?? "");
      });
    }
  }

  get isOpen(): boolean {
    return this.root.classList.contains("is-open");
  }

  open(): void {
    this.root.classList.add("is-open");
    if (this.body) {
      this.body.hidden = false;
    }
    this.toggleButton?.setAttribute("aria-expanded", "true");
    if (this.activeKey === null && this.tabs.length > 0) {
      void this.select(this.tabs[0]?.dataset.readerSource ?? "");
    }
    this.root.querySelector<HTMLElement>("[data-reader-close]")?.focus();
  }

  close(): void {
    this.root.classList.remove("is-open");
    if (this.body) {
      this.body.hidden = true;
    }
    this.toggleButton?.setAttribute("aria-expanded", "false");
    this.toggleButton?.focus();
  }

  toggle(): void {
    if (this.isOpen) {
      this.close();
    } else {
      this.open();
    }
  }

  async select(key: string): Promise<void> {
    const tab = this.tabs.find((candidate) => candidate.dataset.readerSource === key);
    if (!tab || !this.viewer) {
      return;
    }
    const source = sourceFromElement(tab);
    this.generation += 1;
    const generation = this.generation;
    this.activeKey = key;
    for (const candidate of this.tabs) {
      candidate.setAttribute("aria-selected", candidate === tab ? "true" : "false");
    }
    this.pdfHandle?.destroy();
    this.pdfHandle = null;
    this.viewer.classList.remove("has-frame");
    this.viewer.replaceChildren(element("p", "review-reader-message muted", "Loading…"));
    this.renderMeta(source);

    const isCurrent = (): boolean => generation === this.generation;
    try {
      await this.renderSource(source, isCurrent);
    } catch (error) {
      if (isCurrent()) {
        const reason = error instanceof Error ? error.message : String(error);
        this.showMessage(source, `The preview could not be shown: ${reason}`);
      }
    }
  }

  private renderMeta(source: ReaderSource): void {
    if (!this.meta) {
      return;
    }
    const parts = [source.filename, source.format.toUpperCase(), source.size].filter(Boolean);
    const download = element("a", undefined, "Download");
    download.href = source.downloadUrl;
    this.meta.replaceChildren(document.createTextNode(`${parts.join(" · ")} · `), download);
  }

  private showMessage(source: ReaderSource, message: string): void {
    if (!this.viewer) {
      return;
    }
    const box = element("div", "review-reader-message");
    box.append(element("p", undefined, message));
    const download = element("a", "button", "Download file");
    download.href = source.downloadUrl;
    box.append(download);
    if (source.bookUrl) {
      const book = element("a", undefined, "Open the book page (conversions)");
      book.href = source.bookUrl;
      box.append(book);
    }
    this.viewer.classList.remove("has-frame");
    this.viewer.replaceChildren(box);
  }

  private async renderSource(source: ReaderSource, isCurrent: () => boolean): Promise<void> {
    const viewer = this.viewer;
    if (!viewer) {
      return;
    }
    switch (source.kind) {
      case "text":
        await this.renderText(source, viewer, isCurrent);
        return;
      case "document":
        await this.renderDocument(source, viewer, isCurrent);
        return;
      case "images":
        await this.renderImages(source, viewer, isCurrent);
        return;
      case "image": {
        const list = element("div", "review-reader-pages");
        const image = element("img");
        image.src = fileUrl(source);
        image.alt = source.filename;
        list.append(image);
        viewer.replaceChildren(list);
        return;
      }
      case "pdf": {
        const holder = element("div");
        viewer.replaceChildren(holder);
        const workerSrc = this.root.dataset.pdfWorker ?? "";
        const handle = await this.deps.renderPdf(viewer, fileUrl(source), workerSrc);
        if (!isCurrent()) {
          handle.destroy();
          return;
        }
        holder.remove();
        this.pdfHandle = handle;
        if (this.meta) {
          this.meta.prepend(document.createTextNode(`${handle.pageCount} pages · `));
        }
        return;
      }
      default:
        this.showMessage(source, unsupportedMessage(source));
    }
  }

  private async renderText(
    source: ReaderSource,
    viewer: HTMLElement,
    isCurrent: () => boolean,
  ): Promise<void> {
    const pre = element("pre");
    const more = element("button", "secondary review-reader-more", "Load more");
    more.type = "button";
    const note = element("p", "review-reader-message muted");
    let nextPage = 1;

    const loadPage = async (): Promise<void> => {
      const payload = await this.deps.fetchJson(textPageUrl(source, nextPage));
      if (!isCurrent()) {
        return;
      }
      if (!isTextPage(payload)) {
        throw new Error("unexpected text response");
      }
      pre.append(document.createTextNode(payload.text));
      nextPage = payload.page + 1;
      more.hidden = payload.page >= payload.page_count;
      note.textContent =
        payload.truncated && payload.page >= payload.page_count
          ? "The preview stops here; download the file for the complete text."
          : "";
    };

    more.addEventListener("click", () => {
      more.disabled = true;
      loadPage()
        .catch((error: unknown) => {
          note.textContent = error instanceof Error ? error.message : String(error);
        })
        .finally(() => {
          more.disabled = false;
        });
    });
    await loadPage();
    if (isCurrent()) {
      viewer.replaceChildren(pre, more, note);
    }
  }

  private async fetchParts(source: ReaderSource): Promise<PartsPayload> {
    const payload = await this.deps.fetchJson(`${source.baseUrl}parts/`);
    if (!isParts(payload) || payload.parts.length === 0) {
      throw new Error("the file has no readable parts");
    }
    return payload;
  }

  private async renderDocument(
    source: ReaderSource,
    viewer: HTMLElement,
    isCurrent: () => boolean,
  ): Promise<void> {
    const { parts } = await this.fetchParts(source);
    if (!isCurrent()) {
      return;
    }
    const frame = element("iframe");
    // Empty sandbox: no scripts, no same-origin access, no forms, no navigation of the page.
    frame.setAttribute("sandbox", "");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.title = `Preview of ${source.filename}`;

    let current = 1;
    const select = element("select");
    select.setAttribute("aria-label", "Chapter");
    for (const part of parts) {
      const option = element("option", undefined, `${part.index}. ${part.label}`);
      option.value = String(part.index);
      select.append(option);
    }
    const previous = element("button", "secondary small-button", "Previous");
    const next = element("button", "secondary small-button", "Next");
    previous.type = "button";
    next.type = "button";
    const show = (index: number): void => {
      current = Math.min(Math.max(index, 1), parts.length);
      select.value = String(current);
      previous.disabled = current <= 1;
      next.disabled = current >= parts.length;
      frame.src = documentPartUrl(source, current);
    };
    select.addEventListener("change", () => show(Number(select.value)));
    previous.addEventListener("click", () => show(current - 1));
    next.addEventListener("click", () => show(current + 1));
    show(1);

    viewer.classList.add("has-frame");
    if (parts.length > 1) {
      const nav = element("div", "review-reader-nav");
      nav.append(previous, select, next);
      viewer.replaceChildren(nav, frame);
    } else {
      viewer.replaceChildren(frame);
    }
  }

  private async renderImages(
    source: ReaderSource,
    viewer: HTMLElement,
    isCurrent: () => boolean,
  ): Promise<void> {
    const { parts } = await this.fetchParts(source);
    if (!isCurrent()) {
      return;
    }
    const list = element("div", "review-reader-pages");
    for (const part of parts) {
      const image = element("img");
      image.loading = "lazy";
      image.decoding = "async";
      image.alt = `Page ${part.index}: ${part.label}`;
      image.src = pageImageUrl(source, part.index);
      list.append(image);
    }
    viewer.replaceChildren(list);
  }
}

export function initReviewReaders(
  scope: ParentNode = document,
  deps: Partial<ReaderDeps> = {},
): ReviewReader[] {
  return Array.from(scope.querySelectorAll<HTMLElement>("[data-review-reader]"))
    .filter((root) => root.dataset.readerReady !== "true")
    .map((root) => {
      root.dataset.readerReady = "true";
      return new ReviewReader(root, deps);
    });
}

// Module scripts run after parsing, so the server-rendered panel is already present.
initReviewReaders();
