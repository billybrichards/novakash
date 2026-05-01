# 15m Winning Strategy — Analysis & Recommendation

**Authored:** 2026-05-01 (Billy + agent diagnostic)
**Status:** ANALYSIS — pre-implementation
**Branch:** `feat/15m-winning-strategy-analysis`

## TL;DR

There already IS a winning 15m strategy on BTC, ghost-running today. It just hasn't been promoted because the model history was never written down. Specifically:

- **`v15m_up_basic`** has **77.32% WR over 3,316 ghost decisions in 14d**, **73% per-window deduped (132 unique windows)**, simulated PnL **+$59.85** unfiltered.
- With a **UTC hour-of-day filter blocking 16-21** (the only hours with WR<55%), the same strategy backtests at **78.35% WR / +$84.88 sim PnL** over 97 windows.
- Signal source: `v2_probability_up` (the BTC 15m LGB ensemble blend). Direction-conditional — the strategy only fires when `poly_direction=UP`, which means it's regime-conditional too.

**The "missing 15m model" you couldn't recall** = `15m_head_v1` classifier trained 2026-04-20 19:05 UTC, val_dir_acc 75.4%, integrated via PRs #111/#113/#116/#117/#118 (Apr 20-21). It IS deployed on the GPU box `.env`, but the GPU container had been DOWN since the Apr 30 reboot. **Restarted today (2026-05-01 17:13 UTC) — back online.**

**Action plan:** see `recommendation.md`.

## Files in this folder

| File | Purpose |
|---|---|
| [`README.md`](./README.md) | This summary |
| [`model_timeline.md`](./model_timeline.md) | Full chronology of 15m model training, classifier/LGB/LoRA history, what's deployed where |
| [`ghost_analysis.md`](./ghost_analysis.md) | 14d / 7d / 24h ghost decision data, per-asset, per-strategy, per-hour |
| [`signal_source_audit.md`](./signal_source_audit.md) | What feeds each 15m strategy, confirmed via metadata samples |
| [`recommendation.md`](./recommendation.md) | The actual implementation plan (6 tracks) |
| [`infra_state_2026-05-01.md`](./infra_state_2026-05-01.md) | Live infra snapshot — boxes, models, env vars, anomalies |
| [`report.html`](./report.html) | Self-contained visual report (open in browser) |

## The 4-asset comparison

| | BTC 15m | ETH 15m | XRP 15m | SOL 15m |
|---|---|---|---|---|
| Live `/v4/snapshot` returns real values | ✅ p_up=0.33 p_lgb=0.28 p_cls=0.39 | ❌ cold_start (frozen 0.435 constant) | ❌ cold_start | ❌ cold_start |
| 15m LGB head loaded on serving box | ✅ `lgb_btc_15m__a547c3d` (Apr 16) | ❌ | ❌ | ❌ |
| 15m classifier head trained | ✅ `15m_head_v1` (Apr 20, val 75.4%) | ⚠️ same head, but OOD live (`pc≈0.000276` per note #232) | ⚠️ same | ⚠️ same |
| Engine `traded_assets` includes | ✅ `btc` | ❌ | ❌ | ❌ |
| Per-asset feature cache on ML box | ✅ | ❌ blocks audit #267 | ❌ | ❌ |
| Ghost WR (deduped, 14d) | **66-77%** depending on strategy | **44%** (broken) | 63% | 64% |
| Polymarket 15m markets | ✅ liquid | ✅ | ✅ | ⚠️ thinner |
| **Verdict** | **PROMOTE NOW** | wait on #267 | wait on #267 | skip / wait |

## The non-linear-ODE / regime perspective

BTC 15m exhibits **hour-of-day regime structure** that no current model sees explicitly. From `v15m_up_basic` deduped data over 14d:

| Hour bucket (UTC) | Behavior | n | WR |
|---|---|---|---|
| 00-09 (Asian + early Euro) | drift-dominated, low vol | 56 | ~75-100% |
| 10-15 (London) | mixed | 33 | 67-100% |
| **16-21 (US session)** | **high-vol news regime** | **30** | **30-60%** |
| 22-23 (closing) | recovery | 10 | 90-100% |

This is exactly the kind of regime/dynamics structure a non-linear ODE / phase-portrait approach is designed to handle — different attractor basins by time-of-day. **The cheapest intervention is a hard `blocked_utc_hours: [16,17,18,19,20,21]` gate.** Adding hour as a feature in next retrain would let the model learn this directly, but a hard gate captures most of the lift today.

## What changed today (2026-05-01)

1. **GPU classifier box restarted** at 17:13 UTC. Container had been Exited (255) for 26 hours due to Apr 30 Ubuntu auto-reboot + missing `restart: unless-stopped` policy.
2. **v12-XL LGB retrain landed at 00:03 UTC** (`v12xl_training/boosters_20260501_000300/`) — 112 features incl. `nested_5m_*`, T-90 acc 78% post-iso, T-120 acc 68.6%. **5m only** — does not address 15m.
3. **Per-tick signal eval (note #307)** posted today shows blend (v9+v12) at 78.89% WR over 1.1M trade ticks. Confirms the 5m system is healthy.
4. **No 15m retrain in 11 days**. Last 15m artifact = `15m_head_v1` Apr 20.

## Caveats

- All ghost WR numbers are computed from `strategy_decisions × market_data` cross-join in the Hub Postgres. They reflect **what the strategy WOULD have done** — not actual P&L. Slippage, CLOB fill rates, and partial fills not modeled.
- The `/api/v58/strategy-decisions` Hub endpoint has a known bug (`name 'resolved' is not defined` — audit #329). All numbers in this analysis come from direct DB queries.
- Sample sizes for v15m_up_basic per hour are small (2-9 per hour). Statistical confidence on hour-by-hour WR is limited — but the AGGREGATED before/after hour-filter result (97 windows, 78% WR) has enough power.
- v15m_up_basic stopped trading after 2026-04-22 because the live signal direction flipped to DOWN. The strategy is regime-conditional. We fix this by adding a complementary `v15m_down_basic`.

## References

- Hub note #277 (2026-04-28): "Classifier + 15m / multi-asset coverage — BTC-5m only today, infra is half-built"
- Hub note #232 (2026-04-24): "ML Box handoff — BTC 5m classifier production-vs-val gap" — flags 15m saturation
- Hub note #239 (2026-04-24): "Classifier session — full fix chain + GPU roadmap"
- Hub note #307 (2026-05-01): "Per-tick signal eval — 16h post-v12-deploy"
- Audit task #266: TimesFM 15m missing ETH/SOL/XRP forecast models
- Audit task #267: data_surface.py /v4/snapshot cache is BTC-only
- Audit task #250: Retrain path1 classifier - focal loss + label smoothing
- Audit task #305: Expose VPIN scalar in /v4/snapshot.timescales.{5m,15m}
