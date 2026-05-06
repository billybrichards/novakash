"""Pre-deploy validator for the rolling-WR cell-pause gate.

Replays the last 14 days of LIVE trades chronologically through
``engine.services.rolling_wr_monitor.RollingWrMonitor`` and computes
two key metrics that gate the rolling-WR PR's deploy:

  1. **total_pnl_lift** — sum of fill-math PnL of trades that WOULD HAVE
     BEEN paused (i.e. skipped). Positive value means the gate would
     have prevented those losses (or, occasionally, missed wins).
     Spec assertion: ``>= $50``.

  2. **false_pause_rate** — of all paused intervals, what fraction had
     a NEXT trade in the same cell that WOULD HAVE WON (so the pause
     was wrong). Spec assertion: ``< 30%``.

Outputs a Markdown report to ``tmp/cell_pause_replay_report.md`` for
human review (per-cell tables, paused-intervals timeline, false-pause
cases). The PR opens as DRAFT; reviewer must run this script and confirm
both assertions pass before un-DRAFTing.

PnL convention (per memory ``feedback_payoff_math.md``)::

    WIN  = (1 - fill) * (stake / fill) - 0.072 * stake
    LOSS = -stake

Never trust ``trades.pnl_usd`` — it has accumulated regressions
(memory ``feedback_wallet_truth_authority.md``).

Usage
-----
On Montreal (has DATABASE_URL pointing at RDS prod)::

    ssh novakash@15.223.247.178 \\
        'cd /home/novakash/novakash && python3 scripts/sim/replay_cell_pauses.py'

Locally (requires Postgres reachable + RollingWrMonitor importable)::

    python3 scripts/sim/replay_cell_pauses.py [--days 14] [--output tmp/...]

Coordination notes
------------------
``engine/services/rolling_wr_monitor.py`` is shipped by the SISTER PR
``feat/hour-blocks-source-agreement-cell-pause`` (task
``a79c0f3eaff491462``). When that PR's branch is merged into this one,
the import resolves naturally. Until then this script exits with a
clear error message rather than producing fake metrics.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── sys.path bootstrap so we can `from services.X import Y` ─────────────
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_ROOT = REPO_ROOT / "engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


# ── Hard assertions (per spec) ──────────────────────────────────────────
MIN_PNL_LIFT_USD = 50.00
MAX_FALSE_PAUSE_RATE = 0.30

FEE_MULT = 0.072
DEFAULT_DAYS = 14
DEFAULT_OUTPUT = "tmp/cell_pause_replay_report.md"


# ── Trade row + replay primitives ───────────────────────────────────────


@dataclass
class TradeRow:
    """Subset of trades columns we need for replay."""

    id: int
    strategy: str
    direction: str  # YES | NO | UP | DOWN
    fill_price: float
    stake_usd: float
    outcome: str  # WIN | LOSS | PUSH
    created_at: datetime
    resolved_at: Optional[datetime]

    @property
    def cell_session(self) -> str:
        from services.session_label import session_label

        ts = self.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return session_label(ts)

    @property
    def cell_key(self) -> Tuple[str, str, str]:
        return (self.strategy, self.cell_session, self.direction.upper())

    @property
    def fill_math_pnl(self) -> float:
        """Per-trade PnL via fill-math, not the polluted DB column."""
        if self.outcome == "WIN" and self.fill_price > 0:
            return (1.0 - self.fill_price) * (self.stake_usd / self.fill_price) - (
                FEE_MULT * self.stake_usd
            )
        if self.outcome == "LOSS":
            return -float(self.stake_usd)
        return 0.0  # PUSH / unknown


# ── DB load ─────────────────────────────────────────────────────────────


async def _load_trades(days: int) -> List[TradeRow]:
    """Fetch resolved LIVE trades from the last `days` days.

    Imports asyncpg lazily so the script can fail fast with a clear
    error if the user is on an env without the driver.
    """
    try:
        import asyncpg  # type: ignore
    except ImportError:
        raise SystemExit(
            "asyncpg not installed; run on Montreal or `pip install asyncpg`"
        )

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL env var required (RDS prod connection)")
    # asyncpg doesn't accept the `+asyncpg` SQLAlchemy suffix.
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT id, strategy, direction,
                   COALESCE(entry_price, 0)::float AS fill_price,
                   COALESCE(stake_usd, 0)::float AS stake_usd,
                   outcome, created_at, resolved_at
            FROM trades
            WHERE created_at >= $1
              AND outcome IN ('WIN', 'LOSS')
              AND COALESCE(metadata->>'paper_mode', 'false') = 'false'
            ORDER BY COALESCE(resolved_at, created_at) ASC, id ASC
            """,
            cutoff,
        )
    finally:
        await conn.close()

    out: List[TradeRow] = []
    for r in rows:
        out.append(
            TradeRow(
                id=r["id"],
                strategy=r["strategy"] or "unknown",
                direction=(r["direction"] or "UNKNOWN").upper(),
                fill_price=float(r["fill_price"]),
                stake_usd=float(r["stake_usd"]),
                outcome=r["outcome"],
                created_at=r["created_at"],
                resolved_at=r["resolved_at"],
            )
        )
    return out


# ── Replay engine ───────────────────────────────────────────────────────


@dataclass
class ReplayResult:
    total_pnl_lift: float = 0.0
    paused_intervals: List[Dict[str, Any]] = field(default_factory=list)
    pauses_evaluated: int = 0
    false_pauses: int = 0
    per_cell: Dict[Tuple[str, str, str], Dict[str, Any]] = field(default_factory=dict)
    skipped_trades: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def false_pause_rate(self) -> float:
        if self.pauses_evaluated == 0:
            return 0.0
        return self.false_pauses / float(self.pauses_evaluated)


def _import_monitor():
    """Best-effort import of RollingWrMonitor.

    The sister PR ``feat/hour-blocks-source-agreement-cell-pause`` adds
    this module. If absent, raise a clean SystemExit so reviewers see
    why the validator can't run yet.
    """
    try:
        from services.rolling_wr_monitor import (  # type: ignore[import]
            RollingWrMonitor,
        )

        return RollingWrMonitor
    except ImportError as exc:
        raise SystemExit(
            "engine/services/rolling_wr_monitor.py not found. This validator "
            "depends on the sister PR feat/hour-blocks-source-agreement-cell-"
            "pause; rebase onto that branch and re-run.\n"
            f"Underlying error: {exc}"
        )


def replay(trades: List[TradeRow]) -> ReplayResult:
    """Walk ``trades`` chronologically through a fresh RollingWrMonitor
    and accumulate the metrics.

    Expected (duck-typed) ``RollingWrMonitor`` API (per spec)::

        monitor = RollingWrMonitor()
        monitor.on_trade_resolved(
            strategy=..., session=..., direction=..., outcome=...,
            pnl=..., resolved_at=...,
        )
        monitor.is_cell_paused(strategy, session, direction, now=...)
            -> bool
        monitor.cell_pause_intervals
            -> list of dicts {cell, start_at, end_at, reason}

    If the real API differs we duck-type around it via getattr — the
    monitor PR is in flight so this validator stays loose.
    """
    monitor_cls = _import_monitor()
    monitor = monitor_cls()

    result = ReplayResult()

    # Track per-cell aggregate stats for the report.
    per_cell: Dict[Tuple[str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "paused_count": 0,
            "would_have_been_skipped_pnl": 0.0,
            "false_pause_count": 0,
            "next_trade_won": 0,
        }
    )

    # Last paused state per cell so we can detect "first trade after a
    # pause" → counts toward false_pause_rate.
    last_pause_seen: Dict[Tuple[str, str, str], bool] = {}

    for trade in trades:
        cell = trade.cell_key
        cell_stats = per_cell[cell]
        cell_stats["total"] += 1
        if trade.outcome == "WIN":
            cell_stats["wins"] += 1
        else:
            cell_stats["losses"] += 1

        # 1. BEFORE recording, check whether the gate would have paused
        # this cell at trade-time. If so, this trade gets skipped in the
        # counterfactual world.
        is_paused_now = False
        check = getattr(monitor, "is_cell_paused", None)
        if callable(check):
            try:
                is_paused_now = bool(
                    check(
                        strategy=trade.strategy,
                        session=trade.cell_session,
                        direction=trade.direction.upper(),
                        now=trade.created_at,
                    )
                )
            except TypeError:
                # Try positional — different sig
                is_paused_now = bool(
                    check(
                        trade.strategy,
                        trade.cell_session,
                        trade.direction.upper(),
                        trade.created_at,
                    )
                )

        if is_paused_now:
            # Counterfactually skipped: we DON'T take this trade. Lift +=
            # the negation of what we would have made (+stake on a loss,
            # -winnings on a win — i.e. negative lift on missed wins).
            lift_delta = -trade.fill_math_pnl  # avoiding a loss = positive lift
            result.total_pnl_lift += lift_delta
            cell_stats["paused_count"] += 1
            cell_stats["would_have_been_skipped_pnl"] += lift_delta
            result.skipped_trades.append(
                {
                    "trade_id": trade.id,
                    "cell": cell,
                    "outcome": trade.outcome,
                    "fill_math_pnl": round(trade.fill_math_pnl, 4),
                    "lift_delta": round(lift_delta, 4),
                    "created_at": trade.created_at.isoformat(),
                }
            )

            # False-pause check: if THIS trade (the one we paused) would
            # have WON, then the pause was a false alarm.
            result.pauses_evaluated += 1
            if trade.outcome == "WIN":
                result.false_pauses += 1
                cell_stats["false_pause_count"] += 1

        # 2. THEN feed the trade resolution into the monitor so subsequent
        # iterations see updated state. We feed REAL outcomes (not
        # counterfactual) — the gate's job is to learn from history, and
        # we want the replay to mirror what the gate would actually have
        # seen in production.
        on_resolved = getattr(monitor, "on_trade_resolved", None)
        if callable(on_resolved):
            try:
                on_resolved(
                    strategy=trade.strategy,
                    session=trade.cell_session,
                    direction=trade.direction.upper(),
                    outcome=trade.outcome,
                    pnl=trade.fill_math_pnl,
                    resolved_at=trade.resolved_at or trade.created_at,
                )
            except TypeError:
                on_resolved(
                    trade.strategy,
                    trade.cell_session,
                    trade.direction.upper(),
                    trade.outcome,
                    trade.fill_math_pnl,
                    trade.resolved_at or trade.created_at,
                )

        last_pause_seen[cell] = is_paused_now

    # Pull pause-intervals out of the monitor for the report.
    intervals = getattr(monitor, "cell_pause_intervals", None)
    if intervals:
        try:
            result.paused_intervals = list(intervals)  # may be a sequence
        except TypeError:
            result.paused_intervals = []

    result.per_cell = dict(per_cell)
    return result


# ── Markdown report ─────────────────────────────────────────────────────


def _render_report(result: ReplayResult, days: int) -> str:
    lines: List[str] = []
    lines.append(f"# cell_pause replay report ({days}d)")
    lines.append("")
    lines.append(f"- **total_pnl_lift**: ${result.total_pnl_lift:.2f}  "
                 f"(spec: ≥ ${MIN_PNL_LIFT_USD:.2f})")
    lines.append(f"- **false_pause_rate**: {result.false_pause_rate:.1%} "
                 f"({result.false_pauses}/{result.pauses_evaluated}) "
                 f"(spec: < {MAX_FALSE_PAUSE_RATE:.0%})")
    lines.append(f"- **trades replayed**: {sum(c['total'] for c in result.per_cell.values())}")
    lines.append(f"- **cells touched**: {len(result.per_cell)}")
    lines.append("")
    lines.append("## Per-cell breakdown")
    lines.append("")
    lines.append("| strategy | session | direction | n | wins | losses | paused | would-have-been-skipped PnL |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for cell, stats in sorted(result.per_cell.items()):
        s, sess, d = cell
        lines.append(
            f"| {s} | {sess} | {d} | {stats['total']} | {stats['wins']} | "
            f"{stats['losses']} | {stats['paused_count']} | "
            f"${stats['would_have_been_skipped_pnl']:.2f} |"
        )
    lines.append("")

    if result.skipped_trades:
        lines.append("## Counterfactually skipped trades (top 30)")
        lines.append("")
        lines.append("| trade_id | cell | outcome | fill_math_pnl | lift_delta | created_at |")
        lines.append("|---|---|---|---:|---:|---|")
        for sk in result.skipped_trades[:30]:
            lines.append(
                f"| {sk['trade_id']} | {sk['cell']} | {sk['outcome']} | "
                f"${sk['fill_math_pnl']:.2f} | ${sk['lift_delta']:.2f} | "
                f"{sk['created_at']} |"
            )
        lines.append("")

    if result.paused_intervals:
        lines.append("## Pause intervals")
        lines.append("")
        for iv in result.paused_intervals[:50]:
            lines.append(f"- `{iv}`")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── Main ────────────────────────────────────────────────────────────────


async def _amain(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="lookback window")
    p.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                   help="markdown report path")
    args = p.parse_args(argv)

    print(f"loading {args.days}d of LIVE trades...", file=sys.stderr)
    trades = await _load_trades(args.days)
    print(f"  -> {len(trades)} resolved trades loaded", file=sys.stderr)

    print("replaying through RollingWrMonitor...", file=sys.stderr)
    result = replay(trades)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_report(result, args.days))
    print(f"report written to {out_path}", file=sys.stderr)

    print(f"\ntotal_pnl_lift  = ${result.total_pnl_lift:.2f}")
    print(f"false_pause_rate = {result.false_pause_rate:.1%}  "
          f"({result.false_pauses}/{result.pauses_evaluated})")

    # ── Hard assertions per spec ────────────────────────────────────────
    assert result.total_pnl_lift >= MIN_PNL_LIFT_USD, (
        f"14d replay shows lift ${result.total_pnl_lift:.2f} < "
        f"${MIN_PNL_LIFT_USD:.2f}; spec says ≥${MIN_PNL_LIFT_USD:.2f} required"
    )
    assert result.false_pause_rate < MAX_FALSE_PAUSE_RATE, (
        f"false_pause_rate {result.false_pause_rate:.0%} >= "
        f"{MAX_FALSE_PAUSE_RATE:.0%}; spec says <{MAX_FALSE_PAUSE_RATE:.0%}"
    )

    print("\nALL ASSERTIONS PASS — gate is safe to deploy.")
    return 0


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
