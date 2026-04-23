import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { writeFileSync, mkdirSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPORT_DIR = join(HERE, "..", "a11y-reports");
mkdirSync(REPORT_DIR, { recursive: true });

const PUBLIC_ROUTES: Array<{ path: string; name: string }> = [
  { path: "/", name: "marketing" },
  { path: "/pricing", name: "pricing" },
  { path: "/about", name: "about" },
  { path: "/leaderboard", name: "leaderboard" },
  { path: "/health", name: "health" },
  { path: "/sign-in", name: "sign-in" },
];

async function runAxe(page: Page, name: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"])
    .analyze();
  const reportPath = join(REPORT_DIR, `${name}.json`);
  writeFileSync(reportPath, JSON.stringify(results, null, 2));
  const seriousOrCritical = results.violations.filter((v) =>
    ["serious", "critical"].includes(v.impact ?? ""),
  );
  // Print compact summary so the test runner output is the audit log.
  const summary = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes.length,
    help: v.help,
  }));
  console.log(
    `\n[a11y:${name}] ${results.violations.length} violations (${seriousOrCritical.length} serious/critical):\n` +
      JSON.stringify(summary, null, 2),
  );
  return { results, seriousOrCritical };
}

test.describe("axe accessibility audit — public routes", () => {
  for (const { path, name } of PUBLIC_ROUTES) {
    test(`${path} (${name}) has no serious/critical axe violations`, async ({
      page,
    }) => {
      await page.goto(path);
      // Give Clerk widget / lazy chunks a beat to mount.
      await page.waitForLoadState("networkidle").catch(() => {});
      await page.waitForTimeout(750);
      const { seriousOrCritical } = await runAxe(page, name);
      // Soft-fail: we record everything but only fail on serious/critical
      // so the audit can complete + the report is generated even when
      // earlier routes have findings to address.
      expect(
        seriousOrCritical,
        `serious/critical violations: ${seriousOrCritical
          .map((v) => v.id)
          .join(", ")}`,
      ).toEqual([]);
    });
  }
});
