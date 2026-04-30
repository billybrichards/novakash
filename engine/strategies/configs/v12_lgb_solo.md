# v12_lgb_solo

**Pure-v12 LGB strategy** — no v9 ensemble blend. Same gate stack as
`v9_lgb_only` / `v10_lgb_only`; only difference is which model's
probability drives the decision.

## Purpose

Direct apples-to-apples comparison of v12 vs v9 on the **same gate stack**.
Companion to:

- `v9_lgb_only` — production baseline (v5 prod LGB)
- `v10_lgb_only` — v10 LGB shadow (currently GHOSTED, see yaml)
- `v12_lgb_combo` — v9+v12 averaged + Option C contrarian (engine PR #431)

Per Billy's request (hub note #286 — v12 dominance analysis): v12 currently
contributes ~30% weight in production via `v12_lgb_combo`'s
disagreement-contrarian path (Option C, hub note #299). For v12 to
"stand out" as the basis for future fusion / `v8_champion` variants, we
need a clean signal: **how does v12 perform alone?**

## Mechanism

The hook (`v12_lgb_solo.py`) does the following:

1. Read `surface.probability_lgb_v12`. If `None`, return `SKIP` with
   `skip_reason="probability_lgb_v12 unavailable"`.
2. Save `surface.probability_lgb` (v9 prod), then overwrite with
   `probability_lgb_v12` so the gate stack uses v12.
3. Null `probability_classifier` (LGB-only forced) and default
   `v4_regime` to `"chop"` on cold start (same as v9_lgb_only).
4. Delegate to `evaluate_v9_ensemble(surface)`.
5. Restore the original surface fields in `finally` (no leak).
6. Decorate the decision with metadata:
   - `probability_lgb_v12` — the v12 probability used
   - `probability_lgb_prod` — original v9 prob (for filter parity)
   - `lgb_only_forced=True`
   - `v12_solo_model=True`

## Eval-offset window restriction (75-240s)

`min_offset_sec: 75`, `max_offset_sec: 240`.

Below 75s, `current_v12.json` falls back to v9 prod boosters (no v12
trained model for delta 030/060), so trading there would be **identical
to v9_lgb_only** — no signal. Above 240s, we're past the trade window.

Per held-out validation (training data):

| Delta | eval_offset | v12 acc | v9 acc | v12 lift |
|------:|-------------|--------:|-------:|---------:|
|   090 | 75-105s     |  62.7%  | 59.8%  | +2.9pp   |
|   120 | 105-150s    |  61.4%  | 56.3%  | +5.1pp   |
|   180 | 150-210s    |  65.7%  | 58.7%  | **+7.0pp** |
|   240 | 210-270s    |  63.8%  | 55.9%  | **+7.9pp** |

v12 is most dominant at **delta 180-240** — first canary delta target.

## Sizing

Identical schedule to `v10_lgb_only` baseline — max **2.5x kelly** at
the `vhc` tier (`dist >= 0.25` → `confidence_score >= 0.50`). No
aggressive default; safety-equivalent to existing LGB-only strategies.

## Validation gate (before LIVE flip)

1. Ship as `mode: GHOST` (this PR — no live trades).
2. **48h shadow** — measure `v12_lgb_solo` decisions on overlapping
   windows vs `v9_lgb_only`. Track WR via `v_signal_comparison` view
   (engine PR #436 helper).
3. If `v12_lgb_solo` WR ≥ `v9_lgb_only` WR on the overlapping set:
   flip LIVE on a **canary delta first** — start delta 180 (biggest
   replay edge, n=46k+ training examples).
4. Full LIVE expand across all 4 deltas only after canary stability
   (1 week min).
5. Eventually: `v12_lgb_solo` replaces `v9_lgb_only` as the primary
   decision-maker; v9 booster keeps running as feature provider only
   per hub note #295.

## Cross-references

- Hub note **#286** — v12 dominance analysis (pp lift by delta)
- Hub note **#299** — Option C disagreement-contrarian rollout plan
- Hub note **#295** — v9 booster as feature provider role
- Engine PR **#431** — `v12_lgb_combo` (sibling combo strategy)
- Engine PR **#436** — `probability_lgb_v12` columns + `v_signal_comparison` view
- Engine `v10_lgb_only.py` — structural template for this hook

## Regression safety

- `mode: GHOST` default — no live orders on ship
- Eval offset bounds `75-240` prevent redundant trading at delta
  030/060 where v12 manifest falls back to v9
- 6 unit tests covering skip / trade / surface-restoration / metadata
- `surface.probability_lgb_v12` already exists (added by PR #431/#436)
- **Zero modifications** to: `v9_lgb_only`, `v10_lgb_only`,
  `v9_ensemble`, `v8_champion_lgb_only`, `registry.py`, `data_surface.py`
