# Runtime Overrides — Overnight Strategies (2026-05-01)

This document captures the exact Hub-API runtime overrides Billy can apply
to flip each new GHOST strategy to LIVE **after personal review of shadow
data**. Per `feedback_no_auto_model_promotion.md`: **NEVER auto-promote**.
Each strategy must clear the promotion gate before LIVE.

## Promotion gate (apply to ALL three strategies)

After 7 days in GHOST mode, check:

1. **WR > 70%** (above payoff break-even — see `feedback_payoff_math.md`)
2. **n > 30 real-trade decisions** (not raw eval ticks — see
   `reference_per_tick_methodology.md`)
3. **Net positive PnL** computed from real fill_price math
   (`WIN = (1 − fill) × (stake / fill) − 0.072 × stake`,
   `LOSS = −stake`) — DB `pnl_usd` is 4× inflated, do NOT use.
4. **Promote one strategy at a time**. If two specialist strategies fire on
   overlapping signals, the second one promotes only after the first has run
   LIVE for 7 days without regression.
5. **Billy explicit sign-off** (chat-confirmed, not session-implicit).

If any check fails, leave in GHOST and revisit after the next analysis cycle.

## Hub API: how to flip GHOST → LIVE

Hub: `http://16.54.141.121:8091` (direct, NOT nginx proxy).
Auth: `POST /auth/login {"username":"billy","password":"..."}` → access_token (15min).

```bash
# Get token
TOKEN=$(curl -s -X POST http://16.54.141.121:8091/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"billy","password":"..."}' | jq -r '.access_token')

# Flip strategy mode (replace <strategy_id>)
curl -X PUT http://16.54.141.121:8091/api/strategies/<strategy_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mode": "LIVE", "runtime_overrides": { ... }}'
```

If `/api/strategies/<id>` is not the canonical write endpoint on the current
hub build, use `/api/trading-config/{id}` per `reference_config_layering.md`
and restart the engine for DB-layer overrides to take effect.

---

## Strategy 1 — `v9_cascade_fade_early`

**Sweet spot:** CASCADE × DOWN × T-181-240 × dist[0.10-0.20] × k=1
**Forecast:** 1-2 fires/day. WR target ≥ 65% (Hub #305 measured 65.7%).

### LIVE-flip override (apply ONLY after promotion gate passes)

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 181,
    "max_offset_sec": 240,
    "tradeable_v4_regimes": ["risk_off", "volatile_trend"],
    "block_down_vpin_regimes": ["TRANSITION", "CALM", "NORMAL", "NORMAL_TREND", "LOW_VOL"],
    "up_min_fill_price": 1.01,
    "down_min_fill_price": 0.15,
    "lgb_dist_min_down": 0.10,
    "lgb_dist_min_up": 1.01,
    "min_consecutive_pass_ticks": 1,
    "post_loss_cooldown_min": 30,
    "max_collateral_pct": 0.05,
    "fraction": 0.025
  }
}
```

### Conservative-first variant (for first 24h LIVE)

Half the cap, narrow the regime to `risk_off` only:

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 181,
    "max_offset_sec": 240,
    "tradeable_v4_regimes": ["risk_off"],
    "block_down_vpin_regimes": ["TRANSITION", "CALM", "NORMAL", "NORMAL_TREND", "LOW_VOL"],
    "up_min_fill_price": 1.01,
    "lgb_dist_min_down": 0.10,
    "min_consecutive_pass_ticks": 1,
    "max_collateral_pct": 0.025,
    "fraction": 0.02
  }
}
```

### Kill-switch override (if shadow shows bleed)

Block all firing without removing the strategy:

```json
{
  "runtime_overrides": {
    "lgb_dist_min_down": 1.01
  }
}
```

---

## Strategy 2 — `v10_up_late_window`

**Sweet spot:** TRANSITION × UP × T-61-120 × k=3
**Forecast:** 1-3 fires/day. WR target ≥ 70% (Hub #304 measured 75% on real trades).

### LIVE-flip override

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 60,
    "max_offset_sec": 120,
    "tradeable_v4_regimes": ["chop", "volatile_trend"],
    "block_up_vpin_regimes": ["CASCADE"],
    "down_min_fill_price": 1.01,
    "up_min_fill_price": 0.20,
    "lgb_dist_min_up": 0.10,
    "lgb_dist_min_down": 1.01,
    "min_consecutive_pass_ticks": 3,
    "post_loss_cooldown_min": 20,
    "max_collateral_pct": 0.05,
    "fraction": 0.025
  }
}
```

### Conservative-first variant (for first 24h LIVE)

Tighten conviction floor to dist >= 0.13 (matches v10_lgb_only's UP floor),
half the cap:

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 60,
    "max_offset_sec": 120,
    "tradeable_v4_regimes": ["chop", "volatile_trend"],
    "block_up_vpin_regimes": ["CASCADE"],
    "down_min_fill_price": 1.01,
    "lgb_dist_min_up": 0.13,
    "min_consecutive_pass_ticks": 3,
    "max_collateral_pct": 0.025,
    "fraction": 0.02
  }
}
```

### Kill-switch override

```json
{
  "runtime_overrides": {
    "lgb_dist_min_up": 1.01
  }
}
```

---

## Strategy 3 — `all3_strong_cascade_down`

**Sweet spot:** v9 + v10 + v12 ALL agree × CASCADE × DOWN × any T-band × k=1
**Forecast:** 0.2-1 fires/day (sparse). WR target ≥ 80% (Hub #307 measured 84.6% n=52 — exploratory).

### LIVE-flip override (only after extended ghost — sparse strategy needs more samples)

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 24,
    "max_offset_sec": 240,
    "tradeable_v4_regimes": ["risk_off", "volatile_trend", "chop"],
    "block_down_vpin_regimes": ["TRANSITION", "CALM", "NORMAL", "NORMAL_TREND", "LOW_VOL"],
    "up_min_fill_price": 1.01,
    "combo_min_dist_v9": 0.10,
    "combo_min_dist_v10": 0.10,
    "combo_min_dist_v12": 0.10,
    "min_consecutive_pass_ticks": 1,
    "post_loss_cooldown_min": 25,
    "max_collateral_pct": 0.05,
    "fraction": 0.025
  }
}
```

### Conservative-first variant (recommended — n=52 still exploratory)

Triple the per-model conviction floor to dist >= 0.15 each:

```json
{
  "mode": "LIVE",
  "runtime_overrides": {
    "min_offset_sec": 24,
    "max_offset_sec": 240,
    "tradeable_v4_regimes": ["risk_off", "volatile_trend"],
    "block_down_vpin_regimes": ["TRANSITION", "CALM", "NORMAL", "NORMAL_TREND", "LOW_VOL"],
    "up_min_fill_price": 1.01,
    "combo_min_dist_v9": 0.15,
    "combo_min_dist_v10": 0.15,
    "combo_min_dist_v12": 0.15,
    "min_consecutive_pass_ticks": 1,
    "max_collateral_pct": 0.025,
    "fraction": 0.02
  }
}
```

### Kill-switch override

Set any one of the three model-floors to 1.01 — the AND-gate fails → SKIP:

```json
{
  "runtime_overrides": {
    "combo_min_dist_v9": 1.01
  }
}
```

---

## Cross-cutting notes

### Per-direction key trap (`feedback_override_keys.md`)

The engine reads `lgb_dist_min_up` and `lgb_dist_min_down` (PER-DIRECTION).
Setting `lgb_dist_min` (single key) is **silently ignored**. Always use
the per-direction keys in overrides above. Same trap may apply to other
gate-params — grep `_gp.get_float("KEY"...)` in `engine/strategies/configs/`
before adding novel keys.

### Direction lock (`reference_eval_offset_verified.md` + code comment v9_ensemble.py:1121-1196)

`up_min_fill_price = 1.01` and `down_min_fill_price = 1.01` are NOT
VHC-bypassable (enforced AFTER all VHC bypass logic). These are the only
clean direction-lock mechanisms.

`block_up_vpin_regimes` / `block_down_vpin_regimes` ARE VHC-bypassable —
they should be used as **regime guards** (block known anti-cells like
UP×CASCADE = -$71) but NOT as the primary direction lock.

### eval_offset = SECONDS-TO-CLOSE (T-minus)

Verified via real fires (Hub note #305 + memory `reference_eval_offset_verified.md`).

| `min_offset_sec` / `max_offset_sec` | sec-to-close | sec-from-open (300-eval_offset) |
|---|---|---|
| 24-120  | last 1-2 min before close | 180-276s after open |
| 31-120  | last 1-2 min (v12_combo current) | 180-269s |
| 60-120  | T-1-2min (v10_up_late) | 180-240s |
| 181-240 | first minute after open | 60-119s |

**Strategy comparison table** (`PR #446`) uses `300 − eval_offset` (sec-from-open) —
opposite convention. Don't cross-read the two without converting.

### Wallet-truth before any P&L claim

DB `pnl_usd` is 4× inflated for wins (regression #8). Real P&L:
`scripts/ops/wallet_truth.py` on Montreal (RDS access). USDC + pUSD = total.
Memory: `feedback_wallet_truth_authority.md` + `reference_wallet_usdc_only.md`.
