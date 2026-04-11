# v11 Docs Index

> Quick navigation for the v11 release — code fixes, reconciler, log
> ops, and the truth dataset export. If you're a new agent starting
> a session, read these in order.

## 📖 Read these first (in order)

1. **`V11_CHANGELOG_AND_HANDOVER.md`** — the full v11 story: what
   broke, what was fixed, what's deployed, what's still pending.
   Start here.

2. **`APR9_FULL_DAY_ANALYSIS.md`** — the Apr 9 day-of-bleeding
   post-mortem that exposed the adverse-selection pattern (which
   turned out to be the multi-fill bug).

3. **`OVERNIGHT_APR9-10_ANALYSIS.md`** — the overnight session that
   made it impossible to ignore: 81% signal WR vs 49% fill WR. The
   smoking gun.

4. **`DEPLOYMENT.md`** — Montreal deployment rules (SSH via EC2
   Instance Connect, never push from Montreal, engine reads
   `engine/.env` not `.env.local`, etc.).

5. **`CLAUDE.md`** — AI evaluator architecture (Claude Opus 4.6
   primary, Qwen 122B fallback).

## 🛠️ New artifacts in v11

### Code / DB

| File | What |
|------|------|
| `engine/reconciliation/poly_fills_reconciler.py` | Periodic async reconciler class + CLI — fetches Polymarket data-api, detects multi-fills, enriches trade_bible |
| `hub/db/migrations/versions/20260410_01_poly_fills.sql` | Append-only ground-truth fill table with source tagging + multi-fill audit columns |
| `engine/execution/polymarket_client.py` (modified) | Fixed FAK/FOK response parsing (was reading non-existent `size_matched`) |
| `engine/alerts/telegram.py` (modified) | Fixed `_send_telegram` typo + silent exception handler in `send_system_alert` |
| `engine/execution/risk_manager.py` (modified) | Kill switch auto-resume (env-gated, currently disabled) |

### Scripts

| File | What |
|------|------|
| `scripts/backfill_trades_from_polymarket.py` | Manual backfill from Polymarket data-api with multi-fill audit report (`--hours N --link`) |
| `scripts/export_truth_dataset.py` | **NEW** — Exports the correlated CSV dataset for ML / analysis (poly_fills + trade_bible + signal_evaluations + gate_audit) |
| `scripts/restart_engine.sh` | Montreal restart helper with pre-restart log rotation |
| `scripts/logrotate-novakash-engine.conf` | Daily logrotate config for Montreal |

### Data exports

- **`docs/truth_dataset/20260410-115338/`** — latest truth dataset
  (last 36h, generated Apr 10 11:53 UTC). Contains:
  - `poly_fills.csv` (203 rows, authoritative CLOB fills)
  - `poly_fills_enriched.csv` (203 rows, joined with trade_bible + signal_evaluations)
  - `trade_bible.csv` (130 rows, resolved trades)
  - `signal_evaluations.csv` (28,695 rows, every 2s decision)
  - `gate_audit.csv` (28,694 rows, per-window audit)
  - `summary.json` (aggregates + integrity check)
  - `README.md` (human-readable description + pandas examples)

- **`docs/log_archive/`** — pre-v11 Montreal engine logs downloaded
  for post-mortem:
  - `engine-apr10-v11-discovery.log` (87MB) — contains the 316 FAK
    attempts with broken parsing
  - `engine-postreconciler-apr10-1135.log` (108KB) — first v11 run

## 🔍 Where to look for what

### "What's the wallet doing?"
```sql
SELECT balance_usdc, recorded_at FROM wallet_snapshots
ORDER BY recorded_at DESC LIMIT 10;
```

### "Is the multi-fill bug still happening?"
```sql
SELECT multi_fill_total, count(DISTINCT condition_id) as windows
FROM poly_fills
WHERE side='BUY' AND match_time_utc >= NOW() - interval '6 hours'
GROUP BY 1 ORDER BY 1;
```
Post-v11 expectation: 100% `multi_fill_total = 1`. Anything else means
the fix regressed or a new bug appeared.

### "What's the real P&L vs recorded?"
```sql
SELECT
  ROUND(SUM(pf.cost_usd)::numeric, 2) AS actual_spent,
  (SELECT ROUND(SUM(stake_usd)::numeric, 2) FROM trade_bible
    WHERE is_live AND placed_at >= NOW() - interval '24 hours') AS recorded_stake
FROM poly_fills pf
WHERE pf.side='BUY' AND pf.match_time_utc >= NOW() - interval '24 hours';
```
Post-v11 expectation: `actual_spent ≈ recorded_stake` within $5 drift.

### "Are Telegram alerts firing?"
```bash
ssh ubuntu@15.223.247.178 'sudo grep telegram /home/novakash/engine.log | tail -20'
```
Should show `telegram.system_alert_sent` and (eventually) `telegram.entry_alert_sent`.

### "Is the reconciler running?"
```bash
ssh ubuntu@15.223.247.178 'sudo grep -E "poly_fills_reconciler_started|sync_complete" /home/novakash/engine.log | tail -5'
```
Should show `sync_complete` every ~5 minutes.

### "I want to refresh the CSV truth dataset"
```bash
cd /Users/billyrichards/Code/novakash/.claude/worktrees/brave-archimedes
DATABASE_URL='postgresql://postgres:...@hopper.proxy.rlwy.net:35772/railway' \
  python3 scripts/export_truth_dataset.py --hours 36
# Output: docs/truth_dataset/YYYYMMDD-HHMMSS/
```

### "I want to backfill `poly_fills` manually"
```bash
DATABASE_URL='postgresql://...' \
  python3 scripts/backfill_trades_from_polymarket.py --hours 72 --link
```

### "Engine crashed, I want to restart cleanly with log rotation"
```bash
ssh ubuntu@15.223.247.178
sudo chown -R novakash:novakash /home/novakash/novakash/
sudo -u novakash bash -c 'cd /home/novakash/novakash && git pull origin develop'
sudo /home/novakash/novakash/scripts/restart_engine.sh
```

## ⚙️ Current Montreal config (as of Apr 10 11:45 UTC)

- **Version**: v10.5-ish hybrid (NO v10.6/v10.7 tuning deployed)
- **Sizing**: `BET_FRACTION=0.050`, `STARTING_BANKROLL=63` (stale, live wallet is $34.66)
- **Thresholds**: `V10_DUNE_MIN_P=0.60`, `V10_TRANSITION_MIN_P=0.70`, `V10_CASCADE_MIN_P=0.67`
- **Cap**: flat $0.68 (NOT confidence-scaled)
- **Kill switch**: `MAX_DRAWDOWN_KILL=0.80`, auto-resume disabled
- **Delta gate**: `V10_MIN_DELTA_PCT=0.005` (loose)

Full reference: **`V11_CHANGELOG_AND_HANDOVER.md`** §5

## 🎯 Proposed "winning config" (NOT deployed, waiting on data)

See **`V11_CHANGELOG_AND_HANDOVER.md`** §6 for the full proposal. TL;DR:
- Tighten delta gate to 0.03
- Raise DUNE confidence floors to 0.70
- Block CALM / TRENDING / LOW_VOL regimes
- Deploy confidence-scaled cap (v10.6)
- Activate kill switch auto-resume (30 min cooldown)

**Do not deploy until at least 24h of post-v11 clean-fill data confirms
the signal stack is truly 80%+ WR on actual fills (not just evals).**

## 🗓️ Historical changelogs

Individual day changelogs are in:
- `CHANGELOG-2026-04-02.md`
- `CHANGELOG-2026-04-06.md`
- `CHANGELOG-2026-04-07.md`
- `CHANGELOG-2026-04-08.md`
- `CHANGELOG_SEQUOIA.md` (the model retraining run)
- `V11_CHANGELOG_AND_HANDOVER.md` (this release, the parsing bug discovery)

## 🚦 Current engine status (last verified Apr 10 11:45 UTC)

- PID **353249** running on Montreal (`15.223.247.178`)
- Reconciler wired and running every 5 min
- Telegram alerts sending
- First post-v11 WIN: $2.75 stake, +$1.29 P&L at 11:43 UTC
- Wallet: $34.66
- No config changes deployed (just code fixes)

To re-verify anytime:
```bash
ssh ubuntu@15.223.247.178 'ps aux | grep "python3 main.py" | grep -v grep'
```
