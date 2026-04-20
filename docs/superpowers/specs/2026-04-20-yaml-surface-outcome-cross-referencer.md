# YAML × Data-surface × Outcome cross-referencer analyser — spec

**Status:** draft spec
**Author:** billy + claude 2026-04-20
**Primary branch target:** `develop`
**Related:** audit-task #255 (tuning workflow unblock — **blocker**), #256 (v6 bucket override), #259 (wallet v2 DB), note #191 (skip-bucket methodology), spec `2026-04-20-wallet-page-v2-spec.md`

---

## 1. Problem

Every time we tune a strategy, the same loop happens:

1. Agent reads the YAML to see what the strategy *thinks* it's doing.
2. Agent queries `strategy_decisions` and gets 100× re-eval-inflated row counts.
3. Agent tries to join with `outcomes` but resolution backfill is broken for the LIVE era.
4. Agent tries to pull per-decision data surface (VPIN, p_path1, delta_chainlink, etc) but metadata JSON lacks some fields depending on the strategy.
5. Agent produces a report. 50% of the numbers are wrong or uncomputable. Operator can't tune confidently.

Four separate agents this week produced inconsistent skip-bucket counts for the same strategy over the same window (461 vs ~9, 5115 vs ~52). Every agent re-learned the same gotchas. Every tuning change went in without solid evidence.

**What's missing:** one tool that joins {YAML declared thesis} × {data surface at each decision} × {real outcome at the window} in a clean, deduped, outcome-resolved view, and surfaces the deltas.

---

## 2. Goals

Answer the following questions in under 60 seconds for any strategy × time window:

### Declared-vs-observed

1. **Which YAML gates actually fired, and at what rate?** Agreement between the YAML thesis ("skip if VPIN < 0.45") and the realised skip-reason histogram.
2. **Which gates are dead code?** E.g. the `risk_off_override_enabled` flag that never fired because it was evaluated before bucket was computed (v6 pre-6.0.6).
3. **Is the surface field the YAML references even populated?** E.g. does `poly_confidence_distance` have non-null values when `trade_advised` runs?

### Outcome-conditioned

4. **Skip → outcome sensitivity.** For each skip reason, what's the hypothetical WR / P&L if we traded it? Tells you which gates save money vs which over-block.
5. **Trade → outcome by feature band.** For each data-surface feature, bucket the trades and show WR per band. Tells you where the strategy's edge actually lives.
6. **Threshold shift simulation.** If we move gate X from Y to Y', how many more/fewer decisions fire, and projected WR & P&L shift?

### Cross-strategy

7. **Same window, different verdict.** On windows where strategies disagreed, who was right? Isolates the unique signal per strategy.
8. **Dedup race attribution.** Of the trades that filled, which strategy's decision landed first? Is the signal leaking (all strategies copying one)?
9. **Strategy-agnostic feature importance.** Across all strategies, which data-surface feature best predicts WIN on the next 5-min window?

---

## 3. Inputs

All joinable via `metadata->>'dedup_key'` (window-unique key, suffix = unix close timestamp).

### 3.1 YAML strategy config
Parsed from `engine/strategies/configs/*.yaml`. Declarative gate list + `gate_params` block. Each gate has a name, threshold, optional env-override chain.

Structure for the analyser:
```python
class GateSpec:
    name: str                       # e.g. "vpin_min"
    yaml_key: str                   # e.g. "gate_params.vpin_min"
    env_key: Optional[str]          # e.g. "V6_SNIPER_VPIN_MIN"
    default: Any
    active_value: Any               # resolved from YAML → env → default
    surface_field: Optional[str]    # "surface.vpin"
    operator: str                   # ">=", "<=", "in", "==", "!="
    comparison_value: Any           # the threshold
```

Not every gate is declarative in YAML — many live inside hook code (v6_sniper.py classifies buckets inline). For those, introspect by name: map `skip_reason` strings in hook code to a virtual `GateSpec` with `yaml_key=null` and note it as "hook-derived".

### 3.2 Data surface snapshots
Per-decision feature set. Sources:

- `strategy_decisions.metadata` (JSONB) — primary; has most fields the hook read.
- `signal_evaluations` — raw model outputs (LGB, path1, TimesFM) keyed by `(timestamp, timeframe)`.
- `window_traces` — full per-window reasoning trail, only populated when `WINDOW_TRACE_ENABLED=true`.
- `ticks_chainlink` / `ticks_tiingo` / `ticks_clob` — oracle & book state at window boundaries; join on `(timestamp, asset)`.

Standardised columns expected after the cross-referencer's join step:

```
dedup_key, strategy_id, window_open_utc, window_close_utc,
# ── Model surface ──
probability_up_raw, probability_up_calibrated, probability_lgb, probability_classifier_path1,
ensemble_mode, ensemble_config_raw,
path1_age_s, path1_age_source,
# ── Market surface ──
delta_chainlink_bps, delta_tiingo_bps, delta_binance_bps,
sources_agree,
gamma_up_price, gamma_down_price, clob_up_bid, clob_up_ask,
poly_trade_advised, poly_reason, poly_confidence, poly_confidence_distance, poly_max_entry_price,
# ── Flow / regime ──
vpin, regime, v4_regime, v4_regime_confidence,
cg_oi_usd, cg_long_short_ratio, cg_funding_rate,
# ── Timing ──
eval_offset_sec, window_utc_hour, blocked_utc_hour,
```

Not every row will have every column. Analyser emits `n_missing_per_column` so tuners see data-quality holes.

### 3.3 Decision output
One row per decision, deduped to one row per (strategy, window) via `DISTINCT ON (dedup_key, strategy_id) ORDER BY evaluated_at DESC`:

```
action            TRADE | SKIP
skip_reason       string | null
direction         UP | DOWN | null
conviction_bucket agree_strong | pegged_path1 | mid_conf | no_eval | ...
gates_log         array of {gate, passed, reason}
risk_off_override_fired  bool
source_agreement_bypassed bool
```

### 3.4 Real outcome
From `ticks_chainlink` (authoritative close-vs-open sign, per `reference_clob_audit.md` — chainlink is the oracle Polymarket itself uses for resolution).

```
actual_direction   UP | DOWN | FLAT
close_price        decimal
open_price         decimal
resolved_at_utc    timestamp
```

If the matview `strategy_skip_resolved` (audit #255 F1) exists, use that — it has `actual_direction` already joined. If not, compute inline from `ticks_chainlink` (open at window start, close at window start + 300s).

### 3.5 Trade reality
For `action=TRADE` rows: LEFT JOIN `trades` on `(strategy_id, metadata->>'dedup_key')` for fill info:

```
filled            bool
fill_price        decimal
fill_size         decimal
stake_usd         decimal
pnl_usd           decimal
outcome_label     WIN | LOSS | UNFILLED | EXPIRED | null
```

Unfilled intents are the audit-#256 "fak_rfq_exhausted" class — shown distinctly from fills.

---

## 4. Architecture

One Python module: `scripts/ops/strategy_cross_referencer.py`. Plus a Hub endpoint for remote/FE calls.

### 4.1 `load_strategy(path: str) -> StrategySpec`
Parses YAML, walks hook-code imports to detect hook-based gates, returns a `StrategySpec` with all resolvable gates + declared surface fields.

Hook-gate detection: grep the hook file (`v6_sniper.py`, etc.) for `return _skip(f"{reason}"...)` calls and build a virtual gate spec from the skip reason string. Not perfect; document as "best-effort introspection".

### 4.2 `load_decisions(strategy_id, hours) -> DataFrame`
SQL:
```sql
SELECT DISTINCT ON (metadata->>'dedup_key', strategy_id)
  metadata->>'dedup_key' AS dedup_key,
  strategy_id,
  action,
  skip_reason,
  (metadata->>'direction') AS direction,
  (metadata->>'conviction_bucket') AS conviction_bucket,
  metadata->>'vpin' AS vpin,
  metadata->>'probability_classifier' AS p_path1,
  metadata->>'probability_lgb' AS p_lgb,
  metadata->>'risk_off_override_fired' AS risk_off_override_fired,
  metadata->>'source_agreement_bypassed' AS source_agreement_bypassed,
  metadata::jsonb AS full_metadata,
  evaluated_at,
  gates_log
FROM strategy_decisions
WHERE strategy_id = :strategy
  AND evaluated_at > NOW() - make_interval(hours => :hours)
ORDER BY dedup_key, strategy_id, evaluated_at DESC
```

This is the dedup step that kills the 100× re-eval inflation. Audit #255 F2 indexes make this fast.

### 4.3 `load_outcomes(hours) -> DataFrame`
From `strategy_skip_resolved` matview if exists; else compute from `ticks_chainlink`:

```sql
SELECT
  window_close_ts AS close_ts,
  price AS close_price,
  LAG(price, <steps_for_5min>) OVER (ORDER BY ts) AS open_price
FROM ticks_chainlink
WHERE asset='BTC' AND ts > NOW() - make_interval(hours => :hours)
```

Emit `actual_direction = sign(close_price - open_price)`.

### 4.4 `load_trades(strategy_id, hours) -> DataFrame`
One row per window the strategy intended to trade. `actual_filled` + `pnl_usd` from `trades` joined on `metadata->>'dedup_key'`.

### 4.5 `join(decisions, outcomes, trades) -> CrossRef`
Outer join on `dedup_key`. Resulting frame has every window the strategy saw + whether it traded + whether it won + every feature that was available.

### 4.6 Analysers

Each is a small pure function on the joined frame:

```python
def skip_bucket_report(cr: CrossRef) -> dict:
    # per skip_reason: n_distinct_windows, resolved_n, hypo_wr, hypo_pnl
    ...

def gate_threshold_sensitivity(cr: CrossRef, gate: GateSpec, shifts: list[float]) -> dict:
    # For each shift, simulate what action would have fired, project WR delta
    ...

def feature_importance(cr: CrossRef) -> dict:
    # For trades only: per surface field, WR lift between band quartiles
    # Uses simple single-feature WR band; future: mutual information
    ...

def cross_strategy_disagreement(cr_list: list[CrossRef]) -> dict:
    # Windows where ≥2 strategies were evaluated, strategies disagreed on action,
    # outcome favoured one. Which strategy was right how often?
    ...

def dead_gate_detector(cr: CrossRef, spec: StrategySpec) -> dict:
    # Declared gates that never appear in skip_reasons OR gates_log.
    # Flags latent config bugs like pre-6.0.6 risk_off_override.
    ...
```

### 4.7 Report emission
Markdown + JSON. Markdown is human-readable with tables + banded WR heatmaps. JSON is machine-readable for dashboards.

Saved to `/home/novakash/analysis/strategy_cross_ref_{strategy}_{YYYY-MM-DD_HH}.md`.

Also POST to Hub as a note with tags `strategy-analysis,{strategy_id},cross-ref`.

---

## 5. CLI interface

```bash
# Single strategy, 48h window, all analysers
python3 scripts/ops/strategy_cross_referencer.py \
  --strategy v6_sniper --hours 48

# Custom gate sensitivity sweep
python3 scripts/ops/strategy_cross_referencer.py \
  --strategy v6_sniper --hours 48 \
  --sensitivity vpin_min --sweep 0.35,0.40,0.45,0.50

# Cross-strategy comparison
python3 scripts/ops/strategy_cross_referencer.py \
  --strategies v6_sniper,v5_ensemble,v4_fusion --hours 48 \
  --output-dir /tmp/xref

# Machine-readable only, skip markdown
python3 scripts/ops/strategy_cross_referencer.py \
  --strategy v6_sniper --hours 48 --format json
```

Hub endpoint (thin wrapper):
```
GET /api/v58/cross-ref?strategy=v6_sniper&hours=48
  -> returns the analyser JSON
POST /api/v58/cross-ref/run?strategy=v6_sniper&hours=48
  -> enqueues a background job, returns report URL when ready
```

FE consumer: a "Strategy analyser" tab in the Strategies page. Heatmap of feature × WR, dead-gate warnings, threshold sliders showing simulated impact.

---

## 6. Example outputs (what v6 would have shown pre-6.0.6)

### 6.1 Dead-gate detector
```
WARNING: gate `risk_off_override_enabled` declared in YAML but never fired
         across 5115 risk_off skips in last 48h.
         Likely dead code. Recommend: move bucket computation upstream
         OR remove the flag to reduce confusion.
```
This would have surfaced audit #256 before it bled potential P&L.

### 6.2 Skip-bucket report (deduped)
```
Strategy: v6_sniper
Window:   48h (2026-04-18 21:00 → 2026-04-20 21:00 UTC)
Decisions: 175 distinct windows (15925 re-evals, 91× factor)

| Skip reason                     | Windows | Resolved | Hypo WR | Hypo PnL |
|---------------------------------|--------:|---------:|--------:|---------:|
| regime_risk_off                 |      52 |        1 |    n/a  |    n/a   |
| pegged_path1_blocked_by_lgb     |      29 |       29 |   13.8% |  -$ 58   |
| vpin_too_low                    |      11 |        0 |    n/a  |    n/a   |
| mid_conf_blocked                |       8 |        8 |   75.0% |  +$  6   |
| feature_stale                   |       4 |        4 |   50.0% |   $  0   |
```

### 6.3 Threshold sensitivity
```
Gate: vpin_min  (current 0.45)

| Shift to | Windows admitted | Projected WR | Projected ΔPnL |
|---------:|-----------------:|-------------:|---------------:|
|     0.35 |              +8  |        45.0% |      -$ 2.00   |
|     0.40 |              +5  |        60.0% |      +$ 1.50   |
|     0.45 |               0  |           — |          — |
|     0.50 |              -3  |           — |      −$ 3 avoided |

Recommendation: SMALL RELAXATION (0.45→0.40) plausible +$1.5, n=5 low confidence.
```

### 6.4 Feature importance (trades only)
```
Trades resolved: 18
| Feature            | WR Q1 | WR Q2 | WR Q3 | WR Q4 | WR lift Q4-Q1 |
|--------------------|------:|------:|------:|------:|--------------:|
| vpin               |   50% |   60% |   80% |   88% |        +38 pp |
| path1_extremeness  |   45% |   65% |   75% |  100% |        +55 pp |
| delta_chainlink    |   55% |   65% |   70% |   82% |        +27 pp |
| eval_offset_sec    |   72% |   70% |   68% |   80% |         +8 pp |
```

Interpretation: path1 extremeness (closer to 0.00/1.00) is the strongest predictor. VPIN secondary. Timing nearly irrelevant. → Validates pegged_path1 bucket thesis.

---

## 7. Known caveats

1. **Actual_direction from chainlink close-open is a proxy for Polymarket resolution.** Polymarket resolves off chainlink, so they should agree; but there's a ~2-block window where chainlink median differs from Polymarket's settlement price. Small-n outliers possible.

2. **Metadata JSON schema drift.** Older decisions may lack newer fields (e.g. `risk_off_override_fired` only lands post-6.0.6). Analyser marks missing columns as `n_missing`, does NOT silently zero-fill.

3. **Simulation is naive.** Threshold sensitivity assumes all OTHER gates would have behaved identically. In reality, gate interactions exist (e.g. VPIN relaxation exposes more mid_conf windows). Flag uncertainty > 20% as LOW-CONFIDENCE.

4. **Outcome resolution lag.** Very recent windows (< 10 min old) don't have chainlink close yet. Analyser excludes windows with `close_ts > NOW() - 10min` unless `--include-unresolved` passed.

5. **Hook-code introspection is best-effort.** Strategies that compute gates inside Python can't always be mapped back to YAML. For those, dead-gate detector reports "n/a — hook-derived gate".

6. **Cross-strategy disagreement is window-scoped.** If strategy A only evaluates T-60 and B only evaluates T-120, the same window has two decisions that aren't strictly comparable. Analyser groups by `(dedup_key, eval_offset_band)` to keep comparison honest.

---

## 8. Milestones

### M1 — bare skeleton (unblocked by audit #255 F1-F2)
- `load_strategy` + `load_decisions` + `load_outcomes` + basic `join`.
- `skip_bucket_report` + `dead_gate_detector`.
- CLI output as markdown + JSON.
- 1 strategy at a time, CLI only, no Hub endpoint.

Ships: reproduces the v6 skip-bucket table from note #191 in under 30 s.

### M2 — threshold sensitivity + feature importance
- `gate_threshold_sensitivity` with `--sensitivity` CLI flag.
- `feature_importance` on resolved trades.
- Surface-field population audit (`n_missing_per_column`).

Ships: can answer "should I lower VPIN floor?" with a concrete number.

### M3 — Hub endpoint + FE tile
- Wrap CLI output behind `GET /api/v58/cross-ref`.
- FE strategies page shows dead-gate warnings + top-2 threshold-sensitivity rows per strategy.

### M4 — cross-strategy
- Multi-strategy joins.
- `cross_strategy_disagreement` + dedup-race attribution.
- FE: "who's unique?" matrix across live strategies.

### M5 — continuous
- Nightly job runs all LIVE strategies over the past 7d; diffs from prior report.
- Alert if any gate becomes "dead" (fires for <1% of decisions over a week) — signals config drift.
- Feed into `ai_analyses` table for Claude-evaluator consumption.

---

## 9. Dependencies

| Required | Source | Status |
|---|---|---|
| Deduped decisions via matview or indexed subquery | audit #255 F1 + F2 | landed PR #300 |
| Outcome backfill on v6 LIVE era | audit #255 F4 | landed PR #300 |
| `strategy_decisions.executed` / `fill_price` / `fill_size` populated | audit #255 F5 | landed PR #300 |
| `novakash` pg SELECT grants | audit #255 F6 | landed PR #300 |
| Metadata fields for v6.0.6+ (risk_off_override_fired, source_agreement_bypassed) | PR #302 | merged |

All blockers cleared. M1 can ship next.

---

## 10. Anti-goals

- **NOT a realtime dashboard.** This is offline/batch analysis. The live wallet + trade tiles belong on separate endpoints.
- **NOT an auto-tuner.** Reports what SHOULD change. Actual YAML/code changes are human-reviewed PRs.
- **NOT a feature-engineering pipeline.** Uses existing surface fields. New features go through the normal engine PR flow.
- **NOT for non-BTC markets.** Scope = BTC 5-min windows (single asset). Extending to other assets is a separate spec.

---

## 11. Open questions (need billy input)

### 11.1 Where does it run?
Options:
- **(a) Montreal** — same box as engine, co-located with DB. Simplest, no egress.
- **(b) Hub AWS** — same box as Hub, shares FE endpoint. Better isolation from engine.
- **(c) GitHub Actions cron** — offline, pulls from Railway pg. No host cost. Slower.

Leaning (b): Hub already has DB access + serves the FE tile.

### 11.2 Outcome granularity
- **(a) Binary UP/DOWN only** — matches Polymarket resolution, simplest.
- **(b) UP/DOWN/FLAT** with ±1 bps dead zone — more honest on chop windows.
- **(c) Magnitude-weighted** — WR × avg-size — exposes stake sensitivity.

Leaning (a) for M1, add (b) as a flag in M2.

### 11.3 How to represent hook-based gates in reports?
- **(a) Treat as first-class** with best-effort introspection, warn on uncertainty.
- **(b) Exclude entirely** — YAML gates only.
- **(c) Require strategies to declare all gates in YAML** going forward — would need code-gen for hooks.

(a) gives most value; (c) is the right long-term answer.

### 11.4 Retention / storage
Reports accumulate. 7-day? 30-day? Forever?

Leaning 30d markdown + 90d JSON + forever in Hub notes (human-readable archive).

---

## 12. Success criteria

7 days post-ship:

1. No subagent ever again computes skip-bucket counts without dedup — all report generation flows through this tool.
2. Every strategy YAML change PR references a cross-ref report as evidence.
3. Latent dead-code gates (like v6 pre-6.0.6 risk_off) get caught within 24h of ship via the dead-gate detector.
4. Operator answers "which gate should I relax?" in < 1 minute via the CLI or FE tile, instead of spinning a subagent for 15 min.
5. Cross-strategy report reveals at least one currently-invisible edge (a window-class one strategy catches that others miss, worth promoting).

---

## 13. Explicitly NOT in this spec

- Backtesting a hypothetical strategy that doesn't exist. This analyses what LIVE + GHOST strategies actually saw and did.
- Model drift detection on LGB / path1 / TimesFM outputs. Separate tool.
- Polymarket order-book microstructure analysis. Separate tool.
- Wallet-level P&L attribution. Lives in `wallet_truth.py` + the audit #259 refactor.
