# Engine Test Triage Log — Phase 4

**Baseline:** 133 failures (branch claude/nice-germain, 2026-04-16).

Per-test decision record.

| Cluster | Test File(s) | Count | Classification | Action | Notes |
|---|---|---|---|---|---|
| A | test_value_objects.py | 91 | stale-fixture | FIX | `WindowKey` has no `.key` prop; many VOs are stubs (`pass`). Tests specify the full contract. Implement VO fields/validators per tests. |
| B | test_risk_manager.py | 13 | stale-fixture | FIX | `force_kill()` became async; `force_resume()` renamed to `resume()`; paper mode no longer bypasses venue gate first. Fix tests to match production API. |
| C | test_pg_window_state_repo.py | 19 | stale-fixture | FIX | `asyncio.get_event_loop().run_until_complete()` fails on Python 3.14. Convert to `asyncio.run()`. |
| D | test_reconcile_manual_trades_sot.py + test_reconcile_trades_sot.py | 22 | stale-fixture | FIX | Reconciler now uses `poly_fills` DB join path; stub `fetch_*_joined_poly_fills` returns `[]`; `update_*_sot` missing `polymarket_tx_hash` kwarg. Rewrite stubs + `_make_reconciler` to inject poly_fills rows. |
| E | test_manual_trade_fast_path.py | 5 | stale-fixture | FIX | `FakeFiveMinStrategy` lacks `recent_windows` property (has `_recent_windows`); production code calls `.recent_windows` (property). Add property to fake. |
| F | test_data_surface.py + test_strategy_configs.py + test_v4_fusion_strategy.py | 9 | stale-fixture | FIX | delta_source priority changed (chainlink now first, not tiingo); `v4_down_only.mode` flipped to GHOST; V4FusionStrategy `_get_rec_extras` now routes UP/DOWN recommended_side through polymarket path before legacy gates. Fix test expectations. |

**Legend:**
- Classification: `real-bug` / `stale-fixture` / `obsolete-feature` / `infra-blocked`
- Action: `FIX` (code or test change) / `XFAIL` (mark xfail + create hub task) / `DELETE` (rm test)

**Summary:**
- Total at baseline: 133 failures, 370 passed
- All 133 classified as `stale-fixture` — production code moved correctly, tests didn't keep up
- Action plan: FIX all 133 (no XFAIL, no DELETE)
