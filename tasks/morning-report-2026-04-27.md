# Morning Report — 2026-04-27 09:00–14:30 UTC

**Engine PID:** 1146590 (LIVE since 05:22 UTC, false-positive DOWN alerts ignored — process never died)
**v9_lgb_only:** LIVE
**v10_lgb_only:** LIVE
**Multi-tier exit monitor (PR #402):** ACTIVE on both — *currently bleeding money*

---

## TL;DR

| Metric | Value |
|--------|-------|
| Trades since 09:00 UTC | 38 (21 v9, 17 v10) |
| DB nominal PnL | +$97.04 |
| Wallet truth net | **−$22 since morning** ($269 → $246 effective) |
| Gap (unaccounted) | **−$119** |
| Exit-monitor measured cost | −$50 in 6h (4 winners gave away, 4 nominal saves) |
| Unredeemed wins | 2 positions, $28.49 |

**Honest answer: yes, exit monitor IS killing us.** Saves $0.13/loss but gives away $12-17/winner. 50% hit rate × asymmetric outcomes = net −$50 over 6h.

---

## Balance reconciliation

```
Morning (start of day):     ~$269 cash
Now (14:30 UTC):
  Cash USDC:                $218.68
  Pending wins (unrealised): +$27.95
  Effective balance:        $246.63

Net delta:                  −$22.37
DB nominal pnl:             +$97.04
Gap (where the money went): −$119.41
```

Where the gap goes:
- **Exit-monitor leak**: −$50 (8 sells, 4 winners nuked, 4 nominal saves)
- **Polymarket fees** (7.2% per win × winners): ~−$25–35
- **Orphan trades / non-redeemed residuals**: ~−$20-30
- **Pending wins not yet credited**: $27.95 will land

User's intuition "should be $310" assumed DB PnL was real. It isn't. DB doesn't capture:
- Sell-side losses from FAK exits (huge)
- Polymarket platform fees
- Orphan trades that miss the trade table

---

## Trades since 09:00 UTC (38 total)

### v9_lgb_only — 13W / 3L / 5P (W:L = 81% if all P resolves W; ≥81% WR)
**DB PnL: +$94.23**

```
09:37  NO    fill$0.40  WIN  +$10.37
09:48  NO    fill$0.78  WIN  +$ 2.12
10:22  NO    fill$0.68  WIN  +$14.27
10:32  YES   fill$0.75  WIN  +$13.62
10:47  NO    fill$0.80  WIN  +$ 1.88
10:54  NO    fill$0.57  PEND
11:03  YES   fill$0.75  LOSS −$ 7.50  ← exit-monitor SOLD (one of 4 nominal saves)
11:07  NO    fill$0.64  WIN  +$ 4.22
11:47  NO    fill$0.57  WIN  +$ 5.66
11:57  YES   fill$0.79  WIN  +$14.10
12:07  YES   fill$0.73  WIN  +$11.74
12:17  YES   fill$0.47  LOSS −$ 7.50
12:48  NO    fill$0.60  PEND   ← exit-monitor SOLD this (gave away winner)
12:52  YES   fill$0.43  PEND   ← exit-monitor SOLD this (nominal save)
12:57  NO    fill$0.75  WIN  +$ 2.50
13:02  YES   fill$0.62  LOSS −$ 7.50
13:42  YES   fill$0.76  WIN  +$ 2.37
14:02  YES   fill$0.48  PEND   ← exit-monitor SOLD this (nominal save)
14:17  YES   fill$0.60  WIN  +$16.53
14:34  YES   fill$0.73  PEND
```

### v10_lgb_only — 7W / 5L / 5P (54% WR if pending resolves split)
**DB PnL: +$2.81**

```
09:07  YES   fill$0.57  LOSS −$ 7.50
09:33  YES   fill$0.79  WIN  +$ 1.99
09:37  YES   fill$0.54  PEND   ← exit-monitor SOLD this (gave away winner)
10:13  YES   fill$0.71  PEND   ← exit-monitor SOLD this (nominal save)
10:32  YES   fill$0.35  WIN  +$13.93
10:53  YES   fill$0.70  PEND
10:58  YES   fill$0.70  LOSS −$ 7.50
11:03  YES   fill$0.77  LOSS −$ 7.50
11:57  YES   fill$0.59  WIN  +$ 5.21
12:07  YES   fill$0.80  WIN  +$ 1.88
12:52  YES   fill$0.31  LOSS −$ 7.50
13:57  YES   fill$0.59  WIN  +$ 5.21
14:02  YES   fill$0.45  LOSS −$ 7.50
14:17  YES   fill$0.61  WIN  +$ 4.88
14:28  YES   fill$0.42  PEND   ← exit-monitor SOLD this (gave away winner)
14:32  YES   fill$0.40  PEND
```

---

## Exit-monitor analysis (8 sells since 09:00)

All sells fired at $0.01 limit (book has no real bid liquidity above $0.01 stub).

| Time | Strategy | Side | Fill | Outcome | Verdict | $ Impact |
|------|----------|------|------|---------|---------|----------|
| 09:38 | v10 | UP | $0.54 | won | **GAVE AWAY** | −$12.87 |
| 10:13 | v10 | UP | $0.71 | lost | saved | +$0.10 |
| 10:33 | v9  | UP | $0.75 | won | **GAVE AWAY** | −$8.91 |
| 11:04 | v9  | UP | $0.75 | lost | saved | +$0.09 |
| 12:49 | v9  | DOWN | $0.60 | won | **GAVE AWAY** | −$11.88 |
| 12:53 | v9  | UP | $0.43 | lost | saved | +$0.17 |
| 14:03 | v9  | UP | $0.48 | lost | saved | +$0.15 |
| 14:28 | v10 | UP | $0.42 | won | **GAVE AWAY** | −$16.83 |

**Net: −$49.98** over 6h. Run rate **−$200/day** at this pace.

### Why "saves" are nominal

When mark crashes and we'd lose anyway: held loses $7.47 (full stake). FAK-sold at $0.01 stub returns $0.13 (sold portion) + $0 (residual settled). Difference = **$0.13 saved**. Trivial.

When mark dips on a recoverable position: held wins $7-17 (settlement payout × shares). FAK-sold at $0.01 returns $0.13. Difference = **$8-17 lost**. Big.

**Asymmetric:** save~$0.15, miss~$12. Need >99% accuracy to break even. Current: 50%.

---

## v9 vs v10 strategy verdict

| Metric (since 09:00 UTC) | v9 | v10 |
|--------------------------|-----|-----|
| Trades | 21 | 17 |
| Direction bias | mixed | **heavy YES (15/17 UP)** |
| WR (resolved only) | 13/16 = **81%** | 7/12 = **58%** |
| DB PnL | +$94.23 | +$2.81 |
| Real PnL after exits | ~+$60 | ~−$10 |

**v9 is the edge. v10 is borderline-flat.** v10 is dragging down combined performance.

---

## Unredeemed wins (now)

| CID | Side | CV | Cost | Window |
|-----|------|-----|------|--------|
| `0xcacfcb34` | Up | $27.95 | $14.42 | Apr 27 10:30 ET ← v10 dual-fill (cost shows $14.42=2× = v9+v10 same window) |
| `0x336b42fc` | Down | $0.54 | $0.34 | Apr 26 22:40 ET (residual from earlier) |

**Total: $28.49 sitting unredeemed.**

---

## Suggestions to raise effective WR / cash performance

### Tier 1 — Stop the bleed (PR-ready, 1-line YAML each)

1. **Disable exit monitor entirely** — `exit_monitor_enabled: false` on v9 + v10.
   - Saves ~$200/day vs current
   - Reverts to "hold to settlement"
   - **Net: huge positive, immediate**

2. **Ghost v10** — `mode: GHOST` in v10_lgb_only.yaml.
   - v10 at 58% WR is below break-even (~70% needed)
   - Probably saves $20-50/day
   - Lets us investigate v10 weakness without bleed

### Tier 2 — Strategy quality (data-driven from v10 YAML comments)

3. **Tighten v10 LGB thresholds**:
   - Current: `lgb_dist_min_up: 0.13` (80.1% acc per YAML data)
   - Bump to: `lgb_dist_min_up: 0.20` (86.5% acc)
   - Trade frequency drops, WR rises
   - Already-validated thresholds, just being conservative

4. **v10 UP-only bias is a problem** — 15/17 v10 trades today were UP. Looking at v10 YAML comments: "DOWN acc=82.9% at 0.10, UP acc=80.1% at 0.13" — DOWN is more reliably accurate. v10's UP-bias may be statistical artifact; force more balance:
   - Add per-direction sizing weights, OR
   - Tighten UP threshold harder than DOWN

### Tier 3 — Real exit mechanism (design + impl needed)

5. **Replace FAK-sell with CTF mergePositions**:
   - Detect: mark crash + LGB flip strong (`lgb_p_opp ≥ 0.85, dist ≥ 0.20`)
   - Action: buy opposite token at ask (real liquidity exists on ask side)
   - Merge YES + NO via `CTF.mergePositions(amount)` → guaranteed $1 collateral
   - Math: net per share = `1 - fill_price - opposite_ask`. If opposite_ask < (1 − fill) we profit guaranteed.
   - Bypasses bid-stub problem entirely
   - **This is what the user has been asking about — Polymarket-native primitive**

### Tier 4 — Investigation (ongoing)

6. **Orphan trade reconciler** (PR #403 already open) — backfills missing trade rows from data-api so DB matches reality.
7. **TG card claim audit** — engine-internal counter said v10 36W/12L 75% +$73.77 vs DB reality 7W/5L 58%. Counter is buggy. Separate fix.

---

## Recommended action order (subject to ur approval)

1. **NOW**: PR (a) `exit_monitor_enabled: false` on v9 + v10. Stops bleed. Deploy on green-light.
2. **NOW**: PR (b) ghost v10 (`mode: GHOST`). Investigate while not losing money.
3. **NEXT 1h**: Redeem the $28.49 unredeemed (`scripts/ops/onchain_redeem.py`).
4. **TODAY**: Spawn bg agent to design + impl `mergePositions` exit (replaces FAK-sell).
5. **THIS WEEK**: Run orphan reconciler (PR #403) to backfill DB.
6. **NEXT WEEK**: Investigate v10 UP-bias root cause + tighten thresholds.

---

## Key files referenced

- Engine log: `/home/novakash/engine.log.20260427_052221`
- v9 YAML: `engine/strategies/configs/v9_lgb_only.yaml`
- v10 YAML: `engine/strategies/configs/v10_lgb_only.yaml`
- Multi-tier impl: `engine/execution/position_monitor.py`
- Backtest doc: PR #404
- Orphan reconciler: PR #403

— Generated 2026-04-27 ~14:40 UTC
