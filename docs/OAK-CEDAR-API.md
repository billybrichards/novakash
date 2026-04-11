# OAK & CEDAR API — v2.2 Prediction Service

**Status**: OAK active, CEDAR in development

---

## OAK (Current v2.2 Model)

### Endpoint
```
GET /v2/probability?asset=BTC&seconds_to_close=N
```

### Server
- **URL**: `http://3.98.114.0:8080`
- **Location**: Montreal EC2 (ca-central-1)
- **Model Version**: `1d42c8a@v2/btc/btc_5m/1d42c8a/2026-04-06T19-26-58Z` (deployed Apr 6, 2026)

### Response Schema
```json
{
  "asset": "BTC",
  "seconds_to_close": 120,
  "delta_bucket": 120,
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

### Engine Integration
- **Enabled via**: `V2_EARLY_ENTRY_ENABLED=true` in `.env`
- **Client**: `signals.timesfm_v2_client.TimesFMV2Client`
- **Usage**: Early entry gate at T-240 to T-60
- **Gate Logic**: Skip if `v2_dir != v8_dir` or `v2_p < 0.35 or v2_p > 0.65` (low confidence)

### DB Schema
```sql
-- window_snapshots table
v2_probability_up   DOUBLE PRECISION
v2_direction        VARCHAR(10)
v2_agrees           BOOLEAN
v2_model_version    VARCHAR(100)
eval_offset         INTEGER
```

### Known Issues
1. **Overfitting**: Model returns extreme values (0.0 or 1.0) most of the time
2. **DB Write Failures**: v2.2 UPDATE queries fail silently (exception handler catches errors)
3. **Dashboard Not Connected**: Dashboard shows hardcoded 0.009 instead of querying `window_snapshots.v2_probability_up`

---

## CEDAR (Next-Gen Model)

### Status
- **Phase**: Investigation/Development
- **Expected Improvements**: +5-9pp WR over OAK (target: 85-90% gated WR)
- **Migration Plan**: TBD

### Planned Changes
- Better calibrated probabilities (not binary 0/1)
- Improved feature engineering
- Potential endpoint changes (TBD)

---

## Dashboard Wiring Issues

### Current State
| Field | Source | Current Value | Expected Value |
|-------|--------|---------------|----------------|
| `v2_probability_up` | `window_snapshots.v2_probability_up` | 0.009 (hardcoded) | NULL or actual value |
| `v2_direction` | `window_snapshots.v2_direction` | 0.009 (hardcoded) | NULL or actual value |
| `current_bankroll` | **WRONG SOURCE** | 0 | 63.89 USDC |
| `model_name` | None | None | OAK (or CEDAR) |

### Required Fixes
1. **Bankroll**: Query `system_state.current_balance`
2. **v2.2 Data**: Query `window_snapshots.v2_probability_up` and `window_snapshots.v2_direction`
3. **Model Name**: Display "OAK" from `window_snapshots.v2_model_version`
4. **CLOB Data**: Query `clob_feed.prices` for real-time prices

---

## Environment Variables

```bash
# OAK (v2.2)
V2_EARLY_ENTRY_ENABLED=true
TIMESFM_V2_URL=http://3.98.114.0:8080
TIMESFM_V2_TIMEOUT=5.0

# CEDAR (future)
# CEDAR_URL=http://<new-server>:<port>
# CEDAR_ENABLED=false
```

---

## Quick Test

```bash
# Test OAK endpoint
curl -s 'http://3.98.114.0:8080/v2/probability?asset=BTC&seconds_to_close=120'

# Query engine DB for v2.2 data
PGPASSWORD=... psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway -c "
  SELECT window_ts, v2_direction, v2_probability_up 
  FROM window_snapshots 
  WHERE v2_probability_up IS NOT NULL 
  LIMIT 10;
"
```

---

## Migration Checklist (OAK → CEDAR)

- [ ] Deploy CEDAR model server
- [ ] Test endpoint: `/v2/probability` or `/cedar/predict`
- [ ] Update `TIMESFM_V2_URL` in `.env`
- [ ] Update `TimesFMV2Client` if endpoint changes
- [ ] Test early entry gate with CEDAR
- [ ] Monitor WR comparison (OAK vs CEDAR)
- [ ] Update dashboard to show CEDAR model name
- [ ] Document CEDAR performance metrics

---

**Last Updated**: 2026-04-07 18:30 UTC
**Author**: Novakash2
