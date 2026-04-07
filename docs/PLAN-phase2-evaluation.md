# Phase 2 Plan — Window Evaluation Service & Enhanced Notifications

**Date:** April 7, 2026
**Author:** Novakash2
**Status:** Proposed — awaiting Billy's review

---

## 1. Real Trade History & P&L Journey

### Actual Session: Apr 6 23:47 → Apr 7 15:13 UTC

```
START: $130.82 USDC
 ↓
$132.67 ✅ +$1.85  v2.2_early_T180 YES WIN @0.73
$127.67 ❌ -$5.00  v2.2_early_T180 YES LOSS @0.73
$129.52 ✅ +$1.85  v2.2_early_T240 NO WIN @0.73
$132.21 ✅ +$2.69  v2.2_early_T120 NO WIN @0.65
$136.30 ✅ +$4.09  v2.2_early_T240 NO WIN @0.55
$138.15 ✅ +$1.85  v8_standard YES WIN @0.73
$142.24 ✅ +$4.09  v2.2_early_T240 YES WIN @0.55     ← PEAK $142
$144.09 ✅ +$1.85  v8_standard NO WIN @0.73
$145.94 ✅ +$1.85  v8_standard NO WIN @0.73
$147.79 ✅ +$1.85  v2.2_early_T240 NO WIN @0.73
$151.10 ✅ +$3.31  v8_standard NO WIN @0.73
$142.75 ❌ -$8.35  v8_standard YES LOSS @0.73         🛡️ v2.2 blocks
$146.02 ✅ +$3.27  v8_standard YES WIN @0.73
$136.83 ❌ -$9.19  v8_standard NO LOSS @0.73          🛡️ v2.2 blocks
$140.11 ✅ +$3.28  v8_standard NO WIN @0.73
$143.49 ✅ +$3.38  v8_standard YES WIN @0.73
$134.99 ❌ -$8.50  v8_standard NO LOSS @0.73          🛡️ v2.2 blocks
$126.21 ❌ -$8.78  v2.2_early_T240 NO LOSS @0.73      (legit)
$129.24 ✅ +$3.03  v2.2_early_T190 YES WIN @0.73
$132.34 ✅ +$3.10  v2.2_early_T190 YES WIN @0.73
$123.72 ❌ -$8.62  v8_standard NO LOSS @0.73          🛡️ v2.2 blocks
$126.74 ✅ +$3.02  v8_standard YES WIN @0.73
$118.31 ❌ -$8.43  v8_standard NO LOSS @0.73          🛡️ v2.2 blocks
$121.11 ✅ +$2.80  v2.2_confirmed_T100 NO WIN @0.73
$124.00 ✅ +$2.89  v2.2_confirmed_T90 NO WIN @0.73
$126.76 ✅ +$2.76  v2.2_confirmed_T110 YES WIN @0.73
$129.80 ✅ +$3.04  v2.2_early_T210 NO WIN @0.73
$132.63 ✅ +$2.83  v2.2_confirmed_T100 NO WIN @0.73
$135.84 ✅ +$3.21  v2.2_confirmed_T60 NO WIN @0.73
$139.17 ✅ +$3.33  v2.2_confirmed_T90 NO WIN @0.73
$129.84 ❌ -$9.33  v2.2_early_T170 YES LOSS @0.73     (legit)
$120.81 ❌ -$9.03  v2.2_confirmed_T100 NO LOSS @0.65  (legit)
─── DYNAMIC CAP FIX DEPLOYED 09:50 ───
$125.31 ✅ +$4.50  v2.2_confirmed_T90 NO WIN @0.66     ← bigger wins!
$131.10 ✅ +$5.79  v2.2_confirmed_T110 NO WIN @0.66
$118.26 ❌ -$12.84 v2.2_confirmed_T70 NO LOSS @0.76   🛡️ v8.1.1 blocks
$124.67 ✅ +$6.41  v2.2_confirmed_T110 NO WIN @0.65
$132.38 ✅ +$7.71  v2.2_confirmed_T80 NO WIN @0.65
$116.83 ❌ -$15.55 v2.2_confirmed_T70 NO LOSS @0.80   🛡️ v8.1.1 blocks
$107.57 ❌ -$9.26  v2.2_confirmed_T100 YES LOSS @0.65  ⚠️ NORMAL
$112.46 ✅ +$4.89  v2.2_confirmed_T110 YES WIN @0.65
$101.51 ❌ -$10.95 v2.2_confirmed_T70 NO LOSS @0.73   🛡️ v8.1.1 blocks
$93.77  ❌ -$7.74  v2.2_confirmed_T60 YES LOSS @0.73   (legit CASCADE)
$97.34  ✅ +$3.57  v2.2_confirmed_T100 YES WIN @0.65

END: $99.94 USDC (actual wallet)
Calculated: $97.35 (fees account for ~$2.59 difference)
```

### Summary

| Metric | Actual | If All Gates Active |
|--------|--------|-------------------|
| Trades | 43 (29W/14L) | 43 (29W/6L) |
| WR | 67.4% | 82.9% |
| P&L | **-$33.47** | **+$48.96** |
| Wallet | $99.94 | ~$179.78 |
| Losses blocked | 0 | 8 (5 by v2.2 + 3 by v8.1.1) |

---

## 2. Predicted Outcome Generator

### What it does
At the END of each 5-min window (T-0, just before resolution), capture:
- Tiingo close price
- Chainlink close price  
- Predicted winner based on each source
- Whether we traded or skipped (and why)
- Our position (if any)

### Data saved to new table: `window_predictions`

```sql
CREATE TABLE window_predictions (
  window_ts BIGINT PRIMARY KEY,
  asset VARCHAR(10),
  tiingo_open NUMERIC, tiingo_close NUMERIC,
  chainlink_open NUMERIC, chainlink_close NUMERIC,
  tiingo_direction VARCHAR(4),    -- UP/DOWN based on close > open
  chainlink_direction VARCHAR(4),
  our_signal_direction VARCHAR(4),
  v2_direction VARCHAR(4),
  v2_probability NUMERIC,
  vpin_at_close NUMERIC,
  regime VARCHAR(15),
  trade_placed BOOLEAN,
  our_direction VARCHAR(4),       -- if traded
  our_entry_price NUMERIC,
  skip_reason TEXT,
  -- Filled after resolution:
  oracle_winner VARCHAR(4),
  tiingo_correct BOOLEAN,
  chainlink_correct BOOLEAN,
  our_signal_correct BOOLEAN,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Why Tiingo + Chainlink
- Tiingo is our delta source — tracks BTC price closely
- Chainlink IS the oracle source — Polymarket resolves from Chainlink Data Streams
- Comparing both at T-0 tells us: "would our signal have been right at close?"
- Over time: builds accuracy dataset per source per regime

---

## 3. Post-Window AI Evaluator (Macro-Observer Service)

### Architecture

```
Window closes (T-0)
    ↓
Engine saves window_predictions row
    ↓
Oracle resolves (T+60 to T+120)
    ↓
Engine updates oracle_winner
    ↓  webhook / DB poll
Macro-Observer Service (Railway)
    ↓
Queries ALL data for this window:
  - window_predictions (our signal vs outcome)
  - gate_audit (all 19 checkpoint decisions)
  - trades (if we traded: fill, P&L)
  - ticks_clob (CLOB prices during window)
  - window_snapshots (VPIN, delta, regime history)
  - Previous 3 windows (streak context)
  - macro_signals (OI, funding, liquidations)
    ↓
Sends to Claude Sonnet with structured prompt
    ↓
Generates evaluation card
    ↓
Sends to Telegram
```

### AI Evaluation Prompt (per window)

```
Analyse this 5-min BTC window:

WINDOW: {window_ts} | Oracle: {winner}
Signal: {direction} at VPIN {vpin} ({regime}) δ{delta}%
v2.2: P(UP)={prob} → {v2_dir} | Agrees: {agrees}

GATE AUDIT (19 checkpoints):
{checkpoint_table}

TRADE: {traded_or_skipped}
{if traded: Fill ${fill}, Shares {shares}, Outcome {outcome}, P&L ${pnl}}
{if skipped: Reason: {skip_reason}}

PRICES AT CLOSE:
Tiingo: ${tiingo_close} (predicted {tiingo_dir}) — {correct?}
Chainlink: ${chainlink_close} (predicted {chainlink_dir}) — {correct?}

CLOB BOOK AT T-70: UP ask ${up_ask}, DOWN ask ${dn_ask}
MACRO: OI ${oi}B, Funding ${funding}, L/S ${ls_ratio}

PREVIOUS 3 WINDOWS: {W/L/Skip, W/L/Skip, W/L/Skip}

Evaluate:
1. Was our signal correct? Why/why not?
2. Did our gates make the right decision?
3. If we traded: was entry timing optimal? Could we have gotten better price?
4. If we skipped: should we have traded? What would the P&L have been?
5. One actionable insight for improving the next window.
```

### Sample Output Card

```
🔬 Window 15:15 BTC — AI Evaluation

Oracle: DOWN ← Signal was UP (WRONG)
Tiingo predicted: UP (wrong) | Chainlink: DOWN (correct)
VPIN drifted 0.78→0.49 during window — regime collapsed

📊 Gate Analysis:
T-240→T-120: correctly blocked (not CASCADE)
T-100: v2.2 agreed at HIGH conf → TRADE fired
T-70: would have been blocked by v8.1.1 (VPIN 0.49)

💰 Trade: LOSS -$7.74 at $0.73 fill
If skipped: +$0 saved, signal was wrong

🎯 Insight: VPIN was CASCADE at T-100 (0.78) but collapsed
to NORMAL by T-60 (0.49). Late VPIN drop = reversal signal.
Consider: if VPIN drops >0.15 between entry and T-60, hedge or exit.

Accuracy: Chainlink 3/3 last 3 windows, Tiingo 1/3.
```

---

## 4. Enhanced Notification Flow

### Per Window (replaces current 3-4 cards with 1)

```
📊 15:15 BTC 5m — TRADED ⬆️ UP

Signal: VPIN 0.818 CASCADE | δ+0.063%  
v2.2: P(UP)=0.92 HIGH ✅ AGREE
Gate: v2.2_confirmed_T100 → cap $0.65

Order: GTC BUY YES limit $0.65
Fill: $0.65 × 10.2 shares ($6.63) ✅ filled in 5s
CLOB: ↑$0.63 ↓$0.37

🏦 Wallet: $99.94 → $93.31 (held)
⏳ Resolves ~15:20 UTC
```

### Resolution Card

```
❌ 15:15 BTC — LOSS (-$6.63)

Oracle: DOWN | Our UP was wrong
Fill: $0.65 × 10.2 = $6.63 → $0 payout

Tiingo said: UP (wrong) | Chainlink said: DOWN ✅
VPIN: 0.78 → 0.49 (collapsed during window)

🏦 Wallet: $93.31
📊 Today: 29W/15L (66%) | -$40.10 from start
🛡️ With all gates: would be 29W/7L (+$42)
```

### SITREP (every 15 min)

```
📋 SITREP 15:30 UTC
━━━━━━━━━━━━━━━━━━━━━━
🏦 $93.31 USDC (CLOB verified)
📈 P&L: -$37.51 from $130.82

📊 Today: 29W/15L (66% WR)
🛡️ Gates saved: 8 losses blocked ($82.45)

📝 Last 5:
✅⬆️ `15:18` $0.65 T100 +$3.57
❌⬆️ `14:39` $0.73 T60 -$7.74 (CASCADE legit)
❌⬇️ `13:10` $0.73 T70 -$10.95 🛡️ v8.1.1 blocks
✅⬆️ `12:33` $0.65 T110 +$4.89
❌⬆️ `11:23` $0.65 T100 -$9.26 ⚠️ NORMAL

📝 Last 3 skips:
🚫⬇️ `15:25` NORMAL VPIN 0.47 < 0.55 at T-70
🚫⬇️ `15:20` v2.2 disagrees (UP vs DOWN)

⏳ Pending: none
🔬 VPIN: 0.515 NORMAL | BTC $67,850
```

---

## 5. Implementation TODO

### Phase 2A — Data Infrastructure (Montreal engine)
- [ ] Create `window_predictions` table
- [ ] Capture Tiingo + Chainlink close prices at T-0 for each window
- [ ] Save predicted vs actual direction after oracle resolution
- [ ] Persist fill data across restarts (done ✅)
- [ ] Reconciliation service polls CLOB for order status (design doc ready)

### Phase 2B — AI Evaluator (macro-observer on Railway)
- [ ] Add `/evaluate-window` endpoint to macro-observer
- [ ] Queries all DB tables for window context
- [ ] Calls Claude Sonnet with structured prompt
- [ ] Sends evaluation card to Telegram
- [ ] Rate limit: 1 eval per 60s (avoid API cost blowout)

### Phase 2C — Notification Redesign (telegram.py)
- [ ] Kill `window_open` notification
- [ ] Merge trade_decision + fill + outcome into one card per window
- [ ] SITREP every 15 min (not 5)
- [ ] Add "gates saved" running counter
- [ ] Show Tiingo vs Chainlink predicted outcome

### Phase 2D — Stale Code Removal
- [ ] Remove `_execute_from_signal` (521 dead lines)
- [ ] Remove Gamma API fallback in GTC path
- [ ] Remove RFQ path (404s on every call, 2s wasted)
- [ ] Remove TWAP v1 / TimesFM v1 code
- [ ] Remove opinion_connected references

---

## 6. Questions for Billy

1. **NORMAL gate at T-80..T-110?** — The 11:18 loss was NORMAL at T-100. Extend gate?
2. **Bet fraction** — At $99 wallet, 7.3% = $7.22/trade. Want to reduce to 5% while testing?
3. **Macro-observer on Railway** — Current service just reads macro signals. OK to add the evaluator endpoint there? Or separate service?
4. **Notification timing** — Kill window_open entirely? And SITREP every 15 min OK?
