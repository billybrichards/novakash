# comparison_shadow — Option B meta-strategy

**Status:** GHOST only (no live trades).
**Added:** PR #431 (v12 LGB combo PR), shipped alongside v12_lgb_combo.
**Hub note:** #285 — comparison framework.

## Why

We want to A/B compare WR across signal sources (v9 / v10 / v12 / blend /
classifier) on identical windows without running N near-duplicate strategy
files. Each variant differs only in **which probability field on the
surface it reads**; everything else (gate floor, asset, timescale,
direction logic) is identical.

Option B picks: **single hook + N YAML variants**. The hook reads
`gate_params.signal_source`, selects the right field, applies the same
0.10 conviction floor, and emits a decision tagged with the source. SQL
joins on `(window_ts, strategy_id)` against `market_data.outcome` give a
direct WR comparison once the variants accumulate decisions.

## Variants shipped

| YAML                                  | signal_source            | Notes                                       |
| ------------------------------------- | ------------------------ | ------------------------------------------- |
| v4_fusion_compare_v9.yaml             | probability_lgb          | Prod v5 LGB (current LIVE signal).          |
| v4_fusion_compare_v10.yaml            | probability_lgb_v10      | v10 model (already running shadow).         |
| v4_fusion_compare_v12.yaml            | probability_lgb_v12      | v12 — None until ML box flips V12_ENABLED.  |
| v4_fusion_compare_blend.yaml          | probability_up           | (lgb + classifier) / 2, blend semantics.    |
| v4_fusion_compare_classifier.yaml     | probability_classifier   | TimesFM classifier head only.               |

## Hard constraints (re-read before edits)

1. **Mode is always GHOST.** Every variant YAML sets `mode: GHOST` AND
   `sizing.fraction: 0.0`. Either alone is sufficient; both together
   provide belt-and-braces.
2. **The hook never returns an "execute" decision** — `action: TRADE` is
   emitted purely as a label so the row lands in `strategy_decisions` and
   joins cleanly. The engine respects the strategy's `mode: GHOST` and
   does not place an order.
3. **Read-only.** No `object.__setattr__` swaps, no DB writes, no global
   state. Pure function of (surface, gate_params).

## Activation timeline

| Variant         | When decisions start populating                                |
| --------------- | -------------------------------------------------------------- |
| _v9             | Immediately on deploy (probability_lgb is always present).     |
| _v10            | Immediately (probability_lgb_v10 already shipping).            |
| _v12            | After PR #431 lands + DB migration + V12_ENABLED=true on ML.   |
| _blend          | Whenever probability_lgb is present (classifier optional).     |
| _classifier     | When the TimesFM classifier head is up (none currently).       |

Until the source field is populated on the surface, the variant emits a
SKIP with `skip_reason: signal_source_unavailable:<source>` — useful as a
"this signal isn't wired yet" alarm.

## Promotion path

Shadow only. Promotion of any signal to LIVE goes through Billy's manual
promote workflow (see `feedback_no_auto_promote.md`).
