import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "src"),
    },
    dedupe: ["react", "react-dom"],
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      // Pin reporters explicitly so the CI workflow can rely on the
      // `coverage/coverage-summary.json` artifact for the PR comment,
      // and humans can still open `coverage/index.html` locally.
      reporter: ["text", "html", "json", "json-summary"],
      reportsDirectory: "coverage",
      // Only count source code that ships with the app — exclude tests,
      // generated files, build artifacts, and shadcn UI primitives we
      // don't author tests for. This keeps the trend signal honest.
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/*.d.ts",
        "src/main.tsx",
        "src/components/ui/**",
      ],
      all: true,
      // Emit a summary even when coverage is empty so the CI commenter
      // can still post a (zero-percent) baseline instead of erroring.
      reportOnFailure: true,
    },
  },
});
