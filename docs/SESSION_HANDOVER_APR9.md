# Session Handover — Apr 9, 2026

**From:** Claude Opus 4.6 (session Apr 8-9, ~20 hours)
**To:** Next agent session
**Engine version:** v10.4 Option F
**Wallet:** ~$75-115 USDC (fluctuating)

---

## What This System Does

Novakash is a BTC 5-minute prediction market trading system on Polymarket. Every 5 minutes, Polymarket opens a market: "Will BTC go UP or DOWN in the next 5 minutes?" The engine evaluates signals, places FAK/GTC orders, and resolves wins/losses.

## Current Architecture

```
ELM v3 Model (http://3.98.114.0:8080/v2/probability)
  → 7-gate pipeline (gates.py)
  → FAK price ladder (fok_ladder.py)
  → GTC fallback (polymarket_client.py)
  → CLOB Reconciler (reconciler.py) detects outcomes
  → trade_bible (DB trigger auto-populates on trades.outcome update)
```

## The 7-Gate Pipeline (engine/signals/gates.py)

```
G1: Source Agreement — CL+TI must agree on direction (94.7% WR when agree)
G2: Taker Flow Gate — hard block when taker+smart money both oppose
G3: CG Confirmation — 2+ CG signals = -0.01 bonus, 0 signals = +0.02 penalty
G4: DUNE Confidence — regime-specific threshold + offset penalty + DOWN penalty
G5: Spread Gate — block if Polymarket spread > 8%
G6: Dynamic Cap — cap = dune_p - 0.05, bounded [$0.35, $0.68]
```

## Current Production Config (Montreal engine/.env)

```env
V10_DUNE_ENABLED=true
V10_DUNE_MODEL=oak
V10_DUNE_MIN_P=0.65
V10_MIN_EVAL_OFFSET=180

# Regime thresholds
V10_TRANSITION_MIN_P=0.75
V10_CASCADE_MIN_P=0.80
V10_NORMAL_MIN_P=0.65
V10_LOW_VOL_MIN_P=0.65
V10_TRENDING_MIN_P=0.72
V10_CALM_MIN_P=0.72

# v10.4 Option F
V10_NORMAL_MIN_OFFSET=100
V10_TRANSITION_MAX_DOWN_OFFSET=140
V10_OFFSET_PENALTY_MAX=0.06
V10_DOWN_PENALTY=0.05

# CG gates (dampened)
V10_CG_TAKER_GATE=true
V10_CG_TAKER_ALIGNED_BONUS=0.01
V10_CG_CONFIRM_BONUS=0.01
V10_CG_TAKER_OPPOSING_PENALTY=0.05
V10_CG_TAKER_OPPOSING_PCT=55
V10_CG_SMART_OPPOSING_PCT=52

# Cap + sizing
V10_DUNE_CAP_CEILING=0.68
V10_DUNE_CAP_FLOOR=0.35
V10_DUNE_CAP_MARGIN=0.05
BET_FRACTION=0.075
ABSOLUTE_MAX_BET=10.0
STARTING_BANKROLL=115
FIVE_MIN_EVAL_INTERVAL=2
```

**IMPORTANT:** Engine reads `engine/.env` (gitignored, only on Montreal). `engine/.env.local` is a committed REFERENCE copy. They must stay in sync manually.

## Database — Source of Truth Hierarchy

| Table | Purpose | Trust Level |
|-------|---------|-------------|
| **Polymarket wallet** | Real USDC balance | GROUND TRUTH |
| **trade_bible** | Resolved trade outcomes | Primary DB source (auto-populated by trigger) |
| **trades** | All placed trades + metadata | Must sync with trade_bible |
| **signal_evaluations** | Every 2s eval decision | Best for ML — has dune_p, regime, VPIN, offset |
| **window_snapshots** | Per-window CG/TWAP/Gamma | Feature analysis |
| **gate_audit** | Gate pass/fail + direction + would_have_won | ML training |
| **wallet_snapshots** | Balance every ~1 min | Wallet timeline |
| **poly_trade_history** | CLOB fill history from Polymarket API | Cross-reference |

### Key query — one-shot full picture
```sql
SELECT tb.trade_id, tb.trade_outcome, round(tb.pnl_usd::numeric,2) as pnl,
       tb.entry_reason, tb.direction,
       to_char(tb.resolved_at, 'HH24:MI') as resolved,
       se.eval_offset, round(se.v2_probability_up::numeric,3) as p_up, se.regime
FROM trade_bible tb
LEFT JOIN trades t ON t.id = tb.trade_id
LEFT JOIN LATERAL (
    SELECT * FROM signal_evaluations se2
    WHERE se2.window_ts = CAST(t.metadata->>'window_ts' AS bigint) AND se2.decision = 'TRADE'
    ORDER BY se2.evaluated_at DESC LIMIT 1
) se ON true
WHERE tb.is_live = true AND tb.resolved_at > NOW() - INTERVAL '2 hours'
ORDER BY tb.resolved_at DESC;
```

### DB connection
```
PGPASSWORD=wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj psql -h hopper.proxy.rlwy.net -p 35772 -U postgres -d railway
```

## Montreal Deployment

**Instance:** i-0785ed930423ae9fd, ca-central-1b, IP 15.223.247.178

**SSH (requires fresh temp key every time):**
```bash
ssh-keygen -t ed25519 -f /tmp/ec2_temp_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/ec2_temp_key.pub \
  --availability-zone ca-central-1b --region ca-central-1
# As ubuntu (has sudo):
ssh -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -i /tmp/ec2_temp_key ubuntu@15.223.247.178
# As novakash (app user):
# Change --instance-os-user to novakash
```

**Deploy script:**
```bash
sudo chown -R novakash:novakash /home/novakash/novakash/ 2>/dev/null
sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'
sudo pkill -9 -f "python3 main.py"; sleep 5
sudo -u novakash bash -c 'cd /home/novakash/novakash/engine && nohup python3 main.py > /home/novakash/engine.log 2>&1 &'
sleep 10 && ps aux | grep "python3 main.py" | grep -v grep | wc -l  # Must be 1
```

**CRITICAL rules:**
- Never push FROM Montreal — always local → GitHub → Montreal pulls
- Engine reads `engine/.env` not `.env.local`
- Files in `engine/reconciliation/` can become root-owned after crashes → `sudo chown -R novakash:novakash`
- Each restart clears in-memory state but DB-backed dedup survives
- Engine log: `/home/novakash/engine.log`
- Log backups: `/home/novakash/engine-v10.*.log`

## Known Bugs / Issues

### 1. Reconciler PnL Aggregate Bug (PARTIALLY FIXED)
The reconciler sometimes reports Polymarket aggregate position costs instead of per-trade stakes. The `_resolve_position` method now looks up `stake_usd` from the matched trade, but the orphan resolver had the same issue (fixed Apr 9). Some historical trade_bible entries still have inflated PnL.

### 2. ELM Prediction Recorder Broken
`ticks_elm_predictions` has 0 rows — JSON syntax error on write. The `feature_age_ms` field is being passed as a Python repr string, not valid JSON. Low priority fix.

### 3. ticks_clob Write Error
`clob_feed.write_error` fires every 2-3s — schema mismatch between code INSERT and DB table columns. The CLOB tick data IS being read (used for execution) but not persisted to DB. Non-critical but noisy.

### 4. STARTING_BANKROLL Drift
The risk manager reads `STARTING_BANKROLL` from env at startup, not the live wallet. Must be manually updated when wallet changes significantly. Currently set to $115.

### 5. Polymarket Positions API Field Name
Polymarket returns CLOB token ID in `"asset"` field, NOT `"tokenId"`. Fixed in reconciler (commit ae52c7e) but be aware if interacting with the API directly.

## Performance Summary

### Overnight Apr 8-9 (v10.4 production)
- **38W/10L (79.2% WR)**
- Wallet: $57 → $115+ (+$58)
- Consistent $3.40 stakes (5% fraction at the time)
- TRANSITION regime dominated (57% of trades), highest WR

### v10.4 Effective Thresholds
```
effective = regime_base + offset_penalty + down_penalty + cg_modifier - cg_bonus

Offset penalty: linear, 0 at T-60, max 0.06 at T-180
  formula: min(0.06, (offset - 60) / 120 * 0.06)

DOWN penalty: +0.05 for all DOWN (NO) direction trades

CG modifier: +0.05 when taker+smart opposing, 0 otherwise
CG bonus: -0.01 when 2+ CG signals confirm, +0.02 when 0 confirm
```

Example: CASCADE T-120 DOWN with 2 CG confirms:
  0.80 + 0.03 + 0.05 + 0.00 - 0.01 = **0.87** threshold

## Key Files

| File | What | Lines |
|------|------|-------|
| `engine/signals/gates.py` | 7-gate pipeline, all threshold logic | ~700 |
| `engine/strategies/five_min_vpin.py` | Main strategy, signal eval, trade execution | ~3000 |
| `engine/reconciliation/reconciler.py` | CLOB reconciler, orphan detection, bible sync | ~800 |
| `engine/execution/fok_ladder.py` | FAK price ladder, min 5 shares | ~240 |
| `engine/execution/polymarket_client.py` | CLOB API, GTC orders, trade history | ~1250 |
| `engine/strategies/orchestrator.py` | Wires everything together, heartbeat, SITREP | ~3000 |
| `engine/config/runtime_config.py` | Env var loading | ~220 |
| `engine/alerts/telegram.py` | Telegram notifications | ~1700 |

## Docs Reference

| Doc | Content |
|-----|---------|
| `TRADE_INVESTIGATION.md` | One-query full picture, investigation guide |
| `THINGS_TO_MONITOR.md` | 12 monitoring items with SQL queries |
| `OVERNIGHT_APR9_ANALYSIS.md` | Verified overnight results |
| `DEPLOYMENT.md` | Full deployment procedures |
| `BUGS_FOUND.md` | Documented bugs and fixes |
| `V10_3_HONEST_AUDIT.md` | Honest assessment of gate effectiveness |
| `v10_4_proposal.html` | Interactive v10.4 decision surface |
| `ELM_V3_COMPLETE_REFERENCE.md` (timesfm repo) | Model accuracy data |

## What to Do Next

1. **Monitor performance** — run the hourly cron query, check trade_bible W/L
2. **Fix ELM recorder** — JSON syntax on feature_age_ms field
3. **Fix ticks_clob** — column mismatch in INSERT
4. **Update STARTING_BANKROLL** when wallet changes significantly
5. **Consider CASCADE** — it was 5W/14L (-$61) on Apr 8. Threshold raised to 0.80 but may need further tightening or blocking if it continues losing
6. **Build proper Polymarket trade history API integration** — the positions API returns empty for redeemed tokens. Need to save fill data BEFORE tokens expire.
