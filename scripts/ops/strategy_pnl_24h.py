"""24h live-trade PnL report by strategy.

Single source of truth for the kind of summary we paste into Hub notes
and Telegram dumps.  Reads the engine's ``trades`` table in Montreal PG
and groups by ``strategy`` for rows where ``is_live=true``.

Usage (run on any box with DATABASE_URL pointing at the Montreal PG,
or inside the Montreal box itself):

    python3 scripts/ops/strategy_pnl_24h.py                 # last 24h
    python3 scripts/ops/strategy_pnl_24h.py --hours 6       # last 6h
    python3 scripts/ops/strategy_pnl_24h.py --include-paper # include paper trades
    python3 scripts/ops/strategy_pnl_24h.py --json          # JSON output
    python3 scripts/ops/strategy_pnl_24h.py --post-note     # also POST to Hub notes API

The query is intentionally identical to what the ad-hoc audits on
2026-04-15 used, so results are reproducible across sessions:

    SELECT strategy,
           count(*) AS n,
           sum(pnl_usd) AS pnl
    FROM trades
    WHERE created_at >= NOW() - INTERVAL '<hours> hours'
      AND is_live = true          -- omit when --include-paper
    GROUP BY strategy
    ORDER BY sum(pnl_usd) DESC

To post a fresh master-note entry:

    HUB_USER=billy HUB_PASS=... python3 scripts/ops/strategy_pnl_24h.py --post-note

The script authenticates against the Hub ( http://16.54.141.121:8091 ),
updates/creates note titled
"2026-MM-DD: Strategy Performance Snapshot — Master" and tags it
``performance,master``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import asyncpg

HUB_URL = "http://16.54.141.121:8091"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(obj)}")


async def _fetch_pnl(dsn: str, hours: int, include_paper: bool) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        where = [f"created_at >= NOW() - INTERVAL '{hours} hours'"]
        if not include_paper:
            where.append("is_live = true")
        q = f"""
            SELECT strategy,
                   count(*)                      AS n,
                   sum(pnl_usd)                  AS pnl,
                   count(*) FILTER (WHERE pnl_usd > 0) AS wins,
                   count(*) FILTER (WHERE pnl_usd < 0) AS losses,
                   avg(pnl_usd) FILTER (WHERE pnl_usd > 0) AS avg_win,
                   avg(pnl_usd) FILTER (WHERE pnl_usd < 0) AS avg_loss
            FROM trades
            WHERE {' AND '.join(where)}
            GROUP BY strategy
            ORDER BY sum(pnl_usd) DESC
        """
        rows = await conn.fetch(q)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def _format_table(rows: Iterable[dict], hours: int, include_paper: bool) -> str:
    header = f"Live-trade PnL — last {hours}h" + (" (inc. paper)" if include_paper else "")
    lines = [header, "-" * len(header)]
    lines.append(f"{'strategy':<22} {'n':>4} {'pnl_usd':>10} {'wr':>6} {'avg_win':>8} {'avg_loss':>9}")
    total_n = 0
    total_pnl = Decimal("0")
    for r in rows:
        n = int(r["n"])
        pnl = r["pnl"] or Decimal("0")
        wins = int(r["wins"] or 0)
        wr = (wins / n * 100) if n else 0.0
        aw = float(r["avg_win"] or 0)
        al = float(r["avg_loss"] or 0)
        lines.append(
            f"{r['strategy']:<22} {n:>4} {float(pnl):>+10.2f} {wr:>5.0f}% {aw:>+8.2f} {al:>+9.2f}"
        )
        total_n += n
        total_pnl += pnl
    lines.append("-" * len(header))
    lines.append(f"{'TOTAL':<22} {total_n:>4} {float(total_pnl):>+10.2f}")
    return "\n".join(lines)


def _hub_login(user: str, password: str) -> str:
    req = urllib.request.Request(
        f"{HUB_URL}/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def _hub_post_note(token: str, title: str, body: str, tags: str) -> dict:
    req = urllib.request.Request(
        f"{HUB_URL}/api/notes",
        data=json.dumps(
            {"title": title, "body": body, "tags": tags, "author": "script"}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--include-paper", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--post-note", action="store_true",
                    help="Also POST the report to Hub /api/notes as a master snapshot.")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    rows = await _fetch_pnl(dsn, args.hours, args.include_paper)

    if args.json:
        print(json.dumps({"hours": args.hours, "include_paper": args.include_paper,
                          "rows": rows}, default=_json_default, indent=2))
    else:
        print(_format_table(rows, args.hours, args.include_paper))

    if args.post_note:
        user = os.environ.get("HUB_USER", "billy")
        pw = os.environ.get("HUB_PASS")
        if not pw:
            print("HUB_PASS not set — skipping note post", file=sys.stderr)
            return 3
        token = _hub_login(user, pw)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = f"{today}: Strategy Performance Snapshot — Master"
        body_lines = [
            f"# {title}",
            "",
            f"Generated by `scripts/ops/strategy_pnl_24h.py` at "
            f"{datetime.now(timezone.utc).isoformat()}",
            "",
            "```",
            _format_table(rows, args.hours, args.include_paper),
            "```",
            "",
            "## How to reproduce",
            "",
            "```bash",
            "# From Montreal or any box with DATABASE_URL pointing at Montreal PG",
            f"python3 scripts/ops/strategy_pnl_24h.py --hours {args.hours}"
            + (" --include-paper" if args.include_paper else ""),
            "```",
            "",
            "Underlying query:",
            "",
            "```sql",
            f"SELECT strategy, count(*) AS n, sum(pnl_usd) AS pnl,",
            "       count(*) FILTER (WHERE pnl_usd > 0) AS wins,",
            "       count(*) FILTER (WHERE pnl_usd < 0) AS losses",
            "FROM trades",
            f"WHERE created_at >= NOW() - INTERVAL '{args.hours} hours'",
            ("" if args.include_paper else "  AND is_live = true"),
            "GROUP BY strategy ORDER BY sum(pnl_usd) DESC;",
            "```",
        ]
        resp = _hub_post_note(token, title, "\n".join(body_lines),
                              "performance,master,snapshot")
        print(f"\nPosted note #{resp['note']['id']}: {title}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
