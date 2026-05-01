# Recommendation — 6-track plan to a WINNING 15m strategy

**Authored:** 2026-05-01
**Effort estimate:** 1-2 days for Tier 1 (no-retrain), 2-3 weeks for full Tier 3 retrain.
**Risk:** LOW for Tier 1 (GHOST validation first), MEDIUM for Tier 2/3 (engine + ML changes).

## Tier 1 — ship today / this week (no retrain needed)

### Track 1.1 — Restart GPU box [DONE 2026-05-01 17:13 UTC]
- ✅ Container restarted
- ✅ Confirmed serving via `/v4/snapshot` → `status: ok` for BTC 5m + 15m
- ⚠️ `restart: unless-stopped` policy missing from `docker-compose.gpu.yml` — auto-restart on next reboot will fail again. **Open audit task to fix.**
- ⚠️ Per-timescale classifier dispatch broken — 15m head not actually being served (5m output is duplicated to 15m). **Open audit task.**

### Track 1.2 — Promote `v15m_up_basic` GHOST → LIVE with hour filter
**Why:** 73% per-window deduped WR over 14d (132 windows), simulated +$60 PnL. With hour filter blocking UTC 16-21, jumps to 78.4% / +$85.

**Patch (preview only, not committed yet):**
```yaml
# engine/strategies/configs/v15m_up_basic.yaml
mode: LIVE   # was: GHOST
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 180, max_offset: 540 }
  - type: direction
    params: { direction: UP }
  - type: confidence
    params: { min_dist: 0.15 }
  - type: blocked_hours              # NEW
    params: { utc_hours: [16, 17, 18, 19, 20, 21] }

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

**Validation gate before LIVE flip:** Re-pull deduped WR over the most recent 7d (when the strategy is firing again — i.e. when regime returns to UP). If 7d WR ≥ 70% → LIVE. Otherwise stay GHOST another 7d.

**Kill criteria:** rolling 50-trade WR < 65% triggers auto-revert to GHOST.

### Track 1.3 — Add `v15m_down_basic` (mirror, DOWN-only)
**Why:** `v15m_up_basic` only fires in UP regimes. The down side is currently `v15m_down_only` (57.94% WR over 50k decisions, losing). A simpler mirror of `up_basic` should do better.

**Patch (new file `engine/strategies/configs/v15m_down_basic.yaml`):**
```yaml
name: v15m_down_basic
version: "1.0.0"
mode: GHOST   # ghost first, then promote
asset: BTC
timescale: 15m

gates:
  - type: timing
    params: { min_offset: 180, max_offset: 540 }
  - type: direction
    params: { direction: DOWN }
  - type: confidence
    params: { min_dist: 0.15 }
  - type: blocked_hours
    params: { utc_hours: [16, 17, 18, 19, 20, 21] }

sizing:
  type: fixed_kelly
  fraction: 0.025
  max_collateral_pct: 0.05
```

**Ghost for ≥7d, then evaluate.** Expected behavior: fires when `poly_direction=DOWN` (currently most of the time). If 7d deduped WR > 70% → LIVE.

### Track 1.4 — Build `v15m_consensus` meta-strategy
**Why:** When ≥4 of {fusion, up_basic, gate, up_asian, fusion_v5_9} agree on direction, ghost WR was **84% over 31 windows in 14d**. That's the strongest single gate we found.

**Implementation:** New strategy file `v15m_consensus.py` that reads recent `strategy_decisions` rows for the same window_ts and votes. Fires only if `n_distinct_direction_agreement >= 4`.

**Engineering complexity:** moderate — needs to query in-memory recent decisions during evaluation. Could be done as a `post_gate_hook` that runs AFTER all other strategies have evaluated for the same window.

**Ghost for ≥14d** before promotion (small sample).

## Tier 2 — multi-asset (after Tier 1 proves out)

### Track 2.1 — Close audit #267 (per-asset feature cache)
**Why:** ETH/XRP/SOL `/v4/snapshot` returns `status: cold_start` because the ML box only seeds BTC features. Without this, 15m on non-BTC is broken.

**Engineering work:** add ETH/XRP/SOL to `_v5_feature_loader` initialization on the ML box. Bring up `_v2_scorer_15m` instances per asset.

### Track 2.2 — Train per-asset 15m LGB heads
**Why:** Even with feature cache wired, ETH/XRP/SOL have no 15m LGB models in S3 (`v2/eth/btc_15m/` etc. don't exist). Need to train them.

**Training data:** ~5,019 resolved 15m windows per asset over last 14d (~150d if you go back further). Replicate `lgb_btc_15m__a547c3d`'s training pipeline per asset.

**S3 target:**
```
s3://bbrnovakash-models-do-not-delete/v2/eth/btc_15m/<sha>/<ts>/lgb_eth_*.txt
s3://bbrnovakash-models-do-not-delete/v2/sol/btc_15m/<sha>/<ts>/lgb_sol_*.txt
s3://bbrnovakash-models-do-not-delete/v2/xrp/btc_15m/<sha>/<ts>/lgb_xrp_*.txt
```
(Note: bucket name is misleading — `btc_15m` is the schema name, applies to all assets.)

### Track 2.3 — Update `traded_assets` config
After Tier 2.1+2.2, flip `engine/.env` `TRADED_ASSETS=btc` → `TRADED_ASSETS=btc,eth,xrp` (skip SOL — execution costs higher).

## Tier 3 — model retrain (warranted, not blocking)

### Track 3.1 — Audit #250: retrain path1 classifier with focal loss + label smoothing
**Why:** Live classifier (`path1_classifier/2026-04-18/unified_head/`) is under-confident — HC band fires 7% live vs 43% on val (per note #232). Focal loss + label smoothing addresses this.

**Note:** `cls_traj_14f_iso` (Apr 25, val 72.86%, ECE 5e-9) is a partial fix — it's a better classifier, just never promoted to live primary. Could promote it BEFORE the focal-loss retrain. **Recommend: promote `cls_traj_14f_iso` to live primary first** by updating `TIMESFM_CLASSIFIER_HEAD_URI` on `16.52.14.182`. Test for 48h. Then plan focal-loss retrain.

### Track 3.2 — Train v12-XL-equivalent 15m head (single biggest leverage)
**Why:** 5m has v9/v10/v12/v12-XL — a rich LGB ensemble that achieves 75-95% WR in shadow. 15m has only the Apr 16 `a547c3d` LGB. Training a v12-XL-style 15m head with 112 features (incl. `nested_5m_*` aggregates) using 60d of resolved 15m outcomes should lift 15m WR significantly.

**Spec:**
- Architecture: clone `v12xl_training/boosters_20260501_000300/train_v12xl_gpu.py` config
- Features: 112-feature v10 spec, but adjust horizons to 15m offsets (180s, 300s, 480s, 720s instead of 90/120/180/240)
- Labels: BTC 15m `market_data.outcome` (~5k resolved windows in 14d, ~20k in 60d)
- Holdout: last 3 days isotonic-calibrated
- Acceptance criteria: heldout dir_acc ≥ 70%, ECE post-iso < 0.05

**Effort:** 3-5 days (2-3 days training + 2 days deploy + ghost validate).

### Track 3.3 — Add hour-of-day features to next retrain
**Why:** The hour-of-day pattern is ~30pp WR difference between best and worst hours. Adding `hour_utc_sin/cos`, `is_us_session`, `realized_vol_60min` as features lets the model learn this directly instead of relying on a hard gate.

**Note:** keep the hard gate AS WELL as the features in the first version — defense in depth. Once the model proves it captures the regime via features, the hard gate can be relaxed.

## Tier 4 — non-linear ODE work (research, optional)

### Track 4.1 — Phase-portrait regime classifier
**Why:** The 18:00-21:00 UTC failures are about high-vol news regime. A Takens-embedding of (price, vpin, delta) trajectory + a clustering classifier could detect "we're in a chaotic basin right now" and gate ALL strategies, not just hour-of-day.

**Research scope:** prototype on existing `ticks_chainlink` data, evaluate vs hour-of-day baseline. Only worth doing if Tier 3.3 features don't capture enough of the regime signal.

### Track 4.2 — Cross-asset Lyapunov-exponent gate
**Why:** When BTC 15m enters a chaotic regime, ETH and SOL often do too. A cross-asset chaos gate could cut tail-loss days. Phase-space reconstruction across the 4 assets, max Lyapunov exponent estimated over a 60-min rolling window.

**Effort:** 1-2 weeks of research code + 1 week of integration. Only after Tier 1-3 are stable.

## Risk register

| Risk | Mitigation |
|---|---|
| `v15m_up_basic` 73% WR was inflated by Apr 18-22 UP regime | Add hour filter, ghost-validate over next regime period before LIVE |
| `v15m_consensus` correlation between strategies (all read same `probability_up`) inflates "agreement" | Confirm by checking that disagreement zones have ≥30% windows |
| Per-asset 15m heads (Track 2.2) overfit on small datasets | Walk-forward CV on 60d of labels, require holdout dir_acc ≥ 65% before deploy |
| GPU box auto-reboot loses container again | Add `restart: unless-stopped` to `docker-compose.gpu.yml`, AND set up a systemd unit |
| Per-timescale classifier dispatch bug means 15m head NEVER worked | Open audit task; until fixed, `v7_15m_sniper_btc` shouldn't be promoted (uses classifier directly). v15m_up_basic uses `probability_up` blend (LGB-driven for 15m), so unaffected |

## Verification protocol before any LIVE flip

1. Strategy must have ≥7d of GHOST decisions WITH the new gates applied
2. Per-window deduped WR must be ≥70%
3. Sim PnL must be positive over the 7d window
4. No single day in the 7d window may show <50% WR (regime stability check)
5. Manual review of 5 most recent SKIP and TRADE metadata samples to confirm signals are sane
6. Notify on Telegram + post Hub note before flipping mode

## Decision authority

- LIVE promotions: Billy approves manually, no auto-promotion (per existing `feedback_no_auto_model_promotion.md`)
- Ghost mode flips: agent OK
- Engine restarts: Billy notifies; agent does not restart without instruction (per Montreal-only rule)
