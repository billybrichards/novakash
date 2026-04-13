# v2.2/OAK Integration Audit and Fix Plan

## Executive Summary

**v2.2/OAK is INTEGRATED but BROKEN** due to a code ordering bug. The v2.2 client works, the server responds, but the data is never written to the database because the code tries to write to `window_snapshot` before it's created.

## Findings

### 1. v2.2/OAK Client Status

| Component | Status | Details |
|-----------|--------|---------|
| **TimesFMV2Client** | ✅ Instantiated | `orchestrator.py` line 334-336 |
| **V2_EARLY_ENTRY_ENABLED** | ✅ Enabled | `.env`: `V2_EARLY_ENTRY_ENABLED=true` |
| **Server URL** | ✅ Configured | `TIMESFM_V2_URL=http://3.98.114.0:8080` |
| **Server Health** | ✅ Responding | `curl http://3.98.114.0:8080/v2/health` works |
| **Probability Endpoint** | ✅ Working | Returns real data (see below) |

### 2. v2.2/OAK Server Response

```json
{
    "asset": "BTC",
    "seconds_to_close": 120,
    "probability_up": 1.0,
    "probability_down": 0.0,
    "probability_raw": 0.9831469245534392,
    "model_version": "1d42c8a@v2/btc/btc_5m/1d42c8a/2026-04-06T19-26-58Z",
    "feature_freshness_ms": {
        "binance": 0,
        "coinglass": 748,
        "gamma": null,
        "timesfm": 0
    },
    "timesfm": {
        "direction": "DOWN",
        "confidence": 0.9631840177283864,
        "predicted_close": 68621.6953125,
        "spread": 52.515625
    },
    "timestamp": 1775586472.6064808
}
```

**Model Version**: `1d42c8a@v2/btc/btc_5m/1d42c8a/2026-04-06T19-26-58Z` (trained April 6, 2026)

**Issue**: Model returns extreme values (0.0 or 1.0) - likely overfitting.

### 3. Engine Logs Show v2.2 Data

```
v81.early_gate agrees=True cap=0.55 high_conf=True offset=240 v2_dir=UP v2_p=0.778 v8_dir=UP
v81.early_gate agrees=True cap=0.55 high_conf=True offset=230 v2_dir=UP v2_p=0.909 v8_dir=UP
v81.early_gate agrees=False cap=0.55 high_conf=True offset=190 v2_dir=DOWN v2_p=0.000 v8_dir=UP
```

**v2.2 is working in the engine** - it's returning values and affecting gate decisions.

### 4. Database is EMPTY for v2.2

```sql
SELECT window_ts, v2_direction, v2_probability_up FROM window_snapshots WHERE v2_probability_up IS NOT NULL;
-- Result: 0 rows
```

### 5. The Bug

**File**: `engine/strategies/five_min_vpin.py`

**Lines 537-547**:
```python
# v8.1: Query v2.2 for display in ALL notifications (before signal eval)
eval_offset = getattr(window, "eval_offset", None)
if self._timesfm_v2 is not None and eval_offset:
    try:
        _v2_pre = await self._timesfm_v2.get_probability(
            asset=window.asset, seconds_to_close=eval_offset
        )
        if _v2_pre and "probability_up" in _v2_pre:
            window_snapshot["v2_probability_up"] = round(float(_v2_pre["probability_up"]), 4)  # NameError!
            window_snapshot["v2_direction"] = "UP" if float(_v2_pre["probability_up"]) > 0.5 else "DOWN"
            window_snapshot["v2_model_version"] = _v2_pre.get("model_version", "")
            window_snapshot["eval_offset"] = eval_offset
    except Exception:
        pass  # Silent fail - bug is hidden!
```

**Line 599** (where `window_snapshot` is actually created):
```python
window_snapshot = {
    "window_ts": window.window_ts,
    "asset": window.asset,
    ...
}
```

**The Problem**: Code at line 544-547 tries to write to `window_snapshot` BEFORE it's defined at line 599. This causes a `NameError` which is caught by `except Exception: pass`, so the v2.2 data is never written.

## Fix Plan

### Phase 1: Fix v2.2 Data Persistence (Immediate)

**Step 1**: Move v2.2 fetch AFTER window_snapshot creation

**File**: `engine/strategies/five_min_vpin.py`

**Current Code (lines 537-547)**:
```python
# v8.1: Query v2.2 for display in ALL notifications (before signal eval)
eval_offset = getattr(window, "eval_offset", None)
if self._timesfm_v2 is not None and eval_offset:
    try:
        _v2_pre = await self._timesfm_v2.get_probability(
            asset=window.asset, seconds_to_close=eval_offset
        )
        if _v2_pre and "probability_up" in _v2_pre:
            window_snapshot["v2_probability_up"] = round(float(_v2_pre["probability_up"]), 4)
            window_snapshot["v2_direction"] = "UP" if float(float(_v2_pre["probability_up"])) > 0.5 else "DOWN"
            window_snapshot["v2_model_version"] = _v2_pre.get("model_version", "")
            window_snapshot["eval_offset"] = eval_offset
    except Exception:
        pass
```

**Fixed Code** (move to after line 650, after window_snapshot creation):
```python
# v8.1: Query v2.2 for display in ALL notifications (AFTER window_snapshot created)
eval_offset = getattr(window, "eval_offset", None)
if self._timesfm_v2 is not None and eval_offset:
    try:
        _v2_pre = await self._timesfm_v2.get_probability(
            asset=window.asset, seconds_to_close=eval_offset
        )
        if _v2_pre and "probability_up" in _v2_pre:
            window_snapshot["v2_probability_up"] = round(float(_v2_pre["probability_up"]), 4)
            window_snapshot["v2_direction"] = "UP" if float(_v2_pre["probability_up"]) > 0.5 else "DOWN"
            window_snapshot["v2_model_version"] = _v2_pre.get("model_version", "")
            window_snapshot["eval_offset"] = eval_offset
    except Exception as e:
        self._log.warning("v2.probability.fetch_failed", error=str(e)[:100])
```

**Step 2**: Remove duplicate `eval_offset` definition

**Line 252** and **Line 537** both define `eval_offset`. Keep line 252, remove line 537.

**Step 3**: Add logging to v2.2 DB UPDATE

**File**: `engine/strategies/five_min_vpin.py` (lines 812-820)

**Current Code**:
```python
except Exception:
    pass
```

**Fixed Code**:
```python
except Exception as e:
    self._log.warning("v2.db_update_failed", error=str(e)[:100], window_ts=window_snapshot.get("window_ts"))
```

### Phase 2: Dashboard Fixes (After Deploy)

1. **Query v2.2 data from correct column**: `window_snapshots.v2_probability_up` (not hardcoded 0.009)
2. **Query bankroll from correct table**: `system_state.current_balance` (not 0)
3. **Display model name**: `window_snapshots.v2_model_version` → "OAK"

### Phase 3: OAK Model Issues (Post-Fix)

**Problem**: OAK model returns extreme values (0.0 or 1.0)

**Possible Causes**:
1. Model is overfitting
2. Calibration is broken
3. Features are stale

**Actions**:
1. Monitor v2.2 data after fix to confirm values
2. If still extreme, investigate OAK model training
3. Prioritize CEDAR deployment

### Phase 4: CEDAR Migration

**Timeline**: TBD (awaiting OAK fix verification)

**Tasks**:
1. Deploy CEDAR model server
2. Update `TIMESFM_V2_URL` to CEDAR endpoint
3. Test CEDAR predictions
4. Update docs with CEDAR performance

## Rollout Plan

1. **Deploy fix to Montreal** (v8.2.4)
2. **Monitor v2.2 data in DB** for 1 hour
3. **Verify dashboard shows v2.2 data**
4. **Monitor OAK prediction quality**
5. **Plan CEDAR deployment**

## Success Criteria

- [ ] `window_snapshots.v2_probability_up` populated for all windows
- [ ] Dashboard shows real v2.2 values (not 0.009)
- [ ] Dashboard shows correct bankroll (63.89 USDC)
- [ ] OAK predictions are reasonable (0.3-0.7 range, not just 0.0/1.0)
- [ ] CEDAR model tested and ready for deployment

## Risk Assessment

- **Low Risk**: Fix is non-destructive, just moves code order
- **Medium Risk**: OAK model may still return extreme values
- **High Impact**: v2.2 data is critical for early entry gates

## Notes

- v2.2/OAK is a **KEY SIGNAL** for early entry gates (T-240 to T-120)
- Without v2.2 data, the engine skips early entries incorrectly
- The gate logic at line 992-995 works but doesn't persist to DB
- Dashboard is showing stale/hardcoded data (0.009) because DB is empty

---

## CI/CD Pipeline Status (Apr 13, 2026)

### GitHub Actions Auto-Deploy ✅

**Workflow**: `.github/workflows/deploy-engine.yml`

**Triggers**:
- **PRs**: Python syntax check on `engine/**` changes
- **Push to develop**: Auto-deploy to Montreal (15.223.247.178)
- **Manual**: Can be triggered from GitHub Actions tab

**GitHub Secrets Configured** (36 total):
- `ENGINE_SSH_KEY` - Deploy key for SSH access
- `ENGINE_HOST` - 15.223.247.178
- `DATABASE_URL` - Railway PostgreSQL connection
- `COINGLASS_API_KEY` - CoinGlass data feed
- `BINANCE_API_KEY`, `BINANCE_API_SECRET` - Binance WebSocket (empty)
- `POLY_*` - Polymarket API keys, private key, funder address
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - Alerts
- `BUILDER_*` - Polymarket Builder API
- `RELAYER_*` - Polymarket Relayer
- `TIINGO_API_KEY` - Tiingo price feed
- `CHAINLINK_BTC_USD` - Chainlink oracle
- `PAPER_MODE` - Paper trading flag
- `ANTHROPIC_API_KEY` - Claude evaluator
- `POLYGON_RPC_URL` - Polygon RPC endpoint

**Deploy Process**:
1. Python syntax check (engine/main.py, five_min_vpin.py, orchestrator.py)
2. Rsync code to Montreal (`novakash@15.223.247.178:/home/novakash/novakash/engine/`)
3. Rsync scripts directory
4. Write `.env` from GitHub secrets
5. Kill existing engine process
6. Start new engine process
7. Health checks:
   - Process count = 1
   - Error signature scan (last 10k lines of engine.log)
   - Error thresholds: `clob_feed.write_error=0`, `reconciler.resolve_db_error=0`, etc.

**Documentation**:
- `docs/CI_CD_SETUP.md` - GitHub Actions secrets configuration guide
- `docs/MONTREAL_DEPLOYMENT_TROUBLESHOOTING.md` - Server troubleshooting

**Commit**: `068478e` - "CI/CD: Add GitHub Actions secrets for auto-deploy to Montreal"

**Next Steps**:
1. Trigger test deploy from GitHub Actions tab
2. Verify engine starts and health checks pass
3. Monitor engine.log for any new errors
4. Future engine changes auto-deploy on push to develop
