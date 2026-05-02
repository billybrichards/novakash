# Data-backed strategies wiring — feat/data-backed-strategies

> Worktree: `/Users/billyrichards/Code/novakash-data-backed/` (off origin/develop)
> Plan source: `/Users/billyrichards/Code/novakash/docs/data-backed-strategy-plan-2026-05-02/`

## Goal

Wire the 18 data-backed strategies from the plan into the engine. Get GPU box producing ETH/XRP signals so when Billy flips `TIMESFM_URL` → GPU, all 18 strategies have signals to consume.

## Scope (in this worktree)

### New gate types — 3 files

- [ ] `engine/strategies/gates/multi_model_consensus.py` — N-of-M model agreement
- [ ] `engine/strategies/gates/model_disagreement_veto.py` — block if |p_secondary - p_primary| ≥ threshold
- [ ] `engine/strategies/gates/confidence_band_skip.py` — skip when probability falls within no-trade band
- [ ] Register all 3 in `engine/strategies/registry.py`

### Strategy YAML configs — 18 files

Tier 1 — BTC 5m (10):
- [ ] `v_consensus_3of5_up_btc_5m.yaml`
- [ ] `v_consensus_4plus_dn_btc_5m.yaml`
- [ ] `v_v9_strong_up_btc_5m.yaml`
- [ ] `v_v9_strong_dn_btc_5m.yaml`
- [ ] `v_v9_1_strong_up_btc_5m.yaml` (mirror)
- [ ] `v_v9_1_strong_dn_btc_5m.yaml` (mirror)
- [ ] `v_v12_extreme_btc_5m.yaml`
- [ ] `v_consensus_v91_lead_up_btc_5m.yaml`
- [ ] `v_consensus_v91_lead_dn_btc_5m.yaml`
- [ ] `v_consensus_all6_up_btc_5m.yaml`

Tier 2 — BTC 15m (2):
- [ ] `v_consensus_3of5_btc_15m.yaml`
- [ ] `v_15m_premium_hours_btc.yaml`

Tier 3 — ETH/XRP 15m (4):
- [ ] `v_eth_15m_classifier_top20.yaml`
- [ ] `v_eth_15m_classifier_top10.yaml`
- [ ] `v_xrp_15m_classifier_top20.yaml`
- [ ] `v_xrp_15m_classifier_top10.yaml`

### Tests

- [ ] `engine/tests/test_multi_model_consensus.py`
- [ ] `engine/tests/test_model_disagreement_veto.py`
- [ ] `engine/tests/test_confidence_band_skip.py`

### GPU box (out-of-band, no commit)

- [ ] Set `SCORER_ASSETS=btc,eth,xrp` on GPU `.env`
- [ ] Set `FEATURE_CACHE_ASSETS=btc,eth,xrp` on GPU `.env`
- [ ] Restart timesfm-api container
- [ ] Verify `/v4/snapshot?asset=ETH` and `?asset=XRP` return non-null `probability_classifier`
- [ ] Watch GPU CPU/GPU util — fall back to btc,eth only if sustained > 80%

### Verification (Montreal)

- [ ] Push branch, scp to Montreal, run `pytest engine/tests/test_multi_model_consensus.py test_model_disagreement_veto.py test_confidence_band_skip.py` (per CLAUDE.md no local pytest)
- [ ] Test load all 18 YAMLs via `StrategyRegistry.load_from_dir()` in a small driver script — must not raise
- [ ] Smoke test: load each strategy in DRY_RUN, confirm gate pipeline builds, no missing fields in `FullDataSurface`

## Out of scope (do NOT do in this worktree)

- ❌ DO NOT flip engine `TIMESFM_URL` (Billy's explicit go required)
- ❌ DO NOT INSERT INTO strategy_runtime_overrides (no LIVE/GHOST mode flips on RDS yet)
- ❌ DO NOT touch primary box (16.52.14.182)
- ❌ DO NOT merge to develop without Billy's review

## Order of operations

1. Plan (this file) — DONE
2. Read existing similar gates to match style (already done in pre-plan exploration)
3. Implement 3 gate types + tests
4. Register in registry.py
5. Write 18 YAMLs (group by tier)
6. Push branch + scp to Montreal + run pytest
7. GPU box config update + verify ETH/XRP signals flow
8. Hand back to Billy with PR link + verification log

## Success criteria

- All 3 new gate tests pass on Montreal pytest
- All 18 YAML configs load without `Unknown gate type` errors
- GPU box `/v4/snapshot?asset=ETH` returns non-null `probability_classifier` after env update
- Branch pushed, ready for PR review

## Rollback

If something breaks during verification:
- The branch is isolated — no impact on develop, primary, or any LIVE strategy
- GPU box .env has timestamped backups — `cp .env.backup_<TS> .env` then restart
- worktree can be deleted with `git worktree remove`
