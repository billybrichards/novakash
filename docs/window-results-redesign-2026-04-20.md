# Window Results Redesign — per-strategy lineup view (2026-04-20)

Status: partial implementation shipped with this PR. Design captured here for
follow-up UX iterations and to document the API contract `/v58/strategy-windows`
the page now consumes.

## Context

Current strategy lineup (post-2026-04-20):

| Strategy      | Mode  | Version | Role                                 |
| ------------- | ----- | ------- | ------------------------------------ |
| `v6_sniper`   | LIVE  | 6.0.5   | Primary. Bidirectional ensemble sniper. `entry_cap 0.85`. New conviction buckets: `agree_strong`, `pegged_path1`, `mid_conf_blocked`, `no_eval_blocked`. |
| `v5_ensemble` | GHOST | 5.1.0   | Ensemble baseline benchmark. `entry_cap 0.72`. Uses `poly_confidence` blend. |
| `v4_fusion`   | GHOST | 4.2.0   | `polymarket_v2` reference baseline.  |
| `v5_fresh`    | GHOST | 5.3.0   | `v5_ensemble` variant — relaxed path1 freshness gates. |

Other GHOST strategies that remain available behind an "archive" toggle:
`v4_down_only`, `v4_up_basic`, `v4_up_asian`, `v4_fusion_v5_9`, `v10_gate`,
`v5_7c_twap`, `v7_1_regime`, plus all `v15m_*` 15-minute strategies.

The legacy Window History page (`/windows`, `WindowResults.jsx`) was pinned to
the v5.7c / v5.8 / v7.1 signal-source world and had no way to compare what
each strategy in the **current** lineup decided for each 5-min window. The
v6-specific `conviction_bucket` and `path1_age_*` metadata was not surfaced
at all.

## Goal

Per 5-min window, show **what every current strategy decided** (TRADE/SKIP,
direction, why) with LIVE rows highlighted and GHOST rows de-emphasised, so
you can read down a single window card and see:

- Did v6_sniper (LIVE) trade? Which way? What fill price? Win or loss?
- Would v5_ensemble / v4_fusion / v5_fresh (GHOST) have traded the same window?
  What would they have won/lost?
- Which v6 conviction bucket was this window in (`agree_strong` etc)?

## Data flow

### API endpoints already available

* `GET /api/v58/strategy-windows?limit=100&asset=BTC` — per-window strategy
  pivot (primary source for the redesigned page).
* `GET /api/v58/strategy-decisions?limit=500[&strategy_id=…][&resolved=true]`
  — per-decision rows from `strategy_decisions_resolved` (view). Used for
  detail drilldowns that need the full `metadata_json` including
  `conviction_bucket` / `path1_age_*` / gate_results.
* `GET /api/v58/window-detail/{window_ts}` — existing per-window expanded
  view. Preserved for the legacy detail panel.
* `GET /api/v58/outcomes?limit=100` — legacy aggregate (still used when user
  toggles the archive view back on).

### Response shape of `/v58/strategy-windows` (already implemented in
`hub/api/v58_monitor.py`, unchanged by this PR):

```ts
type StrategyWindowsResponse = {
  windows: Array<{
    window_ts: number;          // seconds epoch
    asset: string;              // 'BTC'
    open_price: number;
    close_price: number;
    actual_direction: 'UP' | 'DOWN' | null;
    vpin: number | null;
    regime: string | null;      // CALM | NORMAL | CASCADE | TRANSITION
    delta_source: string | null;
    strategies: Record<string, {
      mode: 'LIVE' | 'GHOST';
      action: 'TRADE' | 'SKIP';
      direction: 'UP' | 'DOWN' | null;
      skip_reason: string | null;
      entry_reason: string | null;
      confidence_score: number | null;
      eval_offset: number | null;
      outcome: 'WIN' | 'LOSS' | 'SKIP' | null;
    }>;
  }>;
  count: number;
  known_strategies: string[];
};
```

Missing from this endpoint today: `fill_price`, `pnl_usd`, `conviction_bucket`,
`path1_age_*`. See "Follow-up API work" below.

## UI design

### Header metrics

Replace the legacy `TOTAL / WINS / LOSSES / ACCURACY / REAL P&L / SHADOW P&L`
strip (which aggregates legacy configs only) with:

```
┌────────────────────────────────────────────────────────────────────────────┐
│ WINDOWS  [N]   RESOLVED [M]   v6_sniper WR  X%  (pnl +$Y)                  │
│                                SHADOW — v5e W%  v4f W%  v5f W%             │
└────────────────────────────────────────────────────────────────────────────┘
```

- Real P&L and WR are computed only from `v6_sniper` LIVE rows (the sole
  LIVE strategy).
- Shadow WRs are per-GHOST, side-by-side, so you can see e.g. "v5_ensemble
  would have beaten v6_sniper over the last 100 windows".

### Filters

Three filter rows:

1. **Outcome** — `ALL / WIN / LOSS / NO_FILL / SKIP / PENDING`
   (computed against the primary LIVE row.)
2. **Strategy focus** — `ALL / v6_sniper (LIVE) / v5_ensemble / v4_fusion /
   v5_fresh`. When a specific strategy is picked, that strategy becomes the
   "primary" row driving the outcome filter + row colouring.
3. **Conviction bucket (v6-only)** — `ALL / agree_strong / pegged_path1 /
   mid_conf_blocked / no_eval_blocked`. Hidden when the strategy focus is
   not v6_sniper.
4. **Time range** — `1h / 24h / 7d / custom`. Implemented as `limit` knob
   until the endpoint supports `since=` / `until=`.
5. **Archive toggle** — `[ show legacy strategies ]` renders the current page
   content with four additional rows (v4_down_only, v4_up_basic, v4_up_asian,
   v10_gate, v7_1_regime).

### Per-window card — collapsed

```
┌───────────────────────────────────────────────────────────────────────────┐
│ 20 Apr 13:13 UTC  │  $74,320 → $74,339  +0.026%  │  Actual: ▲ UP           │
│ vpin 0.58 · regime NORMAL · delta_source chainlink                        │
│ ─────────────────────────────────────────────────────────────────────────│
│ v6_sniper   LIVE  │ TRADE DOWN  pegged_path1  │ fill 0.49  │ ✅ WIN  +$3.13 │
│ v5_ensemble GHOST │ SKIP        low_confidence│   —        │ would-TRADE UP │
│ v4_fusion   GHOST │ TRADE UP                  │ ghost only │ ❌ would-LOSE  │
│ v5_fresh    GHOST │ SKIP        disagreement  │   —        │   —            │
└───────────────────────────────────────────────────────────────────────────┘
```

Key tenets:

- One row per strategy in the current lineup, **not** per legacy-config.
- The LIVE row gets a coloured left-border + bolder weight.
  GHOST rows are at ~70% opacity.
- Direction + outcome visible immediately: green for WIN, red for LOSS,
  grey for SKIP. Each row also shows its `action` text so you can see
  `SKIP` vs `TRADE` at a glance.
- Ghost rows show the **would-have-been** outcome using direction vs
  `actual_direction` (from `strategy_decisions_resolved.outcome_source =
  'shadow'`). This answers "was the ghost right?" without any fills.
- A `conviction_bucket` chip renders for v6_sniper rows when the metadata
  is present. Unique to v6.
- `Actual: UP/DOWN` in the card header is the ground-truth market
  direction from `window_snapshots`, independent of what any strategy
  decided. Lets you see model-right vs model-wrong at a glance.

### Expanded card

Tabs:

1. **Signals** (default) — LGB, Path1 classifier, blend
   `probability_up`, `delta_chainlink`, `delta_tiingo`, `delta_binance`,
   VPIN, regime. Re-uses the current `WindowDetail.DECISION REASONING`
   panel with minor restructure.
2. **Gates per strategy** — side-by-side table of
   `gate_results` from each strategy's `metadata_json`:

   ```
                         v6_sniper  v5_ensemble  v4_fusion  v5_fresh
    timing                 PASS        PASS         PASS       PASS
    source_agreement       PASS        PASS         PASS       PASS
    confidence            PASS:0.61   SKIP:0.42    PASS       SKIP:0.48
    conviction_bucket     agree_strong  —            —          —
    delta_magnitude        PASS        PASS         PASS       PASS
    …
   ```
3. **Execution** — `entry_cap`, `fill_price`, `fill_size`, `fak_prices`
   attempted (from `metadata.fak_prices[]`), Polymarket confirmed fill,
   SOT divergence if any (`sot_reconciliation_state`).

Legacy `what_if`, `entry_timing` and `SparkChart` panels remain visible
inside the expanded view but move **below** the three tabs so they don't
dominate. These were designed for the v5.7c/v7.1 era and still carry
historical signal but are not the primary view anymore.

## What this PR ships

1. `frontend/src/constants/strategies.js` — adds `v6_sniper`, `v5_ensemble`,
   `v5_fresh` entries. Adds `CURRENT_LINEUP_IDS` + `LEGACY_LINEUP_IDS`
   exports. Flags `v4_fusion` with `inCurrentLineup: true`.
2. `frontend/src/pages/WindowResults.jsx` — page-level restructure:
   - Dual-mode renderer: **"Lineup View"** (default — consumes
     `/v58/strategy-windows`) and **"Legacy View"** (toggle — the existing
     `/v58/outcomes` aggregate).
   - Lineup View renders the per-window card above (collapsed form).
   - Header chips show per-strategy WR (LIVE + GHOST side-by-side).
   - Filter bar: outcome + strategy-focus + archive toggle. Conviction
     bucket / date range filters left as TODO inside the component
     (annotated).
   - Expanded card body in Lineup View re-uses the existing
     `WindowDetail` component (hits `/v58/window-detail/{ts}`).
3. `docs/window-results-redesign-2026-04-20.md` — this file.

## What this PR does not ship (follow-up)

1. **Endpoint enrichment.** `/v58/strategy-windows` today does not expose
   `fill_price`, `pnl_usd`, or `metadata.conviction_bucket`. The page
   currently shows these as `—` for LIVE rows. Follow-up to join
   `strategy_decisions_resolved` into the endpoint's `best_per_strategy`
   CTE (or expose an augmented variant). Illustrative TS stub:

   ```ts
   type StrategyCellEnriched = StrategyCell & {
     fill_price?: number | null;
     fill_size?: number | null;
     pnl_usd?: number | null;
     conviction_bucket?:
       | 'agree_strong' | 'pegged_path1'
       | 'mid_conf_blocked' | 'no_eval_blocked' | null;
     path1_age_s?: number | null;
     path1_age_source?: string | null;
     probability_raw?: number | null;
     probability_calibrated?: number | null;
   };
   ```

2. **Gates-per-strategy tab.** Requires the endpoint to return per-strategy
   `gate_results` (or a sibling `/v58/strategy-gate-matrix/{window_ts}`).
   Stubbed out in the expanded card with a placeholder.

3. **Conviction-bucket filter.** Client-side implementation needs
   `conviction_bucket` on the per-strategy cell — deferred to (1).

4. **Date range + custom range.** Current endpoint only accepts `limit`.
   Add `since_ts` / `until_ts` query params before exposing a date
   picker.

5. **Per-strategy P&L in header.** Requires (1). Stubbed in the code but
   renders as `—` until the endpoint enrichment lands.

## Rollout + reverting

Both views share the same route. The page defaults to **Lineup View**; a
pill toggle at the top switches to **Legacy View**. Reverting is one-line
(change the `DEFAULT_VIEW` constant). No migrations. No engine changes. No
strategy YAML touched.
