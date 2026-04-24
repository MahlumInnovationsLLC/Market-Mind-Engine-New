#!/usr/bin/env node
/**
 * Render a coverage delta markdown table from one or two Istanbul-format
 * `coverage-summary.json` files and either print to stdout or write to
 * a path given via `--out`.
 *
 * Usage:
 *   node post-coverage-comment.mjs \
 *       --name "Web (vitest)" \
 *       --marker "<!-- web-vitest-coverage-comment -->" \
 *       --summary path/to/coverage-summary.json \
 *       [--baseline path/to/main-coverage-summary.json] \
 *       [--report-url https://...] \
 *       [--run-url https://...] \
 *       [--out path/to/comment.md]
 *
 * The script intentionally has zero runtime deps: the CI commenter step
 * uses actions/github-script to publish the rendered comment, so all
 * this needs to do is emit clean markdown that the action will sticky-
 * upsert on the PR.
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { argv } from "node:process";

function parseArgs(rawArgs) {
  const out = {};
  for (let i = 0; i < rawArgs.length; i += 1) {
    const flag = rawArgs[i];
    if (!flag.startsWith("--")) continue;
    const key = flag.slice(2);
    const next = rawArgs[i + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      i += 1;
    }
  }
  return out;
}

function loadSummary(path) {
  if (!path || !existsSync(path)) return null;
  try {
    const raw = readFileSync(path, "utf8");
    const parsed = JSON.parse(raw);
    return parsed && parsed.total ? parsed.total : null;
  } catch (err) {
    console.error(`Failed to read coverage summary at ${path}: ${err.message}`);
    return null;
  }
}

function fmtPct(n) {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  return `${n.toFixed(2)}%`;
}

function fmtDelta(curr, base) {
  if (typeof curr !== "number" || typeof base !== "number") return "";
  const delta = curr - base;
  if (Math.abs(delta) < 0.005) return " (±0.00)";
  const sign = delta > 0 ? "+" : "";
  const arrow = delta > 0 ? " 🟢" : " 🔴";
  return ` (${sign}${delta.toFixed(2)}${arrow})`;
}

function fmtRow(label, currMetric, baseMetric) {
  const curr = currMetric ? currMetric.pct : null;
  const base = baseMetric ? baseMetric.pct : null;
  const covered = currMetric ? currMetric.covered : 0;
  const total = currMetric ? currMetric.total : 0;
  return `| ${label} | ${fmtPct(curr)}${fmtDelta(curr, base)} | ${covered} / ${total} |`;
}

function render(opts) {
  const { name, marker, summary, baseline, reportUrl, runUrl } = opts;
  const lines = [marker, `### ${name} coverage`, ""];

  if (!summary) {
    lines.push(
      "_No `coverage-summary.json` was produced — the test run may have failed before reporters ran. Check the workflow logs._",
    );
    if (runUrl) lines.push("", `**Workflow run:** ${runUrl}`);
    return lines.join("\n");
  }

  lines.push("| Metric | This PR | Covered / Total |");
  lines.push("| --- | --- | --- |");
  lines.push(fmtRow("Lines", summary.lines, baseline?.lines));
  lines.push(fmtRow("Statements", summary.statements, baseline?.statements));
  lines.push(fmtRow("Branches", summary.branches, baseline?.branches));
  lines.push(fmtRow("Functions", summary.functions, baseline?.functions));
  lines.push("");

  if (baseline) {
    lines.push("_Delta is vs. the latest committed baseline on `main`._");
  } else {
    lines.push(
      "_No baseline available yet — the next push to `main` will seed one and future PRs will show a delta._",
    );
  }

  if (reportUrl) {
    lines.push("", `**HTML report:** ${reportUrl}`);
  }
  if (runUrl) {
    lines.push(`**Workflow run:** ${runUrl}`);
  }
  return lines.join("\n");
}

function main() {
  const args = parseArgs(argv.slice(2));
  if (!args.marker || !args.name) {
    console.error("Missing required --marker or --name argument.");
    process.exit(2);
  }

  const summary = loadSummary(args.summary);
  const baseline = loadSummary(args.baseline);

  const body = render({
    name: args.name,
    marker: args.marker,
    summary,
    baseline,
    reportUrl: args["report-url"] || "",
    runUrl: args["run-url"] || "",
  });

  if (args.out) {
    writeFileSync(args.out, body);
  } else {
    process.stdout.write(body + "\n");
  }
}

main();
