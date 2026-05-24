#!/usr/bin/env python3
"""
Generate the bbrhtml.billyrichards.com index page.

Walks `reports/` for *.html files, parses their <title> (fallback to filename),
queries `gh pr list` for the most recently merged PRs, and writes a single
self-contained index.html into the output directory.

Designed to run on a GitHub Actions runner (or locally) — only requires
Python 3.10+ and the `gh` CLI on PATH (authenticated).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class Report:
    rel_path: str        # e.g. "scans2025/v9_3_btc_loss_investigation.html"
    title: str
    size_bytes: int
    mtime_utc: dt.datetime


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    merged_at: dt.datetime
    author: str
    summary: str
    url: str


def parse_title(path: Path) -> str:
    try:
        # Only sniff the first ~16KB; titles always live near the top.
        head = path.read_text(encoding="utf-8", errors="ignore")[:16_384]
    except OSError:
        return path.stem
    match = TITLE_RE.search(head)
    if not match:
        return path.stem
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or path.stem


def discover_reports(reports_dir: Path) -> list[Report]:
    reports: list[Report] = []
    for path in sorted(reports_dir.rglob("*.html")):
        if not path.is_file():
            continue
        stat = path.stat()
        reports.append(
            Report(
                rel_path=str(path.relative_to(reports_dir)),
                title=parse_title(path),
                size_bytes=stat.st_size,
                mtime_utc=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
            )
        )
    # Most-recent first.
    reports.sort(key=lambda r: r.mtime_utc, reverse=True)
    return reports


def fetch_pulls(repo: str, limit: int) -> list[PullRequest]:
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,mergedAt,author,body,url",
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    pulls: list[PullRequest] = []
    for item in json.loads(raw):
        body = item.get("body") or ""
        # First non-empty, non-heading line as a one-liner summary.
        summary = ""
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            summary = stripped
            break
        if len(summary) > 200:
            summary = summary[:197] + "..."
        pulls.append(
            PullRequest(
                number=item["number"],
                title=item["title"],
                merged_at=dt.datetime.fromisoformat(item["mergedAt"].replace("Z", "+00:00")),
                author=(item.get("author") or {}).get("login", "?"),
                summary=summary,
                url=item["url"],
            )
        )
    pulls.sort(key=lambda p: p.merged_at, reverse=True)
    return pulls


def human_size(n: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(n)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{n} B"


CSS = """
:root {
  --bg: #0c0f14;
  --bg-elev: #131822;
  --bg-row: #161c27;
  --border: #1f2735;
  --text: #d7dde6;
  --muted: #7c8696;
  --accent: #ffb74d;
  --accent-dim: #b07a2c;
  --link: #82c8ff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular,
               Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
}
header {
  padding: 28px 32px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elev);
}
header h1 {
  margin: 0;
  font-size: 18px;
  letter-spacing: 0.02em;
  color: var(--accent);
}
header .sub {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
}
main { padding: 24px 32px 64px; max-width: 1280px; }
section { margin-bottom: 40px; }
section h2 {
  font-size: 14px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--accent);
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
  margin-bottom: 12px;
}
.meta {
  color: var(--muted);
  font-size: 11px;
  margin-bottom: 12px;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--bg-elev);
  border: 1px solid var(--border);
}
th, td {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
th {
  background: #1a2030;
  color: var(--muted);
  font-weight: 500;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--bg-row); }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
.col-date { white-space: nowrap; color: var(--muted); width: 11ch; }
.col-size { white-space: nowrap; color: var(--muted); width: 9ch; text-align: right; }
.col-num  { white-space: nowrap; width: 6ch; }
.col-author { white-space: nowrap; color: var(--muted); width: 14ch; }
.col-link { white-space: nowrap; width: 12ch; text-align: right; }
.summary { color: var(--muted); font-size: 12px; }
footer {
  padding: 16px 32px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 11px;
}
.empty { color: var(--muted); padding: 12px; font-style: italic; }
"""


def render_reports_table(reports: list[Report]) -> str:
    if not reports:
        return '<div class="empty">No reports indexed.</div>'
    rows = []
    for r in reports:
        date = r.mtime_utc.strftime("%Y-%m-%d")
        size = human_size(r.size_bytes)
        rows.append(
            "<tr>"
            f'<td class="col-date">{date}</td>'
            f'<td class="col-size">{size}</td>'
            f"<td>{html.escape(r.title)}</td>"
            f'<td class="col-link"><a href="reports/{html.escape(r.rel_path)}" target="_blank" rel="noopener">open</a></td>'
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>Date</th><th>Size</th><th>Title</th><th></th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_prs_table(pulls: list[PullRequest]) -> str:
    if not pulls:
        return '<div class="empty">No recent merged PRs.</div>'
    rows = []
    for p in pulls:
        date = p.merged_at.strftime("%Y-%m-%d")
        rows.append(
            "<tr>"
            f'<td class="col-num">#{p.number}</td>'
            f'<td class="col-date">{date}</td>'
            f"<td>{html.escape(p.title)}"
            + (f'<div class="summary">{html.escape(p.summary)}</div>' if p.summary else "")
            + "</td>"
            f'<td class="col-author">{html.escape(p.author)}</td>'
            f'<td class="col-link"><a href="{html.escape(p.url)}" target="_blank" rel="noopener">GitHub</a></td>'
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr>"
        "<th>#</th><th>Merged</th><th>Title</th><th>Author</th><th></th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def render_page(reports: list[Report], pulls: list[PullRequest], repo: str) -> str:
    now = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pr_range = ""
    if pulls:
        oldest = pulls[-1].merged_at.strftime("%Y-%m-%d")
        newest = pulls[0].merged_at.strftime("%Y-%m-%d")
        pr_range = f"{oldest} → {newest}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BBR HTML index — billyrichards.com</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>BBR HTML index — billyrichards.com</h1>
  <div class="sub">{len(reports)} report(s) indexed · {len(pulls)} merged PR(s) listed</div>
</header>
<main>
  <section id="reports">
    <h2>Reports</h2>
    <div class="meta">HTML snapshots and notes — newest first.</div>
    {render_reports_table(reports)}
  </section>

  <section id="prs">
    <h2>Recent merged PRs — {html.escape(repo)}</h2>
    <div class="meta">{html.escape(pr_range)} · pulled live from GitHub on build.</div>
    {render_prs_table(pulls)}
  </section>
</main>
<footer>Generated {now}</footer>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reports", type=Path, required=True, help="Reports source directory")
    ap.add_argument("--output", type=Path, required=True, help="Output directory (will be created)")
    ap.add_argument("--repo", default="billybrichards/novakash")
    ap.add_argument("--pr-limit", type=int, default=20)
    args = ap.parse_args()

    if not args.reports.is_dir():
        print(f"error: reports dir not found: {args.reports}", file=sys.stderr)
        return 2

    reports = discover_reports(args.reports)
    try:
        pulls = fetch_pulls(args.repo, args.pr_limit)
    except subprocess.CalledProcessError as exc:
        print(f"warning: gh pr list failed: {exc.stderr}", file=sys.stderr)
        pulls = []

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "index.html").write_text(render_page(reports, pulls, args.repo), encoding="utf-8")

    # Mirror reports under output/reports/.
    out_reports = args.output / "reports"
    if out_reports.exists():
        # Idempotent: clear and recopy. Cheap (1MB).
        import shutil
        shutil.rmtree(out_reports)
    import shutil
    shutil.copytree(args.reports, out_reports)

    print(f"indexed {len(reports)} reports, {len(pulls)} PRs → {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
