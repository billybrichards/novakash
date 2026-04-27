# Multi-Tier Exit Ladder + Signal-Flip Reversal — Design Doc

**Status:** DESIGN ONLY — no code, no deploy. Awaiting Billy review.
**Author:** Claude (research subagent)
**Date:** 2026-04-26
**Branch context:** feat/log-server (engine v9 + v10 LIVE — do not disturb)

---

## 0. Problem Statement

Current `PositionMonitor.evaluate_exit` (engine/execution/position_monitor.py:141-259)
only fires inside an 18-second window: `eval_offset ∈ [exit_eval_end_offset=30,
exit_eval_start_offset=48]`. Outside that window the function returns `None`.

Symptom (tonight, 2026-04-26):
- ~13 fills, -$117 PnL.
- Most losers crashed to ~0% mark **within 60s of fill** (so eval_offset ≈ T-240
  to T-180), while monitor was still gated off.
- The single observed exit fired at $0.22 / 40% of fill at T-44 — book had
  already collapsed; we were selling into thin air.

Root cause: **single narrow tier**, evaluated way too late. By T-48 the
opposite token has already rallied to 0.95+, our token bid is dust, and any
market-takers are gone.

Two design asks from Billy:

1. **Multi-tier exit ladder** — start watching the position much earlier with
   a *conservative* threshold and *more confirmation*, then progressively
   tighten + speed up as we approach close.
2. **Signal-flip reversal** — if the LGB head produces a strong opposite
   signal for N consecutive ticks, exit (and optionally re-enter the other
   side).

This doc specifies tier semantics, state machine, signal-flip logic,
threshold-validation methodology, safety nets, code surface, test list, and
open questions.

---

## 1. Tier Semantics

### 1.1 Schema (per tier)

```yaml
exit_tiers:
  - name: tier1_early
    start_offset: 200          # T-200 (eval_offset, seconds-before-close)
    end_offset:   120          # T-120
    mark_pct:     0.50         # exit if mark/fill < 0.50
    mark_ticks:   5            # 5 consecutive sub-threshold ticks (~10s @ 2s cadence)
    action:       exit_only    # exit_only | exit_and_emit | exit_and_reverse
  - name: tier2_mid
    start_offset: 120
    end_offset:   60
    mark_pct:     0.55
    mark_ticks:   4            # ~8s
    action:       exit_only
  - name: tier3_aggressive
    start_offset: 60
    end_offset:   30
    mark_pct:     0.70
    mark_ticks:   3            # ~6s
    action:       exit_only
# After T-30: no exits. Book too thin, accept settlement risk.
```

### 1.2 Why these starting values (not guesses)

These are **starting hypotheses** to be validated by §4 backtest. The
reasoning, not the numbers, is what matters:

| Tier | Window | mark_pct | mark_ticks | Rationale |
|------|--------|----------|------------|-----------|
| 1 | T-200..T-120 | **0.50** | **5** (~10s) | Far from close, plenty of book. A 50% mark drop this early means the directional thesis is *broken*, not noisy. Demand long confirmation (10s) because price moves can mean-revert with 3+ minutes left. |
| 2 | T-120..T-60 | **0.55** | **4** (~8s) | Halfway to settlement. Mean-reversion is less likely; thesis is genuinely deteriorating. Slightly looser threshold (0.55 vs 0.50) because we want to act before tier 3's thin book, but still demand confirmation. |
| 3 | T-60..T-30 | **0.70** | **3** (~6s) | Current production setting. Book begins to thin. Need fast trigger because remaining decay is rapid. |
| (none) | T-30..close | — | — | Below T-30 the bid side collapses; selling is value-destructive. Hold to settlement. |

**Why "0.50 → 0.55 → 0.70" and not flat or inverted:**
- Far from close, the *floor* of a position that will eventually win is
  higher (more time to recover) — so a drop *below* 0.50 is a stronger
  bear signal. Tight threshold = high specificity.
- Near close, the *floor* of an eventual winner is also higher (it's
  about to settle near 1.0). So 0.70 still indicates real damage. Loose
  threshold near close = high sensitivity, because we have less time.
- Flat thresholds (e.g. 0.45 everywhere) would either over-trigger early
  (premature exits) or under-trigger late (current behavior).

**Why mark_ticks decreases over time:** confirmation is a function of how
much *more* information will arrive. With T-180 remaining, we can afford 10s
of confirmation. With T-40 remaining, 10s is 25% of remaining life.

### 1.3 Alternative: continuous adaptive threshold

Instead of three discrete tiers, define a piecewise-linear function:

```
mark_pct(eval_offset) = 0.50 + (0.70 - 0.50) * (200 - eval_offset) / (200 - 30)
                      ≈ 0.50 at T-200, 0.59 at T-120, 0.65 at T-90, 0.70 at T-30
mark_ticks(eval_offset) = round(5 - 2 * (200 - eval_offset) / (200 - 30))
                        ≈ 5 at T-200, 4 at T-120, 3 at T-30
```

**Recommendation:** Start with **discrete tiers** (easier to debug, test,
and explain in alerts). Move to continuous in v2 if backtest shows tier
boundary effects (e.g. position survives tier 1 by 1 tick, then immediately
exits in tier 2 — wasted information).

---

## 2. State Machine

### 2.1 States

A `MonitoredPosition` has an implicit *tier state* derived from
`surface.eval_offset`:

```
pre_t200            eval_offset > tier1.start_offset
eval_t1             tier1.end_offset <= eval_offset <= tier1.start_offset
between_t120_t90    tier1.end_offset > eval_offset > tier2.start_offset    (gap if any)
eval_t2             tier2.end_offset <= eval_offset <= tier2.start_offset
between_t60_t48     tier2.end_offset > eval_offset > tier3.start_offset    (gap if any)
eval_t3             tier3.end_offset <= eval_offset <= tier3.start_offset
past_t30            eval_offset < tier3.end_offset
```

If tiers are contiguous (end_offset of N == start_offset of N+1), there are
no `between_*` gap states.

### 2.2 Transitions

Time-driven only. Each eval-loop tick (every ~2s), we recompute
`current_tier` from `surface.eval_offset`:

```python
def current_tier(eval_offset, tiers):
    for t in tiers:
        if t.end_offset <= eval_offset <= t.start_offset:
            return t
    return None  # in a gap or past_t30 or pre_t200
```

If `current_tier is None`, monitor returns `None` (no exit eval), same as
today's gate.

### 2.3 mark_loss_tick_count: reset or carry?

**Recommendation: RESET on tier transition.**

Reasoning:
- Each tier has a *different* `mark_pct` threshold. A count accumulated
  under tier1's 0.50 threshold isn't comparable to tier2's 0.55 — the
  position may currently be at mark=0.52 (failing tier2, passing tier1).
  Carrying the count would conflate semantics.
- Resetting also means `mark_ticks` confirmation is honored *within* each
  tier — the protective property we want.
- Risk of reset: a position that has been bleeding for 30 ticks across
  tier1 boundary into tier2 gets a fresh 4-tick reprieve. That's the
  *correct* behavior — we're explicitly saying "tier2 conditions need
  tier2 confirmation".

**Alternative considered:** carry the count, but multiply by a decay
factor (e.g. `count = count * 0.5` on transition). Rejected: adds
complexity for little benefit, hard to test deterministically.

### 2.4 Action types per tier

```yaml
action: exit_only          # close position, no further action (default)
action: exit_and_emit      # close position + emit a "tier_exit" signal to TG
action: exit_and_reverse   # close + open opposite-direction position (gated, see §3)
```

Tier 1 should default to `exit_only` (or `exit_and_emit` for visibility).
`exit_and_reverse` should never be tier-attached in v1 — it belongs to the
signal-flip path (§3) which has additional gating.

---

## 3. Signal-Flip Reversal

### 3.1 Detection

A "signal flip" occurs when the LGB head (v9/v10) emits a directional
signal *opposite* to the position's direction, with sufficient strength,
for N consecutive ticks.

```
Position direction: D ∈ {UP, DOWN}
Opposite: O = DOWN if D == UP else UP

Per tick, surface includes:
  surface.lgb_p_up                    (probability)
  surface.lgb_p_down  =  1 - lgb_p_up
  surface.lgb_dist    =  abs(lgb_p_up - 0.5) * 2   # signed strength

Flip detected if:
  surface.lgb_p_<O>     >= flip_p_threshold       # e.g. 0.85
  AND surface.lgb_dist  >= flip_dist_threshold    # e.g. 0.20
  for N = flip_consecutive_ticks (default 3)
```

State on `MonitoredPosition`:

```python
flip_consecutive_count: int = 0    # increments on flip-tick, resets on non-flip-tick
```

### 3.2 Action options

| Option | Behavior | Risk |
|--------|----------|------|
| **A: exit-only** | Close current position via SELL FAK. Do NOT re-enter. | Low. Equivalent to a stop-loss based on model belief rather than mark. |
| **B: exit + reverse** | Close current + open new position in opposite direction (BUY FAK on opposite token). | High: signal flicker → 2x fees + slippage. Violates "once-per-window-per-strategy" invariant. |

**Recommendation for v1:** **Option A (exit-only)**, behind feature flag
`enable_signal_reversal_exit` (default **off**). Reversal entry is a v2
feature behind a separate flag `enable_signal_reversal_reentry` (default
**off**, requires schema work — see §3.4).

### 3.3 Flag matrix

```yaml
enable_signal_reversal_exit:    false   # if true, signal flip → close position
enable_signal_reversal_reentry: false   # if true AND _exit is true, also re-enter opposite
flip_p_threshold:               0.85
flip_dist_threshold:            0.20
flip_consecutive_ticks:         3
flip_min_offset:                60      # don't flip below this offset (book too thin)
flip_max_offset:                240     # don't flip above this offset (signal noise high)
```

Both flags off → today's behavior (only mark-based exits).
Only `_exit` on → exit on flip, no re-entry.
Both on → exit + reverse (requires §3.4 schema change).

### 3.4 Schema constraint: once-per-window-per-strategy

The current `strategy_window_fills` table enforces a unique key on
`(strategy_id, window_ts)`. A reversal re-entry would violate this.

**Two paths forward (do NOT pick in v1):**

**Path X — extend the unique key to include a generation/leg counter:**
```sql
ALTER TABLE strategy_window_fills
  ADD COLUMN leg_seq SMALLINT NOT NULL DEFAULT 1;
DROP CONSTRAINT strategy_window_fills_strat_window_uniq;
ADD CONSTRAINT strategy_window_fills_strat_window_leg_uniq
  UNIQUE (strategy_id, window_ts, leg_seq);
```
Reversal inserts `leg_seq = 2`. PnL aggregation must group by
`(strategy_id, window_ts)` summing across legs.

**Path Y — emit reversal as a synthetic strategy_id:**
e.g. `v9_lgb_only_reversal`. No schema change, but pollutes strategy
registry and analytics.

**Recommendation:** prefer **Path X** when v2 lands. Don't touch the
schema for v1. If `enable_signal_reversal_reentry=true` is set in v1,
the code should hard-error at boot ("schema change required").

### 3.5 Order-of-operations on a flip

Within a single eval-loop tick:

```
1. evaluate_exit(tier-based)        → exit_reason A or None
2. evaluate_exit(signal-flip)       → exit_reason B or None
3. If A or B:
     execute_exit(reason = A or B, prefix with source)
4. If B AND enable_signal_reversal_reentry:
     defer to v2 (schema)
```

If both fire, prefer the tier reason in the alert (mark already moved —
"the model agrees" is gravy).

---

## 4. Threshold Validation Methodology

We **cannot** pick `mark_pct` and `mark_ticks` from one trade or one bad
night. Two complementary approaches:

### 4.1 Backtest harness (preferred — offline, fast)

**Data:**
- `ticks_clob` (or whichever table holds the per-2s CLOB bid/ask snapshots)
  for the last 2 weeks of resolved windows.
- `strategy_window_fills` for the same period — every fill we have
  ground truth on, with `entry_price`, `exit_price` (if any), and
  `outcome` (won / lost / pushed).

**Procedure (per candidate `(mark_pct, mark_ticks)` parameter set, per
tier):**

For each historical fill F:
1. Reconstruct the mark series M(t) from CLOB snapshots, t ∈ [F.fill_ts,
   F.window_ts + 300].
2. Walk forward, maintaining `mark_loss_tick_count` per tier rules.
3. Record: would the candidate parameters have triggered an exit at time
   t_exit? At what `mark_pct_at_exit` and what `eval_offset_at_exit`?
4. Compute counterfactual PnL:
   - **If we *would* have exited:** counterfactual PnL = (mark_at_exit -
     fill_price) × size − fees. (Best-effort; assumes the bid was
     fillable at mark — we can adjust by haircut.)
   - **If we *would not* have exited:** counterfactual PnL = actual PnL.

**Key metrics:**
- `loss_avoidance_rate` = % of *actual losers* where the candidate would
  have exited *and* the counterfactual PnL > actual PnL.
- `premature_exit_rate` = % of *actual winners* where the candidate would
  have exited prematurely (counterfactual PnL < actual PnL).
- `net_counterfactual_pnl` = sum of (counterfactual − actual) across all
  fills.

**Goals:**
- `premature_exit_rate < 10%`
- `loss_avoidance_rate > 50%` (of losers)
- `net_counterfactual_pnl > 0`

Run a parameter sweep:
```
mark_pct  ∈ {0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75}
mark_ticks ∈ {2, 3, 4, 5, 6, 7, 10}
tier_window ∈ {(200,120), (180,90), (150,60), (120,30)}  # for tier1 sweep
```

Pick the `(mark_pct, mark_ticks)` per tier that maximizes
`net_counterfactual_pnl` subject to `premature_exit_rate < 10%`.

**Caveat:** counterfactual fills assume liquidity at the observed bid.
For thin-book moments, multiply mark by a haircut (e.g. 0.95) before
computing PnL. Better: replay the actual L2 book if available.

### 4.2 Shadow-mode in production (slower, more honest)

Deploy with `exit_shadow_mode=true` for all tiers + signal flip for
**1 week**. Log every would-have-exited event with the same
counterfactual computation. Compare aggregate metrics to backtest.

If they agree (within ~10% on `loss_avoidance_rate`), promote to live.
If they disagree, investigate: book-replay assumption was wrong, or our
mark function is biased.

### 4.3 Recommendation

**Do both.** Backtest first to set candidate parameters; shadow for 1
week to validate; then promote.

If forced to pick one: **shadow 1 week** is more honest (no liquidity
assumptions), backtest 2 weeks is cheaper. Backtest's flaw is exactly
the failure mode that bit us tonight (assumed bid was fillable at
$0.22, wasn't).

---

## 5. Safety Nets

These apply to every tier and to the signal-flip path.

### 5.1 Stale mark feed

```
if (now - surface.last_clob_update_ts) > 5.0:
    log.warn("position_monitor.stale_mark_skip")
    return None  # don't increment mark_loss_tick_count
```

Without this, a 60s feed outage could fake-trigger every exit tier
sequentially and dump positions at zero.

### 5.2 No buy-side liquidity on opposite token (sell-side check)

Before placing the sell:
```
if pos.direction == "UP":
    sellable_bid = surface.clob_up_bid
else:
    sellable_bid = surface.clob_down_bid

if sellable_bid is None or sellable_bid < 0.02:
    log.warn("position_monitor.dead_book_skip", bid=sellable_bid)
    # DO NOT re-add position to monitor — it's already popped.
    # Record decision and alert.
    return False
```

Open question: should "dead book" *prevent* the pop in `execute_exit`?
If we pop and then can't sell, we lose monitoring. Recommend: check
**before** the pop in `execute_exit`, return False without popping if
book is dead. Then either (a) try again next tick, or (b) give up
after K dead-book attempts and pop with a "dead_book_abandon" reason.

### 5.3 Sell order failure (retry once)

Already implemented in `execute_exit` (`exit_max_retries=1`). Keep, but
on final failure:
- Re-add position to monitor (since we popped it on entry). **This is a
  bug fix** — current code pops and then loses the position if the sell
  fails.
- Alert TG: "sell failed after retries, manual intervention".

### 5.4 Reversal trade can't fill at sane price

```
expected_reversal_price = 1 - pos.fill_price + slippage_buffer
if reversal_quote.ask > expected_reversal_price * (1 + max_reversal_slip):
    log.warn("position_monitor.reversal_skip_price_too_far")
    # Already exited; just don't re-enter.
    return
```

`max_reversal_slip` default 0.05 (5%).

### 5.5 Window mismatch (already handled)

`evaluate_exit` already returns None if `surface.window_ts !=
pos.window_ts` (line 209-211). Keep.

### 5.6 Stale position cleanup (already handled)

330s grace already implemented (line 196-204). Keep.

---

## 6. Code Surface Estimate

### 6.1 Files touched

| File | Change | Est. lines |
|------|--------|------------|
| `engine/execution/position_monitor.py` | Extend `evaluate_exit` to accept `exit_tiers: list[dict]` and iterate, with reset-on-transition. Add signal-flip detector. Add stale-mark and dead-book guards. Fix sell-failure repop. | ~150 added, ~40 modified |
| `engine/strategies/registry.py` | Read tiers from YAML, pass through. Read flip flags. | ~40 added |
| `engine/strategies/configs/v9_lgb_only.yaml` | Add `exit_tiers:` block. Add flip flags (default off). | ~25 added |
| `engine/strategies/configs/v10_lgb_only.yaml` | Same. | ~25 added |
| `engine/persistence/decisions.py` (or equivalent) | Add `tier_name` and `flip_consecutive_count` columns to exit decision record (if those don't exist). | ~10 added |
| `engine/tests/test_position_monitor_tiers.py` (new) | Tier tests (see §7). | ~250 added |
| `engine/tests/test_position_monitor_flip.py` (new) | Flip tests. | ~150 added |
| `docs/DATA_FEEDS.md` or strategy YAML docs | Document new keys. | ~40 added |

**Total estimate:** ~700 lines added, ~40 modified, ~10 new test cases.
**No DB migration required for v1** (reversal re-entry would need one — v2).

### 6.2 Backwards compatibility

Existing keys (`exit_eval_start_offset`, `exit_eval_end_offset`,
`exit_mark_min_pct`, `exit_mark_ticks`) must continue to work. Strategy:

```python
def resolve_tiers(gate_params):
    if "exit_tiers" in gate_params:
        return parse_tiers(gate_params["exit_tiers"])
    # Legacy fallback: synthesize a single tier
    return [Tier(
        name="legacy",
        start_offset=gate_params.get("exit_eval_start_offset", 48),
        end_offset=gate_params.get("exit_eval_end_offset", 30),
        mark_pct=gate_params.get("exit_mark_min_pct", 0.45),
        mark_ticks=gate_params.get("exit_mark_ticks", 6),
        action="exit_only",
    )]
```

This means rolling out the code is a **no-op** until a strategy YAML
adds the `exit_tiers:` block. Safe to deploy under existing
`feat/log-server`.

### 6.3 Telemetry

Each tier eval logs:
- `position_monitor.tier_eval` { strategy_id, window_ts, tier_name,
  mark, mark_pct, mark_loss_tick_count, eval_offset }

Each flip eval logs:
- `position_monitor.flip_eval` { strategy_id, window_ts,
  lgb_p_opposite, lgb_dist, flip_consecutive_count }

Each exit logs:
- `position_monitor.exit` { ..., source: "tier1"|"tier2"|"tier3"|"flip",
  reason }

This is critical for §4.2 shadow-mode counterfactual analysis.

---

## 7. Test List (enumerated, not implemented)

### 7.1 Tier tests

1. **test_tier1_triggers_on_50pct_drop** — eval_offset=180, fill=$0.55,
   mark=$0.27 (49% of fill), 5 consecutive ticks → exit fires.
2. **test_tier1_does_not_trigger_at_55pct** — same, mark=$0.30 (54%) →
   no exit (just over threshold; but note 54%<55%, so should trigger
   *if* mark_pct=0.55. Specifying tier1's pct=0.50 here, so 54%>50%
   → no exit). Exact assertion: with `mark_pct=0.50`, mark=$0.305
   (55.5% of $0.55) does NOT trigger.
3. **test_tier_transition_resets_count** — count=4 in tier1, eval_offset
   crosses tier2 boundary, count resets to 0, mark still failing
   tier2 → need 4 more ticks to fire.
4. **test_tier_gap_no_eval** — eval_offset falls in a gap between
   tier-end and next-tier-start → returns None, count unchanged.
5. **test_past_t30_no_exit** — eval_offset=20, mark crashed to 0.10 →
   no exit (book too thin).
6. **test_pre_t200_no_exit** — eval_offset=250, mark=0.10 → no exit
   (no tier matches).
7. **test_legacy_yaml_synthesizes_single_tier** — YAML has no
   `exit_tiers:` but has legacy keys → behaves identically to
   pre-change code.
8. **test_stale_mark_feed_skips_eval** — `surface.last_clob_update_ts =
   now - 10s` → returns None, count unchanged.
9. **test_dead_book_does_not_sell** — `surface.clob_up_bid = 0.005` →
   `_place_sell_order` returns False without submitting.
10. **test_sell_failure_repops_position** — sell raises after retries
    → position is back in monitor, not lost.

### 7.2 Flip tests

11. **test_signal_flip_3_ticks_exits** — UP position, lgb_p_down=0.87,
    lgb_dist=0.22, 3 consecutive ticks, flag on → exit fires with
    reason "signal_flip".
12. **test_signal_flip_2_ticks_no_exit** — same, only 2 consecutive
    ticks → count=2, no exit.
13. **test_signal_flip_resets_on_non_flip_tick** — 2 flip ticks, then
    1 non-flip tick (lgb_p_down drops to 0.60), count resets to 0.
14. **test_reversal_disabled_by_default** — `enable_signal_reversal_exit
    = false`, all flip conditions met → no exit on flip.
15. **test_reversal_reentry_disabled_in_v1** — `enable_signal_reversal_
    reentry = true` set in YAML → engine refuses to start (schema
    not ready).
16. **test_flip_outside_offset_window_skipped** — eval_offset=20
    (past flip_min_offset=60) → no flip eval.

### 7.3 Metric / shadow-mode tests

17. **test_premature_exit_rate_metric** — fixture: 10 fills, 7 winners,
    of which 1 had a transient mark dip below tier1 threshold for 5
    ticks → premature_exit_rate computed = 1/7 ≈ 14.3%.
18. **test_loss_avoidance_rate_metric** — fixture: 10 fills, 6 losers,
    of which 4 would have exited at tier1 with positive
    counterfactual → 4/6 ≈ 66.7%.

---

## 8. Open Questions for Billy

These need a yes/no before implementation starts.

1. **Should reversal violate "once-per-window-per-strategy" in v1?**
   - **Recommendation: NO.** v1 is exit-only. v2 adds Path-X schema
     change for re-entry.
2. **Should multi-tier apply to v9_lgb_only, v10_lgb_only, or both?**
   - **Recommendation: roll out to v10 first** (newer, fewer in-flight
     fills), in shadow mode for a week. Then v9. This bounds blast
     radius.
3. **Should `mark_loss_tick_count` reset on tier transition or carry?**
   - **Recommendation: RESET.** See §2.3.
4. **Backtest 2 weeks vs shadow-mode 1 week for threshold validation?**
   - **Recommendation: BOTH, in this order** — backtest to set
     candidates, shadow for 1 week to validate, then promote.
5. **Discrete tiers vs continuous adaptive function?**
   - **Recommendation: discrete** for v1 (debuggable, alertable).
     Continuous in v2 if backtest shows boundary effects.
6. **Should tier 1 default action be `exit_only` or `exit_and_emit`?**
   - **Recommendation: `exit_and_emit`** so we get a TG ping when the
     earliest tier fires (rare, high-information event). Tiers 2/3 →
     `exit_only` (already covered by existing exit alerts).
7. **Sell failure: how many K dead-book attempts before giving up?**
   - **Recommendation: 3 ticks (~6s).** After that, alert and pop.
     Position settles to ground truth.
8. **Should the flip detector use `lgb_dist` or raw `lgb_p_opposite`?**
   - **Recommendation: BOTH** (AND condition). `lgb_p_opposite >= 0.85`
     is the strong-direction gate; `lgb_dist >= 0.20` filters noisy
     near-coin-flip ticks where the model is confused.
9. **Do we need a global kill switch for tiered exits?**
   - **Recommendation: YES** — a single `exit_tiers_enabled` master
     flag in trading-config (DB-overridable) so we can disable from
     Hub without engine restart. Defaults to false until shadow
     validation passes.

---

## 9. Rollout Plan (post-approval, separate doc/PR)

Out of scope for this design doc, but for completeness:

1. **PR #1:** Code change + tests + legacy fallback. Default `exit_tiers_
   enabled=false`. Merge to develop. CI green.
2. **PR #2:** YAML for v10 with tiers + `exit_shadow_mode=true` +
   `exit_tiers_enabled=true`. Deploy to Montreal.
3. **Week 1:** Shadow data collection. Counterfactual analysis script.
4. **PR #3 (only if data supports):** Flip `exit_shadow_mode=false` for
   v10. Continue v9 in legacy mode.
5. **Week 2:** Live v10 tiered exits. Monitor.
6. **PR #4:** Repeat for v9.
7. **Phase 2 (separate plan):** signal-flip exit (still no reentry).
8. **Phase 3 (separate plan):** schema change + reentry.

Each step has a kill switch and a clear go/no-go metric.

---

## 10. Non-Goals for v1

- Reversal re-entry (Phase 3)
- Continuous adaptive thresholds (v2)
- Per-asset / per-regime tier tuning (v2 — start with one set of
  numbers across all 4 assets and both strategies)
- ML-based exit policy (long-term)
- Partial exits (sell 50% at tier1, rest at tier2) — explicitly avoided
  to keep state machine simple

---

## 11. Appendix — Reference

- Current monitor: `engine/execution/position_monitor.py:141-259`
- Strategy invocation: `engine/strategies/registry.py:762-815`
- Existing YAML keys: `exit_monitor_enabled`, `exit_shadow_mode`,
  `exit_eval_start_offset` (default 48), `exit_eval_end_offset`
  (default 30), `exit_mark_min_pct` (default 0.45 in code, 0.70 in
  v10 YAML, 0.3145 in v9 override), `exit_mark_ticks` (default 6 in
  code, 3 in YAML), `exit_max_retries`, `exit_retry_timeout_seconds`
- Related Hub notes: #236 (exit spec), #298/#299 (audits), #240, #365
