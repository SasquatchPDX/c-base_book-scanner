import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  build: {
    lib: {
      entry: resolve(projectRoot, "frontend/scantailor-workspace.ts"),
      formats: ["es"],
      fileName: () => "scantailor-workspace.js",
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
