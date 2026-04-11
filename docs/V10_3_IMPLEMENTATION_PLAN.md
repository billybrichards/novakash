# v10.3 FINAL Implementation Plan — Full Decision Surface + CG Taker Gate

**Goal:** Implement the complete v10.1 decision surface from timesfm repo PLUS the CG taker flow hard gate from live alignment data.
**Branch:** `develop` (Montreal auto-deploys from develop)
**Estimated edge improvement:** +$20-35/day over v10.2
**Montreal rules:** All Polymarket API from Montreal VPS only (ca-central-1b). No local trading. Engine restart via systemctl only.

---

## Why Both: Decision Surface + Taker Gate

The v10.1 decision surface (909-line spec in timesfm repo) provides:
- Edge-weighted Kelly sizing (4 quality multipliers)
- Continuous offset sensitivity (per-20s granularity)
- 3-signal CG confirmation bonus (taker + OI + LSR)
- Polymarket spread gate
- Model quality monitors (ECE/skill from dashboard)

The CG taker flow data (from 719 live trades) adds:
- Hard "both opposing = SKIP" gate (58% WR → breakeven death zone)
- Taker opposing penalty (+0.05 threshold boost)

**Neither alone is optimal. Together they form the complete system.**

---

## Phase 1: Rewrite Gate System (engine/signals/gates.py)

The current gates.py is 470 lines with 4 gates. The new version will have 7 gates implementing the full decision surface.

### Step 1.1: Add GateContext Fields
- [ ] Add to GateContext dataclass:
  ```python
  cg_threshold_modifier: float = 0.0   # Set by TakerFlowGate
  cg_confirms: int = 0                 # Set by CGConfirmationGate
  cg_bonus: float = 0.0                # Set by CGConfirmationGate
  gamma_up_price: Optional[float] = None
  gamma_down_price: Optional[float] = None
  ```

**Files:** `engine/signals/gates.py` (GateContext, +5 lines)

### Step 1.2: Add DOWN Penalty to DuneConfidenceGate
- [ ] Read `V10_DOWN_PENALTY` from env (default `0.0`)
- [ ] In `_effective_threshold()`:
  ```python
  down_penalty = self._down_penalty if ctx.agreed_direction == "DOWN" else 0.0
  effective = base + offset_penalty + down_penalty + ctx.cg_threshold_modifier
  ```
- [ ] Change offset penalty to per-20s granularity (matching decision surface spec):
  ```python
  # Old: (offset-60)/120 * max_penalty
  # New: excess * 0.005 / 20, capped at max_penalty
  penalty = min(self._offset_penalty_max, (offset - 60) * 0.005 / 20)
  ```

**Files:** `engine/signals/gates.py` (DuneConfidenceGate, ~15 lines changed)

### Step 1.3: Create TakerFlowGate (NEW — from CG alignment data)
- [ ] New class replacing CoinGlassVetoGate
- [ ] Env vars: `V10_CG_TAKER_GATE` (bool), `V10_CG_TAKER_OPPOSING_PCT` (55), `V10_CG_SMART_OPPOSING_PCT` (52), `V10_CG_TAKER_OPPOSING_PENALTY` (0.05)
- [ ] Logic:
  1. Freshness check: `(now - cg.timestamp) > V10_CG_MAX_AGE_MS` → SKIP
  2. Calculate taker buy_pct = buy / (buy + sell) * 100
  3. For UP: taker_aligned = buy_pct > 55, taker_opposing = sell_pct > 55
  4. For DOWN: inverse
  5. Smart money: top_position_short_pct > 52 (for UP), etc.
  6. Matrix:
     - Both opposing → **HARD SKIP** (58% WR)
     - Taker opposing only → `ctx.cg_threshold_modifier = +0.05`
     - Taker aligned → `ctx.cg_threshold_modifier = -0.02` (included in CG bonus below)
     - Neutral → modifier = 0.0
- [ ] If `V10_CG_TAKER_GATE=false` → pass-through (backward compat)
- [ ] **Runs BEFORE DuneConfidenceGate** so modifier is available

**Files:** `engine/signals/gates.py` (+80 lines new class)

### Step 1.4: Create CGConfirmationGate (NEW — from decision surface spec)
- [ ] Implements the 3-signal CG confirmation bonus
- [ ] Env vars: `V10_CG_CONFIRM_BONUS` (0.02), `V10_CG_CONFIRM_MIN` (2)
- [ ] Logic (from decision surface Section 5, Gate 5):
  ```python
  confirms = 0
  if direction == "UP":
      if cg.taker_buy_volume - cg.taker_sell_volume > 0: confirms += 1  # net buying
      if cg.oi_delta_pct_1m > 0: confirms += 1                         # rising OI
      if cg.long_short_ratio > 1.0: confirms += 1                      # more longs
  else:  # DOWN
      if cg.taker_sell_volume - cg.taker_buy_volume > 0: confirms += 1
      if cg.oi_delta_pct_1m < 0: confirms += 1
      if cg.long_short_ratio < 1.0: confirms += 1
  if confirms >= 2:
      ctx.cg_bonus = 0.02
      ctx.cg_confirms = confirms
  ```
- [ ] The bonus is SUBTRACTED from the effective threshold (lowers the bar)
- [ ] Note: This is SEPARATE from the taker flow gate. TakerFlowGate handles the hard kill/penalty. CGConfirmationGate handles the soft bonus.

**Files:** `engine/signals/gates.py` (+40 lines new class)

### Step 1.5: Create SpreadGate (NEW — from decision surface spec)
- [ ] Kills trades when Polymarket orderbook spread > 8%
- [ ] Env var: `V10_MAX_SPREAD_PCT` (8)
- [ ] Logic:
  ```python
  if ctx.gamma_up_price and ctx.gamma_down_price:
      mid = (ctx.gamma_up_price + ctx.gamma_down_price) / 2
      spread = abs(ctx.gamma_up_price - ctx.gamma_down_price) / mid * 100
      if spread > self._max_spread_pct: return SKIP
  ```
- [ ] Requires passing Gamma prices into GateContext (from strategy)

**Files:** `engine/signals/gates.py` (+25 lines new class)

### Step 1.6: Update DynamicCapGate
- [ ] Lower ceiling from $0.70 → $0.68 (env default change)
- [ ] Already reads from `V10_DUNE_CAP_CEILING` env var

**Files:** `engine/signals/gates.py` (1 line default change)

---

## Phase 2: Edge-Weighted Kelly Sizing (engine/strategies/five_min_vpin.py)

This is the biggest behavioral change — replaces flat BET_FRACTION with the decision surface's composite Kelly formula.

### Step 2.1: Implement position_size() Function
- [ ] New function in `five_min_vpin.py` or separate `engine/signals/sizing.py`:
  ```python
  def edge_weighted_kelly(
      p_up: float,
      direction: str,
      seconds_to_close: int,
      regime: str,
      cg_confirms: int,
      bankroll: float,
      kelly_shrink: float = 0.50,
      cap_ceiling: float = 0.68,
  ) -> float:
      p_dir = p_up if direction == "UP" else (1.0 - p_up)
      edge = 2 * p_dir - 1
      base_kelly = max(0.0, kelly_shrink * edge)

      time_mult = max(0.7, 1.0 - (max(0, seconds_to_close - 60) / 120) * 0.3)
      dir_mult = 1.0 if direction == "UP" else 0.85
      cg_mult = 1.15 if cg_confirms >= 2 else 1.0
      regime_mult = 1.0 if regime in ("NORMAL", "LOW_VOL") else 0.85

      sized = base_kelly * time_mult * dir_mult * cg_mult * regime_mult
      return min(sized, cap_ceiling) * bankroll
  ```
- [ ] Env vars for overrides: `V10_KELLY_SHRINK` (0.50), `V10_KELLY_ENABLED` (bool, default false for safe rollout)

### Step 2.2: Wire Sizing into Trade Execution
- [ ] In `_execute_trade()` (~line 2690), replace:
  ```python
  # OLD: flat fraction
  stake = bankroll * runtime.bet_fraction
  ```
  With:
  ```python
  # NEW: edge-weighted Kelly (if enabled)
  if os.environ.get("V10_KELLY_ENABLED", "false").lower() == "true":
      stake = edge_weighted_kelly(
          p_up=ctx.dune_probability_up,
          direction=pipe_result.direction,
          seconds_to_close=ctx.eval_offset or 60,
          regime=regime,
          cg_confirms=ctx.cg_confirms,
          bankroll=bankroll,
      )
  else:
      stake = bankroll * runtime.bet_fraction  # fallback
  ```
- [ ] ABSOLUTE_MAX_BET still caps the final amount
- [ ] Log the Kelly components for debugging

**Files:** `engine/strategies/five_min_vpin.py` or new `engine/signals/sizing.py` (~60 lines)

### Step 2.3: Sizing Examples at $130 Bankroll

| Scenario | ELM P | Dir | T-offset | Regime | CG | Kelly Frac | Stake |
|---|---|---|---|---|---|---|---|
| Strong UP, CG confirmed | 0.82 | UP | T-60 | NORMAL | 3/3 | 0.368 | $47.84 → **$10** (capped) |
| Moderate UP | 0.70 | UP | T-60 | NORMAL | 1/3 | 0.200 | $26.00 → **$10** (capped) |
| Borderline UP, CG aligned | 0.66 | UP | T-120 | CASCADE | 2/3 | 0.117 | **$15.21** → **$10** (capped) |
| Weak DOWN, late | 0.35 | DOWN | T-150 | CALM | 0/3 | 0.067 | **$8.71** |
| Very weak signal | 0.55 | UP | T-180 | NORMAL | 0/3 | 0.024 | **$3.12** |

**Note:** At $130 bankroll with $10 ABSOLUTE_MAX_BET, Kelly sizing is effectively capped for strong signals. The real value shows for weaker signals where Kelly naturally sizes DOWN (e.g., $3.12 for a weak late entry vs flat $9.75).

---

## Phase 3: Update Pipeline Wiring (engine/strategies/five_min_vpin.py)

### Step 3.1: New 7-Gate Pipeline
- [ ] Change pipeline from 4 gates to 7:
  ```python
  from signals.gates import (
      GateContext, GatePipeline, SourceAgreementGate,
      TakerFlowGate, CGConfirmationGate,
      DuneConfidenceGate, SpreadGate, DynamicCapGate,
  )

  pipeline = GatePipeline([
      SourceAgreementGate(),          # G1: CL+TI agree (94.7% WR)
      TakerFlowGate(),                # G2: CG taker hard gate + modifier
      CGConfirmationGate(),           # G3: CG 3-signal bonus
      DuneConfidenceGate(dune_client=self._timesfm_v2),  # G4: ELM + all modifiers
      SpreadGate(),                   # G5: Polymarket spread check
      DynamicCapGate(),               # G6: cap = dune_p - 0.05
  ])
  ```

### Step 3.2: Pass Gamma Prices into GateContext
- [ ] Add Gamma prices to GateContext construction:
  ```python
  gamma_up_price=getattr(self._gamma, 'up_price', None) if self._gamma else None,
  gamma_down_price=getattr(self._gamma, 'down_price', None) if self._gamma else None,
  ```

### Step 3.3: Update Entry Reason String
- [ ] Include CG info + sizing:
  ```python
  f"v10_DUNE_{regime}_T{offset}_{order_type}_CG{ctx.cg_threshold_modifier:+.02f}_K{kelly_frac:.0%}"
  ```

**Files:** `engine/strategies/five_min_vpin.py` (~25 lines changed around line 560-600)

---

## Phase 4: .env Configuration

### Step 4.1: Complete .env Diff (v10.2 → v10.3 FINAL)
```diff
# === THRESHOLDS (ELM v3 calibrated) ===
- V10_DUNE_MIN_P=0.75
+ V10_DUNE_MIN_P=0.65
- V10_TRANSITION_MIN_P=0.85
+ V10_TRANSITION_MIN_P=0.70
- V10_CASCADE_MIN_P=0.80
+ V10_CASCADE_MIN_P=0.72
- V10_NORMAL_MIN_P=0.78
+ V10_NORMAL_MIN_P=0.65
- V10_LOW_VOL_MIN_P=0.78
+ V10_LOW_VOL_MIN_P=0.65
- V10_TRENDING_MIN_P=0.80
+ V10_TRENDING_MIN_P=0.72
- V10_CALM_MIN_P=0.80
+ V10_CALM_MIN_P=0.72
- V10_OFFSET_PENALTY_MAX=0.05
+ V10_OFFSET_PENALTY_MAX=0.06
- V10_DUNE_CAP_CEILING=0.70
+ V10_DUNE_CAP_CEILING=0.68

# === NEW: Direction penalty ===
+ V10_DOWN_PENALTY=0.03

# === NEW: Taker flow gate (from CG alignment data) ===
+ V10_CG_TAKER_GATE=true
+ V10_CG_TAKER_OPPOSING_PCT=55
+ V10_CG_SMART_OPPOSING_PCT=52
+ V10_CG_TAKER_OPPOSING_PENALTY=0.05
+ V10_CG_MAX_AGE_MS=120000

# === NEW: CG confirmation bonus (from decision surface) ===
+ V10_CG_CONFIRM_BONUS=0.02
+ V10_CG_CONFIRM_MIN=2

# === NEW: Spread gate ===
+ V10_MAX_SPREAD_PCT=8

# === NEW: Edge-weighted Kelly sizing ===
+ V10_KELLY_ENABLED=false
+ V10_KELLY_SHRINK=0.50
```

---

## Phase 5: Testing

### Step 5.1: Unit Tests — TakerFlowGate
- [ ] Both opposing → SKIP
- [ ] Taker aligned + smart aligned → modifier -0.02
- [ ] Taker aligned + smart opposing → modifier -0.02 (taker dominates)
- [ ] Taker opposing + smart aligned → modifier +0.05
- [ ] CG disconnected → pass-through
- [ ] CG stale (>120s) → SKIP
- [ ] Feature flag off → pass-through

### Step 5.2: Unit Tests — CGConfirmationGate
- [ ] UP with net buying + rising OI + LSR > 1 → 3/3 confirms, bonus 0.02
- [ ] UP with net selling + rising OI + LSR > 1 → 2/3 confirms, bonus 0.02
- [ ] UP with net selling + falling OI + LSR < 1 → 0/3 confirms, no bonus
- [ ] DOWN with net selling + falling OI + LSR < 1 → 3/3 confirms, bonus 0.02

### Step 5.3: Unit Tests — SpreadGate
- [ ] Spread 5% → pass
- [ ] Spread 10% → SKIP
- [ ] No Gamma data → pass (no data = allow)

### Step 5.4: Unit Tests — DOWN Penalty + Offset
- [ ] UP NORMAL T-60 → 0.65
- [ ] DOWN NORMAL T-60 → 0.68 (0.65 + 0.03)
- [ ] DOWN CASCADE T-120 with taker opposing → 0.72 + 0.015 + 0.03 + 0.05 = 0.815
- [ ] UP NORMAL T-60 with CG aligned → 0.65 - 0.02 - 0.02 = 0.61

### Step 5.5: Unit Tests — Edge-Weighted Kelly
- [ ] Strong UP T-60 NORMAL 3/3 CG → ~36.8% fraction
- [ ] Weak DOWN T-150 CALM 0/3 → ~6.7% fraction
- [ ] Kelly disabled → falls back to flat BET_FRACTION

### Step 5.6: Integration — Full Pipeline
- [ ] All pass, CG aligned → TRADE with lowered threshold + sized stake
- [ ] DUNE pass, CG both opposing → SKIP
- [ ] DUNE marginal, CG 3/3 confirms → TRADE (rescued by bonus)
- [ ] DUNE marginal, CG taker opposing → SKIP (penalty pushes above)
- [ ] Wide spread → SKIP even if everything else passes

**Files:** `engine/tests/test_gates.py` (~250 lines new/extended)

---

## Phase 6: Staged Deploy to Montreal

### Step 6.1: Code Push + Thresholds Only (all new gates disabled)
- [ ] Push code to develop
- [ ] SSH to Montreal, set new thresholds in .env
- [ ] Set `V10_CG_TAKER_GATE=false`, `V10_DOWN_PENALTY=0`, `V10_KELLY_ENABLED=false`
- [ ] Restart engine
- [ ] **Monitor 20 trades** — verify more trades at same or better WR

### Step 6.2: Enable DOWN Penalty
- [ ] `V10_DOWN_PENALTY=0.03`
- [ ] Restart, monitor 20 DOWN trades

### Step 6.3: Enable Taker Flow Gate
- [ ] `V10_CG_TAKER_GATE=true`
- [ ] Restart, verify CG gate logging, monitor 30 trades
- [ ] Verify ~20% of windows get SKIPPED (both-opposing bucket)

### Step 6.4: Enable Edge-Weighted Kelly
- [ ] `V10_KELLY_ENABLED=true`
- [ ] Restart, verify sizing varies by signal quality
- [ ] Monitor: weak signals should get smaller stakes, strong should hit $10 cap

### Step 6.5: 24-Hour Full Monitoring
- [ ] Check every 4h: 50-trade WR >= 75%, CG skip rate ~20%, PnL +$20-35
- [ ] Circuit breakers:
  - 50-trade WR < 65% → `V10_KELLY_OVERRIDE=0.25`
  - 100-trade WR < 60% → `V10_DUNE_ENABLED=false` (HALT)

---

## Phase 7: Rollback Plan

### Component-Level Feature Flags
| Feature | Disable | Effect |
|---|---|---|
| Taker gate | `V10_CG_TAKER_GATE=false` | Old CoinGlassVetoGate behavior |
| DOWN penalty | `V10_DOWN_PENALTY=0` | UP/DOWN treated equally |
| CG bonus | `V10_CG_CONFIRM_BONUS=0` | No confirmation bonus |
| Spread gate | `V10_MAX_SPREAD_PCT=100` | Never triggers |
| Kelly sizing | `V10_KELLY_ENABLED=false` | Flat BET_FRACTION |
| All thresholds | Revert `V10_*_MIN_P` | Back to v10.2 |

### Full Rollback (nuclear option)
```bash
ssh montreal
cd /home/novakash/novakash/engine
# Revert all new features:
echo "V10_CG_TAKER_GATE=false" >> .env
echo "V10_DOWN_PENALTY=0" >> .env
echo "V10_KELLY_ENABLED=false" >> .env
echo "V10_CG_CONFIRM_BONUS=0" >> .env
echo "V10_MAX_SPREAD_PCT=100" >> .env
# Restore v10.2 thresholds:
sed -i 's/V10_TRANSITION_MIN_P=0.70/V10_TRANSITION_MIN_P=0.85/' .env
sed -i 's/V10_CASCADE_MIN_P=0.72/V10_CASCADE_MIN_P=0.80/' .env
sed -i 's/V10_NORMAL_MIN_P=0.65/V10_NORMAL_MIN_P=0.78/' .env
sed -i 's/V10_LOW_VOL_MIN_P=0.65/V10_LOW_VOL_MIN_P=0.78/' .env
sed -i 's/V10_TRENDING_MIN_P=0.72/V10_TRENDING_MIN_P=0.80/' .env
sed -i 's/V10_CALM_MIN_P=0.72/V10_CALM_MIN_P=0.80/' .env
sed -i 's/V10_OFFSET_PENALTY_MAX=0.06/V10_OFFSET_PENALTY_MAX=0.05/' .env
sed -i 's/V10_DUNE_CAP_CEILING=0.68/V10_DUNE_CAP_CEILING=0.70/' .env
sudo systemctl restart novakash-engine
```

---

## Files Changed Summary

| File | Change | Lines |
|---|---|---|
| `engine/signals/gates.py` | 3 new gate classes + GateContext fields + DOWN penalty + offset fix | +180, ~20 modified |
| `engine/signals/sizing.py` | NEW — edge-weighted Kelly function | +60 new |
| `engine/strategies/five_min_vpin.py` | Pipeline wiring, sizing call, Gamma context, entry_reason | ~30 modified |
| `engine/.env.local` | All new env vars + updated thresholds | ~20 added/changed |
| `engine/tests/test_gates.py` | Comprehensive tests for all new gates + sizing | +250 new |
| `docs/v10_3_config_improved.html` | Config visualization | already written |
| `docs/V10_3_WHAT_IF_ANALYSIS.md` | 24h projections | already written |
| `docs/V10_3_IMPLEMENTATION_PLAN.md` | This file | already written |

**Total: ~540 lines new code, ~50 lines modified, ~250 lines tests**

---

## Success Criteria (after 24 hours)

- [ ] Rolling 100-trade WR >= 75%
- [ ] Net PnL > +$20
- [ ] CG taker gate blocked >= 15% of eligible windows
- [ ] Kelly sizing shows variance (strong signals > weak signals)
- [ ] DOWN trades WR improved >= 3pp
- [ ] No circuit breaker triggers
- [ ] Bankroll > $150 (from $130.82)
- [ ] Zero T-236+ trades (offset gate bug fixed by lower MAX)
