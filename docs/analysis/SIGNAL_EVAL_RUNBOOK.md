# Signal Evaluation Runbook

**Purpose:** Complete guide for any agent to run full signal accuracy analysis against the Railway PostgreSQL database, assess current trading behaviour, and recommend config changes.

**Last updated:** 2026-04-12

---

## Section 1: Quick Start

### Hub API (preferred — no DB credentials needed)

All signal data is accessible via the AWS hub at `http://3.98.114.0:8091`. Use this first.

```bash
# Get JWT token from AWS hub (not Railway — Railway hub may be stale)
TOKEN=$(curl -s -X POST http://3.98.114.0:8091/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"billy","password":"novakash2026"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token',''))")

# Key analysis endpoints
curl -s "http://3.98.114.0:8091/api/v58/accuracy?limit=100" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/strategy-decisions?limit=50" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/prediction-surface?days=7" -H "Authorization: Bearer $TOKEN"
curl -s "http://3.98.114.0:8091/api/v58/execution-hq?asset=btc&timeframe=5m" -H "Authorization: Bearer $TOKEN"

# TimesFM live surface (no auth required)
curl -s "http://3.98.114.0:8080/v4/snapshot?asset=btc&timescales=5m"
curl -s "http://3.98.114.0:8080/v3/snapshot?asset=btc"
```

### Get the Database URL (for direct SQL queries)

**Option A — Railway dashboard**

1. Open Railway dashboard → project → PostgreSQL service → Variables tab
2. Copy `DATABASE_PUBLIC_URL` — it looks like:
   ```
   postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway
   ```

**Option B — Montreal SSH (if you have AWS access)**

```bash
# Step 1: Generate a temp key (valid 60s — must connect immediately after)
ssh-keygen -t ed25519 -f /tmp/analysis_key -N "" -q 2>/dev/null || true

# Step 2: Push public key to instance via EC2 Instance Connect
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key "$(cat /tmp/analysis_key.pub)"

# Step 3: SSH immediately (key expires in 60s)
ssh -i /tmp/analysis_key -o StrictHostKeyChecking=no ubuntu@15.223.247.178 \
  "sudo grep '^DATABASE_URL=' /home/novakash/novakash/engine/.env | sed 's/postgresql+asyncpg/postgresql/'"
```

**How to grant yourself EC2 Instance Connect access (if you have AWS root/admin):**

```bash
# 1. Ensure the IAM user/role has ec2-instance-connect:SendSSHPublicKey permission.
#    Attach the managed policy AmazonEC2InstanceConnectPolicy, or add inline:
#    {
#      "Effect": "Allow",
#      "Action": "ec2-instance-connect:SendSSHPublicKey",
#      "Resource": "arn:aws:ec2:ca-central-1:267815793130:instance/i-0785ed930423ae9fd",
#      "Condition": {"StringEquals": {"ec2:osuser": "ubuntu"}}
#    }
#
# 2. Ensure the instance's security group allows SSH (port 22) from your IP.
#    The instance i-0785ed930423ae9fd is in sg-0de6838438bfc27ec (novakash-vnc).
#    Check: aws ec2 describe-security-groups --region ca-central-1 --group-ids sg-0de6838438bfc27ec
#
# 3. Verify the EC2 Instance Connect endpoint service is enabled in ca-central-1.
#    (It should be — it's a managed AWS service, not custom infrastructure)
#
# 4. Test:
aws sts get-caller-identity  # confirm you're authenticated
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user ubuntu \
  --ssh-public-key "$(cat /tmp/analysis_key.pub)"
# Should return {"RequestId": "...", "Success": true}
```

### Set environment variable and run scripts

```bash
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"

# Quick window accuracy surface
python3 docs/analysis/run_window_analysis.py

# Full current-state report (7 sections)
python3 docs/analysis/full_signal_report.py --hours 4

# With options
python3 docs/analysis/full_signal_report.py --hours 1 --asset BTC --no-color
```

---

## Section 2: Key Tables Reference

| Table | What it stores | Key columns for analysis |
|-------|---------------|--------------------------|
| `signal_evaluations` | V10 gate eval per 2s tick throughout each window | `eval_offset` (secs from close), `v2_direction` (UP/DOWN), `v2_probability_up` (0–1), `vpin`, `regime`, `clob_up_ask`, `clob_down_ask`, `decision`, `gate_failed`, `window_ts`, `asset` |
| `strategy_decisions` | V10+V4 decisions per 2s tick | `strategy_id` ('v4_fusion', 'v10_gate'), `mode` (LIVE/GHOST), `action` (TRADE/SKIP), `direction`, `skip_reason`, `eval_offset`, `evaluated_at`, `metadata_json` (JSONB) |
| `window_snapshots` | Per-window outcome (ground truth) | `window_ts` (bigint epoch), `asset`, `close_price`, `open_price`, `direction` (engine's source-agreement direction — use with caution), `open_time`, `close_time` |
| `ticks_v3_composite` | V3 composite signal across 9 timescales | `ts`, `composite_score`, `timescale`, `elm_signal`, `cascade_signal`, `vpin_signal`, `asset` |
| `ticks_v4_decision` | Full V4 snapshot per tick | `ts`, `asset`, `regime`, `regime_confidence`, `conviction`, `probability_up`, `sub_signals` (JSONB) |
| `trade_bible` | Definitive live trade record | `id`, `strategy`, `direction`, `outcome` (WIN/LOSS), `entry_price`, `stake`, `pnl`, `created_at` |
| `ticks_chainlink` | Chainlink oracle prices (resolution source of truth) | `ts`, `asset`, `price` |
| `ticks_tiingo` | Tiingo top-of-book bid/ask | `ts`, `asset`, `bid`, `ask`, `exchange` |

### Ground truth rule

**Always use:** `CASE WHEN close_price > open_price THEN 'UP' WHEN close_price < open_price THEN 'DOWN' ELSE 'FLAT' END`

**Never use:**
- `window_snapshots.oracle_outcome` — always NULL (reconciler does not populate it)
- `window_snapshots.actual_direction` — column does not exist

---

## Section 3: Standard Analysis Queries

### A. Signal accuracy by eval_offset (the magic window)

Answers: "At what T-minus are our predictions most accurate?"

```sql
SELECT
    FLOOR(se.eval_offset / 15.0) * 15 AS offset_bucket,
    COUNT(*) AS n,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct,
    ROUND(AVG(ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5))::numeric, 3) AS avg_dist
FROM signal_evaluations se
JOIN window_snapshots ws
    ON se.window_ts = ws.window_ts::bigint
    AND se.asset = ws.asset
WHERE se.eval_offset BETWEEN 30 AND 240
  AND se.asset = 'BTC'
  AND se.v2_direction IS NOT NULL
  AND ws.close_price > 0 AND ws.open_price > 0
GROUP BY 1
ORDER BY 1 DESC;
```

**Expected result (2026-04-12 baseline):**

| Offset bucket | Accuracy | Notes |
|--------------|---------|-------|
| T-240 | ~49% | Below random — too early |
| T-180 | ~55% | Improving |
| T-135 | ~56% | Peak |
| T-120 | ~55% | Very good |
| T-90 | ~49% | Drops below 50% |
| T-60 | ~45% | Anti-predictive — market has priced in |

**Key insight:** Signal gets WORSE below T-90. The CLOB has already priced in the outcome. Trade at T-90 to T-150.

---

### B. Accuracy by confidence band

Answers: "Does our confidence score (distance from 0.5) predict edge?"

```sql
SELECT
    FLOOR(se.eval_offset / 30.0) * 30 AS offset_bucket,
    CASE
        WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.06 THEN 'weak(<6%)'
        WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.12 THEN 'mod(6-12%)'
        WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) < 0.20 THEN 'strong(12-20%)'
        ELSE 'high(>20%)'
    END AS confidence_band,
    COUNT(*) AS n,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct
FROM signal_evaluations se
JOIN window_snapshots ws
    ON se.window_ts = ws.window_ts::bigint
    AND se.asset = ws.asset
WHERE se.asset = 'BTC'
  AND se.v2_direction IS NOT NULL
  AND ws.close_price > 0 AND ws.open_price > 0
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

**Expected result (2026-04-12 baseline, T-90 to T-150):**

| Band | Accuracy | Action |
|------|---------|--------|
| high (>20%) | ~65% | Trade |
| strong (12-20%) | ~64% | Trade |
| mod (6-12%) | ~38% | NEVER trade — anti-predictive |
| weak (<6%) | ~32% | NEVER trade — anti-predictive |

**Key rule:** Only trade when `confidence_distance >= 0.12` (strong or high band).

---

### C. CLOB divergence analysis

Answers: "When is Sequoia ahead of the CLOB? That gap is the edge."

```sql
SELECT
    FLOOR(se.eval_offset / 30.0) * 30 AS offset_bucket,
    CASE WHEN ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
         THEN 'hi-conf' ELSE 'lo-conf' END AS conf,
    ROUND(AVG(
        CASE WHEN se.v2_direction = 'UP'   THEN se.clob_up_ask
             WHEN se.v2_direction = 'DOWN' THEN se.clob_down_ask
        END
    )::numeric, 3) AS avg_clob_ask,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct,
    COUNT(*) AS n
FROM signal_evaluations se
JOIN window_snapshots ws
    ON se.window_ts = ws.window_ts::bigint
    AND se.asset = ws.asset
WHERE se.asset = 'BTC'
  AND se.v2_direction IS NOT NULL
  AND ws.close_price > 0 AND ws.open_price > 0
  AND (se.clob_up_ask IS NOT NULL OR se.clob_down_ask IS NOT NULL)
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

**Interpretation:**
- `clob_implied_prob = 1 - clob_up_ask` for UP direction (CLOB YES ask)
- `edge = v2_probability_up - clob_implied_prob` — positive = Sequoia ahead of CLOB
- Best case: DOWN prediction + cheap NO (clob_down_ask <= $0.58) → historically 82.5% WR
- Worst case: UP prediction + cheap YES → 1.8% WR (market already disagrees)

---

### D. V4 paper trade analysis

Answers: "How is V4 actually performing in paper trading mode?"

```sql
-- V4 TRADE decisions with outcomes
SELECT
    sd.direction,
    sd.eval_offset,
    sd.evaluated_at,
    ws.close_price,
    ws.open_price,
    CASE WHEN (sd.direction = 'UP'   AND ws.close_price > ws.open_price)
           OR (sd.direction = 'DOWN' AND ws.close_price < ws.open_price)
    THEN 'WIN' ELSE 'LOSS' END AS outcome,
    sd.metadata_json
FROM strategy_decisions sd
JOIN window_snapshots ws
    ON sd.window_ts = ws.window_ts::bigint
    AND sd.asset = ws.asset
WHERE sd.strategy_id = 'v4_fusion'
  AND sd.action = 'TRADE'
  AND ws.close_price > 0 AND ws.open_price > 0
  AND sd.evaluated_at >= NOW() - INTERVAL '4 hours'
ORDER BY sd.evaluated_at DESC;

-- V4 skip reason distribution (last 4h)
SELECT
    skip_reason,
    COUNT(*) AS n,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM strategy_decisions
WHERE strategy_id = 'v4_fusion'
  AND action = 'SKIP'
  AND evaluated_at >= NOW() - INTERVAL '4 hours'
GROUP BY 1
ORDER BY 2 DESC;
```

---

### E. V3 composite regime correlation

Answers: "Does high V3 composite score predict better accuracy?"

```sql
-- V3 signals aggregated to window level, joined to outcomes
SELECT
    CASE WHEN v3.avg_composite > 0.15 THEN 'high(>0.15)'
         WHEN v3.avg_composite > 0.05 THEN 'mid(0.05-0.15)'
         ELSE 'low(<0.05)' END AS composite_band,
    COUNT(*) AS windows,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct
FROM signal_evaluations se
JOIN window_snapshots ws
    ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
JOIN (
    -- Window-level aggregation required — ticks_v3_composite ts doesn't align directly
    SELECT
        asset,
        (DATE_TRUNC('second', ts) - INTERVAL '1 second' *
            MOD(EXTRACT(EPOCH FROM ts)::int, 300)) AS window_ts_approx,
        AVG(composite_score) AS avg_composite
    FROM ticks_v3_composite
    GROUP BY 1, 2
) v3 ON se.asset = v3.asset
    AND ABS(se.window_ts - EXTRACT(EPOCH FROM v3.window_ts_approx)) < 300
WHERE se.eval_offset BETWEEN 90 AND 150
  AND se.asset = 'BTC'
  AND se.v2_direction IS NOT NULL
  AND ws.close_price > 0 AND ws.open_price > 0
  AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
GROUP BY 1
ORDER BY 1;
```

**Note:** V3 timestamp alignment is imprecise. Use window-level aggregation (GROUP BY approximate window bucket), not direct ts match.

---

### F. Recent 4h and 1h performance

**Ungated signal (last 4h):**

```sql
SELECT
    COUNT(*) AS total_evals,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct,
    SUM(CASE WHEN ws.close_price > ws.open_price THEN 1 ELSE 0 END) AS actual_up,
    SUM(CASE WHEN ws.close_price < ws.open_price THEN 1 ELSE 0 END) AS actual_down,
    ROUND(AVG(ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5))::numeric, 3) AS avg_dist
FROM signal_evaluations se
JOIN window_snapshots ws
    ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
WHERE se.asset = 'BTC'
  AND se.eval_offset BETWEEN 90 AND 150
  AND se.v2_direction IS NOT NULL
  AND ws.close_price > 0 AND ws.open_price > 0
  AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
  AND se.evaluated_at >= NOW() - INTERVAL '4 hours';
```

**Gated decisions (last 4h):**

```sql
SELECT
    sd.strategy_id,
    sd.action,
    COUNT(*) AS n
FROM strategy_decisions sd
WHERE sd.evaluated_at >= NOW() - INTERVAL '4 hours'
  AND sd.asset = 'BTC'
GROUP BY 1, 2
ORDER BY 1, 2;
```

---

## Section 4: Full Analysis Report Template

When running the full analysis, produce a structured report with these sections:

### 1. Data Coverage

```
Windows total:         <COUNT from window_snapshots>
Signal evaluations:    <COUNT from signal_evaluations>
Date range:            <MIN(evaluated_at)> to <MAX(evaluated_at)>
V4 trade decisions:    <COUNT where strategy_id='v4_fusion' AND action='TRADE'>
V10 ghost decisions:   <COUNT where strategy_id='v10_gate'>
```

### 2. Current Market Regime (last 4h)

```
UP windows (last 4h):    X%
DOWN windows (last 4h):  Y%
Average VPIN:            Z
Dominant HMM regime:     <regime with highest count>
Average confidence_dist: Z
```

### 3. Ungated Signal Performance

Report for BOTH last 4h and all-time:
- Overall accuracy (dist >= 0.12, T-90 to T-150)
- Accuracy by eval_offset bucket (15s granularity, T-30 to T-240)
- Accuracy by confidence band (weak/mod/strong/high)

### 4. V4 Paper Trade Performance (last 4h)

```
TRADE decisions:     N
  - WIN:             N (X%)
  - LOSS:            N (X%)
  - Unresolved:      N (window close not yet in DB)
SKIP decisions:      N
Skip reason breakdown:
  - confidence_too_low:   N (X%)
  - regime_risk_off:      N (X%)
  - timing_not_optimal:   N (X%)
  - consensus_unsafe:     N (X%)
  - other:                N (X%)
```

### 5. V10 Ghost Performance (last 4h)

```
Gate eval count:         N
Gate failure distribution:
  - delta_magnitude:     N (X%)
  - vpin_threshold:      N (X%)
  - timing_window:       N (X%)
  - regime_filter:       N (X%)
  - <other gates>:       N (X%)
Would-have trades:       N
  - Would-have WIN:      N (X%)
  - Would-have LOSS:     N (X%)
```

### 6. CLOB Divergence Check

For each recent V4 TRADE decision, compare:
- `v2_probability_up` vs `1 - clob_up_ask` (for UP) or `1 - clob_down_ask` (for DOWN)
- Positive edge (Sequoia ahead of CLOB) = good, negative edge = bad
- Report average edge at time of trade

### 7. Config Recommendations

Based on the data, state one of:
- "Keep config, signal strong" / "Loosen confidence threshold" / "Tighten timing window" / "Pause, investigate"

See Section 7 for the decision framework.

---

## Section 5: Schema Gotchas

These are known issues that cause silent query errors or wrong results.

| Issue | Wrong | Correct |
|-------|-------|---------|
| Ground truth column | `window_snapshots.actual_direction` (does not exist) | `CASE WHEN close_price > open_price THEN 'UP' ...` |
| Oracle outcome | `oracle_outcome` (always NULL) | Use close_price vs open_price |
| Rounding doubles | `ROUND(value, 2)` (PG type error) | `ROUND(value::numeric, 2)` |
| Time filter for decisions | `created_at` | Use `evaluated_at` on `strategy_decisions` |
| eval_offset units | milliseconds | Seconds (90 = T-90s from window close) |
| V3 timestamp join | Direct ts match | Window-level aggregation (GROUP BY 300s bucket) |
| signal_evaluations direction | `direction` column | `v2_direction` column |
| window_snapshots join key | `window_ts` as timestamp | `window_ts` is bigint epoch — cast: `ws.window_ts::bigint` |

---

## Section 6: One-Command Full Report

```bash
export PUB_URL="postgresql://postgres:PASSWORD@hopper.proxy.rlwy.net:35772/railway"
python3 docs/analysis/full_signal_report.py
```

See `/docs/analysis/full_signal_report.py` for the complete script.

---

## Section 7: Config Decision Framework

### Accuracy-based action table

| Last 4h accuracy (dist>=0.12, T-90-150) | Action |
|------------------------------------------|--------|
| > 65% | Keep config. Consider increasing position size. |
| 55–65% | Keep config. Maintain current position size. |
| 45–55% | Consider tightening confidence threshold to 0.15. |
| < 45% | Pause. Investigate regime change. Do not trade. |

### V4 showing 0 TRADE decisions

Check in order:
1. `confidence_distance` — is it below 0.12? If most evals are weak/mod, signal is flat.
2. HMM regime — is it `risk_off` or `chop`? V4 correctly blocks in these regimes.
3. Timing — are evals arriving outside T-90 to T-120? Check `eval_offset` distribution.
4. `consensus_safe` — if source agreement is split, V4 will skip.

### Confidence threshold tuning

Run query B at thresholds 0.10, 0.12, 0.15 to find optimal cut-off:
- If 0.10 band accuracy >= 60%: relax to 0.10 (more trades, similar WR)
- If 0.15 band accuracy > 66%: tighten to 0.15 (fewer trades, higher WR)
- Default: 0.12 (strong empirical basis from Apr 2026 analysis)

### Eval offset window tuning

Run query A and look for the peak bucket:
- If peak shifts earlier (T-150+): widen window to 90–180
- If peak collapses: data regime may have changed — run more data
- If below T-90 looks useful: do NOT relax. That is market efficiency, not signal.

### CLOB ask gate

Current rule: buy when `clob_ask <= 0.58` for the predicted direction.
- If accuracy at ask > 0.58 is >= 60%: raise cap to 0.62
- If accuracy at ask <= 0.54 is >= 70%: lower cap to 0.54 (only cheap CLOB)
- For UP direction: extra caution — only trade when clob_up_ask is demonstrably cheap

### Regime filter

V4 blocks `risk_off` and `chop` regimes. To verify these blocks are warranted:

```sql
SELECT
    se.regime,
    COUNT(*) AS n,
    ROUND(
        100.0 * SUM(
            CASE WHEN (se.v2_direction = 'UP'   AND ws.close_price > ws.open_price)
                   OR (se.v2_direction = 'DOWN' AND ws.close_price < ws.open_price)
            THEN 1 ELSE 0 END
        )::numeric / COUNT(*), 1
    ) AS accuracy_pct
FROM signal_evaluations se
JOIN window_snapshots ws ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset
WHERE se.asset = 'BTC'
  AND se.eval_offset BETWEEN 90 AND 150
  AND ABS(COALESCE(se.v2_probability_up, 0.5) - 0.5) >= 0.12
  AND ws.close_price > 0 AND ws.open_price > 0
GROUP BY 1
ORDER BY 2 DESC;
```

If `chop` or `risk_off` accuracy >= 60%: consider removing regime block. If < 50%: block is justified.

---

## Appendix: Quick Reference

### Key thresholds (current as of 2026-04-12)

| Parameter | Value | Source |
|-----------|-------|--------|
| eval_offset window | T-90 to T-120 | V4 LIVE config (post-flip) |
| confidence_distance | >= 0.06 (V4 gate), >= 0.12 (optimal from analysis) | strategy_decisions + analysis |
| VPIN filter | >= 0.55 adds +0.9pp | window analysis |
| CLOB ask cap | <= $0.58 | window analysis |
| V4 mode | LIVE paper | flipped 2026-04-12 13:00 UTC |
| V10 mode | GHOST | flipped 2026-04-12 13:00 UTC |

### Active strategies

| Strategy | strategy_id | mode | eval window |
|----------|-------------|------|-------------|
| V4 Fusion | `v4_fusion` | LIVE paper | T-90 to T-120 |
| V10 Gate Stack | `v10_gate` | GHOST | T-90 to T-120 |

### Table join patterns

```sql
-- signal_evaluations → window_snapshots
ON se.window_ts = ws.window_ts::bigint AND se.asset = ws.asset

-- strategy_decisions → window_snapshots
ON sd.window_ts = ws.window_ts::bigint AND sd.asset = ws.asset

-- ticks_v3_composite → window_snapshots (approximate, bucket join)
WHERE ABS(v3_ts_epoch - ws.window_ts) < 300
```
