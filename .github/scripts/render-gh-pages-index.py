#!/usr/bin/env python3
"""Generate ``index.html`` for the gh-pages branch.

The publisher in ``.github/workflows/web-e2e.yml`` writes Playwright HTML
reports under::

    pr-<number>/run-<runid>/        # for pull-request runs
    branch-<sanitized>/run-<runid>/ # for push / workflow_dispatch runs

Each ``run-<runid>/`` directory may also contain a ``_meta.json`` file with
extra metadata (publish timestamp, commit SHA, PR number, workflow run URL).

This script walks those directories and writes a single ``index.html`` at the
root of the working tree listing the most recent reports per pull request and
per branch, with timestamps and links so operators visiting the Pages root can
discover reports without relying on the sticky PR comment.

Usage::

    python3 .github/scripts/render-gh-pages-index.py <gh-pages-work-dir>
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RUN_DIR_RE = re.compile(r"^run-(\d+)$")


@dataclass
class RunEntry:
    parent_kind: str
    parent_label: str
    parent_dir: str
    run_id: int
    run_dir: str
    published_at: Optional[str] = None
    sha: Optional[str] = None
    ref: Optional[str] = None
    run_url: Optional[str] = None

    @property
    def href(self) -> str:
        return f"{self.parent_dir}/{self.run_dir}/"


@dataclass
class ParentGroup:
    kind: str
    label: str
    dir_name: str
    runs: list[RunEntry] = field(default_factory=list)

    @property
    def latest_run_id(self) -> int:
        return self.runs[0].run_id if self.runs else 0


def _load_meta(run_dir: Path) -> dict:
    meta_path = run_dir / "_meta.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def collect(root: Path) -> tuple[list[ParentGroup], list[ParentGroup]]:
    """Walk ``root`` and return (pr_groups, branch_groups) sorted newest first."""
    pr_groups: dict[str, ParentGroup] = {}
    branch_groups: dict[str, ParentGroup] = {}

    for parent in sorted(root.iterdir()):
        if not parent.is_dir():
            continue
        name = parent.name
        if name.startswith("pr-"):
            kind = "pr"
            tail = name[len("pr-") :]
            if not tail.isdigit():
                continue
            label = f"PR #{tail}"
            target = pr_groups
        elif name.startswith("branch-"):
            kind = "branch"
            label = name[len("branch-") :] or "(unnamed)"
            target = branch_groups
        else:
            continue

        group = ParentGroup(kind=kind, label=label, dir_name=name)

        for run_dir in parent.iterdir():
            if not run_dir.is_dir():
                continue
            match = RUN_DIR_RE.match(run_dir.name)
            if not match:
                continue
            run_id = int(match.group(1))
            meta = _load_meta(run_dir)
            group.runs.append(
                RunEntry(
                    parent_kind=kind,
                    parent_label=label,
                    parent_dir=name,
                    run_id=run_id,
                    run_dir=run_dir.name,
                    published_at=meta.get("published_at"),
                    sha=meta.get("sha"),
                    ref=meta.get("ref_name") or meta.get("ref"),
                    run_url=meta.get("run_url"),
                )
            )

        if not group.runs:
            continue
        group.runs.sort(key=lambda r: r.run_id, reverse=True)
        target[name] = group

    pr_list = sorted(
        pr_groups.values(), key=lambda g: g.latest_run_id, reverse=True
    )
    branch_list = sorted(
        branch_groups.values(), key=lambda g: g.latest_run_id, reverse=True
    )
    return pr_list, branch_list


def fmt_published(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return html.escape(str(value))


def fmt_sha(value: Optional[str]) -> str:
    if not value:
        return "—"
    return html.escape(str(value)[:7])


def _run_link(run: RunEntry) -> str:
    label = f"run-{run.run_id}"
    primary = f'<a href="{html.escape(run.href)}">{label}</a>'
    if run.run_url:
        primary += (
            f' <a class="muted" href="{html.escape(run.run_url)}"'
            ' title="GitHub Actions run">↗</a>'
        )
    return primary


def render_group(group: ParentGroup) -> str:
    rows = []
    for run in group.runs:
        rows.append(
            "<tr>"
            f"<td>{_run_link(run)}</td>"
            f"<td>{fmt_published(run.published_at)}</td>"
            f"<td><code>{fmt_sha(run.sha)}</code></td>"
            "</tr>"
        )
    table = (
        '<table class="runs"><thead><tr>'
        "<th>Run</th><th>Published</th><th>Commit</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    return (
        '<section class="group">'
        f"<h3>{html.escape(group.label)}"
        f' <span class="muted">({len(group.runs)} run'
        f"{'s' if len(group.runs) != 1 else ''})</span></h3>"
        f"{table}"
        "</section>"
    )


def render_section(title: str, groups: list[ParentGroup], empty_msg: str) -> str:
    total_runs = sum(len(g.runs) for g in groups)
    header = (
        f'<h2>{html.escape(title)} '
        f'<span class="muted">({len(groups)} '
        f"{'group' if len(groups) == 1 else 'groups'}, "
        f"{total_runs} run{'s' if total_runs != 1 else ''})</span></h2>"
    )
    if not groups:
        body = f'<p class="muted"><em>{html.escape(empty_msg)}</em></p>'
    else:
        body = "".join(render_group(g) for g in groups)
    return header + body


def render_html(
    pr_groups: list[ParentGroup], branch_groups: list[ParentGroup]
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Playwright reports</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1f2328;
  }}
  h1 {{ margin: 0; }}
  h2 {{ margin-top: 2.5rem; padding-bottom: 0.25rem;
       border-bottom: 1px solid #d0d7de; }}
  h3 {{ margin-bottom: 0.5rem; }}
  p.lead {{ color: #6e7781; margin-top: 0.25rem; }}
  .muted {{ color: #6e7781; font-weight: normal; font-size: 0.9em; }}
  table.runs {{ width: 100%; border-collapse: collapse; margin-bottom: 1rem; }}
  table.runs th, table.runs td {{
    text-align: left; padding: 0.4rem 0.6rem;
    border-bottom: 1px solid #eaeef2;
  }}
  table.runs th {{ background: #f6f8fa; font-weight: 600; }}
  table.runs tr:hover td {{ background: #f6f8fa; }}
  code {{ font-family: ui-monospace, SFMono-Regular, monospace;
          background: #eaeef2; padding: 0.05rem 0.35rem; border-radius: 4px; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  footer {{ margin-top: 3rem; padding-top: 1rem;
            border-top: 1px solid #d0d7de;
            color: #6e7781; font-size: 12px; }}
</style>
</head>
<body>
<h1>Playwright reports</h1>
<p class="lead">Most recent failed-run HTML reports, grouped by pull request and branch.</p>
{render_section("Pull requests", pr_groups, "No pull-request reports yet.")}
{render_section("Branches", branch_groups, "No branch reports yet.")}
<footer>
  Generated {generated_at}. Reports are written under
  <code>pr-&lt;number&gt;/run-&lt;id&gt;/</code> and
  <code>branch-&lt;name&gt;/run-&lt;id&gt;/</code> by
  <code>.github/workflows/web-e2e.yml</code>, and pruned by
  <code>.github/workflows/gh-pages-prune.yml</code>.
</footer>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        type=Path,
        help="Path to the gh-pages working directory.",
    )
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 1

    pr_groups, branch_groups = collect(args.root)
    output = args.root / "index.html"
    output.write_text(render_html(pr_groups, branch_groups), encoding="utf-8")
    pr_runs = sum(len(g.runs) for g in pr_groups)
    branch_runs = sum(len(g.runs) for g in branch_groups)
    print(
        f"Wrote {output} "
        f"({len(pr_groups)} PR group(s), {pr_runs} PR run(s); "
        f"{len(branch_groups)} branch group(s), {branch_runs} branch run(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
