import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

// pdf.js runs its parser in a module worker; ship the prebuilt worker byte-for-byte
// next to the bundles. Django serves it from static files, so it keeps a .js name.
function copyPdfWorker(): Plugin {
  return {
    name: "copy-pdfjs-worker",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "pdf.worker.min.js",
        source: readFileSync(
          resolve(projectRoot, "node_modules/pdfjs-dist/build/pdf.worker.min.mjs"),
        ),
      });
    },
  };
}

export default defineConfig({
  plugins: [copyPdfWorker()],
  build: {
    lib: {
      entry: {
        "scantailor-workspace": resolve(projectRoot, "frontend/scantailor-workspace.ts"),
        "review-reader": resolve(projectRoot, "frontend/review-reader.ts"),
      },
      formats: ["es"],
      fileName: (_format, entryName) => `${entryName}.js`,
    },
    outDir: resolve(projectRoot, "src/ebook_server/core/static/core/dist"),
    emptyOutDir: true,
    sourcemap: true,
    target: "es2022",
  },
  test: {
    environment: "jsdom",
    include: ["frontend/**/*.test.ts"],
  },
});
