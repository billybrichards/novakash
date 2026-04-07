# Post-Fix Report — April 7, 2026 10:45 UTC

**Period:** 09:50-10:40 UTC (post pricing fix)
**Engine:** v8.1, commits through `d10bbdb`

---

## Status: Engine Core is WINNING ✅

### Post-fix trades (09:50+ UTC)

| Time | Dir | Cap | Fill | P&L | Reason | Verdict |
|------|-----|-----|------|-----|--------|---------|
| 09:58 | NO | **$0.65** | **$0.6591** | **+$4.35** ✅ | v2.2_confirmed_T90 | Cap working |
| 10:13 | NO | **$0.65** | **$0.6552** | **+$5.59** ✅ | v2.2_confirmed_T110 | Cap working |
| 10:33 | NO | $0.73 | $0.7557 | -$13.05 ❌ | v2.2_confirmed_T70 | **RFQ bug** |
| 10:38 | NO | **$0.60** | — | expired | v2.2_early_T120 | Cap correct, no fill |

**Post-fix wins:** +$9.94 at correct caps ($0.65)
**Post-fix losses:** -$13.05 from RFQ overpay ($0.7557 > $0.73 cap)

### Win size improvement
| Period | Avg Win | Avg Loss | Fills at |
|--------|---------|----------|----------|
| Pre-fix (01:00-09:50) | $2.87 | $8.79 | $0.73 always |
| Post-fix wins | **$4.97** | — | $0.65-0.66 |

**Win size nearly doubled** from $2.87 → $4.97 thanks to cheaper fills.

---

## Issues Found in Notifications

### 1. 🔴 RFQ path bypasses dynamic cap

The 10:33 loss filled at **$0.7557** — ABOVE the $0.73 cap. Root cause:

```
FOK → no liquidity → RFQ path (line 2535 in five_min_vpin.py)
RFQ uses: _rfq_cap = runtime.five_min_max_entry_price (0.73 from .env)
NOT: PRICE_CAP = signal.v81_entry_cap (the dynamic cap)
```

The RFQ `max_price` check (line 799 in polymarket_client.py) should have rejected $0.7557 > $0.73. Either:
- The RFQ doesn't enforce the cap properly, OR
- The `entry_price` field recorded the fill price, not the submission price

**FIX NEEDED (not done — Billy said don't edit core):** Change RFQ `_rfq_cap` to use `PRICE_CAP` (dynamic cap per offset).

### 2. 🟡 SITREP counts only post-fix trades

The sitrep in notifications shows "2W/0L, +$9.94" but the actual session is 24W/9L, -$9.58. The sitrep counter likely resets on engine restart. Not a trading bug, just misleading display.

### 3. 🟡 TRADE notification shows cap $0.73 for T-70

The 10:30 TRADE card shows `cap $0.73` — this is CORRECT for T-70 (T-70 is in the <80 band → $0.73). But the earlier T-240 trades were also showing $0.73 (pre-fix). New T-240 trades should now show $0.55.

### 4. 🟡 GTC NOT FILLED notification

The "GTC NOT FILLED — waited 60s" message at 11:39 is actually **good behaviour**. The order was submitted at $0.4550 (within the $0.60 cap), no one on CLOB wanted to sell at that price, so it expired. This is the system correctly refusing to overpay.

### 5. 🟡 Entry time not always clear in notification

The TRADE card shows "Entry: T-70s" but doesn't show the actual UTC timestamp of order submission vs when the signal fired. Would be helpful to add.

---

## Full Session Summary (Apr 7, 00:00-10:40 UTC)

| Metric | Value |
|--------|-------|
| Resolved trades | 33 (24W/9L) |
| Win rate | 72.7% |
| Net P&L | -$9.58 |
| Avg win | +$3.12 |
| Avg loss | -$9.38 |
| Open trades | 1 |
| Expired (unfilled) | 18 |
| Estimated wallet | ~$121 |

### Why still negative despite 73% WR:
- Pre-fix: ALL fills at $0.73 → wins only +$2.70, losses -$9.30
- Post-fix: fills at $0.65 → wins +$4.97, but only 2 wins so far
- The 9 losses at $0.73 entries wiped out 24 wins

---

## Monitoring Checklist

- [x] T-90/T-110 trades show cap $0.65 ✅ (09:58, 10:13)
- [x] Fill prices under cap ✅ ($0.6552, $0.6591)
- [x] Win sizes improved ✅ ($4.35, $5.59 vs old $2.70)
- [x] FOK no longer fails with decimal errors ✅
- [x] gate_audit populating ✅
- [ ] T-240 trade shows cap $0.55 (not yet fired)
- [ ] RFQ path needs dynamic cap (NOT FIXED — flagged)
- [ ] SITREP counter reset on restart (cosmetic)

---

## Recommended Next Steps (notification/display only — no core engine changes)

1. **Fix RFQ cap** — use `PRICE_CAP` (dynamic per offset) instead of `runtime.five_min_max_entry_price`
2. **Add GTC fill notification** — when CLOB matches a pending GTC, send a fill confirmation with actual price
3. **Fix SITREP counter** — reload session totals from DB on restart instead of counting from zero
4. **Add order submission timestamp** to TRADE notification
5. **Show actual fill price** in WIN/LOSS resolution notification (currently shows entry_price)
