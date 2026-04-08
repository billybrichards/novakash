# v9.0 Definitive Analysis & v10 Proposal

**Date:** 2026-04-08 11:00 UTC
**Source:** `trade_bible` table (definitive source of truth)
**Period:** 2026-04-07 20:00 to 2026-04-08 11:00 UTC (~15 hours)

## How to Query the Bible

```sql
-- The definitive source of truth for all live trade outcomes
SELECT * FROM trade_bible WHERE is_live = true ORDER BY resolved_at;

-- By config version
SELECT config_version, eval_tier, count(*),
       count(*) FILTER (WHERE trade_outcome='WIN') as wins,
       ROUND(SUM(pnl_usd)::numeric, 2) as pnl
FROM trade_bible WHERE is_live = true
GROUP BY config_version, eval_tier;

-- Tag: use "bible" or "source-of-truth" to reference this table
```

---

## Definitive Results

| Config | Tier | W | L | WR | PnL |
|--------|------|---|---|------|-----|
| v9.0 | **GOLDEN** | **16** | **5** | **76%** | **+$21.30** |
| v9.0 | EARLY_CASCADE | 4 | 10 | 29% | -$41.71 |
| v8.0 | v8_confirmed | 7 | 3 | 70% | -$0.52 |
| v8.0 | v8_early | 0 | 1 | 0% | -$4.26 |
| **ALL** | | **27** | **19** | **59%** | **-$25.19** |

### Key Corrections from Earlier Claims

| Claim | Reality |
|-------|---------|
| "Golden zone 100% WR (6/6)" | **76% WR (16W/5L)** — morning resolutions were v9 golden trades |
| "Morning losses all old v8" | **Mixed** — 14 were v9 EARLY, 5 were v9 GOLDEN losses |
| "94.7% agreement WR" | **76% golden, 29% early** — overnight market is different |
| "OAK binary garbage" | **True** — OAK still outputs 1.0000 at T-60 |
| "DUNE much better" | **True** — DUNE outputs 0.9225 (continuous, not binary) |

---

## Why 76% Not 94%?

The historical 94.7% WR was from **100 daytime windows on Apr 7** (16:26-17:11 UTC). Overnight (23:00-11:00 UTC) is a different market:

1. **Thin CLOB liquidity** — GTC orders sit unfilled for minutes, prices stale
2. **Chainlink updates every ~30s** — in choppy overnight markets, direction can flip
3. **VPIN is noisier** — low volume means VPIN fluctuates more
4. **CASCADE regime is dangerous** — 4W/10L = 29% WR. High VPIN overnight = choppy, not directional

The golden zone is still **profitable** (+$21.30) at 76% WR with $0.65 cap (breakeven ~65%). But the early CASCADE zone is destroying money.

---

## DUNE Model — The Key to v10

### Current State
- **OAK:** P(UP) = 1.0000 (binary garbage, useless)
- **DUNE:** P(UP) = 0.9225 (continuous, 11 calibration levels, genuinely different predictions)

### DUNE Test Accuracy (from training)

| Delta | DUNE Acc | OAK Acc | Improvement |
|-------|----------|---------|-------------|
| T-30 | 83.5% | 77.7% | +5.8pp |
| T-60 | 75.9% | 69.6% | +6.3pp |
| T-90 | 73.2% | 61.7% | +11.5pp |
| T-120 | 70.6% | 59.4% | +11.2pp |
| T-180 | 67.9% | 58.3% | +9.6pp |
| T-240 | 56.4% | 53.0% | +3.4pp |

### Why DUNE Changes Everything

1. **Continuous probabilities** — can set dynamic caps based on P(correct), not flat tiers
2. **Better calibrated** — 11 levels vs OAK's binary 0/1
3. **Trained on Tiingo + Chainlink** — oracle-aligned features
4. **Asset-specific** — separate models for BTC/ETH/SOL/XRP

---

## v10 Proposal: DUNE-Gated Dynamic Pricing

### Architecture

```
Window opens → Continuous eval every 10s (T-240 to T-60)
  │
  ├─ G1: CL+TI Source Agreement (HARD GATE — keep from v9)
  │     When disagree → SKIP (this still works at ~80% accuracy)
  │
  ├─ G2: DUNE P(direction) >= threshold (REPLACES VPIN gate)
  │     Query DUNE at each eval: /v2/probability/cedar?seconds_to_close=N
  │     If P(agreed_direction) >= 0.65 → TRADE
  │     If P(agreed_direction) < 0.65 → WAIT (retry next eval)
  │
  ├─ G3: CoinGlass Veto (keep, soft gate)
  │
  ├─ G4: Dynamic Cap = f(DUNE_P, offset)
  │     cap = min(DUNE_P - 0.05, 0.75)  # 5pp safety margin below model confidence
  │     floor = $0.30, ceiling = $0.75
  │     Example: DUNE says 80% UP → cap = $0.75
  │     Example: DUNE says 65% UP → cap = $0.60
  │
  └─ G5: FAK at cap → FAK at cap+π → GTC at cap
```

### Key Changes from v9

| Component | v9.0 | v10 |
|-----------|------|-----|
| VPIN gate | 0.45 golden, 0.65 early | **REMOVED** — DUNE replaces VPIN as confidence signal |
| Early/Golden tiers | Two fixed tiers | **REMOVED** — DUNE gives per-window confidence |
| Dynamic cap | $0.55/$0.65 fixed | **DUNE-driven**: cap = P(correct) - 5pp |
| CASCADE handling | Disabled (29% WR) | **DUNE handles it** — if DUNE P < 0.65 in CASCADE, skip |
| Entry timing | First agreement at any offset | **DUNE must confirm** — agreement + DUNE P >= 0.65 |

### Expected Performance

Based on DUNE test accuracy at different offsets:

| Offset | DUNE Acc | With Agreement | Est. Cap | Est. WR |
|--------|----------|----------------|----------|---------|
| T-240 | 56.4% | ~70% | $0.55 | ~75% |
| T-180 | 67.9% | ~80% | $0.63 | ~85% |
| T-120 | 70.6% | ~82% | $0.65 | ~87% |
| T-90 | 73.2% | ~85% | $0.68 | ~90% |
| T-60 | 75.9% | ~87% | $0.70 | ~92% |
| T-30 | 83.5% | ~92% | $0.75 | ~95% |

### Implementation

```env
# v10 env vars
V10_DUNE_ENABLED=true
V10_DUNE_URL=http://3.98.114.0:8080/v2/probability/cedar
V10_DUNE_MIN_P=0.65           # min DUNE P(direction) to trade
V10_DUNE_CAP_MARGIN=0.05      # cap = DUNE_P - margin
V10_DUNE_CAP_FLOOR=0.30
V10_DUNE_CAP_CEILING=0.75
V9_SOURCE_AGREEMENT=true      # keep CL+TI agreement gate
```

### Rollback
```env
V10_DUNE_ENABLED=false         # falls back to v9.0 VPIN-based gates
```

---

## Immediate Actions

1. **Keep v9.0 golden-only running** — 76% WR is profitable at $0.65 cap
2. **Start logging DUNE predictions** alongside every signal evaluation
3. **Shadow-compare**: for each v9 trade, what would DUNE have said?
4. **After 100+ DUNE-shadowed windows**: validate DUNE accuracy matches test numbers
5. **Deploy v10**: once DUNE shadow confirms ≥70% accuracy at T-120+

---

## Bible Table Reference

Access the definitive source of truth:
```sql
-- All data
SELECT * FROM trade_bible ORDER BY resolved_at DESC;

-- v9 golden zone performance
SELECT * FROM trade_bible WHERE eval_tier = 'GOLDEN' AND config_version = 'v9.0';

-- By hour
SELECT EXTRACT(HOUR FROM resolved_at) as hr,
       count(*) FILTER (WHERE trade_outcome='WIN'),
       count(*) FILTER (WHERE trade_outcome='LOSS')
FROM trade_bible GROUP BY hr ORDER BY hr;
```

Tag in conversations: use `bible` or `source-of-truth` to reference `trade_bible` table.
