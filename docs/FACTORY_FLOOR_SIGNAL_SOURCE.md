# Factory Floor — RECENT FLOW TIMELINE column glossary

**Task:** FACTORY-01
**Date:** 2026-04-11
**User question:** "the factory floor looks GREAT i notice from the table there would be lots of trades that we would win !! a few losses but this is good !! what signal is that exactly? not super clear"

This note answers that question definitively, with file:line citations, so the SIGNAL / ACTUAL / SRC / GATES / REASON / RESULT columns on `/factory` `RECENT FLOW TIMELINE` are self-explanatory the next time we look at them.

---

## Data pipeline for the table

1. Frontend component: `frontend/src/pages/FactoryFloor.jsx:1407-1494`.
2. Fetches from: `/api/v58/outcomes?limit=15` (see `FactoryFloor.jsx:892`).
3. Hub route handler: `hub/api/v58_monitor.py:1016-1083` (`get_outcomes`) which runs a SQL query against `window_snapshots` joined with `market_snapshots` and `trades`.
4. Row mapping: `hub/api/v58_monitor.py:316-390` (`_row_to_window`).
5. Outcome calculation + `actual_direction`: `hub/api/v58_monitor.py:863-1011` (`_calc_outcome_row`).
6. Signal written by the engine: `engine/strategies/five_min_vpin.py:1082-1183` (the `window_snapshot` dict assembly, field `"direction"` on line 1114).
7. Signal computed by the gate pipeline (v10.5+): `engine/signals/gates.py:281-420` (`SourceAgreementGate`), wired into the pipeline at `engine/strategies/five_min_vpin.py:695-707`.

---

## Column meanings

### `SIGNAL` (a.k.a. `o.direction`)

**UI code:** `frontend/src/pages/FactoryFloor.jsx:1449-1456` (renders `o.direction`).

**Hub source:** `window_snapshots.direction` column, selected in `hub/api/v58_monitor.py:1037` and passed through in `_row_to_window` at `hub/api/v58_monitor.py:335`.

**Engine writer:** `engine/strategies/five_min_vpin.py:1114`:

```
"direction": signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN"),
```

**What `signal.direction` actually is depends on which pipeline produced the window:**

- **v10.5+ / `V10_6_ENABLED=true` (current production path):** `signal.direction = pipe_result.direction` from the 8-gate pipeline (`engine/strategies/five_min_vpin.py:695-707`). `pipe_result.direction` is itself set to `ctx.agreed_direction` in the pipeline result (see `engine/signals/gates.py:1199,1215,1222`), which is the UP/DOWN consensus vote from `SourceAgreementGate` across Chainlink + Tiingo + Binance deltas (2/3 majority in Mode A, CL+TI unanimous in Mode B with `V11_POLY_SPOT_ONLY_CONSENSUS=true`). See `engine/signals/gates.py:281-420`.
- **v9 legacy fallback:** `signal.direction` is the delta-sign of the primary feed chosen by `DELTA_PRICE_SOURCE` (`engine/strategies/five_min_vpin.py:2103,2117,2124`), optionally overridden by the v9 source-agreement flag at `engine/strategies/five_min_vpin.py:1036-1037`.

**In plain English:** SIGNAL is the engine's predicted direction for the 5-minute Polymarket UP/DOWN market — the direction it *would* trade if all gates passed. On the v10.5+ pipeline that powers the current prod engine, it is the source-agreement vote across the price feeds (NOT the DUNE model probability — DUNE enters later via `DuneConfidenceGate` as a confidence filter).

---

### `ACTUAL` (a.k.a. `o.actual_direction`)

**UI code:** `frontend/src/pages/FactoryFloor.jsx:1457-1468`.

**Hub computation:** `hub/api/v58_monitor.py:876-889` (inside `_calc_outcome_row`):

```
actual_direction = None
if poly_outcome and trade_direction:
    # Polymarket resolved this window — use that as truth
    if trade_direction == "YES":
        actual_direction = "UP" if poly_outcome == "WIN" else "DOWN"
    else:
        actual_direction = "DOWN" if poly_outcome == "WIN" else "UP"
elif open_p is not None and close_p is not None:
    # Fallback: Binance T-60s price
    actual_direction = "UP" if close_p > open_p else "DOWN"
```

**In plain English:** ACTUAL is the ground-truth direction for the window. Preferred source is the `trades` table's Polymarket resolution (WIN/LOSS combined with the direction we bet). If we didn't trade the window (shadow row), it falls back to `window_snapshots.close_price > window_snapshots.open_price`, where both are Binance T-60s prices written by the engine at window close. The UI highlights the cell in red when `SIGNAL != ACTUAL`.

---

### `SRC` (a.k.a. `o.delta_source`)

**UI code:** `frontend/src/pages/FactoryFloor.jsx:1469-1477` — formats as `TNG` / `CL` / `BN`.

**Hub source:** `window_snapshots.delta_source` (`hub/api/v58_monitor.py:1045`, `_row_to_window` at `hub/api/v58_monitor.py:360`).

**Engine writer:** `engine/strategies/five_min_vpin.py:1160` (`"delta_source": _price_source_used`), which is assigned in the `DELTA_PRICE_SOURCE` selector at `engine/strategies/five_min_vpin.py:449-487`. Typical values:

- `tiingo_rest` / `tiingo_websocket` — Tiingo candle close at T-0 (preferred)
- `chainlink` — Chainlink on-chain oracle
- `binance` — Binance spot REST/WS
- `chainlink_fallback` — Tiingo unreachable, fell back to Chainlink

**In plain English:** SRC is the price feed whose delta was fed into the signal evaluator for this window — i.e. which oracle "won" the `DELTA_PRICE_SOURCE` selection. It's NOT the feed that voted for the signal direction (the source-agreement gate reads all three deltas regardless). It's the single delta that ended up in `window_snapshots.delta_pct` and drove the legacy v9 direction fallback.

---

### `GATES` (a.k.a. `outcomeGateString(o)`)

**UI code:** `frontend/src/pages/FactoryFloor.jsx:270-283`:

```
const checks = [
  !skip.includes('VPIN'),
  !skip.includes('TWAP'),
  !skip.includes('DELTA'),
  !skip.includes('CG'),
  !skip.includes('FLOOR'),
  !skip.includes('CAP'),
];
```

**What each slot means (order, left-to-right):**

1. `VPIN`   — VPIN gate (`current_vpin >= five_min_vpin_gate`, default 0.45). `engine/strategies/five_min_vpin.py:2063-2071`.
2. `TWAP`   — legacy TWAP gate (removed in v10 cleanup, slot retained for backfill on old rows).
3. `DELTA`  — delta magnitude gate (`|delta_pct| >= min_delta`). `engine/strategies/five_min_vpin.py:2100,2114,2121`.
4. `CG`     — CoinGlass taker-flow / OI confirmation. `engine/signals/gates.py` (`TakerFlowGate`, `CGConfirmationGate`).
5. `FLOOR`  — Polymarket gamma price floor ($0.30). Skip reason contains "FLOOR".
6. `CAP`    — Polymarket gamma price cap ($0.83, or dynamic cap from v10.3+). `DynamicCapGate` in `engine/signals/gates.py`.

**Caveat (known low-fidelity):** this is an *approximation* from substring matches on `window_snapshots.skip_reason`. Any gate not mentioned in the skip reason shows as `✓`, which is why downstream rows sometimes display all-green even when, say, the DUNE gate was the actual reason for the skip. A cleaner version would read `window_snapshots.gates_passed` / `gate_failed` directly. Tracked as a follow-up — not in scope for FACTORY-01.

---

### `REASON` (a.k.a. `o.skip_reason`)

**UI code:** `frontend/src/pages/FactoryFloor.jsx:1479-1481`.

**Hub source:** `window_snapshots.skip_reason`, a human-readable string written by the first failing gate. Examples seen in prod:

- `"DUNE P(UP)=0.539 < 0.600 (NORMAL T-120 cg_confirms=0/3)"` — DUNE gate's confidence was below the regime threshold.
- `"VPIN 0.412 < gate 0.45"` — VPIN was too low.
- `"CASCADE: delta 0.0013% < scaled threshold 0.0150% (VPIN 0.832)"` — delta below the cascade-scaled floor.

Empty when the window actually traded. UI falls back to `'traded'` when `v71_would_trade` or `v58_would_trade` is set but skip_reason is null.

---

### `RESULT`

**UI code:** `frontend/src/pages/FactoryFloor.jsx:260-268` (`outcomeLabel`), rendered at `1482-1488`.

Logic: Polymarket-resolved windows show `WIN` / `LOSS` based on `v71_correct`. Unresolved + `v71_would_trade==false` + `v58_would_trade==false` rows show `SKIP`. Falls back to `v58_correct` if v71 is null.

---

## The answer to "what signal is that exactly?"

> **SIGNAL is the engine's UP/DOWN prediction for the Polymarket 5-min window** — specifically the direction produced by the v10.5+ gate pipeline's SourceAgreementGate (a 2/3 majority vote across Chainlink, Tiingo, and Binance price deltas — or CL+TI unanimous in v11 spot-only mode). It is **not** the final "trade placed" flag (that's RESULT=SKIP/WIN/LOSS), and it is **not** the DUNE model's direction (DUNE is a confidence filter downstream of the SourceAgreementGate, not the source of the direction). Every row has a SIGNAL even if the window was skipped, because `signal.direction` is written to `window_snapshots.direction` before gate rejection on the skip path.

The rows in the user's screenshot all showed `UP` because the sampled window set was during a momentum regime where the three price feeds all ticked up. The RESULT column is what tells you whether the engine acted on that signal (WIN/LOSS) or let it pass through the gates (SKIP).
