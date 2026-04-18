# Margin Engine v5_ensemble Upgrade Design

**Author:** Claude Code audit swarm (4 agents)
**Date:** 2026-04-18
**Status:** PROPOSED
**Branch:** `feat/margin-engine-v5-ensemble-design`

---

## Executive Summary

The margin engine has been running the v2-probability strategy on BTC/15m with a **4.4% win rate and -$65.22 cumulative P&L** across 341 paper trades. Meanwhile, the main engine's v5_ensemble (LGB + Path 1 classifier head) has gone LIVE on Montreal with a validated **+6.12pp ensemble edge** over the production LGB scorer. This document proposes upgrading the margin engine to consume the v5_ensemble signal.

---

## 1. Overnight Context (2026-04-18)

### What Happened

A dense overnight work session (hub notes #151-#171) produced the following milestones:

1. **Path 1 classifier validated** across 3 progressively rigorous tests:
   - 369-window initial: 89.6% WR@gate (+8.83pp vs prod)
   - 626-window OOS: 85.6% WR@gate (+8.21pp vs prod)
   - Fair-fight t-60: solo only +1.81pp, but **ensemble +6.12pp**

2. **6-delta tournament won** at every single delta (+4.38pp to +7.54pp)

3. **v5_ensemble deployed LIVE** on Montreal with real money (GHOST -> LIVE same day)

4. **Oracle-label bug discovered**: `actual_direction` was labeled from Binance, not Chainlink oracle. 42.9% disagreement rate. Backfill rewrote 12,341 rows. Prior shadow WR jumped from 67.9% to 80.5% post-fix.

5. **Two classifier shadow bugs found**: MacroV2Classifier dead code + HIGH_VOL regime encoder NaN leak

### Performance Data

| Metric | Value |
|--------|-------|
| Path 1 solo WR@gate (t-60 fair-fight, OOS) | 79.1% (+1.81pp vs prod) |
| 50/50 ensemble WR@gate (t-60, OOS) | 83.4% (+6.12pp vs prod) |
| Abstain-gate WR@gate (t-60, OOS) | 86.0% (on 300/590 trades) |
| 6-delta tournament ensemble range | +4.38pp to +7.54pp |
| Unified head avg ensemble edge | +4.28pp |
| Early-window (T-240..T-180) live WR | 87% (n=15, only net-positive bucket) |

### Current State on Montreal

- **v4_fusion**: LIVE, trading real money, LGB-only probability
- **v5_ensemble**: LIVE (v5.1.0), real money, unified classifier head, 50/50 ensemble weights, disagreement gate OFF, fallback sanity gate ON
- Both strategies fire on every BTC 5m window = double stake on same direction

---

## 2. TimesFM Repo Changes (139 new commits)

### New Architecture: v5 Classifier + Ensemble

The timesfm service now serves a **5-layer prediction stack**:

| Layer | Function |
|-------|----------|
| **v1 (TimesFM)** | Google TimesFM 2.5 200M zero-shot quantile forecasts |
| **v2 (LightGBM)** | Binary classifier P(UP) on 70+ features |
| **v3 (Composite)** | 7-signal fusion across 9 timescales |
| **v4 (Fusion)** | Stateless composition into snapshot JSON |
| **v5 (Ensemble)** | LGB + Path 1 classifier head blend |

**Path 1 Classifier**: Fine-tuned MLP head (600->256->64->1, ~170K params) on frozen TimesFM + LoRA adapter outputs. Trained on `signal_evaluations` rows (15x more data than per-window approach).

**Ensemble blend**: 50/50 weighted average of calibrated LGB p_up and classifier p_up. Served transparently via `/v4/snapshot` -- the `probability_up` field now contains the blended value when `V5_ENSEMBLE_PATH1_ENABLED=true`.

### New API Fields on `/v4/snapshot`

The per-timescale block now includes:
```json
{
  "probability_up": 0.72,           // blended ensemble value
  "probability_lgb": 0.68,          // raw LGB calibrated
  "probability_classifier": 0.76,   // Path 1 classifier
  "ensemble_config": {
    "mode": "ensemble",              // or "fallback_lgb_only"
    "weights": {"lgb": 0.5, "classifier": 0.5},
    "disagreement": 0.08
  }
}
```

### Risks Identified

1. Single EC2 instance, no HA/failover
2. Feature contract coupling across 3 repos (enforced by CI parity check only)
3. Auto-promotion disabled with no automated replacement
4. Smoothing features train/serve gap (20 features may be NaN at inference)

---

## 3. Current Margin Engine State

### Architecture

The margin engine is a **separate system** from the main Polymarket engine:
- **Main engine**: 5-minute binary option trading on Polymarket
- **Margin engine**: Continuous margin/perp positions on Hyperliquid/Binance

Currently running the **v2 path** (`engine_use_v4_actions=False`), polling `/v2/probability/15m` every 30s.

### Performance (All-Time Paper)

| Metric | Value |
|--------|-------|
| Total trades | 341 |
| Win rate | 4.4% (15W / 324L) |
| Cumulative P&L | -$65.22 |
| Avg trade P&L | -$0.19 |
| Dominant exit reason | CASCADE_EXHAUSTED (84%) |

### Critical Bug: CASCADE_EXHAUSTED Bypass

`expiry.py` fires `check_cascade_exhausted_exit()` whenever `v4 is not None`, **regardless** of the `engine_use_v4_actions` flag. Since the v4 adapter polls continuously, cascade exits fire even though v4 actions are supposed to be disabled. This caused 107 of 127 recent exits (84%) to be CASCADE_EXHAUSTED with -$0.12 avg P&L.

### Strategy Services (Inactive)

8 strategy services exist but none are wired into the hot path:
- `RegimeAdaptiveRouter` (trend/MR/no-trade dispatch)
- `CascadeFadeStrategy` / `CascadeDetector`
- `FeeAwareContinuation` / `ContinuationAlignment`
- `QuantileVaRSizer`

---

## 4. Gap Analysis

### What the Margin Engine is Missing

| Gap | Current State | Required |
|-----|--------------|----------|
| **Ensemble fields in V4Snapshot** | `TimescalePayload` has only `probability_up` | Need `probability_lgb`, `probability_classifier`, `ensemble_config` |
| **Signal source selection** | Hardcoded to `probability_up` | Configurable: ensemble / lgb_only / path1_only |
| **Ensemble gates** | None | Fallback sanity gate + disagreement gate |
| **v4 path activation** | `engine_use_v4_actions=False` | Must be `True` for ensemble to make sense |
| **CASCADE_EXHAUSTED bug** | Fires regardless of v4 flag | Must respect feature flag |
| **Position audit trail** | No ensemble metadata on positions | Need signal source, p_lgb, p_classifier, ensemble_mode |
| **Strategy registry** | Not wired to hot path | Either wire or use direct integration |

---

## 5. Implementation Plan

### Phase 1: Fix the Foundation (Day 1)

**1.1 Fix CASCADE_EXHAUSTED gating bug**
- File: `margin_engine/application/use_cases/position_management/expiry.py`
- Change: Gate `check_event_guard_exit()` and `check_cascade_exhausted_exit()` behind `engine_use_v4_actions`
- Impact: Stops 84% of spurious exits immediately

**1.2 Add ensemble fields to V4Snapshot**
- File: `margin_engine/domain/value_objects/v4_data.py`
- Add to `TimescalePayload`:
  ```python
  probability_lgb: Optional[float] = None
  probability_classifier: Optional[float] = None
  ensemble_config: Optional[dict] = None
  ```
- Update `_parse_timescale()` to extract from `/v4/snapshot` response
- Scope: ~10 LOC

### Phase 2: Signal Source Selection (Day 1-2)

**2.1 New settings**
- File: `margin_engine/infrastructure/config/settings.py`
- Add:
  ```python
  v5_ensemble_enabled: bool = False
  v5_ensemble_signal_source: str = "ensemble"
  v5_ensemble_disagreement_threshold: float = 0.0
  v5_ensemble_skip_on_fallback: bool = True
  ```

**2.2 Modify V4Strategy entry gates**
- File: `margin_engine/application/use_cases/entry_strategies/v4_strategy.py`
- Add probability source selector (reads `probability_lgb` / `probability_classifier` / default `probability_up` based on setting)
- Add Gate 6a: `ensemble_fallback_sanity`
- Add Gate 6b: `ensemble_disagreement`
- Scope: ~50 LOC

**2.3 Modify continuation path**
- File: `margin_engine/application/use_cases/position_management/expiry.py`
- Apply same signal source selection for probability flip and conviction checks

### Phase 3: DTOs and Wiring (Day 2)

**3.1 Update DTOs**
- `application/dto/open_position.py` -- add ensemble config fields
- `application/dto/manage_positions.py` -- same

**3.2 Wire through main.py**
- Pass ensemble settings through use case constructors
- Update `build_execution_info()` for dashboard

### Phase 4: Audit Trail (Day 2-3)

**4.1 Position entity**
- File: `margin_engine/domain/entities/position.py`
- Add: `v4_entry_signal_source`, `v4_entry_probability_lgb`, `v4_entry_probability_classifier`, `v4_entry_ensemble_mode`

**4.2 DB migration**
- Add columns to `margin_positions` table

### Phase 5: Activate v4 + GHOST (Day 3)

**5.1 Flip `engine_use_v4_actions=True`**
- With the cascade bug fixed, this is now safe
- v5_ensemble runs as v4 path extension

**5.2 Deploy as GHOST first**
- `v5_ensemble_enabled=True` + separate GHOST mode flag
- Log decisions without executing
- Monitor for 48h before LIVE flip

---

## 6. File Change Summary

| # | File | Change | LOC |
|---|------|--------|-----|
| 1 | `domain/value_objects/v4_data.py` | Add ensemble fields + parser | ~10 |
| 2 | `infrastructure/config/settings.py` | Add v5 ensemble settings | ~10 |
| 3 | `application/use_cases/entry_strategies/v4_strategy.py` | Signal source + 2 gates | ~50 |
| 4 | `application/use_cases/position_management/expiry.py` | Fix cascade bug + ensemble continuation | ~30 |
| 5 | `application/dto/open_position.py` | Add ensemble fields | ~10 |
| 6 | `application/dto/manage_positions.py` | Add ensemble fields | ~10 |
| 7 | `domain/entities/position.py` | Audit trail fields | ~10 |
| 8 | `main.py` | Wire ensemble settings | ~20 |
| 9 | `adapters/persistence/pg_repository.py` | Schema migration | ~15 |
| **Total** | | | **~165 LOC** |

---

## 7. Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Signal quality unknown for 15m margin | GHOST mode for 48h, compare ensemble vs v2-only on same windows |
| Cascade bug fix changes trade behavior | Fix is gating-only (no new behavior), reduces spurious exits |
| Ensemble head may regress | Fallback sanity gate ON (skip when classifier unavailable) |
| v4 activation may increase trade frequency | Monitor position count, keep `max_open_positions=1` |
| TimesFM service outage | Existing ProbabilityHttpAdapter fallback path remains |

---

## 8. Success Criteria

Before LIVE flip:
- [ ] 48h GHOST data shows ensemble WR > v2-only WR by >= 3pp
- [ ] CASCADE_EXHAUSTED exit rate drops from 84% to < 10%
- [ ] No signal staleness warnings during GHOST period
- [ ] Ensemble fields parsing validated against live `/v4/snapshot` responses
- [ ] Position audit trail correctly records signal source metadata

---

## 9. Open Questions

1. **Should we activate v4 path first** (without ensemble) to validate the gate stack independently?
2. **Which timescale?** Currently 15m. Should we add 5m for Polymarket alignment?
3. **Per-delta specialist heads** (audit #245) -- wait for canary results before margin engine integration?
4. **Disagreement gate threshold** -- start at 0 (disabled) or use a conservative value?
