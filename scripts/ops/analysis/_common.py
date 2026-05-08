"""Shared helpers for ``scripts/ops/analysis/`` alpha mining suite.

Centralises:
- RDS connection (DATABASE_URL from engine/.env, Montreal-local override)
- Wilson 95% CI calculator
- Real P&L math (DB ``pnl_usd`` is broken — regression #8 / audit #331)
- T-band bucketer (5 fixed bands matching strategy ledger / SE eval_offset)
- Numeric quintile helper
- Markdown table renderer

Memory references
-----------------

- ``feedback_payoff_math.md`` — fill regime + break-even math
- ``feedback_wallet_truth_authority.md`` — DB pnl_usd unreliable
- ``project_strategy_ledger.md`` — gate sets + T-band conventions
- Hub notes #287 / #298 / #301 / #302 / #305 / #307 / #308

All read-only. No write paths.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

DEFAULT_STAKE_USD = 7.50
POLY_FEE_MULT = 0.072  # 7.2% Polymarket crypto fee
DEFAULT_HOURS = 87
DEFAULT_ASSET = "BTC"
DEFAULT_TIMEFRAME = "5m"

# Fixed T-band SQL CASE used everywhere in this suite. Matches ranges from
# strategy_ledger and SIGNAL_EVAL_RUNBOOK; eval_offset is seconds-from-window-start
# (NOT seconds-to-close). A 5m window has eval_offset 0..300, but practical
# evaluation lives between ~24 (T-276s) and ~270 (T-30s). We bucket by
# ``seconds_to_close = 300 - eval_offset`` for human-readable T-band names.
TBAND_SQL = """
        CASE
            WHEN (300 - eval_offset) BETWEEN 0 AND 30 THEN 'T-30'
            WHEN (300 - eval_offset) BETWEEN 30 AND 60 THEN 'T-60'
            WHEN (300 - eval_offset) BETWEEN 60 AND 90 THEN 'T-90'
            WHEN (300 - eval_offset) BETWEEN 90 AND 120 THEN 'T-120'
            WHEN (300 - eval_offset) BETWEEN 120 AND 180 THEN 'T-180'
            WHEN (300 - eval_offset) BETWEEN 180 AND 240 THEN 'T-240'
            ELSE 'T-other'
        END
"""

REGIMES = ("CALM", "NORMAL", "TRANSITION", "CASCADE")
DIRECTIONS = ("UP", "DOWN")


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load engine/.env from common locations (Montreal first, Mac second)."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    for path in (
        "/home/novakash/novakash/engine/.env",
        os.path.expanduser("~/Code/novakash/engine/.env"),
        os.path.expanduser("~/Code/novakash/.claude/worktrees/romantic-dewdney-d53298/engine/.env.local"),
        "engine/.env",
        "engine/.env.local",
        ".env",
    ):
        if os.path.exists(path):
            load_dotenv(path, override=False)


def get_pg_url() -> str:
    """Return synchronous psycopg/asyncpg-compatible DATABASE_URL.

    Strips ``+asyncpg`` so the URL works for both ``asyncpg`` (async) and
    ``psycopg2``/``psycopg`` (sync). All callers should treat it as opaque.
    """
    _load_env()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: DATABASE_URL not set. Run from Montreal or source engine/.env.local", file=sys.stderr)
        sys.exit(2)
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def connect_pg():
    """Return an asyncpg connection — caller is responsible for closing."""
    import asyncpg  # type: ignore
    return await asyncpg.connect(get_pg_url())


# ---------------------------------------------------------------------------
# Wilson 95% CI
# ---------------------------------------------------------------------------

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Return (low, high) Wilson 95% CI for win-rate.

    Standard Wilson score interval; better than normal-approx for small n
    or extreme p-hat. Returns (0.0, 1.0) on n=0.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return (max(0.0, low), min(1.0, high))


# ---------------------------------------------------------------------------
# Real P&L math
# ---------------------------------------------------------------------------

def real_pnl(outcome_correct: bool, fill_price: float, stake: float = DEFAULT_STAKE_USD) -> float:
    """Compute REAL P&L in USD for one bet.

    Engine fills around 0.74–0.79 — at those fills each win pays only 21–26 cents
    relative to stake while each loss = full stake. DB pnl_usd is BROKEN for
    multiple historical reasons (see feedback_wallet_truth_authority.md). Use
    this formula end-to-end:

      WIN:  shares * (1 - fill) - fee
            shares = stake / fill, so payout = stake * (1 - fill) / fill
            then minus 7.2% fee on STAKE.
      LOSS: -stake (fee already inherent — the lost contracts revert to 0).
    """
    if fill_price is None or fill_price <= 0 or fill_price >= 1:
        return 0.0
    if outcome_correct:
        return (stake * (1.0 - fill_price) / fill_price) - (POLY_FEE_MULT * stake)
    return -stake


def real_pnl_sql_expr(outcome_col: str, dir_col: str, fill_col: str, stake: float = DEFAULT_STAKE_USD) -> str:
    """Return SQL CASE that computes real_pnl per row.

    Args
    ----
    outcome_col : column name for actual outcome (UP/DOWN string)
    dir_col     : column name for predicted direction (UP/DOWN string)
    fill_col    : column name for fill price (0..1 float)
    stake       : USD stake per bet

    Returns
    -------
    SQL expression — wrap in SELECT/SUM to aggregate.
    """
    s = stake
    fee = POLY_FEE_MULT * s
    return f"""
    CASE
        WHEN {fill_col} IS NULL OR {fill_col} <= 0 OR {fill_col} >= 1 THEN 0
        WHEN {dir_col} = {outcome_col}
            THEN ({s} * (1.0 - {fill_col}) / {fill_col}) - {fee}
        ELSE -{s}
    END
    """


# ---------------------------------------------------------------------------
# Result row + table rendering
# ---------------------------------------------------------------------------

@dataclass
class CellResult:
    """One alpha cell — a (signal, threshold, T-band, regime, direction) tuple."""
    label: str
    n: int
    wins: int
    wr: float
    wilson_low: float
    wilson_high: float
    avg_fill: float
    real_pnl: float
    extra: str = ""

    @property
    def wilson_low_pct(self) -> float:
        return self.wilson_low * 100

    @property
    def wilson_high_pct(self) -> float:
        return self.wilson_high * 100

    @property
    def is_exploratory(self) -> bool:
        # Bonferroni-light: any cell with n<30 is exploratory; 95% CI > 0.30 wide also flagged.
        return self.n < 30 or (self.wilson_high - self.wilson_low) > 0.30

    @property
    def quality_score(self) -> float:
        """Wilson_low * sqrt(n) — favours BOTH high-confidence WR AND sample."""
        if self.n <= 0:
            return 0.0
        return self.wilson_low * math.sqrt(self.n)


def render_md_table(rows: Sequence[CellResult], top: int = 25, sort_key: str = "quality") -> str:
    """Render results as a markdown table sorted by quality_score (default).

    Other sort keys: 'pnl', 'wr', 'n'.
    """
    sort_fn = {
        "quality": lambda r: -r.quality_score,
        "pnl": lambda r: -r.real_pnl,
        "wr": lambda r: -r.wr,
        "n": lambda r: -r.n,
    }.get(sort_key, lambda r: -r.quality_score)
    sorted_rows = sorted(rows, key=sort_fn)[:top]

    lines = [
        "| label | n | wins | WR | Wilson 95% | avg_fill | real_pnl | flag |",
        "|---|---:|---:|---:|---|---:|---:|---|",
    ]
    for r in sorted_rows:
        flag = "EXP" if r.is_exploratory else ""
        lines.append(
            f"| {r.label} | {r.n} | {r.wins} | {r.wr*100:.1f}% | "
            f"[{r.wilson_low_pct:.1f}, {r.wilson_high_pct:.1f}] | "
            f"{r.avg_fill:.3f} | ${r.real_pnl:+.2f} | {flag} |"
        )
    return "\n".join(lines)


def render_csv(rows: Sequence[CellResult]) -> str:
    """CSV dump of all rows — for spreadsheet drilldown."""
    out = ["label,n,wins,wr,wilson_low,wilson_high,avg_fill,real_pnl,exploratory,extra"]
    for r in rows:
        out.append(
            f"\"{r.label}\",{r.n},{r.wins},{r.wr:.4f},{r.wilson_low:.4f},"
            f"{r.wilson_high:.4f},{r.avg_fill:.4f},{r.real_pnl:.2f},"
            f"{int(r.is_exploratory)},\"{r.extra}\""
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cell_from_row(label: str, n: int, wins: int, avg_fill: float, real_pnl_usd: float, extra: str = "") -> CellResult:
    wr = (wins / n) if n > 0 else 0.0
    lo, hi = wilson_ci(wins, n)
    return CellResult(
        label=label, n=n, wins=wins, wr=wr,
        wilson_low=lo, wilson_high=hi,
        avg_fill=avg_fill or 0.0,
        real_pnl=real_pnl_usd or 0.0,
        extra=extra,
    )


def write_output(out_dir: str, script_name: str, md_body: str, csv_body: Optional[str] = None) -> str:
    """Write per-script outputs to ``docs/analysis/auto/<DATE>/`` (or supplied dir)."""
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"{script_name}.md")
    with open(md_path, "w") as f:
        f.write(md_body)
    if csv_body:
        csv_path = os.path.join(out_dir, f"{script_name}.csv")
        with open(csv_path, "w") as f:
            f.write(csv_body)
    return md_path
