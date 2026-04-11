# Plan: v10.7 — Adverse Selection Mitigation + Robust Risk Management

## Context

Overnight Apr 9-10 exposed a fundamental execution problem: **adverse selection on GTC orders in choppy markets**.

### The Key Finding

```
Overnight Apr 9-10 (SEQUOIA running):
  Filled trades:      20W / 21L = 49% WR   (our orders that matched)
  Unfilled (expired): 44W / 10L = 81% WR   (GTCs that never filled)
  Overall eval WR:    86W / 38L = 69% WR
```

The model is correct (81% WR on all signals). Gates are correct. But **when our GTC fills in a choppy market, it's because a taker sweeps through our bid** — and that taker is often the informed side. We get picked off.

Compare overnight Apr 8-9 (trending market): 34W/0L = 100% WR. Same model, different regime. In trending markets, fills happen when price moves WITH us. In choppy markets, fills happen when price reverses INTO us.

### BTC Context

```
Overnight range: $71,540 - $72,881 ($1,341 range, $282 stddev)
Direction: CHOPPY, no clean trend
Result: 48.8% WR on 41 filled trades, -$39.36
```

Compared to Apr 8-9 overnight: clean trend, +$56.65, 100% WR.

### The 5 Systemic Issues From Apr 9 Full Day (still relevant)

1. **Kill switch never auto-recovers** — persistent until manual resume
2. **Peak tracking is stale** — computed from all-time peak, not session-relative
3. **No session awareness** — sizing is flat across all hours
4. **STARTING_BANKROLL static** — daily loss limit from stale baseline
5. **Consecutive loss cooldown too loose** (10)

### The NEW #1 issue: Execution mode not regime-aware

v10.6 (confidence-scaled pricing) makes the GTC cap LOWER for lower-confidence trades. In a choppy market, **lower bids are hit MORE adversely** (only informed sweeps reach them). v10.6 would make tonight worse, not better.

---

## Changes (v10.7)

### Change 1 (NEW): Regime-Aware Execution Mode

**File:** `engine/strategies/five_min_vpin.py` + `engine/execution/polymarket_client.py`

In choppy/reversal regimes, switch from GTC (maker) to FAK (taker):
- **GTC** = pay 0% fee, but adversely selected in choppy markets
- **FAK** = pay 7.2% fee, but fill only when book has genuine liquidity at your price

**Detection:** Use BTC 5-min volatility as a regime proxy:
```python
vol_ratio = price_range_5m / avg_price  # normalized range
if vol_ratio > V10_CHOPPY_VOL_THRESHOLD:
    execution_mode = "FAK"  # pay fee, avoid adverse selection
else:
    execution_mode = "GTC"  # maker, 0% fee
```

**Alternative (simpler):** Use VPIN as the regime gate. Low VPIN + no clear direction = choppy:
```python
if vpin < 0.5 and abs(delta_pct) < 0.05:
    execution_mode = "FAK"  # noisy market, pay for guaranteed fill
```

**Env vars:**
```
V10_EXECUTION_MODE=auto       # auto | gtc | fak
V10_CHOPPY_VOL_THRESHOLD=0.002  # 0.2% 5-min range triggers FAK
V10_FAK_VPIN_MAX=0.50           # low VPIN = choppy
```

### Change 2: Kill Switch Auto-Recovery (risk_manager.py)

Same as before — the kill switch is still broken and blocked 21 trades yesterday.

```python
# In __init__
self._kill_switch_triggered_at: Optional[datetime] = None
self._kill_auto_resume_minutes = int(os.environ.get("KILL_AUTO_RESUME_MINUTES", "30"))

# In is_killed property
if (self._kill_switch_active 
        and self._kill_auto_resume_minutes > 0
        and self._kill_switch_triggered_at
        and (datetime.utcnow() - self._kill_switch_triggered_at).total_seconds() > self._kill_auto_resume_minutes * 60):
    self._kill_switch_active = False
    self._kill_switch_triggered_at = None
    log.warning("risk.kill_auto_resumed")
```

### Change 3: Session Sizing — DROPPED

~~Session-aware sizing~~ — **Remove from plan.** Overnight Apr 9-10 broke the Asian = 100% WR assumption. Session sizing is curve-fit to one day of data. The real pattern is **volatility regime**, not clock time.

Replace with **volatility-aware sizing**:
```python
# High vol = reduce stake (choppy, unpredictable)
# Low vol trending = full stake
vol_pct = price_range_5m / avg_price
if vol_pct > 0.004:   # >0.4% chop
    stake_mult = 0.50
elif vol_pct > 0.002: # 0.2-0.4% normal
    stake_mult = 0.75
else:                 # <0.2% trending
    stake_mult = 1.0
```

### Change 4: Tighter Consecutive Loss Cooldown

Env only:
```
CONSECUTIVE_LOSS_COOLDOWN=3
COOLDOWN_SECONDS=600
```

Today's 01:xx hour had 1W/4L — a 3-loss cooldown would have paused after loss 3, saving at least one more loss.

### Change 5: Raise MAX_DRAWDOWN_KILL

```
MAX_DRAWDOWN_KILL=0.60
```

With auto-resume, a 60% kill threshold is a pause not a halt. Combined with volatility sizing, large drawdowns become less likely.

### Change 6: Hold Off on v10.6 Deployment

**v10.6 is on develop but DO NOT deploy yet.**

The confidence-scaled pricing was designed for trending markets where filled = winning. In choppy markets (like tonight), lower caps make adverse selection WORSE. Need regime-aware execution first.

---

## Updated Logic Flow

```
Signal eval passes all gates
       ↓
Check volatility regime
  vol_pct < 0.2%:  TRENDING  → GTC maker, v10.6 cap scaling, full stake
  vol_pct 0.2-0.4%: NORMAL   → GTC maker, v10.6 cap scaling, 0.75x stake
  vol_pct > 0.4%:  CHOPPY    → FAK taker at cap, 0.5x stake
       ↓
Risk manager check
  kill_switch active (auto-resumes after 30min)
  consecutive_losses (paused at 3, 10min)
  daily_loss_limit (from live wallet baseline)
       ↓
Place order (GTC or FAK based on regime)
```

---

## Implementation Files

| Change | File | Lines |
|--------|------|-------|
| Volatility regime detection | `engine/signals/regime_classifier.py` (existing) — extend | ~15 |
| Regime-aware execution | `engine/strategies/five_min_vpin.py` | ~30 |
| Stake multiplier from vol | `engine/strategies/five_min_vpin.py` | ~10 |
| Kill switch auto-recover | `engine/execution/risk_manager.py` | ~20 |
| Env config | `engine/.env.local` | Update |

---

## Priority Order

1. **Kill switch auto-resume** (Change 2) — deploy immediately, zero code risk, fixes the biggest problem (blocked 91% WR trades)
2. **Volatility-aware sizing** (Change 3 replacement) — 1 day of code, would have saved ~$20 overnight
3. **Regime-aware execution** (Change 1) — 2-3 days of careful work, fixes adverse selection
4. **v10.6 deployment** — hold until regime detection is working

---

## Verification

1. **Kill auto-resume:** Trigger kill, wait 30 min, verify `risk.kill_auto_resumed`
2. **Vol sizing:** Check stake logs during high-vol periods — should be 50% of baseline
3. **Execution mode:** Check logs — FAK during choppy hours, GTC during trending
4. **Adverse selection test:** After deploy, measure filled WR vs signal WR. Target: filled WR should approach signal WR (currently 49% filled vs 81% signal).

## Rollback

All env-driven:
- `V10_EXECUTION_MODE=gtc` — force maker always
- `V10_CHOPPY_VOL_THRESHOLD=10.0` — effectively disable choppy detection
- `KILL_AUTO_RESUME_MINUTES=0` — disable auto-recovery

---

## Key Insight for v10.7

**The model is not broken. The execution strategy is broken for choppy markets.**

- Signal accuracy: 69-81% across 124 evaluations overnight
- Filled trade WR: 49% — adversely selected
- The difference (40 percentage points) is pure execution loss

The solution isn't a better model or tighter gates — it's **fill quality management**. Either pay for the fill (FAK) or don't trade in regimes where GTCs get picked off.
