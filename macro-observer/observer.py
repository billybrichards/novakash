"""
Macro Observer — Standalone Railway Service

Polls market conditions every 60 seconds, calls Anthropic claude-sonnet,
writes a MacroSignal to the macro_signals DB table.

The engine (Montreal) reads the latest macro_signals row every window.
No coupling — DB is the only interface.

Environment variables required:
  DATABASE_URL       — Railway postgres connection string
  ANTHROPIC_API_KEY  — Anthropic API key
  POLL_INTERVAL      — seconds between calls (default: 60)
  ANTHROPIC_TIMEOUT  — seconds before falling back to cache (default: 10)
  LOG_LEVEL          — debug|info|warning (default: info)
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import anthropic
import asyncpg
import structlog

log = structlog.get_logger()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
ANTHROPIC_TIMEOUT = int(os.environ.get("ANTHROPIC_TIMEOUT", "10"))

ANTHROPIC_MODEL = "claude-sonnet-4-5"
ANTHROPIC_MAX_TOKENS = 300

COINBASE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# ─── DB Setup ─────────────────────────────────────────────────────────────────

async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS macro_signals (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT now(),
                bias VARCHAR(8) NOT NULL,
                confidence INT NOT NULL,
                direction_gate VARCHAR(12) NOT NULL,
                threshold_modifier FLOAT NOT NULL DEFAULT 1.0,
                size_modifier FLOAT NOT NULL DEFAULT 1.0,
                override_active BOOL NOT NULL DEFAULT false,
                reasoning TEXT,
                oracle_up_ratio_1h FLOAT,
                oracle_up_ratio_4h FLOAT,
                btc_delta_1h FLOAT,
                btc_delta_4h FLOAT,
                btc_delta_15m FLOAT,
                coinbase_price FLOAT,
                kraken_price FLOAT,
                binance_price FLOAT,
                exchange_spread_usd FLOAT,
                funding_rate FLOAT,
                top_trader_long_pct FLOAT,
                taker_buy_ratio FLOAT,
                oi_delta_1h FLOAT,
                vpin_current FLOAT,
                recent_spike BOOL DEFAULT false,
                upcoming_event TEXT,
                raw_payload JSONB,
                raw_response JSONB,
                input_tokens INT,
                output_tokens INT,
                latency_ms INT,
                cost_usd FLOAT
            );
        """)

        # Add macro columns to window_snapshots if they don't exist
        for col, col_type in [
            ("macro_bias", "VARCHAR(8)"),
            ("macro_confidence", "INT"),
            ("macro_override_active", "BOOL"),
            ("macro_signal_id", "INT"),
            ("coinbase_price", "FLOAT"),
            ("exchange_spread_usd", "FLOAT"),
        ]:
            await conn.execute(f"""
                ALTER TABLE window_snapshots
                ADD COLUMN IF NOT EXISTS {col} {col_type};
            """)

        log.info("db.init_complete")


# ─── Data Fetchers ────────────────────────────────────────────────────────────

async def fetch_btc_prices(session: aiohttp.ClientSession) -> dict:
    """Fetch BTC price from Binance, Coinbase, and Kraken."""
    prices = {}

    async def _get(url, key, parser):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    prices[key] = parser(data)
        except Exception as e:
            log.warning("price_fetch.failed", source=key, error=str(e))

    await asyncio.gather(
        _get(BINANCE_URL, "binance", lambda d: float(d["price"])),
        _get(COINBASE_URL, "coinbase", lambda d: float(d["data"]["amount"])),
        _get(KRAKEN_URL, "kraken", lambda d: float(d["result"]["XXBTZUSD"]["c"][0])),
    )
    return prices


async def fetch_oracle_history(pool: asyncpg.Pool) -> dict:
    """Pull last 12 and 48 resolved Polymarket window outcomes."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT outcome, window_ts
            FROM market_data
            WHERE asset = 'BTC' AND timeframe = '5m'
              AND outcome IS NOT NULL
            ORDER BY window_ts DESC
            LIMIT 48
        """)

    outcomes = [r["outcome"] for r in rows]
    last_12 = outcomes[:12]
    last_48 = outcomes[:48]

    up_12 = sum(1 for o in last_12 if o == "UP")
    up_48 = sum(1 for o in last_48 if o == "UP")

    return {
        "last_12_outcomes": last_12,
        "last_48_summary": {"up": up_48, "down": len(last_48) - up_48, "total": len(last_48)},
        "up_ratio_1h": round(up_12 / len(last_12), 3) if last_12 else 0.5,
        "up_ratio_4h": round(up_48 / len(last_48), 3) if last_48 else 0.5,
    }


async def fetch_btc_deltas(pool: asyncpg.Pool) -> dict:
    """Calculate BTC price deltas at multiple timeframes from ticks_binance.

    Fix (v8.0 Phase 3): ticks_binance.ts is TIMESTAMPTZ, not a Unix integer.
    Previously the code passed `now_ts - 86400` (int) to a TIMESTAMPTZ column,
    causing silent failures ("missing price deltas"). Now uses interval arithmetic
    and compares datetime objects returned by asyncpg correctly.
    """
    now_dt = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        # Try ticks_binance first, fall back to window_snapshots
        try:
            rows = await conn.fetch("""
                SELECT price, ts
                FROM ticks_binance
                WHERE ts >= NOW() - INTERVAL '24 hours'
                ORDER BY ts ASC
                LIMIT 1000
            """)

            if not rows:
                raise ValueError("no ticks data")

            price_now = rows[-1]["price"]

            def _delta(seconds_ago: int):
                """Return % delta from `seconds_ago` seconds in the past to now."""
                from datetime import timedelta
                cutoff_dt = now_dt - timedelta(seconds=seconds_ago)
                # asyncpg returns ts as timezone-aware datetime
                past = next((r["price"] for r in rows if r["ts"] >= cutoff_dt), None)
                if past and past > 0:
                    return round((price_now - past) / past * 100, 4)
                return None

            return {
                "btc_price": price_now,
                "delta_15m": _delta(900),
                "delta_1h": _delta(3600),
                "delta_4h": _delta(14400),
                "delta_24h": _delta(86400),
            }
        except Exception:
            # Fall back to window_snapshots
            row = await conn.fetchrow("""
                SELECT btc_price, open_price, window_ts
                FROM window_snapshots
                WHERE asset = 'BTC' AND timeframe = '5m'
                ORDER BY window_ts DESC LIMIT 1
            """)
            if row:
                return {"btc_price": row["btc_price"], "delta_15m": None, "delta_1h": None, "delta_4h": None, "delta_24h": None}
            return {}


async def fetch_coinglass_snapshot(pool: asyncpg.Pool) -> dict:
    """Pull latest CoinGlass data from ticks_coinglass."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT *
            FROM ticks_coinglass
            ORDER BY created_at DESC LIMIT 1
        """)
        if not row:
            return {}

        # Also get 4h ago for OI trend
        row_4h = await conn.fetchrow("""
            SELECT oi_usd
            FROM ticks_coinglass
            WHERE created_at <= NOW() - INTERVAL '4 hours'
            ORDER BY created_at DESC LIMIT 1
        """)

    result = {
        "funding_rate": row.get("funding_rate"),
        "top_trader_long_pct": row.get("top_long_pct"),
        "crowd_long_pct": row.get("long_pct"),
        "taker_buy_ratio": row.get("taker_buy_usd") / (row.get("taker_buy_usd", 0) + row.get("taker_sell_usd", 1)) if row.get("taker_buy_usd") else None,
        "liq_long_usd": row.get("liq_long_usd"),
        "liq_short_usd": row.get("liq_short_usd"),
        "oi_usd": row.get("oi_usd"),
    }

    if row_4h and row.get("oi_usd") and row_4h.get("oi_usd"):
        oi_now = row.get("oi_usd", 0)
        oi_then = row_4h.get("oi_usd", oi_now)
        if oi_then > 0:
            result["oi_delta_4h_pct"] = round((oi_now - oi_then) / oi_then * 100, 3)

    return result


async def fetch_vpin_snapshot(pool: asyncpg.Pool) -> dict:
    """Get recent VPIN readings and trend."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT vpin, regime, window_ts
            FROM window_snapshots
            WHERE asset = 'BTC' AND timeframe = '5m'
            ORDER BY window_ts DESC LIMIT 6
        """)

    if not rows:
        return {}

    vpins = [r["vpin"] for r in rows if r["vpin"]]
    current = vpins[0] if vpins else None
    trend = None
    if len(vpins) >= 3:
        if vpins[0] > vpins[2]:
            trend = "rising"
        elif vpins[0] < vpins[2]:
            trend = "falling"
        else:
            trend = "flat"

    latest_regime = rows[0]["regime"] if rows else None
    regime_streak = 1
    if rows:
        for r in rows[1:]:
            if r["regime"] == latest_regime:
                regime_streak += 1
            else:
                break

    return {
        "vpin_current": round(current, 4) if current else None,
        "vpin_trend": trend,
        "regime_current": latest_regime,
        "regime_streak": f"{latest_regime} x{regime_streak}" if latest_regime else None,
    }


async def fetch_recent_spike(pool: asyncpg.Pool) -> bool:
    """Check if any recent window had a large move (>0.8%)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COUNT(*) as spike_count
            FROM window_snapshots
            WHERE asset = 'BTC' AND timeframe = '5m'
              AND window_ts >= EXTRACT(EPOCH FROM NOW() - INTERVAL '30 minutes')::bigint
              AND ABS(delta_pct) > 0.008
        """)
    return (row["spike_count"] > 0) if row else False


async def fetch_recent_ai_analyses(pool: asyncpg.Pool) -> list[str]:
    """Pull last 3 Claude pre-trade assessment summaries from ai_analyses."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT analysis_text, created_at
            FROM ai_analyses
            ORDER BY created_at DESC LIMIT 3
        """)
    return [r["analysis_text"][:300] for r in rows if r["analysis_text"]]


async def fetch_upcoming_events(pool: asyncpg.Pool) -> Optional[str]:
    """Check macro_events table for upcoming HIGH/EXTREME events in next 90 min."""
    async with pool.acquire() as conn:
        # Table might not exist yet — handle gracefully
        try:
            row = await conn.fetchrow("""
                SELECT event_name, impact,
                       EXTRACT(EPOCH FROM (event_time - NOW())) / 60 as minutes_until
                FROM macro_events
                WHERE event_time BETWEEN NOW() AND NOW() + INTERVAL '90 minutes'
                  AND impact IN ('HIGH', 'EXTREME')
                ORDER BY event_time ASC LIMIT 1
            """)
            if row:
                return f"{row['event_name']} ({row['impact']}) in {int(row['minutes_until'])} min"
        except Exception:
            pass
    return None


async def fetch_session_stats(pool: asyncpg.Pool) -> dict:
    """Get today's session wins/losses and drawdown streak."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE outcome = 'WIN') as wins,
                COUNT(*) FILTER (WHERE outcome = 'LOSS') as losses
            FROM trades
            WHERE is_live = true
              AND created_at >= NOW() - INTERVAL '24 hours'
              AND outcome IS NOT NULL
        """)

        # Drawdown streak — consecutive losses from most recent
        streak_rows = await conn.fetch("""
            SELECT outcome
            FROM trades
            WHERE is_live = true AND outcome IS NOT NULL
            ORDER BY created_at DESC LIMIT 10
        """)

    wins = row["wins"] if row else 0
    losses = row["losses"] if row else 0
    total = wins + losses
    wr = round(wins / total, 3) if total > 0 else None

    streak = 0
    for r in streak_rows:
        if r["outcome"] == "LOSS":
            streak += 1
        else:
            break

    return {"session_wins": wins, "session_losses": losses, "session_wr": wr, "drawdown_streak": streak}


# ─── Anthropic Call ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a macro trend analyser for a BTC 5-minute prediction market strategy.
You assess whether conditions favour UP or DOWN bets for the next 30-60 minutes.
Return ONLY valid JSON. Be calibrated — NEUTRAL is correct when there is genuine uncertainty.
Never invent data. If inputs are missing, lower your confidence accordingly."""

USER_PROMPT_TEMPLATE = """Analyse this market data and return a MacroSignal JSON:

{payload}

Return ONLY this JSON (no markdown, no explanation):
{{
  "bias": "BULL|BEAR|NEUTRAL",
  "confidence": <integer 0-100>,
  "direction_gate": "ALLOW_ALL|SKIP_DOWN|SKIP_UP",
  "threshold_modifier": <float 0.5-1.5>,
  "size_modifier": <float 0.5-1.5>,
  "override_active": <true if confidence >= 80, else false>,
  "reasoning": "<one sentence max>"
}}

Rules:
- SKIP_DOWN = don't bet DOWN (use in bull macro)
- SKIP_UP = don't bet UP (use in bear macro)
- threshold_modifier < 1.0 = easier to enter bets aligned with bias
- threshold_modifier > 1.0 = harder to enter bets against bias
- override_active = true ONLY when confidence >= 80
- size_modifier > 1.0 ONLY when override_active = true"""


async def call_anthropic(payload: dict) -> Optional[dict]:
    """Call Anthropic with timeout. Returns parsed signal dict or None."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt_text = USER_PROMPT_TEMPLATE.format(payload=json.dumps(payload, indent=2))

    start = time.time()
    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt_text}],
            timeout=ANTHROPIC_TIMEOUT,
        )
        elapsed_ms = int((time.time() - start) * 1000)

        raw_text = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        signal = json.loads(raw_text)

        cost = (msg.usage.input_tokens * 0.000003) + (msg.usage.output_tokens * 0.000015)

        log.info("anthropic.call_ok",
                 bias=signal.get("bias"),
                 confidence=signal.get("confidence"),
                 latency_ms=elapsed_ms,
                 cost_usd=round(cost, 5))

        return {
            "signal": signal,
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "latency_ms": elapsed_ms,
            "cost_usd": cost,
        }

    except anthropic.APITimeoutError:
        log.warning("anthropic.timeout", timeout_s=ANTHROPIC_TIMEOUT)
        return None
    except json.JSONDecodeError as e:
        log.error("anthropic.json_parse_error", error=str(e))
        return None
    except Exception as e:
        log.error("anthropic.error", error=str(e))
        return None


# ─── Write Signal ─────────────────────────────────────────────────────────────

async def write_signal(pool: asyncpg.Pool, signal: dict, payload: dict, meta: dict) -> int:
    """Write MacroSignal to macro_signals table. Returns new row ID."""
    async with pool.acquire() as conn:
        row_id = await conn.fetchval("""
            INSERT INTO macro_signals (
                bias, confidence, direction_gate,
                threshold_modifier, size_modifier, override_active, reasoning,
                oracle_up_ratio_1h, oracle_up_ratio_4h,
                btc_delta_1h, btc_delta_4h, btc_delta_15m,
                coinbase_price, kraken_price, binance_price, exchange_spread_usd,
                funding_rate, top_trader_long_pct, taker_buy_ratio, oi_delta_1h,
                vpin_current, recent_spike, upcoming_event,
                raw_payload, raw_response,
                input_tokens, output_tokens, latency_ms, cost_usd
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29
            ) RETURNING id
        """,
            signal.get("bias", "NEUTRAL"),
            signal.get("confidence", 0),
            signal.get("direction_gate", "ALLOW_ALL"),
            signal.get("threshold_modifier", 1.0),
            signal.get("size_modifier", 1.0),
            signal.get("override_active", False),
            signal.get("reasoning"),
            payload.get("oracle", {}).get("up_ratio_1h"),
            payload.get("oracle", {}).get("up_ratio_4h"),
            payload.get("price", {}).get("delta_1h"),
            payload.get("price", {}).get("delta_4h"),
            payload.get("price", {}).get("delta_15m"),
            payload.get("prices", {}).get("coinbase"),
            payload.get("prices", {}).get("kraken"),
            payload.get("prices", {}).get("binance"),
            payload.get("prices", {}).get("spread_coinbase_binance"),
            payload.get("coinglass", {}).get("funding_rate"),
            payload.get("coinglass", {}).get("top_trader_long_pct"),
            payload.get("coinglass", {}).get("taker_buy_ratio"),
            payload.get("coinglass", {}).get("oi_delta_4h_pct"),
            payload.get("vpin", {}).get("vpin_current"),
            payload.get("recent_spike", False),
            payload.get("upcoming_event"),
            json.dumps(payload),
            json.dumps({"signal": signal, **meta}),
            meta.get("input_tokens"),
            meta.get("output_tokens"),
            meta.get("latency_ms"),
            meta.get("cost_usd"),
        )
    return row_id


def _fallback_signal() -> dict:
    """Return a safe NEUTRAL signal when Anthropic is unavailable."""
    return {
        "bias": "NEUTRAL",
        "confidence": 0,
        "direction_gate": "ALLOW_ALL",
        "threshold_modifier": 1.0,
        "size_modifier": 1.0,
        "override_active": False,
        "reasoning": "Fallback — Anthropic unavailable, no macro filter applied",
    }


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def run_observer():
    log.info("macro_observer.starting", poll_interval=POLL_INTERVAL)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    await init_db(pool)

    async with aiohttp.ClientSession() as session:
        while True:
            loop_start = time.time()
            try:
                # ── Gather all data concurrently ──────────────────────────
                oracle, price_deltas, cg, vpin_data, spike, ai_notes, upcoming, session_stats, btc_prices = await asyncio.gather(
                    fetch_oracle_history(pool),
                    fetch_btc_deltas(pool),
                    fetch_coinglass_snapshot(pool),
                    fetch_vpin_snapshot(pool),
                    fetch_recent_spike(pool),
                    fetch_recent_ai_analyses(pool),
                    fetch_upcoming_events(pool),
                    fetch_session_stats(pool),
                    fetch_btc_prices(session),
                )

                # ── Compute exchange spread ────────────────────────────────
                spread = None
                if btc_prices.get("coinbase") and btc_prices.get("binance"):
                    spread = round(btc_prices["coinbase"] - btc_prices["binance"], 2)
                    oracle_divergence = "HIGH" if abs(spread) > 30 else "MEDIUM" if abs(spread) > 15 else "LOW"
                else:
                    oracle_divergence = "UNKNOWN"

                # ── Build payload ──────────────────────────────────────────
                payload = {
                    "oracle": {
                        "last_12_outcomes": oracle.get("last_12_outcomes", []),
                        "last_48_summary": oracle.get("last_48_summary", {}),
                        "up_ratio_1h": oracle.get("up_ratio_1h"),
                        "up_ratio_4h": oracle.get("up_ratio_4h"),
                    },
                    "price": {
                        "btc_price": price_deltas.get("btc_price"),
                        "delta_15m": price_deltas.get("delta_15m"),
                        "delta_1h": price_deltas.get("delta_1h"),
                        "delta_4h": price_deltas.get("delta_4h"),
                        "delta_24h": price_deltas.get("delta_24h"),
                    },
                    "prices": {
                        "binance": btc_prices.get("binance"),
                        "coinbase": btc_prices.get("coinbase"),
                        "kraken": btc_prices.get("kraken"),
                        "spread_coinbase_binance": spread,
                        "oracle_divergence_risk": oracle_divergence,
                    },
                    "coinglass": cg,
                    "vpin": vpin_data,
                    "recent_spike": spike,
                    "upcoming_event": upcoming,
                    "session": session_stats,
                    "recent_ai_notes": ai_notes,
                }

                # ── Call Anthropic ─────────────────────────────────────────
                result = await call_anthropic(payload)

                if result:
                    signal = result["signal"]
                    meta = {k: v for k, v in result.items() if k != "signal"}
                    signal_id = await write_signal(pool, signal, payload, meta)
                    log.info("macro_observer.signal_written",
                             id=signal_id,
                             bias=signal["bias"],
                             confidence=signal["confidence"],
                             gate=signal["direction_gate"],
                             override=signal["override_active"])
                else:
                    # Write fallback so engine always has a fresh row
                    fallback = _fallback_signal()
                    signal_id = await write_signal(pool, fallback, payload, {})
                    log.warning("macro_observer.fallback_written", signal_id=signal_id)

            except Exception as e:
                log.error("macro_observer.loop_error", error=str(e))

            # ── Sleep until next poll ──────────────────────────────────────
            elapsed = time.time() - loop_start
            sleep_for = max(0, POLL_INTERVAL - elapsed)
            log.debug("macro_observer.sleeping", sleep_s=round(sleep_for, 1))
            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    asyncio.run(run_observer())
