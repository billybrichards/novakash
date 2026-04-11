"""
Macro Observer — Standalone Railway Service

Polls market conditions every 60 seconds, calls a self-hosted LLM via an
OpenAI-compatible HTTP endpoint, writes a MacroSignal to the macro_signals
DB table.

The engine (Montreal) reads the latest macro_signals row every window.
No coupling — DB is the only interface.

## LLM backend

Originally targeted Anthropic Claude Sonnet 4.6. Swapped to a self-hosted
Qwen 3.5 122B (abliterated, llama-server build) for three reasons:
  1. Anthropic API calls were timing out on Railway, causing every row
     to be a fallback NEUTRAL/0/ALLOW_ALL/1.0 — no actual macro filter.
  2. Self-hosted eliminates the 60s Anthropic API spend while adding
     no new Railway cost.
  3. Qwen 3.5 122B with reasoning disabled handles the bias-classifier
     task in ~0.8s and is more than capable for a fixed-schema output.

### Reasoning-model gotcha (CRITICAL)

Qwen 3.5 is a reasoning model. By default, `/v1/chat/completions` returns
ALL output in a non-standard `reasoning_content` field and leaves
`content` empty until the model finishes thinking. For a 2048-token
budget the thinking alone burns the whole budget and the call returns
empty (finish_reason=length). The fix is to disable thinking via the
chat template kwarg: `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`.
With thinking off, latency drops from ~46s → ~0.8s and the response
goes into `content` as normal. See `call_llm()` below.

Environment variables:
  DATABASE_URL       — Railway postgres connection string
  QWEN_BASE_URL      — Qwen endpoint base URL (default: http://194.228.55.129:39633/v1)
  QWEN_API_KEY       — Bearer token for the Qwen endpoint
  QWEN_MODEL         — model ID (default: qwen35-122b-abliterated)
  QWEN_MAX_TOKENS    — response cap (default: 512, NOT 2048 — we don't
                       need a thinking budget because thinking is off)
  QWEN_TIMEOUT_S     — HTTP timeout (default: 60)
  POLL_INTERVAL      — seconds between calls (default: 60)
  LOG_LEVEL          — debug|info|warning (default: info)

  ANTHROPIC_API_KEY  — still read so the window evaluator (Telegram)
                       can optionally fall back to Claude for long-form
                       commentary if QWEN_API_KEY is unset. The main
                       bias classifier no longer touches Anthropic.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import asyncpg
import structlog
from openai import AsyncOpenAI

log = structlog.get_logger()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# ─── Qwen (self-hosted OpenAI-compatible) config ──────────────────────────
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "http://194.228.55.129:39633/v1")
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen35-122b-abliterated")
QWEN_MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "1536"))
QWEN_TIMEOUT_S = float(os.environ.get("QWEN_TIMEOUT_S", "60"))

# Legacy Anthropic config — still read so the evaluator can fall back
# to Claude if the Qwen endpoint is unreachable. The main bias classifier
# always uses Qwen.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

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

        # Per-timescale macro bias map (added April 2026 for the 5m/15m/1h/4h
        # multi-horizon upgrade). See migrations/add_macro_signals_timescale_map.sql
        # for the full rationale. The column is nullable — NULL rows are
        # from pre-upgrade observer builds or the fallback path when the LLM
        # endpoint is unreachable. Readers must handle both shapes.
        #
        # Self-healing by design: this runs on every container startup, so a
        # fresh deploy applies the migration automatically without requiring
        # a manual `psql` step or Railway CLI ceremony. IF NOT EXISTS makes
        # it safe to re-run indefinitely.
        await conn.execute("""
            ALTER TABLE macro_signals
            ADD COLUMN IF NOT EXISTS timescale_map JSONB;
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_macro_signals_timescale_map
                ON macro_signals USING gin (timescale_map jsonb_path_ops);
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


# ─── LLM Call (Qwen 3.5 122B via OpenAI-compatible endpoint) ──────────────────

# Timescales the LLM must analyse independently in every call. These match
# the horizons the novakash margin engine trades on (5m scalp, 15m fee-aware,
# 1h swing, 4h vol breakout) and the timescales the timesfm /v4/snapshot
# surface exposes. See docs/V4_MARGIN_ENGINE_INTEGRATION.md for the decision
# reference mapping these bias fields to engine gates.
MACRO_TIMESCALES = ("5m", "15m", "1h", "4h")

SYSTEM_PROMPT = """You are a macro trend analyser for a BTC multi-timescale trading system.

You assess the directional bias for FOUR separate trading horizons in the same
session, using the same market data:

  5m   — scalp,     next 5-15 minutes,  order-flow / microstructure driven
  15m  — fee-aware, next 15-45 minutes, needs expected move to clear fee wall
  1h   — swing,     next 1-3 hours,     requires trend alignment
  4h   — position,  next 4-12 hours,    macro + funding + oracle-bias driven

The horizons are INDEPENDENT — you can be BULL 1h and BEAR 5m at the same time
if the short-term has overextended against the longer trend. Conversely a
fresh breakout can be BULL 5m before the 1h composite has confirmed. Commit
to NEUTRAL when the horizon genuinely lacks signal — do not reuse the bias
from a different horizon just to avoid saying "I don't know".

You also produce an `overall` synthesis block: a single-horizon view for
consumers that don't want per-timescale nuance (dashboards, alerts,
backward-compat code paths). The overall bias should reflect the horizon
most relevant to "right now" — usually 15m-1h — and should not simply
echo any one per-timescale block.

Return ONLY valid JSON. Never invent data. If inputs are missing, lower
the confidence for affected horizons."""

USER_PROMPT_TEMPLATE = """Analyse this market data and return a MultiMacroSignal JSON.

{payload}

Return JSON with this shape (values chosen per the rules below):
{{
  "timescales": {{
    "5m":  {{"bias":"BULL|BEAR|NEUTRAL","confidence":<int 0-100>,"direction_gate":"ALLOW_ALL|SKIP_UP|SKIP_DOWN","threshold_modifier":<0.5-1.5>,"size_modifier":<0.5-1.5>,"override_active":<bool>,"reasoning":"<one sentence>"}},
    "15m": {{"...same fields..."}},
    "1h":  {{"...same fields..."}},
    "4h":  {{"...same fields..."}}
  }},
  "overall": {{"...same fields, synthesising across horizons..."}}
}}

Rules (apply to every timescale block AND the overall block):
- SKIP_DOWN = don't bet DOWN (use in bull macro at that horizon)
- SKIP_UP = don't bet UP (use in bear macro at that horizon)
- threshold_modifier < 1.0 = easier to enter bets aligned with that horizon's bias
- threshold_modifier > 1.0 = harder to enter bets against that horizon's bias
- override_active = true ONLY when confidence >= 80 at that horizon
- size_modifier > 1.0 ONLY when override_active = true at that horizon
- NEUTRAL + ALLOW_ALL + 1.0/1.0 + false is the correct output for a horizon
  with insufficient signal — prefer this over guessing

Horizon-specific guidance:
- 5m uses micro inputs: 15m price delta, taker_buy_ratio, VPIN, recent_spike
- 15m uses short-term: 1h price delta, funding, taker flow, upcoming event proximity
- 1h uses medium: 4h delta, oracle_up_ratio_1h/_4h, top_trader_long_pct
- 4h uses macro: 24h delta, funding_rate trend, oi_delta_4h_pct, session_stats"""


# JSON schema for llama-server's grammar-constrained sampling. The shared
# "horizon_bias" definition is reused via $ref so the five blocks
# (5m/15m/1h/4h/overall) all satisfy identical structural constraints.
_HORIZON_BIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["BULL", "BEAR", "NEUTRAL"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "direction_gate": {
            "type": "string",
            "enum": ["ALLOW_ALL", "SKIP_DOWN", "SKIP_UP"],
        },
        "threshold_modifier": {"type": "number", "minimum": 0.5, "maximum": 1.5},
        "size_modifier": {"type": "number", "minimum": 0.5, "maximum": 1.5},
        "override_active": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "bias", "confidence", "direction_gate",
        "threshold_modifier", "size_modifier", "override_active", "reasoning",
    ],
}

MACRO_SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "timescales": {
            "type": "object",
            "properties": {
                "5m":  _HORIZON_BIAS_SCHEMA,
                "15m": _HORIZON_BIAS_SCHEMA,
                "1h":  _HORIZON_BIAS_SCHEMA,
                "4h":  _HORIZON_BIAS_SCHEMA,
            },
            "required": list(MACRO_TIMESCALES),
        },
        "overall": _HORIZON_BIAS_SCHEMA,
    },
    "required": ["timescales", "overall"],
}


# Module-level async client, lazily initialised in call_llm() so
# imports don't fail if QWEN_API_KEY is unset at import time.
_llm_client: Optional[AsyncOpenAI] = None


def _get_llm_client() -> Optional[AsyncOpenAI]:
    """Return a cached AsyncOpenAI client, or None if QWEN_API_KEY is missing."""
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    if not QWEN_API_KEY:
        return None
    _llm_client = AsyncOpenAI(
        base_url=QWEN_BASE_URL,
        api_key=QWEN_API_KEY,
        timeout=QWEN_TIMEOUT_S,
        max_retries=0,  # we handle retries at the poll-loop level
    )
    return _llm_client


async def call_llm(payload: dict) -> Optional[dict]:
    """
    Call the Qwen 3.5 122B endpoint for a MacroSignal JSON.

    Returns a dict with {signal, input_tokens, output_tokens, latency_ms, cost_usd}
    on success, or None on failure (connection error, timeout, schema
    violation). The caller treats None as "fall back to _fallback_signal()".

    Critical runtime behaviour:
      - `chat_template_kwargs={"enable_thinking": False}` disables Qwen 3's
        reasoning mode. Without this, the model routes output into a
        non-standard `reasoning_content` field (which the OpenAI SDK
        silently drops), blocking the visible `content` until thinking
        finishes. See the module docstring for the gotcha.
      - `response_format={"type": "json_schema", ...}` activates
        llama-server's grammar-constrained sampling, so the model CANNOT
        emit a malformed JSON — the tokens are rejected at generation
        time. No post-hoc markdown-fence stripping needed.
      - `cost_usd=0.0` because Qwen is self-hosted. Kept in the return
        shape so write_signal() doesn't need a schema migration.
    """
    client = _get_llm_client()
    if client is None:
        log.warning("llm.no_api_key", msg="QWEN_API_KEY unset — skipping call")
        return None

    prompt_text = USER_PROMPT_TEMPLATE.format(payload=json.dumps(payload, indent=2))

    start = time.time()
    try:
        resp = await client.chat.completions.create(
            model=QWEN_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt_text},
            ],
            max_tokens=QWEN_MAX_TOKENS,
            temperature=0.3,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "macro_signal",
                    "schema": MACRO_SIGNAL_SCHEMA,
                    "strict": True,
                },
            },
            # extra_body injects into the underlying JSON body — llama-server
            # reads chat_template_kwargs.enable_thinking at template-render
            # time to skip the <think>...</think> block entirely.
            extra_body={
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        elapsed_ms = int((time.time() - start) * 1000)

        content = resp.choices[0].message.content
        if not content:
            log.error(
                "llm.empty_content",
                finish_reason=resp.choices[0].finish_reason,
                completion_tokens=resp.usage.completion_tokens if resp.usage else None,
                note="model may have fallen back to reasoning mode — check enable_thinking flag",
            )
            return None

        parsed = json.loads(content)

        # Schema: {"timescales": {"5m": {...}, "15m": {...}, "1h": {...}, "4h": {...}}, "overall": {...}}
        # Grammar-constrained decoding guarantees the shape is present, but
        # defensive extraction here protects against any future schema drift.
        timescales_map = parsed.get("timescales") or {}
        overall = parsed.get("overall") or {}

        # Validate we actually got the 4 horizons we asked for. If not,
        # treat as failure — partial responses are a schema regression.
        missing = [ts for ts in MACRO_TIMESCALES if ts not in timescales_map]
        if missing:
            log.error(
                "llm.missing_timescales",
                missing=missing,
                content_preview=content[:300],
            )
            return None

        # The "overall" block feeds the existing flat top-level columns
        # (bias, confidence, direction_gate, etc.) so every current consumer
        # — the dashboard, the old /v4/macro reader, hub/api/v58_monitor —
        # keeps working unchanged. The per-timescale map goes into the new
        # JSONB `timescale_map` column (added by migration).
        usage = resp.usage
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0

        log.info(
            "llm.call_ok",
            overall_bias=overall.get("bias"),
            overall_confidence=overall.get("confidence"),
            timescale_biases={ts: timescales_map[ts].get("bias") for ts in MACRO_TIMESCALES},
            latency_ms=elapsed_ms,
            prompt_tokens=in_tok,
            completion_tokens=out_tok,
        )

        return {
            # `signal` is the flat overall block — backward compat with every
            # existing consumer that reads a single scalar bias.
            "signal": overall,
            # `timescale_map` is new — the per-horizon breakdown that
            # downstream consumers (v4 snapshot assembler) use to apply
            # timescale-aware gates.
            "timescale_map": timescales_map,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "latency_ms": elapsed_ms,
            "cost_usd": 0.0,  # self-hosted
        }

    except asyncio.TimeoutError:
        log.warning("llm.timeout", timeout_s=QWEN_TIMEOUT_S)
        return None
    except json.JSONDecodeError as e:
        log.error("llm.json_parse_error", error=str(e), content_preview=(content or "")[:200])
        return None
    except Exception as e:
        log.error("llm.error", error=str(e)[:200], error_type=type(e).__name__)
        return None


# Backwards-compat alias — some older code paths may still reference
# call_anthropic. The main loop call site below uses the new name.
call_anthropic = call_llm


# ─── Write Signal ─────────────────────────────────────────────────────────────

async def write_signal(pool: asyncpg.Pool, signal: dict, payload: dict, meta: dict) -> int:
    """
    Write a MacroSignal row to the macro_signals table.

    Schema contract (Phase 2 of the macro-observer upgrade):
      - The existing flat columns (`bias`, `confidence`, `direction_gate`,
        `threshold_modifier`, `size_modifier`, `override_active`, `reasoning`)
        are populated from the `signal` dict — which is the "overall"
        synthesis block from the LLM. Every existing consumer (dashboard,
        hub/api, /v4/macro older code path) keeps working unchanged.
      - `timescale_map` is a new JSONB column added by migration
        `add_macro_signals_timescale_map.sql`. It holds the per-horizon
        breakdown ({"5m": {...}, "15m": {...}, "1h": {...}, "4h": {...}}),
        enabling timescale-aware consumers to gate each horizon independently.

    `meta` is the rest of the `call_llm` return dict (input_tokens,
    output_tokens, latency_ms, cost_usd, plus the new `timescale_map`).
    """
    # Extract the per-timescale map from meta if present. It lives here
    # rather than in `signal` so the existing write_signal callers
    # (including _fallback_signal code paths) keep producing valid rows
    # without a dict comprehension change.
    timescale_map = meta.get("timescale_map")
    timescale_map_json = json.dumps(timescale_map) if timescale_map else None

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
                input_tokens, output_tokens, latency_ms, cost_usd,
                timescale_map
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,
                $30::jsonb
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
            json.dumps({"signal": signal, **{k: v for k, v in meta.items() if k != "timescale_map"}}),
            meta.get("input_tokens"),
            meta.get("output_tokens"),
            meta.get("latency_ms"),
            meta.get("cost_usd"),
            timescale_map_json,
        )
    return row_id


_FALLBACK_HORIZON_BIAS: dict = {
    "bias": "NEUTRAL",
    "confidence": 0,
    "direction_gate": "ALLOW_ALL",
    "threshold_modifier": 1.0,
    "size_modifier": 1.0,
    "override_active": False,
    "reasoning": "Fallback — Qwen LLM endpoint unreachable, no macro filter applied",
}


def _fallback_signal() -> dict:
    """
    Return a safe NEUTRAL bias when the LLM endpoint is unreachable. Shape
    matches what call_llm() returns on success — `signal` is the flat
    overall bias and `timescale_map` is the per-horizon map (every horizon
    set to the same safe NEUTRAL).

    This guarantees downstream consumers never see a NULL `timescale_map`
    column when they expect it populated.
    """
    return {
        "signal": dict(_FALLBACK_HORIZON_BIAS),
        "timescale_map": {ts: dict(_FALLBACK_HORIZON_BIAS) for ts in MACRO_TIMESCALES},
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
        "cost_usd": 0.0,
    }


# ─── Window Evaluator (v8.1.2) ────────────────────────────────────────────────
# Runs alongside the observer. After each oracle resolution, evaluates the window
# with full context and sends an analysis card to Telegram.

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EVAL_INTERVAL = int(os.environ.get("EVAL_INTERVAL", "60"))


async def send_telegram(text: str) -> bool:
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("evaluator.no_telegram_config")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
    except Exception as exc:
        log.warning("evaluator.telegram_failed", error=str(exc)[:100])
        return False


async def fetch_window_context(pool: asyncpg.Pool, window_ts: int, asset: str = "BTC") -> dict:
    """Gather all context for a resolved window."""
    ctx = {}
    async with pool.acquire() as conn:
        # Window prediction
        ctx["prediction"] = await conn.fetchrow(
            "SELECT * FROM window_predictions WHERE window_ts=$1 AND asset=$2",
            window_ts, asset
        )
        # Window snapshot
        ctx["snapshot"] = await conn.fetchrow(
            "SELECT * FROM window_snapshots WHERE window_ts=$1 AND asset=$2 AND timeframe='5m'",
            window_ts, asset
        )
        # Gate audit (all checkpoints)
        ctx["gates"] = await conn.fetch(
            "SELECT eval_offset, decision, gate_failed, vpin, delta_pct "
            "FROM gate_audit WHERE window_ts=$1 AND asset=$2 "
            "ORDER BY eval_offset DESC",
            window_ts, asset
        )
        # Trade (if any)
        ctx["trade"] = await conn.fetchrow(
            "SELECT direction, outcome, entry_price, stake_usd, pnl_usd, "
            "metadata->>'actual_fill_price' as fill, metadata->>'size_matched' as shares, "
            "metadata->>'entry_reason' as reason, metadata->>'v81_entry_cap' as cap "
            "FROM trades WHERE metadata->>'window_ts'=$1 AND outcome IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            str(window_ts)
        )
        # Previous 3 windows for streak context
        ctx["prev_windows"] = await conn.fetch(
            "SELECT window_ts, direction, poly_winner, trade_placed, vpin, skip_reason "
            "FROM window_snapshots WHERE window_ts < $1 AND asset=$2 AND timeframe='5m' "
            "ORDER BY window_ts DESC LIMIT 3",
            window_ts, asset
        )
        # Latest macro signal
        ctx["macro"] = await conn.fetchrow(
            "SELECT macro_bias, macro_confidence, macro_reasoning "
            "FROM macro_signals ORDER BY created_at DESC LIMIT 1"
        )
    return ctx


def build_eval_prompt(window_ts: int, ctx: dict) -> str:
    """Build the structured prompt for Claude Sonnet evaluation."""
    pred = ctx.get("prediction") or {}
    snap = ctx.get("snapshot") or {}
    trade = ctx.get("trade")
    gates = ctx.get("gates") or []
    prev = ctx.get("prev_windows") or []
    macro = ctx.get("macro") or {}

    oracle = pred.get("oracle_winner") or snap.get("poly_winner") or "?"
    ti_dir = pred.get("tiingo_direction", "?")
    cl_dir = pred.get("chainlink_direction", "?")
    ti_correct = pred.get("tiingo_correct")
    cl_correct = pred.get("chainlink_correct")
    sig_dir = pred.get("our_signal_direction") or snap.get("direction", "?")
    vpin = pred.get("vpin_at_close") or snap.get("vpin") or 0
    regime = pred.get("regime") or snap.get("regime", "?")
    v2_dir = pred.get("v2_direction", "?")
    v2_prob = pred.get("v2_probability")

    # Gate audit table
    gate_lines = []
    for g in gates:
        offset = g.get("eval_offset", "?")
        dec = g.get("decision", "?")
        failed = g.get("gate_failed", "")
        gvpin = g.get("vpin", 0)
        gdelta = g.get("delta_pct", 0)
        gate_lines.append(f"T-{offset}: {dec} {'(' + failed + ')' if failed else ''} VPIN={gvpin:.3f} d={gdelta:+.4f}%")

    # Trade info
    if trade:
        trade_info = (
            f"TRADED: {trade['direction']} at ${float(trade.get('fill') or trade.get('entry_price') or 0):.2f}, "
            f"{trade.get('shares', '?')} shares, cap ${trade.get('cap', '?')}\n"
            f"Outcome: {trade['outcome']}, P&L: ${float(trade.get('pnl_usd') or 0):.2f}"
        )
    elif pred.get("bid_unfilled"):
        trade_info = f"BID UNFILLED: signal passed gates, placed on CLOB at ${pred.get('our_entry_price', '?')}, no counterparty"
    else:
        skip = pred.get("skip_reason") or snap.get("skip_reason", "?")
        trade_info = f"SKIPPED: {skip}"

    # Previous windows
    prev_lines = []
    for p in prev:
        pw = p.get("poly_winner", "?")
        pd = p.get("direction", "?")
        pt = "TRADED" if p.get("trade_placed") else "SKIP"
        correct = "correct" if pd and pw and pd.upper() == pw.upper() else "wrong"
        prev_lines.append(f"{pt} {pd} → oracle {pw} ({correct})")

    prompt = f"""Analyse this resolved 5-min BTC window:

WINDOW: {window_ts} | Oracle resolved: {oracle}
Signal: {sig_dir} at VPIN {vpin:.3f} ({regime})
v2.2: {v2_dir} P={v2_prob:.2f if v2_prob else '?'} | Tiingo: {ti_dir} ({'correct' if ti_correct else 'wrong'}) | Chainlink: {cl_dir} ({'correct' if cl_correct else 'wrong'})

GATE AUDIT ({len(gates)} checkpoints):
{chr(10).join(gate_lines[:10])}{'... +' + str(len(gates)-10) + ' more' if len(gates) > 10 else ''}

{trade_info}

MACRO: bias={macro.get('macro_bias', '?')}, conf={macro.get('macro_confidence', '?')}

PREVIOUS 3 WINDOWS:
{chr(10).join(prev_lines) if prev_lines else 'No history'}

Evaluate concisely (4-5 sentences):
1. Was our signal/gate decision correct?
2. Key factor that determined the outcome (VPIN trend, delta flip, v2.2 accuracy)?
3. If traded: was entry timing and price optimal?
4. One actionable insight for the next window."""

    return prompt


async def evaluate_resolved_windows(pool: asyncpg.Pool):
    """Find newly resolved windows and evaluate them."""
    try:
        async with pool.acquire() as conn:
            # Find windows with oracle_winner set but not yet evaluated
            # Use a simple marker: check if we've already sent an eval
            rows = await conn.fetch("""
                SELECT wp.window_ts, wp.asset, wp.oracle_winner
                FROM window_predictions wp
                WHERE wp.oracle_winner IS NOT NULL
                  AND wp.created_at > NOW() - INTERVAL '15 minutes'
                  AND NOT EXISTS (
                    SELECT 1 FROM telegram_notifications tn
                    WHERE tn.notification_type = 'ai_window_eval'
                      AND tn.window_id = CONCAT('eval-', wp.window_ts::text)
                  )
                ORDER BY wp.window_ts DESC
                LIMIT 1
            """)

        if not rows:
            return

        for row in rows:
            wts = row["window_ts"]
            asset = row["asset"]
            oracle = row["oracle_winner"]

            log.info("evaluator.evaluating", window_ts=wts, oracle=oracle)

            ctx = await fetch_window_context(pool, wts, asset)
            prompt = build_eval_prompt(wts, ctx)

            # Call the LLM for a freeform commentary on the resolved window.
            # Unlike the bias classifier above, we WANT reasoning here —
            # the evaluator's value is the chain-of-thought analysis that
            # identifies what went right or wrong. Keep enable_thinking on
            # (the default) and give it a generous token budget.
            try:
                llm = _get_llm_client()
                if llm is None:
                    raise RuntimeError("QWEN_API_KEY unset")
                resp = await llm.chat.completions.create(
                    model=QWEN_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    # Generous budget because Qwen will think before
                    # emitting prose. The commentary itself is short
                    # (~100-200 tokens) but reasoning can eat 1000+.
                    max_tokens=4096,
                    temperature=0.5,
                )
                analysis = (resp.choices[0].message.content or "").strip()
                if not analysis:
                    # Reasoning ate the whole budget — fall back gracefully.
                    analysis = "AI evaluation unavailable (reasoning budget exceeded)"
            except Exception as exc:
                log.warning("evaluator.llm_failed", error=str(exc)[:100])
                analysis = "AI evaluation unavailable"

            # Build Telegram card
            pred = ctx.get("prediction") or {}
            trade = ctx.get("trade")
            ti_ok = "correct" if pred.get("tiingo_correct") else "wrong"
            cl_ok = "correct" if pred.get("chainlink_correct") else "wrong"

            from datetime import datetime as _dt
            wtime = _dt.fromtimestamp(wts + 300, tz=timezone.utc).strftime("%H:%M")

            if trade:
                outcome_emoji = "WIN" if trade["outcome"] == "WIN" else "LOSS"
                outcome_icon = "✅" if trade["outcome"] == "WIN" else "❌"
                pnl = float(trade.get("pnl_usd") or 0)
                trade_line = f"{outcome_icon} *{outcome_emoji}* `${pnl:+.2f}` at ${float(trade.get('fill') or trade.get('entry_price') or 0):.2f}"
            elif pred.get("bid_unfilled"):
                trade_line = "⏳ BID UNFILLED — no counterparty at cap"
            else:
                trade_line = f"🚫 SKIPPED — {(pred.get('skip_reason') or '?')[:50]}"

            card = (
                f"🔬 *{wtime} BTC* — Oracle: *{oracle}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{trade_line}\n"
                f"Tiingo: {pred.get('tiingo_direction', '?')} ({ti_ok}) | "
                f"Chainlink: {pred.get('chainlink_direction', '?')} ({cl_ok})\n"
                f"\n"
                f"{analysis}\n"
            )

            sent = await send_telegram(card)

            # Mark as evaluated
            if sent:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "INSERT INTO telegram_notifications (bot_id, location, window_id, "
                            "notification_type, message_text) VALUES ($1, $2, $3, $4, $5)",
                            "macro-observer", "evaluator", f"eval-{wts}",
                            "ai_window_eval", card[:500]
                        )
                except Exception:
                    pass

            log.info("evaluator.sent", window_ts=wts, oracle=oracle, sent=sent)

    except Exception as exc:
        log.error("evaluator.error", error=str(exc)[:200])


async def evaluator_loop(pool: asyncpg.Pool):
    """Background loop that evaluates resolved windows."""
    log.info("evaluator.started", interval=EVAL_INTERVAL)
    while True:
        try:
            await evaluate_resolved_windows(pool)
        except Exception as exc:
            log.error("evaluator.loop_error", error=str(exc)[:200])
        await asyncio.sleep(EVAL_INTERVAL)


# ─── Main Loop ────────────────────────────────────────────────────────────────

async def run_observer():
    log.info("macro_observer.starting", poll_interval=POLL_INTERVAL)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    await init_db(pool)

    # Start window evaluator as background task. Gated on both Telegram
    # credentials (needed to send the card) AND an LLM key (needed to
    # generate the commentary). Either Qwen or Anthropic satisfies the
    # LLM half — but the evaluator call site above uses Qwen, so in
    # practice this means QWEN_API_KEY.
    if TELEGRAM_BOT_TOKEN and (QWEN_API_KEY or ANTHROPIC_API_KEY):
        asyncio.create_task(evaluator_loop(pool))
        log.info("macro_observer.evaluator_enabled")
    else:
        log.warning(
            "macro_observer.evaluator_disabled",
            reason="missing TELEGRAM_BOT_TOKEN or LLM API key (QWEN_API_KEY/ANTHROPIC_API_KEY)",
        )

    async with aiohttp.ClientSession() as session:
        while True:
            loop_start = time.time()
            # Heartbeat for the docker-compose healthcheck. Touched at the
            # top of every poll iteration so a stuck loop (DB pool wedged,
            # asyncio.gather hanging on a fetcher, etc.) lets the file go
            # stale and the container is bounced by the restart-policy.
            try:
                with open("/tmp/observer.alive", "w") as _hb:
                    _hb.write(str(loop_start))
            except OSError:
                pass  # Healthcheck is best-effort; never block the loop
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

                # ── Call LLM (Qwen 3.5 122B via self-hosted endpoint) ──────
                # Both call_llm() and _fallback_signal() return the same
                # wrapper shape: {signal, timescale_map, input_tokens,
                # output_tokens, latency_ms, cost_usd}. The main loop
                # unwraps once, so fallback rows are persisted with the
                # exact same column coverage as real rows (including the
                # new per-timescale timescale_map JSONB).
                result = await call_llm(payload)
                if result is None:
                    result = _fallback_signal()
                    log.warning("macro_observer.fallback_rowed")

                signal = result["signal"]
                meta = {k: v for k, v in result.items() if k != "signal"}
                signal_id = await write_signal(pool, signal, payload, meta)
                log.info(
                    "macro_observer.signal_written",
                    id=signal_id,
                    bias=signal["bias"],
                    confidence=signal["confidence"],
                    gate=signal["direction_gate"],
                    override=signal["override_active"],
                    has_timescale_map=bool(meta.get("timescale_map")),
                )

            except Exception as e:
                log.error("macro_observer.loop_error", error=str(e))

            # ── Sleep until next poll ──────────────────────────────────────
            elapsed = time.time() - loop_start
            sleep_for = max(0, POLL_INTERVAL - elapsed)
            log.debug("macro_observer.sleeping", sleep_s=round(sleep_for, 1))
            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    asyncio.run(run_observer())
