---
name: Frontend monitor gaps (live mode)
description: Critical UI issues found when going live 2026-04-12 — Monitor, Evaluate, Floor all missing data
type: project
---

**Identified 2026-04-12 21:37 UTC** when user toggled LIVE mode.

## Monitor page issues
- "ENGINE: PAPER" shown next to LIVE toggle — contradicts the LIVE state
- No v4_down_only or v4_up_asian visible anywhere — only V10 gates shown
- Recent Flow shows only "SKIP" with no strategy breakdown
- No BTC price ticker on Monitor
- Gate Pipeline only shows V10 — needs all 4 strategies or at least the LIVE ones

## Evaluate page issues  
- V10 Gate card shows "LIVE" badge — should show "GHOST"
- Mode badges on strategy cards read from wrong source (strategy_decisions.mode instead of runtime config)
- Table rows show "SKIP GHOST/LIVE" but no skip reason, no signal direction
- No BTC price column
- No "would have been right?" indicator for skipped windows
- ACTUAL column shows direction but no connection to what each strategy predicted

## Floor page issues
- Only V10 Gate + V4 Fusion shown — v4_down_only and v4_up_asian missing entirely
- strategy-decisions endpoint only fetches v10_gate and v4_fusion (hardcoded)
- All rows show SKIP with dashes — blank confidence and actual direction

## What the user wants
- All 4 strategies visible on every page
- Clear LIVE/PAPER mode indicator
- BTC price visible
- For each window: signal direction, actual outcome, whether each strategy would have traded, and if the skip was correct
- Skip reason visible so gates can be evaluated for tightness

**How to apply:** These are FE-MONITOR-02 scope items. Pick up as a focused frontend PR.
