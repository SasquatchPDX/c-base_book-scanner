import { afterEach, describe, expect, it, vi } from "vitest";

import {
  documentPartUrl,
  initReviewReaders,
  pageImageUrl,
  parsePreviewKind,
  ReviewReader,
  sourceFromElement,
  textPageUrl,
  unsupportedMessage,
  type ReaderDeps,
} from "./review-reader";

interface TabSpec {
  key: string;
  kind: string;
  format: string;
  bookUrl?: string;
  hint?: string;
}

function mountPanel(tabs: TabSpec[]): HTMLElement {
  const buttons = tabs
    .map(
      (tab) => `<button type="button" role="tab" aria-selected="false"
        data-reader-source="${tab.key}" data-kind="${tab.kind}" data-format="${tab.format}"
        data-filename="book.${tab.format}" data-size="1 KB"
        data-base-url="/reviews/7/reader/${tab.key}/"
        data-download-url="/reviews/7/reader/${tab.key}/file/?download=1"
        data-book-url="${tab.bookUrl ?? ""}" data-hint="${tab.hint ?? ""}">${tab.format}</button>`,
    )
    .join("");
  document.body.innerHTML = `
    <aside class="review-reader" data-review-reader data-pdf-worker="/static/core/dist/pdf.worker.min.js">
      <button type="button" data-reader-toggle aria-expanded="false">Read the book</button>
      <section data-reader-body hidden>
        <button type="button" data-reader-close>Close</button>
        <div role="tablist">${buttons}</div>
        <p data-reader-meta></p>
        <div data-reader-viewer></div>
      </section>
    </aside>`;
  return document.querySelector<HTMLElement>("[data-review-reader]")!;
}

function viewer(): HTMLElement {
  return document.querySelector<HTMLElement>("[data-reader-viewer]")!;
}

function stubDeps(responses: Record<string, unknown>): ReaderDeps & {
  fetchJson: ReturnType<typeof vi.fn>;
} {
  return {
    fetchJson: vi.fn(async (url: string) => {
      if (!(url in responses)) {
        throw new Error(`unexpected ${url}`);
      }
      return responses[url];
    }),
    renderPdf: vi.fn(async () => ({ pageCount: 3, destroy: vi.fn() })),
  };
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("reader source helpers", () => {
  it("reads tab data attributes and builds endpoint URLs", () => {
    mountPanel([{ key: "v3", kind: "document", format: "epub" }]);
    const source = sourceFromElement(document.querySelector<HTMLElement>("[data-reader-source]")!);

    expect(source.kind).toBe("document");
    expect(textPageUrl(source, 2)).toBe("/reviews/7/reader/v3/text/?page=2");
    expect(documentPartUrl(source, 4)).toBe("/reviews/7/reader/v3/document/?part=4");
    expect(pageImageUrl(source, 9)).toBe("/reviews/7/reader/v3/pages/9/");
  });

  it("treats unknown preview kinds as unsupported", () => {
    expect(parsePreviewKind("pdf")).toBe("pdf");
    expect(parsePreviewKind("javascript")).toBe("none");
    expect(parsePreviewKind(undefined)).toBe("none");
  });
});

describe("ReviewReader panel", () => {
  it("starts collapsed and folds out on toggle, loading the first file", async () => {
    const root = mountPanel([{ key: "import", kind: "text", format: "txt" }]);
    const deps = stubDeps({
      "/reviews/7/reader/import/text/?page=1": {
        text: "Chapter one",
        page: 1,
        page_count: 1,
        truncated: false,
        encoding: "utf-8",
      },
    });
    const reader = new ReviewReader(root, deps);
    const body = root.querySelector<HTMLElement>("[data-reader-body]")!;
    const toggle = root.querySelector<HTMLButtonElement>("[data-reader-toggle]")!;

    expect(body.hidden).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    toggle.click();
    await vi.waitFor(() => expect(viewer().querySelector("pre")?.textContent).toBe("Chapter one"));
    expect(body.hidden).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(reader.activeKey).toBe("import");

    root.querySelector<HTMLButtonElement>("[data-reader-close]")!.click();
    expect(body.hidden).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders text as text, never as markup, and pages through it", async () => {
    const root = mountPanel([{ key: "v1", kind: "text", format: "txt" }]);
    const deps = stubDeps({
      "/reviews/7/reader/v1/text/?page=1": {
        text: "<img src=x onerror=alert(1)>",
        page: 1,
        page_count: 2,
        truncated: false,
        encoding: "utf-8",
      },
      "/reviews/7/reader/v1/text/?page=2": {
        text: " end",
        page: 2,
        page_count: 2,
        truncated: true,
        encoding: "utf-8",
      },
    });
    const reader = new ReviewReader(root, deps);
    await reader.select("v1");

    const pre = viewer().querySelector("pre")!;
    expect(pre.querySelector("img")).toBeNull();
    expect(pre.textContent).toBe("<img src=x onerror=alert(1)>");

    const more = viewer().querySelector<HTMLButtonElement>(".review-reader-more")!;
    expect(more.hidden).toBe(false);
    more.click();
    await vi.waitFor(() => expect(pre.textContent).toBe("<img src=x onerror=alert(1)> end"));
    expect(more.hidden).toBe(true);
    expect(viewer().textContent).toContain("download the file for the complete text");
  });

  it("shows sanitized documents in a fully sandboxed frame with chapter navigation", async () => {
    const root = mountPanel([{ key: "v2", kind: "document", format: "epub" }]);
    const deps = stubDeps({
      "/reviews/7/reader/v2/parts/": {
        parts: [
          { index: 1, label: "cover.xhtml" },
          { index: 2, label: "chapter1.xhtml" },
        ],
      },
    });
    const reader = new ReviewReader(root, deps);
    await reader.select("v2");

    const frame = viewer().querySelector("iframe")!;
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("src")).toBe("/reviews/7/reader/v2/document/?part=1");

    const [previous, next] = Array.from(viewer().querySelectorAll<HTMLButtonElement>("button"));
    expect(previous?.disabled).toBe(true);
    next?.click();
    expect(frame.getAttribute("src")).toBe("/reviews/7/reader/v2/document/?part=2");
    expect(next?.disabled).toBe(true);
  });

  it("lists comic pages as lazily loaded images", async () => {
    const root = mountPanel([{ key: "v4", kind: "images", format: "cbz" }]);
    const deps = stubDeps({
      "/reviews/7/reader/v4/parts/": {
        parts: [
          { index: 1, label: "001.png" },
          { index: 2, label: "002.png" },
        ],
      },
    });
    await new ReviewReader(root, deps).select("v4");

    const images = Array.from(viewer().querySelectorAll("img"));
    expect(images.map((image) => image.getAttribute("src"))).toEqual([
      "/reviews/7/reader/v4/pages/1/",
      "/reviews/7/reader/v4/pages/2/",
    ]);
    expect(images.every((image) => image.loading === "lazy")).toBe(true);
  });

  it("hands PDFs to the pdf renderer with the configured worker", async () => {
    const root = mountPanel([{ key: "v5", kind: "pdf", format: "pdf" }]);
    const deps = stubDeps({});
    await new ReviewReader(root, deps).select("v5");

    expect(deps.renderPdf).toHaveBeenCalledWith(
      viewer(),
      "/reviews/7/reader/v5/file/",
      "/static/core/dist/pdf.worker.min.js",
    );
    expect(root.querySelector("[data-reader-meta]")?.textContent).toContain("3 pages");
  });

  it("explains unsupported formats and offers download and conversion links", async () => {
    const root = mountPanel([
      {
        key: "v6",
        kind: "none",
        format: "mobi",
        bookUrl: "/library/books/2/?view=html",
        hint: "Kindle/MOBI files cannot be rendered in the browser.",
      },
    ]);
    await new ReviewReader(root, stubDeps({})).select("v6");

    expect(viewer().textContent).toContain("Kindle/MOBI files cannot be rendered");
    const links = Array.from(viewer().querySelectorAll("a")).map((link) =>
      link.getAttribute("href"),
    );
    expect(links).toEqual(["/reviews/7/reader/v6/file/?download=1", "/library/books/2/?view=html"]);
    expect(unsupportedMessage(sourceFromElement(root.querySelector("[data-reader-source]")!))).toContain(
      "request a conversion",
    );
  });

  it("switches between formats and marks the selected tab", async () => {
    const root = mountPanel([
      { key: "v1", kind: "none", format: "djvu" },
      { key: "v2", kind: "image", format: "png" },
    ]);
    const reader = new ReviewReader(root, stubDeps({}));
    await reader.select("v1");
    await reader.select("v2");

    const tabs = Array.from(root.querySelectorAll("[data-reader-source]"));
    expect(tabs.map((tab) => tab.getAttribute("aria-selected"))).toEqual(["false", "true"]);
    expect(viewer().querySelector("img")?.getAttribute("src")).toBe("/reviews/7/reader/v2/file/");
  });

  it("ignores a slow response for a file the reviewer already left", async () => {
    const root = mountPanel([
      { key: "v1", kind: "text", format: "txt" },
      { key: "v2", kind: "none", format: "mobi" },
    ]);
    let release: (value: unknown) => void = () => undefined;
    const deps = stubDeps({});
    deps.fetchJson.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    const reader = new ReviewReader(root, deps);
    const pending = reader.select("v1");
    await reader.select("v2");
    release({ text: "stale", page: 1, page_count: 1, truncated: false, encoding: "utf-8" });
    await pending;

    expect(viewer().querySelector("pre")).toBeNull();
    expect(viewer().textContent).toContain("no in-browser preview for MOBI");
  });

  it("reports load failures inside the viewer", async () => {
    const root = mountPanel([{ key: "v3", kind: "document", format: "epub" }]);
    const deps = stubDeps({});
    deps.fetchJson.mockRejectedValue(new Error("The EPUB archive failed safety checks."));
    await new ReviewReader(root, deps).select("v3");

    expect(viewer().textContent).toContain("The EPUB archive failed safety checks.");
  });

  it("initializes each panel only once", () => {
    mountPanel([{ key: "v1", kind: "text", format: "txt" }]);
    expect(initReviewReaders()).toHaveLength(1);
    expect(initReviewReaders()).toHaveLength(0);
  });
});
