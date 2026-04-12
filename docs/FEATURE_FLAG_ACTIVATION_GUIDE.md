# Feature Flag Activation Guide

> Generated 2026-04-12 from a full codebase scan of `develop` (be7f934).
> Covers every env-var feature flag that gates behaviour in the Polymarket
> engine (`engine/`), margin engine (`margin_engine/`), and CI/CD workflows.

---

## 1. Activate NOW -- safe, tested, already templated in CI

These flags are already set to `true` in the deploy-engine.yml workflow
and/or `.env.local`. They are safe to confirm as active on Montreal.

| Flag | Env Var | Default | What it enables | PR | Status |
|------|---------|---------|-----------------|-----|--------|
| 5-Min BTC Pipeline | `FIVE_MIN_ENABLED` | `false` | Master switch for the 5-min Polymarket BTC strategy loop | Pre-v8 | **READY** |
| 15-Min BTC Pipeline | `FIFTEEN_MIN_ENABLED` | `false` | 15-min Polymarket strategy (BTC) -- same code path, longer window | Pre-v8 | **READY** |
| Live Trading Gate | `LIVE_TRADING_ENABLED` | `false` | Hard gate in `PolymarketClient.__init__` -- must be `true` for non-paper mode | Pre-v8 | **READY** |
| V2 Early Entry | `V2_EARLY_ENTRY_ENABLED` | `true` | v2.2 early-offset entry gate in `five_min_vpin.py` and `timesfm_v2_client.py` | v2.2 | **READY** |
| FOK Execution Ladder | `FOK_ENABLED` | `true` | Replace GTC single-order with FOK attempt ladder (`runtime_config.fok_enabled`) | v8.0 | **READY** |
| V9 Source Agreement | `V9_SOURCE_AGREEMENT` | `false` | CL+TI direction must agree (94.7% WR when agree, 9.1% when disagree) | v9.0 | **READY** |
| V9 Dynamic Caps | `V9_CAPS_ENABLED` | `false` | Two-tier dynamic entry caps based on empirical agreement WR | v9.0 | **READY** |
| V10 DUNE Confidence | `V10_DUNE_ENABLED` | `false` | Use Sequoia ML model as confidence gate (replaces VPIN); controls full v10 pipeline | v10.0 | **READY** |
| V10 CoinGlass Taker Gate | `V10_CG_TAKER_GATE` | `false` (env only) | CoinGlass taker flow alignment gate -- 81.7% WR when aligned | v10.3 | **READY** |
| Reconciler | `RECONCILER_ENABLED` | `true` | New reconciler loop; `false` falls back to legacy 5-min reconcile | PR #18 | **READY** |
| TimesFM Feed | `TIMESFM_ENABLED` | `false` | Enable TimesFM v2 forecast polling (direction + confidence). Fetched and logged even when agreement gate is off | v6.0 | **READY** |
| Margin V4 Actions | `MARGIN_ENGINE_USE_V4_ACTIONS` | `false` | Consume v4 snapshot gates for entry + continuation in margin engine use cases | PR #16 (margin) | **READY** -- templated `true` in `deploy-margin-engine.yml` |
| Margin V4 Primary Timescale | `MARGIN_V4_PRIMARY_TIMESCALE` | `15m` | Which timescale the margin engine reads from the v4 snapshot | PR #16 (margin) | **READY** -- templated `15m` |
| Margin V4 Macro Mode | `MARGIN_V4_MACRO_MODE` | `advisory` | Controls how the macro direction gate is applied. `advisory` = log-only + size haircut, `veto` = hard-skip | Margin settings | **READY** -- `advisory` is the safe default after 24h audit |

### Activation checklist (Montreal engine/.env)

These should already be set by the deploy workflow. Confirm by SSH:

```bash
ssh ubuntu@$ENGINE_HOST 'sudo grep -E "FIVE_MIN_ENABLED|V10_DUNE_ENABLED|V9_SOURCE_AGREEMENT|V9_CAPS_ENABLED|V10_CG_TAKER_GATE|RECONCILER_ENABLED|TIMESFM_ENABLED|LIVE_TRADING_ENABLED" /home/novakash/novakash/engine/.env'
```

---

## 2. Activate after validation -- needs 48h paper-mode observation

| Flag | Env Var | Default | What it enables | PR | Status |
|------|---------|---------|-----------------|-----|--------|
| V10.6 Eval Offset Bounds | `V10_6_ENABLED` | `false` | Hard-block trades outside [T-90, T-180] band. Gate `EvalOffsetBoundsGate` in `gates.py` line ~248 | DS-01 (PR #82) | **NEEDS_TESTING** |
| V10.6 Min Eval Offset | `V10_6_MIN_EVAL_OFFSET` | `90` | Lower bound (too close to close). Only read when `V10_6_ENABLED=true` | DS-01 | **NEEDS_TESTING** |
| V10.6 Max Eval Offset | `V10_6_MAX_EVAL_OFFSET` | `180` | Upper bound (too far from close). Only read when `V10_6_ENABLED=true` | DS-01 | **NEEDS_TESTING** |
| V11 Spot-Only Consensus | `V11_POLY_SPOT_ONLY_CONSENSUS` | `false` | SourceAgreementGate ignores delta_binance, requires unanimous CL+TI. Oracle-aligned for Polymarket | DQ-01 | **NEEDS_TESTING** |
| Clean-Arch Evaluate Window | `ENGINE_USE_CLEAN_EVALUATE_WINDOW` | `false` | Route `_evaluate_window` through the extracted `EvaluateWindowUseCase` in `engine/use_cases/evaluate_window.py` | CA-01 Phase 3 (PR #103) | **NEEDS_TESTING** |
| Margin V4 Mark Divergence | `MARGIN_V4_MAX_MARK_DIVERGENCE_BPS` | `0.0` (no-op) | When > 0, rejects trades where Binance spot diverges from exchange mark price by more than N bps | DQ-07 | **NEEDS_TESTING** -- set to `20` after validation |
| Margin V4 NO_EDGE Override | `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` | `None` (off) | Allow entries in NO_EDGE regime when TimesFM expected move clears a threshold. 74-sample bucket with 100% hit rate but suspiciously clean | Margin settings | **NEEDS_TESTING** -- needs 7-day replay |

### Validation protocol

1. Set the flag on Montreal with `PAPER_MODE=true`
2. Run for 48 hours of active market windows
3. Compare: trades taken vs trades the legacy path would have taken (check telemetry logs)
4. If trade-level agreement > 95% and no regressions, flip for live

```bash
# Example: activate V10.6 in paper mode
ssh ubuntu@$ENGINE_HOST 'sudo -u novakash bash -c "echo V10_6_ENABLED=true >> /home/novakash/novakash/engine/.env"'
# Restart engine
ssh ubuntu@$ENGINE_HOST 'sudo bash /home/novakash/novakash/scripts/restart_engine.sh'
```

---

## 3. Do NOT activate yet -- dependencies missing or untested

| Flag | Env Var | Default | What it enables | Blocker | Status |
|------|---------|---------|-----------------|---------|--------|
| ENGINE_USE_STRATEGY_PORT | `ENGINE_USE_STRATEGY_PORT` | N/A | Pluggable strategy port system (V10/V4 strategies via clean-arch ports) | **CODE DOES NOT EXIST** -- only a design doc (`f65367e`). No implementation landed | **NOT_READY** |
| V4 Fusion (Polymarket) | `V4_FUSION_ENABLED` / `V4_FUSION_MODE` | N/A | V4 ghost strategy for the Polymarket engine. margin_engine has v4, Polymarket engine does not | **CODE DOES NOT EXIST** -- zero references in engine/. AuditChecklist task V4-01 tracks this gap | **NOT_READY** |
| V10 Kelly Sizing | `V10_KELLY_ENABLED` | `false` | Edge-weighted Kelly sizing replacing flat BET_FRACTION. Code in `engine/signals/sizing.py` but NOT wired into runtime_config reads | **NOT WIRED** -- `runtime_config.py` has no `v10_kelly_enabled` attribute. `.env.local` sets `false`. Needs integration PR | **NOT_READY** |
| TWAP Override | `TWAP_OVERRIDE_ENABLED` | `false` | Allow TWAP+Gamma to override point-delta direction. v8 audit found it blocked 8 winners -- net harmful | **DELIBERATELY DISABLED** -- Tiingo as delta source makes TWAP direction redundant | **NOT_READY** |
| TWAP Gamma Gate | `TWAP_GAMMA_GATE_ENABLED` | `false` | Allow TWAP should_skip to block trades early | **DELIBERATELY DISABLED** -- blocked more winners than losers | **NOT_READY** |
| TimesFM Agreement Gate | `TIMESFM_AGREEMENT_ENABLED` | `false` | Allow TimesFM forecast to gate/modify confidence | **DELIBERATELY DISABLED** -- TimesFM accuracy 47.8%, worse than coin flip as a gate | **NOT_READY** |
| Macro Observer | `MACRO_OBSERVER_ENABLED` | `false` | v8 spec says "DO NOT ENABLE YET -- collecting data only" | **DATA COLLECTION ONLY** -- no evidence of positive edge yet | **NOT_READY** |
| V10 Vol Sizing | `V10_VOL_SIZING_ENABLED` | `false` | Volatility-based position sizing. Referenced in `docs/CHANGELOG-v10.7-RISK-FIXES.md` | **DOCS ONLY** -- no code reads this flag in engine/*.py | **NOT_READY** |
| V10 Session Sizing | `V10_SESSION_SIZING_ENABLED` | `false` | Session-aware sizing. Referenced in `docs/v10_7_config_proposal.md` | **DOCS ONLY** -- no code reads this flag in engine/*.py | **NOT_READY** |
| Playwright Auto-Redemption | `PLAYWRIGHT_ENABLED` | `false` | Browser-based auto-redemption of Polymarket positions | **SPEC ONLY** -- design in `docs/superpowers/specs/`, Railway env has `true` but no production code path | **NOT_READY** |

---

## 4. Reference: All margin engine settings (`margin_engine/infrastructure/config/settings.py`)

The margin engine uses pydantic-settings with `env_prefix="MARGIN_"`. All env vars are prefixed `MARGIN_`.

| Setting | Env Var | Default | Notes |
|---------|---------|---------|-------|
| `paper_mode` | `MARGIN_PAPER_MODE` | `true` | Master paper/live gate |
| `exchange_venue` | `MARGIN_EXCHANGE_VENUE` | `hyperliquid` | DQ-06 fix: paper must be hyperliquid |
| `engine_use_v4_actions` | `MARGIN_ENGINE_USE_V4_ACTIONS` | `false` | V4 snapshot consumption. CI templates `true` |
| `v4_snapshot_url` | `MARGIN_V4_SNAPSHOT_URL` | `http://3.98.114.0:8080` | Montreal timesfm service |
| `v4_primary_timescale` | `MARGIN_V4_PRIMARY_TIMESCALE` | `15m` | CI templates `15m` |
| `v4_timescales` | `MARGIN_V4_TIMESCALES` | `5m,15m,1h,4h` | CSV for snapshot request |
| `v4_strategy` | `MARGIN_V4_STRATEGY` | `fee_aware_15m` | Which strategy profile |
| `v4_poll_interval_s` | `MARGIN_V4_POLL_INTERVAL_S` | `2.0` | Snapshot poll cadence |
| `v4_freshness_s` | `MARGIN_V4_FRESHNESS_S` | `10.0` | Max age before stale |
| `v4_macro_mode` | `MARGIN_V4_MACRO_MODE` | `advisory` | `advisory` or `veto` |
| `v4_macro_hard_veto_confidence_floor` | `MARGIN_V4_MACRO_HARD_VETO_CONFIDENCE_FLOOR` | `80` | Minimum confidence for veto to fire |
| `v4_macro_advisory_size_mult_on_conflict` | `MARGIN_V4_MACRO_ADVISORY_SIZE_MULT_ON_CONFLICT` | `0.75` | Size haircut when macro conflicts |
| `v4_max_mark_divergence_bps` | `MARGIN_V4_MAX_MARK_DIVERGENCE_BPS` | `0.0` (off) | DQ-07 mark divergence gate |
| `v4_allow_no_edge_if_exp_move_bps_gte` | `MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE` | `None` (off) | NO_EDGE regime override |
| `telegram_enabled` | `MARGIN_TELEGRAM_ENABLED` | `true` | Margin engine Telegram alerts |

---

## 5. Deploy workflow flag templates

### `deploy-engine.yml` (Polymarket engine, Montreal)

Lines 206-228 template these flags on every deploy:

```
V10_6_ENABLED=true
V10_6_MIN_EVAL_OFFSET=90
V10_6_MAX_EVAL_OFFSET=180
V10_DUNE_ENABLED=true
V10_CG_TAKER_GATE=true
V9_SOURCE_AGREEMENT=true
V9_CAPS_ENABLED=true
V11_POLY_SPOT_ONLY_CONSENSUS=true
ENGINE_USE_CLEAN_EVALUATE_WINDOW=true
FIVE_MIN_ENABLED=true
TELEGRAM_ALERTS_PAPER=true
TIMESFM_ENABLED=true
```

### `deploy-margin-engine.yml` (margin engine, Montreal)

Lines 79-86 template these flags:

```
MARGIN_PAPER_MODE=true
MARGIN_ENGINE_USE_V4_ACTIONS=true
MARGIN_V4_SNAPSHOT_URL=http://3.98.114.0:8080
MARGIN_V4_PRIMARY_TIMESCALE=15m
MARGIN_V4_TIMESCALES=5m,15m,1h,4h
MARGIN_V4_STRATEGY=fee_aware_15m
MARGIN_V4_POLL_INTERVAL_S=2.0
MARGIN_V4_FRESHNESS_S=10.0
```

---

## 6. Flag interaction matrix

Some flags have dependencies or conflicts:

| If you set... | Also verify... | Why |
|---------------|---------------|-----|
| `V10_DUNE_ENABLED=true` | `TIMESFM_ENABLED=true` | DUNE gate calls the Sequoia/ELM model endpoint |
| `V10_6_ENABLED=true` | `V10_DUNE_ENABLED=true` | V10.6 offset bounds are part of the v10 pipeline |
| `V11_POLY_SPOT_ONLY_CONSENSUS=true` | `V9_SOURCE_AGREEMENT=true` | Spot-only modifies the source agreement gate |
| `ENGINE_USE_CLEAN_EVALUATE_WINDOW=true` | All v10 flags active | The use case reimplements the same pipeline |
| `V9_CAPS_ENABLED=true` | `V10_DUNE_ENABLED` checked | When V10 is on, V9 caps are bypassed (v10 pipeline takes over) |
| `MARGIN_ENGINE_USE_V4_ACTIONS=true` | `MARGIN_V4_SNAPSHOT_URL` reachable | V4 use cases fail if the timesfm service is down |
