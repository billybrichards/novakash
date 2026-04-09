# Plan: v10.7 — Robust Risk Management + Session-Aware Config

## Context

Apr 9 exposed five systemic coordination failures in the risk management system:

1. **Kill switch never recovers** — Triggered at 19:38 UTC (81.7% drawdown from $73.56 peak). Wallet recovered to $63.32 via position resolutions, but engine stayed killed. Blocked 21 would-be trades that were **19W/2L = 90.5% WR** — ~$30 in lost profit.

2. **Peak tracking is stale** — Peak was $73.56 (from overnight). The engine computes `drawdown = 1 - current/peak`. When wallet drops to $13 then recovers to $63, drawdown is still 14% from peak — but the *real* operational context has changed. On restart, peak resets to STARTING_BANKROLL, creating discontinuity.

3. **No session awareness** — The risk manager treats 03:00 UTC (100% WR, 34W/0L) and 17:00 UTC (44% WR, 4W/5L) identically. Stakes should scale with session quality.

4. **STARTING_BANKROLL stale** — Set to $63 in env, never auto-updates. Used for daily loss limits. After wallet doubles overnight ($122), the daily loss limit is still based on $63.

5. **Consecutive loss cooldown too loose** — Set to 10, the evening had 4 losses in a row and kept trading. Should be tighter during dangerous sessions.

**Full day (65 trades):**
```
SESSION      W/L    WR%    PnL       Avg Loss Stake
ASIAN        34/0   100%   +$56.65   n/a
LONDON        8/9    47%   -$39.16   $6.76
US_OPEN       4/1    80%   +$3.00    $3.40
EVENING       4/5    44%   -$10.58   $3.39
BLOCKED(kill) 19/2   91%   +$0 (missed ~$30)
```

---

## Changes

### Change 1: Kill Switch Auto-Recovery (risk_manager.py)

**Problem:** `_kill_switch_active = True` persists forever once set. The `is_killed` property has a dual check (`_kill_switch_active OR drawdown >= threshold`), but the drawdown-triggered kill also sets the flag — so even when drawdown recovers, the flag stays True.

**Fix:** After a configurable cooldown period, auto-clear `_kill_switch_active` and let the drawdown check gate naturally. The kill switch becomes a "pause" not a "halt".

**File:** `engine/execution/risk_manager.py`

In `__init__` (after line 52):
```python
self._kill_switch_triggered_at: Optional[datetime] = None
self._kill_auto_resume_minutes = int(os.environ.get("KILL_AUTO_RESUME_MINUTES", "0"))  # 0 = disabled
```

In `record_outcome` (line 135-137, where kill triggers):
```python
if self._drawdown_pct >= runtime.max_drawdown_kill:
    self._kill_switch_active = True
    self._kill_switch_triggered_at = datetime.utcnow()
    log.critical("risk.drawdown_kill", drawdown=f"{self._drawdown_pct:.1%}")
```

In `is_killed` property (lines 182-184) — add auto-resume check:
```python
@property
def is_killed(self) -> bool:
    # Auto-resume after cooldown
    if (self._kill_switch_active 
            and self._kill_auto_resume_minutes > 0
            and self._kill_switch_triggered_at
            and (datetime.utcnow() - self._kill_switch_triggered_at).total_seconds() > self._kill_auto_resume_minutes * 60):
        self._kill_switch_active = False
        self._kill_switch_triggered_at = None
        log.warning("risk.kill_auto_resumed", minutes=self._kill_auto_resume_minutes)
    
    return self._kill_switch_active or self._drawdown_pct >= runtime.max_drawdown_kill
```

**Env var:** `KILL_AUTO_RESUME_MINUTES=30` — after 30 min cooldown, kill switch clears. The drawdown check still gates trading if bankroll hasn't recovered, but once wallet is topped up or positions resolve, trading resumes.

### Change 2: Session-Aware Stake Sizing (five_min_vpin.py)

**Problem:** Stakes are flat regardless of session quality. London bleeds at $6-8 stakes.

**Fix:** Apply a session multiplier to the base stake calculation. The multiplier is read from env vars so it's tunable without code changes.

**File:** `engine/strategies/five_min_vpin.py` — in the stake calculation section (around line 2370-2400 where `stake` is computed)

```python
# Session-aware sizing (v10.7)
_session_sizing = os.environ.get("V10_SESSION_SIZING_ENABLED", "false").lower() == "true"
if _session_sizing:
    _hour = datetime.utcnow().hour
    if 0 <= _hour <= 8:
        _s_mult = float(os.environ.get("V10_SESSION_ASIAN_MULT", "1.0"))
    elif 9 <= _hour <= 12:
        _s_mult = float(os.environ.get("V10_SESSION_LONDON_MULT", "0.50"))
    elif 13 <= _hour <= 16:
        _s_mult = float(os.environ.get("V10_SESSION_US_MULT", "1.0"))
    else:
        _s_mult = float(os.environ.get("V10_SESSION_EVENING_MULT", "0.50"))
    stake = stake * _s_mult
```

**Env vars:**
```
V10_SESSION_SIZING_ENABLED=true
V10_SESSION_ASIAN_MULT=1.0
V10_SESSION_LONDON_MULT=0.50
V10_SESSION_US_MULT=1.0
V10_SESSION_EVENING_MULT=0.50
```

**Impact on today:** London 9 losses avg $6.76 → at 50% = $3.38 → saves $30.42. Evening 5 losses avg $3.39 → at 50% = $1.70 → saves $8.48. Total saved: ~$39.

### Change 3: Tighter Consecutive Loss Cooldown

**Problem:** `CONSECUTIVE_LOSS_COOLDOWN=10` is too loose. Evening had 4 losses in a row before any cooldown.

**Fix:** Env change only — lower to 3 (which is already the default in constants.py but overridden in .env to 10).

```
CONSECUTIVE_LOSS_COOLDOWN=3
COOLDOWN_SECONDS=600   # 10 min (was 300)
```

At 3 consecutive losses with 10 min cooldown, the evening's 4-loss streak would have been interrupted after 3 losses (saving ~$3.40 on the 4th).

### Change 4: Dynamic STARTING_BANKROLL Sync

**Problem:** STARTING_BANKROLL is static. Daily loss limit is computed as `day_start_bankroll × 10%`. When wallet doubles overnight, the limit doesn't adjust.

**Fix:** In the daily reset logic (risk_manager.py line 252-259), sync `_day_start_bankroll` from the current wallet balance instead of the stale `_starting_bankroll`.

**File:** `engine/execution/risk_manager.py` — in the daily reset section:

```python
async def _daily_reset(self) -> None:
    today = datetime.utcnow().date()
    if today > self._daily_reset_date:
        self._daily_reset_date = today
        self._day_start_bankroll = self._current_bankroll  # Use live balance, not stale starting
        self._daily_pnl = 0.0
        log.info("risk.daily_reset", day_start=f"${self._day_start_bankroll:.2f}")
```

This is already partially implemented — need to verify it uses `_current_bankroll` not `_starting_bankroll`.

### Change 5: Raise MAX_DRAWDOWN_KILL

**Problem:** 45% drawdown kill is too aggressive for a volatile session-dependent strategy. A normal London session can draw down 40% from the overnight peak.

**Fix:** Env change:
```
MAX_DRAWDOWN_KILL=0.60   # was 0.45
```

Combined with auto-resume (Change 1), this means:
- Kill triggers at 60% drawdown (more tolerant of session volatility)
- Auto-resumes after 30 minutes even if still above threshold
- Session sizing (Change 2) prevents large losses during dangerous sessions

---

## Complete Env Config (v10.7)

```bash
# === SEQUOIA + v10.6 (already on develop) ===
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.60
V10_MIN_EVAL_OFFSET=200

# Regime thresholds (SEQUOIA calibrated)
V10_TRANSITION_MIN_P=0.70
V10_CASCADE_MIN_P=0.67
V10_NORMAL_MIN_P=0.60
V10_LOW_VOL_MIN_P=0.60
V10_TRENDING_MIN_P=0.67
V10_CALM_MIN_P=0.72

# Offset penalty (two-tier)
V10_OFFSET_PENALTY_MAX=0.04
V10_OFFSET_PENALTY_EARLY_MAX=0.04
V10_DOWN_PENALTY=0.03

# Confidence-scaled cap (v10.6)
V10_CAP_SCALE_BASE=0.48
V10_CAP_SCALE_CEILING=0.72
V10_CAP_SCALE_MIN_CONF=0.65
V10_CAP_SCALE_MAX_CONF=0.88
V10_DUNE_CAP_FLOOR=0.35

# Early entry zone (T-180..200)
V10_EARLY_ENTRY_MIN_CONF=0.90
V10_EARLY_ENTRY_CAP_MAX=0.63
V10_EARLY_ENTRY_OFFSET=180

# === v10.7 NEW ===
# Session-aware sizing
V10_SESSION_SIZING_ENABLED=true
V10_SESSION_ASIAN_MULT=1.0
V10_SESSION_LONDON_MULT=0.50
V10_SESSION_US_MULT=1.0
V10_SESSION_EVENING_MULT=0.50

# Risk management
MAX_DRAWDOWN_KILL=0.60
CONSECUTIVE_LOSS_COOLDOWN=3
COOLDOWN_SECONDS=600
KILL_AUTO_RESUME_MINUTES=30

# Sizing
BET_FRACTION=0.050
ABSOLUTE_MAX_BET=6.0
STARTING_BANKROLL=100

# Delta gate (v10.5)
V10_MIN_DELTA_PCT=0.005
V10_TRANSITION_MIN_DELTA=0.010
```

---

## Files to Modify

| File | Change | Lines |
|------|--------|-------|
| `engine/execution/risk_manager.py` | Kill auto-resume + daily reset fix | ~20 lines |
| `engine/strategies/five_min_vpin.py` | Session-aware sizing | ~15 lines |
| `engine/.env.local` | Full v10.7 config reference | Update existing |

---

## Verification

1. **Kill auto-resume:** After deploy, if kill switch fires, wait 30 min → verify `risk.kill_auto_resumed` in logs → trading resumes
2. **Session sizing:** Check logs during London (09-12 UTC) → stakes should be ~50% of Asian stakes
3. **Consecutive cooldown:** Trigger 3 losses → verify 10-min pause in logs
4. **Cap scaling:** Check `cap.scaled` log entries → verify cap varies with confidence (not flat $0.68)
5. **Daily loss limit:** After midnight UTC reset → verify `risk.daily_reset` with live balance, not stale $63

## Rollback

All changes are env-var controlled:
- Session sizing: `V10_SESSION_SIZING_ENABLED=false`
- Kill auto-resume: `KILL_AUTO_RESUME_MINUTES=0`
- Old kill threshold: `MAX_DRAWDOWN_KILL=0.45`
- Old cooldown: `CONSECUTIVE_LOSS_COOLDOWN=10`
