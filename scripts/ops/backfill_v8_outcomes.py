"""One-shot backfill for v8_champion trades stuck at outcome=NULL.

Problem (pre-#340): v8_champion trade metadata stores `token_id` + `market_slug`
but NEVER `condition_id`. The post-redeem backstop `resolve_from_redeem` in
`engine/reconciliation/reconciler.py` matched by `metadata->>'condition_id'`
only → zero matches. Redeemer swept WINs → position vanished from CLOB
positions API → `ReconcilePositionsUseCase` never saw them either. Trades
stayed `outcome IS NULL` forever.

PR #340 / #341 fix the forward path. This script resolves the historical
backlog (52 rows as of 2026-04-24T12:00Z).

Three-tier resolution:
  1. Polymarket positions API — for positions still visible (not yet redeemed).
     Use Polymarket's own WIN/LOSS + pnl aggregate.
  2. Polymarket activity (REDEEM events) — for positions already redeemed.
     Match by `conditionId` to compute payout_usdc; pnl = usdc - stake.
  3. `window_snapshots.poly_resolved_outcome` — for markets where the first
     two paths come up empty (rare; fallback only). Infer WIN/LOSS from
     trade direction vs oracle-resolved outcome.

Usage
-----
Dry-run (default — prints plan, no writes):

    python3 scripts/ops/backfill_v8_outcomes.py

Apply:

    python3 scripts/ops/backfill_v8_outcomes.py --apply

Scope:

    python3 scripts/ops/backfill_v8_outcomes.py --strategy v8_champion --limit 100
    python3 scripts/ops/backfill_v8_outcomes.py --all-strategies --apply

Safety
------
- Reads `DATABASE_URL` + `POLY_FUNDER_ADDRESS` from engine/.env (same env as
  wallet_truth.py); fall back to process env.
- UPDATE clause includes `WHERE outcome IS NULL` — cannot stomp on trades the
  live reconciler already resolved.
- Prints every per-trade decision with evidence (tier, source, payout).
- Idempotent — re-running skips already-resolved rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

try:
    import asyncpg  # type: ignore
except ImportError:
    print("ERROR: asyncpg not installed. `pip install asyncpg`", file=sys.stderr)
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────
ENV_PATH = Path("/home/novakash/novakash/engine/.env")
POLY_DATA_API = "https://data-api.polymarket.com"
POLY_GAMMA_API = "https://gamma-api.polymarket.com"
# When Polymarket returns a position with outcome WIN, the aggregate `pnl`
# already nets fees. We trust it. When the position is already redeemed,
# we infer WIN from the REDEEM activity row's `usdcSize` field and compute
# pnl = payout - cost. For LOSS we always use -stake (Polymarket's `pnl`
# agrees but we don't want to re-parse it).


def _load_env() -> dict[str, str]:
    """Pull DATABASE_URL + POLY_FUNDER_ADDRESS from engine/.env or env."""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    # Process env wins over .env file so user can override per-invocation.
    for k in ("DATABASE_URL", "POLY_FUNDER_ADDRESS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    missing = [k for k in ("DATABASE_URL", "POLY_FUNDER_ADDRESS") if not env.get(k)]
    if missing:
        print(f"ERROR: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    # asyncpg wants plain postgresql://
    env["DATABASE_URL"] = env["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    return env


def _http_get_json(url: str, timeout: int = 15) -> Any:
    """Simple JSON GET with a browser UA (Polymarket CDN blocks default python UA)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} on {url[:80]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  HTTP error on {url[:80]}: {e}", file=sys.stderr)
        return None


def poly_positions(funder: str) -> list[dict]:
    """Current (unredeemed) positions for the proxy wallet."""
    rows = _http_get_json(f"{POLY_DATA_API}/positions?user={funder}&limit=500") or []
    return [p for p in rows if float(p.get("size", 0)) > 0.001]


def poly_activity_redeems(funder: str, since_ts: int) -> list[dict]:
    """REDEEM events from activity API since ``since_ts`` (unix seconds).

    The activity endpoint paginates at 500/page. We stop once we hit a row
    older than ``since_ts`` (activity is reverse-chronological).
    """
    all_rows: list[dict] = []
    offset = 0
    while offset < 5000:  # hard cap for safety
        url = f"{POLY_DATA_API}/activity?user={funder}&limit=500&offset={offset}"
        rows = _http_get_json(url) or []
        if not rows:
            break
        all_rows.extend(rows)
        # Stop early if we crossed the cutoff
        if any(r.get("timestamp", 0) < since_ts for r in rows):
            break
        if len(rows) < 500:
            break
        offset += 500
    return [r for r in all_rows if r.get("type") == "REDEEM" and r.get("timestamp", 0) >= since_ts]


async def fetch_pending_trades(conn, strategy: Optional[str], limit: int) -> list[dict]:
    """Pull unresolved live trades (most recent first)."""
    where = ["outcome IS NULL", "is_live = true"]
    params: list[Any] = []
    if strategy:
        where.append(f"strategy = ${len(params)+1}")
        params.append(strategy)
    sql = f"""
        SELECT id, strategy, direction, stake_usd, entry_price, fill_price,
               created_at, metadata
        FROM trades
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
        LIMIT ${len(params)+1}
    """
    params.append(limit)
    rows = await conn.fetch(sql, *params)
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("metadata"), str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                d["metadata"] = {}
        d["metadata"] = d["metadata"] or {}
        result.append(d)
    return result


async def fetch_window_resolution(conn, window_ts: int, asset: str = "BTC") -> Optional[str]:
    """Tier-3 fallback: look up oracle-resolved outcome from window_snapshots."""
    r = await conn.fetchrow(
        """SELECT poly_resolved_outcome, actual_direction
           FROM window_snapshots
           WHERE window_ts = $1 AND asset = $2
           LIMIT 1""",
        window_ts,
        asset,
    )
    if not r:
        return None
    # Prefer Polymarket-resolved outcome if present (YES/NO). Fall back to
    # actual_direction (UP/DOWN from Chainlink oracle label pass).
    if r["poly_resolved_outcome"]:
        return str(r["poly_resolved_outcome"]).upper()
    if r["actual_direction"]:
        # Translate UP/DOWN → YES/NO so comparison below is uniform.
        d = str(r["actual_direction"]).upper()
        return "YES" if d == "UP" else "NO"
    return None


def _token_prefix_match(trade_tid: str, pos_tid: str) -> bool:
    """Prefix match in both directions — mirrors `_backfill_on_startup` logic."""
    if not trade_tid or not pos_tid:
        return False
    return trade_tid.startswith(pos_tid) or pos_tid.startswith(trade_tid)


def _direction_to_outcome(direction: str) -> str:
    """YES/UP → YES, NO/DOWN → NO."""
    d = (direction or "").upper()
    return "YES" if d in ("UP", "YES") else "NO"


def resolve_from_tier1(
    trade: dict, positions: list[dict]
) -> Optional[tuple[str, float, str]]:
    """Tier 1: match by token_id prefix against current positions, infer
    outcome from on-chain ``curPrice`` (the only deterministic truth signal).

    Important: Polymarket's data-api position record has an ``outcome`` field
    but it labels which SIDE the bet is on ("Up"/"Down"), NOT WIN/LOSS — using
    that field as a resolution signal caused audit #314 (124 trades falsely
    marked WIN). Truth signal is ``curPrice``: at-or-below 0.01 = LOSS, at-or-
    above 0.99 = WIN, anything in between = market not yet resolved → skip
    and let tier2/tier3 try.

    Returns (outcome, pnl, reason) or None.
    """
    tid = str(trade["metadata"].get("token_id") or "")
    if not tid:
        return None
    stake = float(trade["stake_usd"] or 0)
    for p in positions:
        pos_tid = str(p.get("asset") or p.get("tokenId") or "")
        if not _token_prefix_match(tid, pos_tid):
            continue
        try:
            cur_price = float(p.get("curPrice", 0))
        except (TypeError, ValueError):
            return None
        if cur_price <= 0.01:
            return (
                "LOSS",
                round(-stake, 4),
                f"tier1: poly_positions curPrice={cur_price:.4f} (lost)",
            )
        if cur_price >= 0.99:
            # Confirmed winner — prefer Polymarket's realizedPnl when present
            # (already net of fees / slippage). Fall back to position pnl,
            # then to a stake-based approximation.
            try:
                realized = float(p.get("realizedPnl") or 0)
            except (TypeError, ValueError):
                realized = 0.0
            if realized > 0:
                pnl = round(realized, 4)
            else:
                try:
                    pnl = round(float(p.get("pnl", 0)), 4)
                except (TypeError, ValueError):
                    pnl = 0.0
            return (
                "WIN",
                pnl,
                f"tier1: poly_positions curPrice={cur_price:.4f} (won)",
            )
        # Mid-range curPrice → market not yet resolved on-chain. Skip so a
        # stale Gamma cache (or window_snapshots) can't override.
        return None
    return None


def gamma_market_resolution(slug: str) -> Optional[dict]:
    """Query Gamma markets API for a RESOLVED market. Returns the market dict
    with outcomes/outcomePrices/clobTokenIds, or None if not found OR if the
    market is closed-but-not-yet-resolved (where outcomePrices reflects last
    trade price, not oracle resolution — this caused 124 false WIN marks
    before the resolution-status guard was added; see audit #314).

    Polymarket resolution data shape:
        outcomes: ["Up", "Down"]                       (order matters)
        outcomePrices: ["0", "1"]   → Down won (1.0)   (only after resolution)
        clobTokenIds: ["<up_tid>", "<down_tid>"]       (same order as outcomes)
        umaResolutionStatus: "resolved"                (REQUIRED — not just closed)
        closed: true
    """
    rows = _http_get_json(
        f"{POLY_GAMMA_API}/markets?slug={slug}&closed=true&limit=1"
    )
    if not rows:
        return None
    m = rows[0]
    # Resolution status guard. `closed=true` means trading window ended but
    # oracle has not necessarily resolved yet. During that gap, outcomePrices
    # mirrors last trade price (e.g. ["0.31","0.69"] for a tight-priced market)
    # and a naive `>= 0.5` check would mass-mark losers as winners.
    resolution_status = str(m.get("umaResolutionStatus") or "").lower()
    if resolution_status != "resolved":
        return None
    prices_raw = m.get("outcomePrices")
    tokens_raw = m.get("clobTokenIds")
    # These come back as JSON-encoded strings from Gamma, e.g. '["0", "1"]'.
    # Decode if needed.
    try:
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)
        if isinstance(tokens_raw, str):
            tokens_raw = json.loads(tokens_raw)
    except Exception:
        return None
    if not prices_raw or not tokens_raw or len(prices_raw) != len(tokens_raw):
        return None
    m["outcomePrices"] = prices_raw
    m["clobTokenIds"] = tokens_raw
    return m


def resolve_from_tier2(
    trade: dict, redeems: list[dict], market_cache: dict
) -> Optional[tuple[str, float, str]]:
    """Tier 2: Gamma API authoritative resolution + activity payout lookup.

    Workflow:
      1. Fetch market resolution from Gamma (cached per slug).
      2. Locate the trade's token_id in ``clobTokenIds`` — its index reveals
         whether this outcome won (outcomePrices[i] == "1") or lost ("0").
      3. For WIN, match the wallet's REDEEM for this slug with usdcSize > 0
         to compute pnl = payout - stake. For multi-REDEEM markets, prefer
         the non-zero row (losing side always has usdcSize = 0).
      4. For LOSS, pnl = -stake.
    """
    stake = float(trade["stake_usd"] or 0)
    md = trade["metadata"]
    market_slug = str(md.get("market_slug") or "")
    token_id = str(md.get("token_id") or "")
    if not market_slug:
        return None

    market = market_cache.get(market_slug)
    if market is None:
        market = gamma_market_resolution(market_slug)
        market_cache[market_slug] = market  # cache None too — avoids re-fetch
    if not market:
        return None

    prices = market["outcomePrices"]
    tokens = market["clobTokenIds"]
    # Match trade's token_id to one of the two market outcome tokens.
    idx = None
    if token_id:
        for i, tid in enumerate(tokens):
            if _token_prefix_match(token_id, str(tid)):
                idx = i
                break
    if idx is None:
        # Fallback: match by direction vs outcome label. Outcomes are
        # ["Up", "Down"]; trade direction is UP/YES or DOWN/NO.
        outcomes = [str(x).upper() for x in (market.get("outcomes") or [])]
        tdir = (trade["direction"] or "").upper()
        want = "UP" if tdir in ("UP", "YES") else "DOWN"
        if want in outcomes:
            idx = outcomes.index(want)
    if idx is None:
        return None

    try:
        won = float(prices[idx]) >= 0.5
    except (ValueError, TypeError):
        return None

    if not won:
        return "LOSS", round(-stake, 4), f"tier2: gamma outcomePrices[{idx}]=0 (lost)"

    # WIN — pull exact payout from activity REDEEMs for this slug if possible.
    # Multi-REDEEM markets: prefer non-zero usdcSize (winning-side row).
    slug_hits = [r for r in redeems if str(r.get("slug") or "") == market_slug]
    nonzero = [r for r in slug_hits if float(r.get("usdcSize") or 0) > 0.01]
    if nonzero:
        payout = float(nonzero[0]["usdcSize"])
        pnl = round(payout - stake, 4)
        return "WIN", pnl, f"tier2: gamma+REDEEM payout=${payout:.2f}"
    # No REDEEM yet (market resolved but redeemer hasn't swept) — approximate
    # from fill_price so the row doesn't stay NULL.
    fp = float(trade.get("fill_price") or 0) or float(trade.get("entry_price") or 0)
    if fp > 0:
        shares = stake / fp
        pnl = round(shares - stake, 4)
        return "WIN", pnl, f"tier2: gamma (approx pnl, no REDEEM yet)"
    return "WIN", 0.0, "tier2: gamma WIN (payout unknown — redeem pending)"


async def resolve_from_tier3(
    trade: dict, conn
) -> Optional[tuple[str, float, str]]:
    """Tier 3: window_snapshots oracle fallback."""
    md = trade["metadata"]
    # Pull window_ts from metadata.dedup_key (format: `v8_champion:<ts>:<dir>`)
    # OR from the market_slug (format: `btc-updown-5m-<window_ts>`).
    window_ts: Optional[int] = None
    dk = str(md.get("dedup_key") or "")
    if ":" in dk:
        parts = dk.split(":")
        if len(parts) >= 3 and parts[1].isdigit():
            window_ts = int(parts[1])
    if window_ts is None:
        slug = str(md.get("market_slug") or "")
        if slug.startswith("btc-updown-5m-") and slug[14:].isdigit():
            window_ts = int(slug[14:])
    if window_ts is None:
        return None
    resolved = await fetch_window_resolution(conn, window_ts, asset="BTC")
    if not resolved:
        return None
    trade_dir = _direction_to_outcome(trade["direction"])  # YES / NO
    stake = float(trade["stake_usd"] or 0)
    # resolved is YES/NO — WIN if matches, LOSS otherwise. No payout info
    # available at this tier; approximate WIN pnl from fill_price:
    #   payout = stake / fill_price  (shares)  ...  winning share → $1 each
    #   pnl = shares - stake
    if resolved == trade_dir:
        fp = float(trade.get("fill_price") or trade.get("entry_price") or 0)
        if fp > 0:
            shares = stake / fp
            pnl = round(shares - stake, 4)
        else:
            pnl = 0.0  # can't compute — flag with zero, user can manual-fix
        return "WIN", pnl, f"tier3: window_snapshots.resolved={resolved} (approx pnl)"
    return "LOSS", round(-stake, 4), f"tier3: window_snapshots.resolved={resolved}"


async def apply_update(conn, trade_id: int, outcome: str, pnl: float) -> bool:
    """UPDATE with WHERE outcome IS NULL guard. Returns True if row was written."""
    status = "RESOLVED_WIN" if outcome == "WIN" else "RESOLVED_LOSS"
    result = await conn.execute(
        """UPDATE trades SET outcome = $1, pnl_usd = $2, status = $3,
                  resolved_at = NOW()
           WHERE id = $4 AND outcome IS NULL""",
        outcome,
        pnl,
        status,
        trade_id,
    )
    # asyncpg returns "UPDATE N"
    try:
        return int(str(result).split()[-1]) > 0
    except (IndexError, ValueError):
        return False


async def main_async(args: argparse.Namespace) -> None:
    env = _load_env()
    funder = env["POLY_FUNDER_ADDRESS"]

    print(f"Backfill v8 outcomes — {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"  funder: {funder}")
    print(f"  scope: {'all strategies' if args.all_strategies else f'strategy={args.strategy}'}")
    print(f"  limit: {args.limit}")
    print()

    # Fetch Polymarket state once (cheap — two HTTP calls)
    print("Fetching Polymarket positions + redeems…", end=" ", flush=True)
    positions = poly_positions(funder)
    # REDEEM events — last 7 days is plenty for 5-min markets
    since = int((asyncio.get_event_loop().time()) + 0)  # placeholder
    import time
    since_ts = int(time.time()) - 7 * 24 * 3600
    redeems = poly_activity_redeems(funder, since_ts)
    print(f"positions={len(positions)} redeems_7d={len(redeems)}")
    print()

    conn = await asyncpg.connect(env["DATABASE_URL"])
    try:
        strat = None if args.all_strategies else args.strategy
        trades = await fetch_pending_trades(conn, strat, args.limit)
        if not trades:
            print("Nothing to backfill — zero pending trades matched.")
            return
        print(f"Found {len(trades)} pending trades. Resolving…\n")

        market_cache: dict = {}  # slug -> gamma market row (or None)
        resolved_ct = 0
        skipped_ct = 0
        wins_ct = 0
        losses_ct = 0
        total_pnl = 0.0

        for t in trades:
            tid = str(t["metadata"].get("token_id") or "")[:16]
            slug = str(t["metadata"].get("market_slug") or "")[:32]
            dir_ = t["direction"] or "?"
            stake = float(t["stake_usd"] or 0)
            hdr = f"  #{t['id']:>5} {dir_:4} stake=${stake:5.2f} slug={slug:<32} tid={tid:<16}"

            # Tier 1 → 2 → 3
            result = resolve_from_tier1(t, positions)
            if not result:
                result = resolve_from_tier2(t, redeems, market_cache)
            if not result:
                result = await resolve_from_tier3(t, conn)

            if not result:
                print(f"{hdr}  SKIP  no match in any tier")
                skipped_ct += 1
                continue

            outcome, pnl, reason = result
            marker = "✓" if args.apply else "·"
            print(f"{hdr}  {marker} {outcome:4} pnl=${pnl:+7.2f}  [{reason}]")

            if args.apply:
                written = await apply_update(conn, t["id"], outcome, pnl)
                if not written:
                    print(f"       └─ UPDATE touched 0 rows (race with live reconciler? skipping)")
                    skipped_ct += 1
                    continue

            resolved_ct += 1
            if outcome == "WIN":
                wins_ct += 1
            else:
                losses_ct += 1
            total_pnl += pnl

        print()
        print("=" * 60)
        verb = "RESOLVED" if args.apply else "WOULD RESOLVE"
        print(f"{verb}: {resolved_ct} ({wins_ct}W / {losses_ct}L)  net=${total_pnl:+.2f}")
        print(f"SKIPPED:  {skipped_ct}")
        if not args.apply and resolved_ct > 0:
            print()
            print("Dry-run only. Re-run with --apply to write these changes.")
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write UPDATEs. Default is dry-run (print plan only).",
    )
    p.add_argument(
        "--strategy",
        default="v8_champion",
        help="Strategy to target (default: v8_champion).",
    )
    p.add_argument(
        "--all-strategies",
        action="store_true",
        help="Ignore --strategy filter, backfill every pending live trade.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max trades to consider (default: 500).",
    )
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
