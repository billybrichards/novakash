# Telegram Notification Overhaul — Proposal

**Date:** 2026-04-16
**Status:** Draft — PR-ready proposal
**Author:** Claude (for Billy)

## Problem

7 alert types, 100–200 msgs/hr, no hierarchy. Billy can't tell at a glance: today's P&L, effective balance, or which "LOSS" is fresh vs legacy orphan. Pending-redeem wins ($47 today) are invisible; wallet alone misreads as drawdown.

## Section 1 — Alert Tier System

### Tier 1 — TACTICAL (fire immediately, always audible)
| Event | Condition | Example prefix |
|---|---|---|
| `trade_fill` | New fill on live/paper trade | `⚡ TRADE` |
| `trade_resolved` | Fresh win/loss at T+300s (NOT orphan replay) | `✅ WIN` / `❌ LOSS` |
| `mode_switch` | Actual paper↔live flip, engine deploy | `🔧 MODE` |
| `kill_switch` | Drawdown kill, manual halt | `🛑 KILL` |
| `poly_sot_divergence` | Engine vs on-chain fill mismatch | `❗ POLY-SOT` |
| `regime_flip` | LOW↔HIGH↔CASCADE | `🌀 REGIME` |

Rule: a `❌` emoji may only appear for a fresh loss, never for orphan reconcile or pending redemption. Reconcile messages use `🗂 LEGACY` prefix instead.

### Tier 2 — HEARTBEAT (cadence-driven, quiet)
| Event | Cadence | Silent-when |
|---|---|---|
| `hourly_digest` | Top of every hour | Never — always fires (even on 0 trades) |
| `effective_balance` | Every 30 min | Wallet + pending unchanged by >$0.50 |

### Tier 3 — DIAGNOSTIC (silent unless exception)
| Event | Fires only when |
|---|---|
| `reconcile_pass` | `orphans_resolved > 0` OR `mismatch > 0` |
| `redemption_sweep` | `usdc_change > 0` OR `failed > 0` after 2nd retry |
| `window_snapshot` | **Dropped entirely.** Data moves to hourly digest. |
| `clob_reconciliation` | Divergence vs engine state only |

**Net volume target:** ~15–25 msgs/hr under normal trading (down 80%+).

---

## Section 2 — Hourly Digest Format

Fires at `:00` every hour. Single message, fixed-width:

```
📊 HOURLY DIGEST — 14:00–15:00 UTC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Trades:        4  (3W/1L, 75% WR)
Realized:      +$14.20
Unrealized:    +$47.65  (6 pending NegRisk)
Effective Δ:   +$61.85

Wallet:        $135.57
Pending:       +$47.65
Effective:     $183.22

Today (UTC):   +$59.30 (8W/3L, 73% WR, 11 trades)

Regime:        NORMAL   Consensus: STRONG-UP
Top skip:      spread_too_wide (×12)
Divergences:   0
Open:          0 positions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Data sources:
- Trades + WR: `trades` table, `created_at` ≥ hour start
- Pending: `positions` from Polymarket data-api (filter `redeemable=true`)
- Skip reason: `signal_evaluations.skip_reason` mode over hour
- Regime / consensus: latest `regime_snapshots`, `consensus_state`

---

## Section 3 — "Effective Balance" Line

Replaces current `CLOB RECONCILIATION` alert body.

**Source of pending:** `GET https://data-api.polymarket.com/positions?user=<addr>` → sum `size × curPrice` for rows where `redeemable=true` and `marketEndDate < now`. Already used by the redeemer; plumb into `ClobReconciler.get_effective_balance()` behind 60s cache.

**Format (both heartbeat and digest):**
```
Wallet:              $135.57 USDC
Pending redeem:      +$47.65  (6 wins awaiting NegRisk)
Effective balance:   $183.22

Today P&L:           +$11.65 realized + $47.65 unrealized = +$59.30
```

If `pending == 0`, collapse to single `Wallet: $X` line (no noise).

---

## Section 4 — Silent-Unless-Exception Rules

Drop these under normal conditions:
1. `window_snapshot` — **all of them.** Aggregate into hourly digest.
2. `reconcile_pass` when `resolved=0 AND mismatch=0` (noop passes).
3. `redemption_sweep` when `usdc_change=0 AND failed=0` (NegRisk auto-handled).
4. `mode_switch` when new mode == old mode (heartbeat echoes).
5. `clob_reconciliation` when engine wallet matches on-chain within $0.01.
6. `heartbeat` ping — kill entirely; digest covers liveness.

---

## Section 5 — Implementation Sketch

**Files touched:**
- `engine/alerts/telegram.py` — add tier routing + mute predicates
- `engine/alerts/digest.py` — **NEW** (~200 LOC): hourly aggregator, cron-triggered
- `engine/execution/clob_reconciler.py` — `get_effective_balance()` helper
- `engine/persistence/db.py` — digest queries (trades, skip reasons)
- `engine/config/constants.py` — tier cadence flags

**Env flags (gradual rollout):**
```
TG_TIER_HEARTBEAT_ENABLED=true
TG_TIER_DIAGNOSTIC_SILENT=true
TG_DROP_WINDOW_SNAPSHOTS=true
TG_HOURLY_DIGEST_ENABLED=true
TG_EFFECTIVE_BALANCE_ENABLED=true
```

Default all to `false` on merge; flip per-flag after 24h observation.

**Diff estimate:** ~350 LOC added, ~80 removed, 6 files.

**Migration:**
- Ship behind flags, all `false` → zero behavior change on deploy.
- Flip `TG_DROP_WINDOW_SNAPSHOTS` first (biggest noise win, lowest risk).
- Flip `TG_HOURLY_DIGEST_ENABLED` second, observe 2 cycles.
- Flip heartbeat/diagnostic mutes last.

**Rollback:** set all flags `false` — reverts to current firehose. No DB migrations, no breaking schema changes.

**Tests:**
- Unit: mute predicate truth table, digest SQL correctness, pending-redeem calc.
- Integration: dry-run 1h of prod events, assert ≤25 msgs emitted.
