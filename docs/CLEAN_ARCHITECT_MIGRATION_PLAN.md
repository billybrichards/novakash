# Clean-Architect Migration Plan — `engine/` → Clean Architecture

**Status**: Planning only. No code changes in this PR.
**Audit IDs covered**: CA-01, CA-02, CA-03, CA-04
**Reference implementation**: `margin_engine/` (Clean Architecture, shipped in PR #10 and iterated since).
**Target**: bring `engine/` (the Polymarket 5-minute binary-options trader) closer to `margin_engine/`'s SHAPE without changing any behaviour on the hot path.
**Author**: clean-architect migration planner (2026-04-11).

---

## 1. Executive summary

`engine/` today is a working, profitable trading system built as two overlapping god-classes: `engine/strategies/five_min_vpin.py` (3,109 LOC) owns window state, multi-source price assembly, gate-pipeline invocation, signal persistence, manual-trade token lookup, and the strategy loop itself; and `engine/strategies/orchestrator.py` (3,330 LOC) owns component wiring, mode switching, reconciler scheduling, heartbeat publication, and manual-trade polling. The only clean piece in the tree is `engine/signals/gates.py` (1,137 LOC), where each gate is already a single-responsibility class with an `evaluate(ctx) -> GateResult` method.

`margin_engine/` — written later by the same developer — uses a classic ports-and-adapters layout: a 283-line `domain/ports.py`, a 572-line `domain/value_objects.py`, two 500–700-LOC use cases, and a 483-line `main.py` composition root. It is testable, the dependency rule is respected, and adapters can be swapped (paper ↔ live, Binance ↔ Hyperliquid) without touching use-case code.

This document is a phased plan to migrate `engine/` to the same shape. Target end-state: `five_min_vpin.py` <500 LOC (or deleted), `engine/domain/ports.py` defines every abstract interface the engine depends on, `engine/use_cases/` holds the four use cases enumerated in §5, `engine/adapters/` holds the concrete implementations, and `engine/infrastructure/main.py` is a pure composition root. Non-goals: changing any gate logic, rewriting the DB schema, replacing the Telegram alerter, or migrating configuration (CFG-01 tracks that separately). Zero behaviour change on the hot path through Phase 7; Phase 8 is the deletion pass and the only phase that can regress anything.

Why this matters: the audit (CA-01..CA-04) identifies this debt as the largest single risk to engine maintainability. The PE-02 and PE-05 asyncpg type-deduction bugs that shipped in April 2026 both originated in `reconciler.py`'s direct raw-SQL code path — exactly the kind of bug that ports-and-adapters separation would surface at review time instead of at 3am on a production host. Every new feature (DQ-01 Polymarket spot consensus, DS-01 V10.6 eval-offset bounds gate, V10.6 full decision surface, PE-06 prediction_recorder rename) currently requires touching the god class, which makes each feature expensive and risk-laden. The migration amortises that cost.

---

## 2. Current-state inventory

This section documents each major `engine/` module's LOC count, top-level shape, external dependencies, the layer it should belong to vs the layer concerns it actually mixes, and the known pain points with concrete file-line citations.

### 2.1 `engine/strategies/five_min_vpin.py` — 3,109 LOC — **GOD CLASS**

**Top-level defs**:
- `_get_v81_cap(offset: int) -> float` (module-level helper, line 61)
- `@dataclass class FiveMinSignal` (line 79) — mutable signal DTO
- `class FiveMinVPINStrategy(BaseStrategy)` (line 92) — the god class, 28 methods

**Key methods** (line numbers current on `origin/develop` 8c93938):
| Method | Line | LOC | Concerns |
|---|---|---|---|
| `__init__` | 100 | 71 | DI (13 params, 7 `Optional[...] = None` escape hatches) |
| `_cleanup_old_traded_windows` | 218 | 18 | In-memory dedup state maintenance |
| `on_market_state` | 245 | 48 | Loop entry — dispatches to `_evaluate_window` |
| `_evaluate_window` | 293 | **1,744** | The body — see §2.1.1 below |
| `_evaluate_signal` | 2037 | 302 | v8/v9/v10 signal decision math (duplicates parts of `gates.py`) |
| `_calculate_confidence` | 2339 | 43 | Confidence tier helper |
| `_check_rate_limit` | 2382 | 29 | Rate-limit state machine |
| `_check_circuit_breaker` | 2417 | 11 | Circuit-breaker check |
| `_on_order_error` / `_on_order_success` | 2428 / 2463 | 35 + 6 | OM callbacks |
| `_execute_trade` | 2469 | **482** | FOK ladder + CLOB retry + Telegram alerts |
| `_fetch_current_price` / `_fetch_fresh_gamma_price` | 2951 / 2968 | 17 + 26 | Inline HTTP |
| `_calculate_stake` | 3016 | 85 | Kelly-style stake math |

**External dependencies** (module-level imports):
```
execution.fok_ladder.FOKLadder, FOKResult
execution.order_manager.Order, OrderManager, OrderStatus
execution.polymarket_client.PolymarketClient
execution.risk_manager.RiskManager
signals.vpin.VPINCalculator
signals.twap_delta.TWAPTracker, TWAPResult
signals.timesfm_client.TimesFMClient, TimesFMForecast
signals.window_evaluator.WindowEvaluator, WindowState, WindowSignal
strategies.base.BaseStrategy
```
Every single one is a **concrete class**, not an interface. The constructor additionally accepts `alerter`, `cg_enhanced`, `cg_feeds`, `claude_evaluator`, `db_client`, `on_window_signal`, `geoblock_check_fn`, `twap_tracker`, `timesfm_client` — all as `Optional[...] = None`.

**Inline I/O inside `_evaluate_window`**:
- Line ~356: hardcoded Tiingo API key string literal `"3f44…"` (security finding in `docs/CLEAN_ARCHITECTURE_REVIEW.md` §1)
- Lines ~360–390: raw `aiohttp.ClientSession` call to Tiingo REST, with inline timeout and nested try/except
- Line ~414: DB read via `self._db.get_latest_chainlink_price(...)`
- Line ~1185: second inline HTTP fetch for TimesFM v2.2 via `self._timesfm_v2`
- Line ~1302: third inline HTTP fetch for fresh Gamma prices via `_fetch_fresh_gamma_price`
- Line ~1334: `self._db.get_latest_macro_signal()` DB read
- Lines ~1346–1504: `self._db.write_window_snapshot(...)` and `self._db.write_evaluation(...)` DB writes
- Lines ~1934–2036: Telegram alert dispatches via `self._alerter.send_...`

**What layer is this?** It mixes all four layers: domain logic (delta direction, confidence tier classification), use-case orchestration (gate pipeline invocation, trade decision), adapter concerns (HTTP, SQL, WS client), and infrastructure concerns (rate limits, circuit breaker, retry). The dependency rule (outer → inner only) is violated everywhere.

**Known pain points**:
- **CA-01**: god-class bloat. 28 methods, ~48 unique `self._*` instance fields, no unit tests for the strategy itself (coupling is too deep).
- **CA-02**: no port seam — Tiingo REST block cannot be swapped, tested, or rate-limited without editing the strategy file.
- **Hardcoded secret** in source control (line 356).
- Dual dedup state with reconciler (see CA-04 below).
- **Dynamically-created attributes via `hasattr` checks** (`self._recent_windows`, `self._v9_disagree_notified`) — silently break anything that assumes they exist.

### 2.1.1 Anatomy of `_evaluate_window` (line 293 → ~2036, ~1,744 LOC)

The CLEAN_ARCHITECTURE_REVIEW.md sketches the sub-sections; confirmed in-place on `develop`:

| Line range | Section | External I/O |
|---|---|---|
| 293–330 | Bootstrap, dedup check, current price | `_fetch_current_price` (Binance REST) |
| 332–412 | Multi-source delta: Tiingo REST + DB fallback | **inline `aiohttp` Tiingo call at line ~360**, API key hardcoded |
| 414–422 | Chainlink delta via `self._db.get_latest_chainlink_price` | PG read |
| 424–488 | Direction consensus + primary-source selection | pure |
| 523–550 | TWAP-delta evaluation | pure |
| 551–586 | TimesFM v1 forecast (comparison only) | HTTP via `self._timesfm` |
| 587–602 | Regime detection + v9 default variables | pure |
| 603–988 | v10 DUNE gate pipeline invocation | `_gate_pipeline.evaluate()` (~385 LOC) |
| 990–1048 | v9.0 dynamic caps + signal evaluation | `_evaluate_signal` helper |
| 1050–1066 | CoinGlass snapshot capture | pure |
| 1067–1184 | Window snapshot dict building (117 LOC) | pure |
| 1185–1246 | v8.1 TimesFM v2.2 query for early entry | HTTP via `self._timesfm_v2` |
| 1247–1301 | Gate results + confidence tier for Telegram | pure |
| 1302–1332 | Fresh Gamma price fetch | HTTP |
| 1334–1345 | Macro observer signal injection | PG read |
| 1346–1393 | DB write (`write_window_snapshot`) | PG write |
| 1394–1504 | Gate audit write (`write_evaluation`), 110 LOC | PG write |
| 1505–1564 | Signal evaluation capture | PG write |
| 1565–1807 | v8.1 early-entry gate check (242 LOC) | pure + v2.2 client |
| 1808–1911 | CLOB cap/floor sanity check | pure |
| 1912–1933 | Consolidated skip summary Telegram | Telegram |
| 1934–2036 | Trade decision alert + dispatch to `_execute_trade` | Telegram + OM |

### 2.2 `engine/strategies/orchestrator.py` — 3,330 LOC

**Top-level shape**: `class Orchestrator` at line 77. Single class, ~30 methods.
**Key methods**: `__init__` (line 84, ~325 LOC of wiring), `start` (line 419), `run` (853), `stop` (859), `_heartbeat_loop` (1689), `_manual_trade_poller` (2514).

**External dependencies** (module-level imports): 30+ concrete types — `TelegramAlerter`, `MarketAggregator`, 8 concrete feed types (`BinanceWebSocketFeed`, `ChainlinkRPCFeed`, `TiingoFeed`, `CLOBFeed`, `CoinGlassAPIFeed`, `CoinGlassEnhancedFeed`, `PolymarketWebSocketFeed`, `Polymarket5MinFeed`), `ClaudeEvaluator`, `PostResolutionEvaluator`, `PlaywrightService`, `OpinionClient`, `OrderManager`, `PolymarketClient`, `RiskManager`, `DBClient`, `TickRecorder`, `ArbScanner`, `CascadeDetector`, `RegimeClassifier`, `VPINCalculator`, 5 concrete strategies, `TWAPTracker`, `TimesFMClient`.

**What layer is this?** It aspires to be the composition root (`margin_engine/main.py`'s role). Instead it mixes:
- Composition (ctor)
- Lifecycle (start, stop, run)
- Mode switching — reads `system_state.paper_enabled/live_enabled` from DB in `_heartbeat_loop` at line ~1755 and hot-flips `self._poly_client.paper_mode = want_paper`
- Heartbeat publication — writes `update_system_state` at line ~1740, and builds a 5-min SITREP at line ~1900 with raw SQL queries
- Manual-trade polling — `_manual_trade_poller` at line 2514 with token-ID fallback logic (LT-02 fix at line ~2554)
- Feed callback routing
- Inline Anthropic API calls inside `_on_five_min_window` (flagged in CLEAN_ARCHITECTURE_REVIEW.md §2)
- Direct access to `self._db._pool` (private attribute) for raw SQL (in 15+ places)
- Direct attribute injection bypassing constructors: `self._five_min_strategy._timesfm = self._timesfm_client` (line 328), `self._five_min_strategy._pending_windows.append(...)` (line 1097)

**Known pain points**:
- 12+ responsibilities jammed into one class (CLEAN_ARCHITECTURE_REVIEW.md §2 enumerates them).
- 325-line `__init__` that's essentially a composition root written as an imperative script.
- Hardcoded Polymarket funder wallet address in a URL string at line ~2015.
- Cross-component state mutation through private attributes (fragile — the LT-02 fix had to guard `if self._five_min_strategy and hasattr(self._five_min_strategy, '_recent_windows')`).

### 2.3 `engine/signals/gates.py` — 1,137 LOC — **ALREADY CLEAN** (mostly)

**Top-level shape** — each gate is its own class with a single `evaluate(ctx) -> GateResult` method:
- `@dataclass class GateResult` (line 40)
- `@dataclass class PipelineResult` (line 49)
- `@dataclass class GateContext` (line 61, **mutable**, ~25 fields)
- `class Gate(Protocol)` (line 124)
- `class EvalOffsetBoundsGate` (line 132) — DS-01 / V10.6
- `class SourceAgreementGate` (line 281)
- `class DeltaMagnitudeGate` (line 336)
- `class DuneConfidenceGate` (line 389)
- `class TakerFlowGate` (line 694)
- `class CGConfirmationGate` (line 803)
- `class SpreadGate` (line 884)
- `class CoinGlassVetoGate` (line 925)
- `class DynamicCapGate` (line 1014)
- `class GatePipeline` (line 1091) — iterates gates, returns on first failure

**External dependencies** (module-level): `structlog`, `os`, `dataclasses`, `typing.Protocol`, and a lazy import of `signals.v2_feature_body` inside `DuneConfidenceGate` (the v11.1 addition). **No framework dependencies**, no DB access, no HTTP. This file already obeys the dependency rule — this is the template for the rest of the migration.

**What layer is this?** Domain + use-case, correctly. Each gate is a decision function with injected config.

**Known pain points (CA-03)**:
- `GateContext` is `@dataclass` (mutable) — gates mutate it in place:
  - `SourceAgreementGate.evaluate()` at line ~326: `ctx.agreed_direction = agreed_dir`
  - `TakerFlowGate.evaluate()` at lines ~776, ~785: `ctx.cg_threshold_modifier = penalty_or_bonus`
  - `CGConfirmationGate.evaluate()` at lines ~861, 865, 869: `ctx.cg_confirms`, `ctx.cg_bonus`
  - `DuneConfidenceGate.evaluate()` at lines ~644–646: `ctx.dune_probability_up`, `ctx.dune_direction`, `ctx.dune_model_version`
- **Implicit ordering**: `DuneConfidenceGate._effective_threshold` at ~line 448 reads `ctx.cg_threshold_modifier` and `ctx.cg_bonus`. If the pipeline order were rearranged to run `DuneConfidenceGate` before `TakerFlowGate` + `CGConfirmationGate`, the DUNE gate would see zero modifiers and apply a lower threshold — a silent trading regression with no test coverage protecting against it.
- `GatePipeline.evaluate()` at line 1098 iterates in construction order — the ordering contract lives only in `five_min_vpin.py`'s pipeline-construction call site.

### 2.4 `engine/reconciliation/reconciler.py` — 1,081 LOC

**Top-level shape**: `class CLOBReconciler` (line 36). Lifecycle methods (`start`/`stop`), poll loops (`_poll_loop`, `_poll_once`, `_backfill_on_startup`), the resolution body (`_resolve_position` at line 710), orphan-fill checker, and reporting loop.

**External dependencies**: `reconciliation.state` (pure VOs), type-only `alerts.telegram.TelegramAlerter` and `execution.polymarket_client.PolymarketClient`, and **direct `asyncpg` pool access** via `self._pool` — lines 165, 178, 204, 319, 320, 424, 609, 610, 626, 741, 745, 765, 786, 810, 834, 938, 940, and ~10 more.

**What layer is this?** This should be pure use-case (resolution logic) + adapter (polling loop + DB + Polymarket reads). In reality it's a mixing bowl: decision logic, SQL strings, `asyncpg` type-coercion workarounds, and Telegram reporting all in one class.

**Known pain points**:
- **PE-02 bug class** — lines 757–776: comment documents that using `$1` and `$2` in a bidirectional LIKE made `asyncpg` unable to deduce whether the parameter was `text` or `varchar`, raising "inconsistent types deduced". The fix was an explicit `::text` cast. This is exactly the kind of low-level SQL plumbing that should live in a repository adapter, not in the resolution use case.
- **PE-05 bug class** — lines 824–835: same "inconsistent types" issue inside a `CASE WHEN` around `outcome = $1`. Fix was pre-computing the status value and passing it as a separate parameter. Again: SQL plumbing leaking into use-case logic.
- `self._known_resolved: set[str] = set()` (line 65) — second owner of "has this window been acted on" dedup state (the first is `FiveMinVPINStrategy._traded_windows` — see CA-04). No invariant keeps them consistent. A third owner exists on the order-manager callback path in `Orchestrator._resolved_by_order_manager`.
- Raw SQL strings live inline in the reconciler body — a single-line schema change breaks both the reconciler and any external tool that happens to match.

### 2.5 `engine/persistence/db_client.py` — 2,046 LOC

**Top-level shape**: single `class DBClient` (line 26) with ~50 async methods. Purposes span: connection/pool lifecycle (`connect`, `close`), trade writes (`write_trade`, `save_trade`), feed status (`update_feed_status`), system-state heartbeats (`update_system_state`, `update_heartbeat`), window snapshots (`write_window_snapshot`, `update_window_outcome`), gate audit (`write_gate_audit`, `write_evaluation`, `write_signal_evaluation`), manual-trade token lookup (`get_token_ids_from_market_data`), macro/chainlink/tiingo reads, mode toggle reads (`get_mode_toggles`), and many more.

**External dependencies**: `asyncpg`, `config.settings`, a few DTOs.

**What layer is this?** It aspires to be a repository layer but is really a **bag of SQL helper methods** organised around the shapes of the callers, not around aggregates. There's no `WindowRepo`, `TradeRepo`, `SignalRepo` — there's just `DBClient` with methods that touch several of those tables interchangeably.

**Known pain points**:
- Same asyncpg type-deduction footguns as the reconciler (different methods, same bug class).
- No type boundary: methods take and return raw `dict`s, not validated VOs.
- Tests either need a live PG instance or have to monkeypatch individual methods.

### 2.6 Supporting directories — current state

- `engine/data/feeds/` — concrete feed implementations (Binance WS, Chainlink, Tiingo, CLOB, CoinGlass, Polymarket 5-min). Already each in its own file, mostly single-responsibility, but not implementing a port protocol.
- `engine/execution/` — `FOKLadder`, `OrderManager`, `PolymarketClient`, `RiskManager`. `FOKLadder` is already clean (stateless). `PolymarketClient` is the IO boundary — also reasonably clean. `OrderManager` mixes state management with fills.
- `engine/alerts/telegram.py` — concrete `TelegramAlerter`; single responsibility, no port interface.
- `engine/config/` — `settings.py` is a pydantic `BaseSettings` (good), `constants.py` has string constants, `runtime_config.py` does DB-backed hot-reload. The hot-reload flow is a side channel that bypasses the settings object — part of the CFG-01 scope, not this migration.
- `engine/evaluation/` — `ClaudeEvaluator`, `PostResolutionEvaluator`. Already single-class, single-responsibility.
- `engine/tests/` — mix of pytest and a few `unittest` files. No tests for `FiveMinVPINStrategy` itself.

---

## 3. Target-state architecture

The post-migration layout mirrors `margin_engine/`'s directory structure exactly, so an engineer fluent in one is fluent in the other.

### 3.1 Layer diagram

```
┌───────────────────────────────────────────────────────────────┐
│                    engine/infrastructure/                    │
│                                                              │
│   main.py                — composition root, ~500 LOC        │
│   config/settings.py     — pydantic BaseSettings              │
│                                                              │
└───────────────▲──────────────────────────────────────────────┘
                │ instantiates
┌───────────────┴──────────────────────────────────────────────┐
│                      engine/adapters/                        │
│                                                              │
│   market_feed/    (BinanceWS, Tiingo REST, Chainlink DB)     │
│   consensus/      (PriceConsensus builder)                   │
│   polymarket/     (Gamma, CLOB, FOK ladder)                  │
│   prediction/     (TimesFM v1, v2)                           │
│   persistence/    (PgSignalRepo, PgTradeRepo, PgWindowRepo)  │
│   alert/          (TelegramAlerter wrapper)                  │
│   clock/          (SystemClock)                              │
│                                                              │
└───────────────▲──────────────────────────────────────────────┘
                │ implement ports from
┌───────────────┴──────────────────────────────────────────────┐
│                     engine/use_cases/                        │
│                                                              │
│   evaluate_window.py       (EvaluateWindowUseCase)           │
│   execute_manual_trade.py  (ExecuteManualTradeUseCase)       │
│   reconcile_positions.py   (ReconcilePositionsUseCase)       │
│   publish_heartbeat.py     (PublishHeartbeatUseCase)         │
│                                                              │
└───────────────▲──────────────────────────────────────────────┘
                │ depends only on
┌───────────────┴──────────────────────────────────────────────┐
│                       engine/domain/                         │
│                                                              │
│   ports.py          — abstract protocols                     │
│   value_objects.py  — frozen dataclasses (Window, Tick,      │
│                       DeltaSet, SignalEvaluation, etc.)      │
│   entities/         — Trade, Position (if needed)            │
│                                                              │
└───────────────────────────────────────────────────────────────┘
```

**The dependency rule**: `engine/domain/` never imports from `engine/adapters/`, `engine/infrastructure/`, or concrete execution classes. `engine/use_cases/` imports only from `engine/domain/`. `engine/adapters/` implements ports from `engine/domain/ports.py`. `engine/infrastructure/main.py` imports from all three and wires them together. Enforced by code review in Phase 0–7; optionally by `import-linter` in CI after Phase 8.

### 3.2 Target directory layout

```
engine/
├── domain/                             ← NEW
│   ├── __init__.py
│   ├── ports.py                        # ~400 LOC — abstract protocols only
│   └── value_objects.py                # ~400 LOC — frozen dataclasses
├── use_cases/                          ← NEW
│   ├── __init__.py
│   ├── evaluate_window.py              # ~500 LOC — Phase 3
│   ├── execute_manual_trade.py         # ~250 LOC — Phase 6/7
│   ├── reconcile_positions.py          # ~250 LOC — Phase 6/7
│   └── publish_heartbeat.py            # ~200 LOC — Phase 6/7
├── adapters/                           ← NEW
│   ├── __init__.py
│   ├── market_feed/
│   │   ├── binance_ws.py
│   │   ├── tiingo_rest.py              # ← inline aiohttp extracted here
│   │   ├── tiingo_db.py
│   │   └── chainlink_db.py
│   ├── consensus/
│   │   └── three_source.py             # CL/TI/BIN delta triple
│   ├── polymarket/
│   │   ├── gamma_http.py
│   │   └── clob_api.py
│   ├── prediction/
│   │   ├── timesfm_v1.py
│   │   └── timesfm_v2.py
│   ├── persistence/
│   │   ├── pg_signal_repo.py           # SignalRepository impl
│   │   ├── pg_trade_repo.py
│   │   └── pg_window_repo.py
│   ├── alert/
│   │   └── telegram.py
│   └── clock/
│       └── system_clock.py
├── infrastructure/                     ← NEW
│   ├── __init__.py
│   ├── config/
│   │   └── settings.py                 # pydantic BaseSettings, ENGINE_ prefix
│   └── main.py                         # composition root
├── strategies/
│   ├── five_min_vpin.py                # ← shrunk to <500 LOC or deleted
│   ├── orchestrator.py                 # ← lifecycle glue only, ~500 LOC
│   └── ...                             # sub_dollar_arb, vpin_cascade unchanged
├── signals/
│   └── gates.py                        # ← gates return (GateResult, GateDelta)
├── reconciliation/
│   └── reconciler.py                   # ← 1,081 → ~400 LOC, uses WindowRepo
├── persistence/
│   └── db_client.py                    # ← kept as low-level asyncpg wrapper
├── data/feeds/                         # ← impl-detail of adapters/market_feed/
├── execution/                          # ← impl-detail of adapters/polymarket/
├── alerts/                             # ← impl-detail of adapters/alert/
├── config/                             # ← moves into infrastructure/config/
├── evaluation/
├── polymarket_browser/
└── tests/
    ├── unit/
    │   ├── domain/
    │   ├── use_cases/
    │   └── adapters/
    └── integration/
```

**Shrinkage targets**:
- `engine/strategies/five_min_vpin.py`: **3,109 → <500 LOC** (target 350)
- `engine/strategies/orchestrator.py`: **3,330 → ~500 LOC** (lifecycle glue only)
- `engine/reconciliation/reconciler.py`: **1,081 → ~400 LOC** (poll loop + port calls)
- `engine/persistence/db_client.py`: **2,046 → ~1,000 LOC** (kept as low-level pool wrapper; per-aggregate repos live in `adapters/persistence/`)

---

## 4. Port contracts

Each port below is the exact interface (signatures + docstring contract) that Phase 0 will add to `engine/domain/ports.py`. Types are specified against the value objects that arrive in Phase 1 (§5 references them by name).

### 4.1 `MarketFeedPort`

```python
class MarketFeedPort(abc.ABC):
    """Reads live and recent-historical prices for a single asset.

    Implementations: BinanceWebSocketAdapter (live mid), TiingoRestAdapter
    (5-min candles), ChainlinkDbAdapter (latest on-chain price from PG).

    The port is intentionally narrow — the full historical query surface
    belongs on a separate HistoricalFeedPort if we need it later. For now
    this is what _evaluate_window needs at window close.
    """

    @abc.abstractmethod
    async def get_latest_tick(self, asset: str) -> Optional[Tick]:
        """Return the most recent price observation this feed has seen.

        MUST NOT block on the network — implementations should cache the
        latest value from their ingest loop. Returns None if the feed
        has never produced a tick (cold start) or the latest tick is
        older than the feed's own staleness threshold.
        """
        ...

    @abc.abstractmethod
    async def get_window_delta(
        self, asset: str, window_ts: int, open_price: float,
    ) -> Optional[float]:
        """Percentage delta open→eval for the 5m window starting at
        window_ts, using this feed's price series.

        Returns None when the feed cannot answer — a miss is NOT an
        error, it's a normal fallback signal. Implementations MUST
        swallow network errors, timeouts, non-200 statuses, parse
        failures, and missing-field errors into a single `return None`
        path. They log at DEBUG (not WARNING) so the skip summary can
        distinguish expected-miss from unexpected-miss.
        """
        ...

    @abc.abstractmethod
    def subscribe_window_close(
        self, asset: str, timeframe: str,
    ) -> AsyncIterator[WindowClose]:
        """Async iterator that yields once per window close.

        The orchestrator consumes this to drive the EvaluateWindowUseCase
        loop — each yield produces a WindowClose value object with the
        window_ts, open_price, close_ts, and a snapshot of the feed's
        latest tick at the moment of close.
        """
        ...
```

### 4.2 `ConsensusPricePort`

```python
class ConsensusPricePort(abc.ABC):
    """Computes the CL/TI/BIN delta triple for a window.

    One implementation composes three MarketFeedPort instances
    (chainlink_db, tiingo_rest, binance_ws) and returns a DeltaSet.
    """

    @abc.abstractmethod
    async def get_deltas(
        self, asset: str, window_ts: int, open_price: float,
    ) -> DeltaSet:
        """Fetch deltas from all sources in parallel.

        Returns a DeltaSet with per-source Optional[float] entries —
        missing sources are None, not errors. The caller decides how
        to handle partial data (currently: require at least 2/3 sources
        with matching sign for the SourceAgreementGate to pass).
        """
        ...
```

### 4.3 `SignalRepository`

```python
class SignalRepository(abc.ABC):
    """Append-only sink for per-evaluation audit + execution trail.

    Replaces the scattered DBClient.write_window_snapshot,
    write_evaluation, write_signal_evaluation, write_gate_audit,
    write_clob_book_snapshot, write_fok_ladder_attempt methods — each
    of those becomes one `save_*` method on this repository.
    """

    @abc.abstractmethod
    async def write_signal_evaluation(self, row: SignalEvaluation) -> None:
        """Persist one SignalEvaluation VO to signal_evaluations table.
        Idempotent by (asset, window_ts, eval_offset) — second write
        for the same key is a no-op.
        """
        ...

    @abc.abstractmethod
    async def write_clob_snapshot(self, row: ClobSnapshot) -> None:
        """Persist one ClobSnapshot VO to clob_book_snapshots table."""
        ...

    @abc.abstractmethod
    async def write_gate_audit(self, audit: GateAuditRow) -> None:
        """Persist one GateAuditRow with the gates-that-ran tuple."""
        ...

    @abc.abstractmethod
    async def write_window_snapshot(self, snapshot: WindowSnapshot) -> None:
        """Persist a WindowSnapshot VO to windows table. Used for
        backfill and UI hydration, not for trading decisions."""
        ...
```

### 4.4 `PolymarketClient` (port)

```python
class PolymarketClient(abc.ABC):
    """Trading side of Polymarket (CLOB + Gamma reads + manual-trade poll).

    Wraps today's execution.polymarket_client.PolymarketClient. The
    concrete adapter delegates to the existing class so zero behaviour
    changes during Phase 2.
    """

    @abc.abstractmethod
    async def place_order(
        self, token_id: str, side: str, size: float, price: float,
    ) -> FillResult:
        """Place a CLOB order. `side` is 'YES' | 'NO', 'price' is in
        [0.0, 1.0] Polymarket units. Returns a FillResult with actual
        filled size, filled price, fees, order_id. Raises PolymarketError
        on definitive failure (network, rejection, insufficient funds).
        """
        ...

    @abc.abstractmethod
    async def get_window_market(
        self, asset: str, window_ts: int,
    ) -> Optional[WindowMarket]:
        """Look up the Gamma market for (asset, window_ts). Returns
        None if the market doesn't exist yet or has been delisted.
        """
        ...

    @abc.abstractmethod
    async def get_book(self, token_id: str) -> Optional[OrderBook]:
        """Read the live CLOB book for a token. Returns None on miss."""
        ...

    @abc.abstractmethod
    async def poll_pending_trades(self) -> list[PendingTrade]:
        """Poll the manual-trades table for rows with status='pending'.
        Used by ExecuteManualTradeUseCase as its input source.
        """
        ...
```

### 4.5 `AlerterPort`

```python
class AlerterPort(abc.ABC):
    """Telegram and any future alert channels.

    Wraps today's alerts.telegram.TelegramAlerter. The concrete adapter
    delegates to the existing class so Phase 2 is purely structural.
    """

    @abc.abstractmethod
    async def send_system_alert(self, message: str) -> None:
        """System-level alert (mode switch, kill switch, manual-trade
        failure). No formatting — plain text.
        """
        ...

    @abc.abstractmethod
    async def send_trade_alert(
        self, window: WindowKey, decision: TradeDecision,
    ) -> None:
        """Structured trade-decision alert with Markdown formatting."""
        ...

    @abc.abstractmethod
    async def send_skip_summary(
        self, window: WindowKey, summary: SkipSummary,
    ) -> None:
        """Consolidated all-offsets-skipped summary at T-0."""
        ...

    @abc.abstractmethod
    async def send_heartbeat_sitrep(self, sitrep: SitrepPayload) -> None:
        """5-minute SITREP message published by PublishHeartbeatUseCase."""
        ...
```

### 4.6 `Clock`

```python
class Clock(abc.ABC):
    """Time source — allows deterministic testing.

    Identical to margin_engine.domain.ports.ClockPort — same interface
    intentionally so a future consolidation can use the same port.
    """

    @abc.abstractmethod
    def now(self) -> float:
        """Unix epoch seconds."""
        ...
```

### 4.7 `WindowStateRepository` (CA-04)

```python
class WindowStateRepository(abc.ABC):
    """Single owner of 'has this window been traded / resolved?'.

    Replaces:
      - FiveMinVPINStrategy._traded_windows (in-memory set)
      - CLOBReconciler._known_resolved (in-memory set)
      - Orchestrator._resolved_by_order_manager (in-memory set)
    """

    @abc.abstractmethod
    async def was_traded(self, key: WindowKey) -> bool: ...

    @abc.abstractmethod
    async def mark_traded(
        self, key: WindowKey, order_id: str,
    ) -> None: ...

    @abc.abstractmethod
    async def was_resolved(self, key: WindowKey) -> bool: ...

    @abc.abstractmethod
    async def mark_resolved(
        self, key: WindowKey, outcome: WindowOutcome,
    ) -> None: ...

    @abc.abstractmethod
    async def load_recent_traded(self, hours: int) -> set[WindowKey]:
        """Bulk load at engine startup to warm any in-memory cache the
        adapter chooses to maintain."""
        ...
```

### 4.8 `ConfigPort` (deferred — tracked as CFG-01)

```python
class ConfigPort(abc.ABC):
    """DB-backed runtime config. Only declared here for future use;
    the Phase 0–8 migration does NOT wire this — the engine continues
    to read os.environ directly, gated by the existing runtime_config
    hot-reload path. When CFG-01 lands, the ConfigPort replaces those
    reads without touching use-case code.
    """

    @abc.abstractmethod
    async def get_float(self, key: str, default: float) -> float: ...

    @abc.abstractmethod
    async def get_str(self, key: str, default: str) -> str: ...

    @abc.abstractmethod
    async def get_bool(self, key: str, default: bool) -> bool: ...
```

---

## 5. Use case extraction

The engine's current shape is one big loop (`Orchestrator.run` → `FiveMinVPINStrategy.on_market_state` → `_evaluate_window`) with side-tasks (manual-trade poller, reconciler, heartbeat) running as separate asyncio tasks. Clean Architecture makes each of those side-tasks and the loop body into an explicit use case.

### 5.1 `EvaluateWindowUseCase`

**Single-sentence responsibility**: run the gate pipeline against a single window-close event, persist the resulting SignalEvaluation row, and return an Optional[TradeDecision] to the caller.

**Ports consumed**: `MarketFeedPort` (×3 — binance, tiingo, chainlink), `ConsensusPricePort`, `PolymarketClient` (for the Gamma cap/floor check), `PredictionPort` (TimesFM v1 / v2), `SignalRepository`, `AlerterPort` (skip summary), `WindowStateRepository`, `Clock`.

**Value objects consumed**: `WindowClose` (from the MarketFeedPort iterator).

**Value objects produced**: `SignalEvaluation` (written to the repo), `Optional[TradeDecision]` (returned to caller).

**Where today's logic lives**: `engine/strategies/five_min_vpin.py::_evaluate_window`, lines 293–2036 (1,744 LOC). See §2.1.1 for the sub-section breakdown. After extraction the use case should be ~500 LOC.

**Shape**:
```python
class EvaluateWindowUseCase:
    def __init__(
        self,
        binance_feed: MarketFeedPort,
        tiingo_feed: MarketFeedPort,
        chainlink_feed: MarketFeedPort,
        consensus: ConsensusPricePort,
        polymarket: PolymarketClient,
        prediction: PredictionPort,
        signal_repo: SignalRepository,
        alerts: AlerterPort,
        window_state: WindowStateRepository,
        clock: Clock,
        gate_pipeline: ImmutableGatePipeline,  # arrives in Phase 4
        *,
        delta_price_source: str = "tiingo",
        min_eval_offset: int = 60,
        max_eval_offset: int = 200,
    ) -> None: ...

    async def execute(
        self, window: WindowClose, vpin: float, regime: str,
    ) -> Optional[TradeDecision]: ...
```

### 5.2 `ExecuteManualTradeUseCase`

**Single-sentence responsibility**: drain the `manual_trades` table of pending rows, look up each row's CLOB token_id (ring buffer → DB fallback), and place the trade (or mark `failed_no_token`).

**Ports consumed**: `PolymarketClient` (place_order, poll_pending_trades, get_window_market), `WindowStateRepository` (for the ring-buffer-equivalent lookup), `AlerterPort` (for LT-02 failure alerts), `Clock`.

**Value objects consumed**: `PendingTrade` (returned by `poll_pending_trades`).

**Value objects produced**: `Optional[FillResult]` per row; writes status transitions on each row (`pending → executing → open` or `failed_no_token`).

**Where today's logic lives**: `engine/strategies/orchestrator.py::_manual_trade_poller` at line 2514, ~150 LOC. The LT-02 fix (token_id fallback from market_data table) lives at lines ~2554–2600. The fallback currently reads `self._five_min_strategy._recent_windows` directly — that's the ring buffer that needs to become part of a `WindowStateRepository` query or a dedicated `TokenIdLookupPort`.

**Shape**:
```python
class ExecuteManualTradeUseCase:
    def __init__(
        self,
        polymarket: PolymarketClient,
        window_state: WindowStateRepository,
        alerts: AlerterPort,
        clock: Clock,
    ) -> None: ...

    async def drain_once(self) -> list[ManualTradeOutcome]: ...
```

### 5.3 `ReconcilePositionsUseCase`

**Single-sentence responsibility**: poll Polymarket for position outcomes, match resolved positions to trade rows in the DB, update each trade's outcome / PnL / resolved_at.

**Ports consumed**: `PolymarketClient` (get position outcomes), `SignalRepository`/`TradeRepository` (the match + update), `WindowStateRepository` (mark resolved), `AlerterPort` (report loop), `Clock`.

**Value objects consumed**: `PositionOutcome` (from PolymarketClient).

**Value objects produced**: `ResolutionResult` per matched trade; side-effect: updates `trades.outcome`, `trades.pnl_usd`, `trades.resolved_at`.

**Where today's logic lives**: `engine/reconciliation/reconciler.py::_resolve_position` at line 710, ~215 LOC. The PE-02 / PE-05 asyncpg type-deduction workarounds are inline in that method at lines 757–776 and 824–835. Post-migration they move to `adapters/persistence/pg_trade_repo.py` where they can be unit-tested against an in-process PG.

**Shape**:
```python
class ReconcilePositionsUseCase:
    def __init__(
        self,
        polymarket: PolymarketClient,
        trade_repo: TradeRepository,  # new port, Phase 0
        window_state: WindowStateRepository,
        alerts: AlerterPort,
        clock: Clock,
    ) -> None: ...

    async def resolve_one(
        self, condition_id: str, position_data: dict,
    ) -> Optional[ResolutionResult]: ...

    async def backfill_on_startup(self) -> int: ...
```

### 5.4 `PublishHeartbeatUseCase`

**Single-sentence responsibility**: every 10 seconds, read risk-manager state, wallet balance, open-order count, runtime-config snapshot, and write a system-state heartbeat row; every 5 minutes, additionally build and publish a SITREP Telegram message.

**Ports consumed**: `RiskManagerPort` (read-only state query), `PolymarketClient` (wallet balance when live), `SignalRepository` (write system-state row — or a dedicated `SystemStateRepository` port, see §10 open questions), `AlerterPort` (SITREP send), `Clock`.

**Value objects consumed**: `RiskStatus`, `WalletSnapshot`.

**Value objects produced**: `HeartbeatRow` (written to PG), `Optional[SitrepPayload]` (sent to Telegram on the 5-minute boundary).

**Where today's logic lives**: `engine/strategies/orchestrator.py::_heartbeat_loop` at line 1689, ~270 LOC. The mode-switch detection at line ~1755 is a sub-responsibility that should migrate to a `ModeSyncUseCase` as a follow-up — not in the Phase 0–8 scope because the mode flip mutates `self._poly_client.paper_mode` and `self._risk_manager._paper_mode` via attribute assignment, which requires a re-architecture of the adapter tier that's outside this plan.

**Shape**:
```python
class PublishHeartbeatUseCase:
    def __init__(
        self,
        risk_manager: RiskManagerPort,
        polymarket: PolymarketClient,
        system_state_repo: SystemStateRepository,
        alerts: AlerterPort,
        clock: Clock,
    ) -> None: ...

    async def tick(self) -> None: ...   # called every 10s
```

---

## 6. Phase plan

Each phase has explicit entry criteria (what must be true before starting), exit criteria (what must be true before the next phase can start), and a rollback plan. Phases 0–2 are pure-add (zero runtime risk). Phases 3–5 mutate code paths and require characterization tests. Phases 6–7 are rearrangement. Phase 8 is cleanup.

### Phase 0 — Port protocols defined

**Goal**: Add `engine/domain/ports.py` with every abstract protocol listed in §4. No concrete implementations. No imports from the new file anywhere else.

**Entry criteria**:
- `develop` is green.
- No in-flight PRs against `engine/strategies/` or `engine/signals/gates.py` (we want the next phase's diff to apply cleanly).

**Deliverables**:
- `engine/domain/__init__.py` (empty)
- `engine/domain/ports.py` — ~400 LOC of `abc.ABC` + `@abstractmethod` stubs
- `engine/domain/value_objects.py` — **forward-declaration stubs only** for any type names referenced in the port signatures (replaced with real frozen dataclasses in Phase 1)

**Exit criteria**:
- `grep -r "from engine.domain.ports" engine/` returns zero matches outside `engine/domain/`
- Existing test suite passes unchanged (nothing new imports these files)
- `mypy engine/domain/` clean

**Rollback**: `git revert`. Zero runtime impact because nothing imports the new files.

**Estimated effort**: 2–4 hours.

### Phase 1 — Value objects extracted

**Goal**: Replace the stub `engine/domain/value_objects.py` with real frozen dataclasses, each with `__post_init__` validation.

**Entry criteria**:
- Phase 0 merged.
- Port signatures finalised (any changes to the protocol signatures happen by amending Phase 0 before Phase 1 lands).

**Deliverables**:
- `engine/domain/value_objects.py` populated with: `WindowKey`, `Tick`, `WindowClose`, `DeltaSet`, `PriceConsensus`, `SignalEvaluation`, `ClobSnapshot`, `TradeDecision`, `GateResult` (promoted from `signals/gates.py`, kept there as a re-export), `GateContextDelta` (new), `FillResult`, `WindowMarket`, `OrderBook`, `PendingTrade`, `ManualTradeOutcome`, `RiskStatus`, `WalletSnapshot`, `HeartbeatRow`, `SitrepPayload`, `ResolutionResult`, `WindowOutcome`, `PositionOutcome`.
- `engine/tests/unit/domain/test_value_objects.py` — one test per `__post_init__` validation rule.

**Exit criteria**:
- `engine/domain/value_objects.py` has at least 15 frozen dataclasses
- New unit test file has 80%+ coverage of the `__post_init__` branches
- No other file in `engine/` imports from `engine/domain/value_objects.py` yet (adapter shims in Phase 2 will be the first consumers)
- `mypy engine/domain/` clean

**Rollback**: `git revert`. Zero runtime impact.

**Estimated effort**: 4–8 hours.

### Phase 2 — Adapter shims

**Goal**: Introduce `engine/adapters/` with concrete classes that implement each port by **delegating to existing code**. Zero behaviour change. This is the biggest phase by file count but the smallest by logic change — each adapter is ~50–150 lines of delegation.

**Entry criteria**:
- Phases 0 and 1 merged.
- No pending DQ-01 / DS-01 edits that will touch `engine/strategies/five_min_vpin.py`'s `_build_price_consensus` region — those should either land before Phase 2 (and the adapter shims inherit them) or wait until Phase 3 (and they land on top of the new use case).

**Deliverables**:
- `engine/adapters/__init__.py`
- `engine/adapters/market_feed/binance_ws.py` — wraps `data/feeds/binance_ws.BinanceWebSocketFeed`
- `engine/adapters/market_feed/tiingo_rest.py` — **extracts the inline aiohttp block** from `five_min_vpin.py:~360–390`, reads `TIINGO_API_KEY` from env (no more hardcoded key). Only new genuine code in Phase 2.
- `engine/adapters/market_feed/tiingo_db.py` — wraps `DBClient.get_latest_tiingo_price`
- `engine/adapters/market_feed/chainlink_db.py` — wraps `DBClient.get_latest_chainlink_price`
- `engine/adapters/consensus/three_source.py` — composes the three feeds, returns `DeltaSet`
- `engine/adapters/polymarket/gamma_http.py` — wraps `data/feeds/polymarket_5min.Polymarket5MinFeed` + `_fetch_fresh_gamma_price`
- `engine/adapters/polymarket/clob_api.py` — wraps `execution/polymarket_client.PolymarketClient`
- `engine/adapters/prediction/timesfm_v1.py` — wraps `signals/timesfm_client.TimesFMClient`
- `engine/adapters/prediction/timesfm_v2.py` — wraps `signals/timesfm_v2_client.TimesFMV2Client`
- `engine/adapters/persistence/pg_signal_repo.py` — delegates to `DBClient.write_evaluation`, `write_window_snapshot`, `write_gate_audit`, `write_clob_book_snapshot`
- `engine/adapters/persistence/pg_trade_repo.py` — delegates to `DBClient.write_trade`, `poll_pending_live_trades`, `mark_trade_expired`, `update_manual_trade_status`. **PE-02 / PE-05 SQL workarounds move to this file** so they can be unit-tested.
- `engine/adapters/persistence/pg_window_repo.py` — delegates to `DBClient.load_recent_traded_windows` for `load_recent_traded`; implements `was_traded` / `mark_traded` against a new `window_states` table (schema ships in Phase 5).
- `engine/adapters/alert/telegram.py` — wraps `alerts/telegram.TelegramAlerter`
- `engine/adapters/clock/system_clock.py` — `time.time()`
- `engine/tests/unit/adapters/*` — one test file per adapter, using the port interface as the test seam. Tiingo-rest adapter gets a `responses`-library fixture with recorded candles.

**Exit criteria**:
- 14+ new files under `engine/adapters/`
- Every port in `engine/domain/ports.py` has at least one implementation (except `WindowStateRepository` which gets a skeleton — schema migration is Phase 5)
- **Zero runtime changes**: no existing file imports from `engine/adapters/` yet
- All new unit tests pass
- `grep -r "3f4456e457a4184d76c58a1320d8e1b214c3ab16" engine/adapters/` returns zero matches (the API key must come from env, not be reintroduced)

**Rollback**: delete the `engine/adapters/` tree. Zero runtime impact.

**Estimated effort**: 1.5–3 weekends.

### Phase 3 — `EvaluateWindowUseCase` extracted (full migration pattern proven)

**Goal**: Create `engine/use_cases/evaluate_window.py` with `EvaluateWindowUseCase`. Wire it into `FiveMinVPINStrategy._evaluate_window` as the sole code path. All existing tests must still pass. This is the phase where the migration pattern gets proven end-to-end.

**Entry criteria**:
- Phases 0–2 merged.
- Characterization test suite (see §8) green against the existing `_evaluate_window` implementation — captures current behaviour against a set of fixture windows.
- Feature flag `ENGINE_USE_CLEAN_EVALUATE_WINDOW` added (default off).

**Deliverables**:
- `engine/use_cases/__init__.py`
- `engine/use_cases/evaluate_window.py` — the full extraction (~500 LOC)
- `engine/tests/unit/use_cases/test_evaluate_window.py` — use-case unit tests with fake ports (steal the `MockExchange` pattern from `margin_engine/tests/use_cases/test_open_position_macro_advisory.py`)
- Modified `engine/strategies/five_min_vpin.py::__init__` — accepts `evaluate_use_case: EvaluateWindowUseCase` kwarg (default `None`, backward compatible)
- Modified `engine/strategies/five_min_vpin.py::_evaluate_window` — thin delegation (`if feature flag and self._uc is not None: return await self._uc.execute(...)`); else runs the legacy body unchanged
- Modified `engine/strategies/orchestrator.py::__init__` — builds the use case from adapters and injects it; Phase 2's adapters are wired up for the first time here

**Exit criteria**:
- Characterization test suite passes against both the legacy path AND the new path (feature flag flipped for the second run)
- 80%+ line coverage on `engine/use_cases/evaluate_window.py`
- Staging deploy runs both paths in parallel for 24 hours — the use case writes to `signal_evaluations_shadow` table, the legacy path writes to the real `signal_evaluations` — row diff shows zero mismatches on (asset, window_ts, eval_offset, passed, failed_gate)
- Flag flipped to `true` on staging; 24h observation; zero rollback triggered
- Flag flipped to `true` on production; 48h observation
- `develop` branch sets the flag default to `true` only after production has been clean for 48h

**Rollback**: flip the feature flag off. Legacy code path is still live (not deleted until Phase 8). If the new path needs a code fix, revert the Phase 3 merge.

**Estimated effort**: 2 weekends — 1 for the extraction, 1 for the dual-path validation.

**Risk**: HIGH — this is the first phase where real trading logic moves between files. See §7 for the mitigation detail.

### Phase 4 — Gate pipeline made immutable-input (CA-03)

**Goal**: Replace in-place mutations in `GateContext` with an explicit `(GateResult, GateContextDelta)` return tuple. Pipeline folds deltas into a new `GateContext` between gates.

**Entry criteria**:
- Phase 3 merged and stable on production for at least 48h.
- All existing gate tests green.
- New characterization test covering the gate order (see §8 — "property test: same input → same decision regardless of delta-fold ordering — except when ordering is semantically required").

**Deliverables**:
- Modified `engine/signals/gates.py`:
  - `@dataclass(frozen=True) class GateContext` (was mutable)
  - New `@dataclass(frozen=True) class GateContextDelta` — the explicit modifier bundle
  - Every gate's `evaluate()` returns `tuple[GateResult, GateContextDelta]` (empty delta for gates that don't modify)
  - `GatePipeline.evaluate()` folds deltas between gates via `ctx = ctx.merge(delta)` (a new method on the frozen VO that returns a new instance)
- New file `engine/signals/gate_requires.py` (optional safety net): a static table of which `GateContextDelta` field each gate **requires** to be populated before it runs. `GatePipeline.__init__` validates the ordering at construction time — raises `PipelineOrderingError` if a gate's requires aren't satisfied by earlier gates' deltas.
- Modified `engine/use_cases/evaluate_window.py` — uses the new pipeline signature
- New test `engine/tests/unit/signals/test_gate_pipeline_immutable.py` — regression test that constructs `DuneConfidenceGate` BEFORE `TakerFlowGate` + `CGConfirmationGate` and proves it raises `PipelineOrderingError` (guard against the silent-trading-regression scenario documented in §2.3)

**Exit criteria**:
- `grep -r "ctx\.agreed_direction = " engine/signals/` returns zero matches (the in-place mutation pattern is gone)
- `grep -r "ctx\.cg_threshold_modifier = " engine/signals/` returns zero matches
- `grep -r "ctx\.cg_bonus = " engine/signals/` returns zero matches
- Same characterization test suite passes against the refactored gates (bit-for-bit identical decisions on every fixture window)
- Feature flag `ENGINE_IMMUTABLE_GATES=true` runs on production for 48h with zero drift vs the pre-Phase-4 decision log

**Rollback**: revert the Phase 4 merge. Evaluate use case still works because it still talks to the same pipeline interface (the return-type change is additive from the caller's perspective — the old code ignores the delta).

**Estimated effort**: 1–1.5 weekends.

### Phase 5 — Window state repository (CA-04)

**Goal**: Make `WindowStateRepository` the **single** owner of "has this window been traded / resolved?" state. Replace `FiveMinVPINStrategy._traded_windows`, `CLOBReconciler._known_resolved`, and `Orchestrator._resolved_by_order_manager` with calls to the repository.

**Entry criteria**:
- Phases 3 and 4 merged and stable.
- Schema migration file ready (does NOT land yet): `migrations/NNN_window_states.sql` with a `window_states` table schema (`window_key VARCHAR(64) PRIMARY KEY`, `asset`, `window_ts`, `duration_secs`, `traded_at`, `traded_order_id`, `resolved_at`, `resolved_outcome`, `resolved_pnl_usd`, `created_at`).

**Deliverables (sequenced, each is its own commit on the phase branch)**:
1. Land schema + empty adapter — no callers, table is an unused DB object
2. Dual-write: `FiveMinVPINStrategy._evaluate_window` (now inside the Phase 3 use case) calls `window_state.mark_traded(...)` alongside its existing `_traded_windows.add(key)`. `CLOBReconciler._resolve_position` calls `window_state.mark_resolved(...)` alongside its existing `_known_resolved.add(condition_id)`. **Run for 1 week**, periodically reconcile both sources (see §8)
3. Flip readers one call site at a time: `if await window_state.was_traded(window):` replaces `if window_key in self._traded_windows:`, etc. Each flip is its own commit
4. Delete old state: once every reader has been flipped and production has been clean for 48h, delete `self._traded_windows`, `self._known_resolved`, `self._resolved_by_order_manager`, and their maintenance loops (`_cleanup_old_traded_windows`). This is the entry for the Phase 8 shrink pass.

**Exit criteria**:
- `grep -r "self\._traded_windows\|self\._known_resolved\|self\._resolved_by_order_manager" engine/` returns zero matches
- `migrations/NNN_window_states.sql` applied on production
- `SELECT COUNT(*) FROM window_states WHERE traded_at IS NOT NULL` returns a plausible number (roughly equal to `signal_evaluations` rows with `decided_to_trade=true` over the same window)

**Rollback per step**:
- If step (2) dual-write fails, revert the dual-write commit. The old in-memory sets are authoritative.
- If step (3) reader-flip fails, revert that single commit. Previous call sites still use the in-memory sets.
- If step (4) deletion fails, revert the deletion commit.

**Estimated effort**: 3 days to 1 week (depending on how quickly the dual-write observation window passes).

### Phase 6 — Orchestrator split (composition root emerges)

**Goal**: The top-level `Orchestrator.__init__` becomes a composition root (like `margin_engine/main.py`). Every loop body becomes an explicit use case. The orchestrator's role shrinks to lifecycle management (`start`, `run`, `stop`) plus a thin task-spawning layer.

**Entry criteria**:
- Phases 0–5 merged and stable.
- `engine/use_cases/` has `evaluate_window.py` from Phase 3.
- Manual-trade poller, heartbeat loop, and reconciler loop have characterization tests capturing current side-effect shapes (they don't need to be unit-testable yet — just locked against regression).

**Deliverables**:
- `engine/use_cases/execute_manual_trade.py` — extracted from `Orchestrator._manual_trade_poller` (~250 LOC). The LT-02 token-id fallback becomes a `WindowStateRepository.find_token_ids(window_key, direction)` call on the repository (new method added in this phase).
- `engine/use_cases/reconcile_positions.py` — extracted from `CLOBReconciler._resolve_position` (~250 LOC). The PE-02 / PE-05 SQL workarounds already moved to `adapters/persistence/pg_trade_repo.py` in Phase 2 — this phase just wires the use case through that adapter.
- `engine/use_cases/publish_heartbeat.py` — extracted from `Orchestrator._heartbeat_loop` (~200 LOC). Mode-switch detection STAYS in the orchestrator for now (tracked as a follow-up — see §9).
- New `engine/infrastructure/main.py` — takes over composition-root duty from `Orchestrator.__init__`. Pattern mirrors `margin_engine/main.py`: read settings, build pool, build adapters, build use cases, wire loops, `asyncio.gather(...)`. Target ~500 LOC.
- Modified `engine/strategies/orchestrator.py` — `__init__` shrinks to accepting the already-built use cases as constructor kwargs; `start` and `run` still schedule the loops, each loop now just calls `await use_case.tick()` or similar.
- Modified `engine/reconciliation/reconciler.py` — `_resolve_position` becomes a 5-line wrapper around `ReconcilePositionsUseCase.resolve_one(...)`.
- Modified `start_engine.sh` / `engine/main.py` entry point — if an `engine/main.py` exists that constructs `Orchestrator(settings)`, it now constructs `infrastructure/main.py:run()` instead. If only `start_engine.sh` points at `Orchestrator`, it now points at the new main.

**Exit criteria**:
- `wc -l engine/strategies/orchestrator.py` < 800
- `engine/infrastructure/main.py` exists with the composition-root layout
- Every use case enumerated in §5 has its own file under `engine/use_cases/`
- Characterization tests still pass (nothing observable changed)
- Integration test: engine starts cleanly, evaluates at least one window end-to-end, reconciler picks up at least one resolution, manual-trade poll runs at least once, heartbeat loop writes at least one row

**Rollback**: revert the merge. The legacy `Orchestrator.__init__` composition path is still the last known-good config.

**Estimated effort**: 2–3 weekends.

### Phase 7 — God class shrink

**Goal**: `engine/strategies/five_min_vpin.py` goes from 3,109 LOC to <500 LOC. Every method that has been superseded by a use-case call gets deleted.

**Entry criteria**:
- Phases 0–6 merged and stable on production for at least 1 week.
- Every call site that used to read `FiveMinVPINStrategy._recent_windows`, `_traded_windows`, `_window_eval_history`, `_pending_windows` directly has been flipped to use a port.

**Deliverables (deletions)**:
- Delete `_evaluate_window` body (now ~1,744 LOC of dead code behind the Phase 3 feature flag). Keep the method as a 10-line wrapper that delegates to `self._evaluate_use_case.execute(...)`.
- Delete `_evaluate_signal` (302 LOC) — superseded by the use case.
- Delete `_calculate_confidence` (43 LOC) — moves to a helper function in the use case.
- Delete `_cleanup_old_traded_windows` (18 LOC) — owned by `WindowStateRepository` now.
- Delete `_parse_window_ts` (static helper, ~6 LOC) — ditto.
- Delete `_recent_windows` ring buffer — callers go through `WindowStateRepository.find_token_ids(...)`.
- Delete the windowsnapshot dict construction at lines 1067–1184 — superseded by `SignalEvaluation` VO construction inside the use case.
- Delete `_execute_trade` (482 LOC) — moves to a new `engine/use_cases/place_polymarket_order.py` use case OR to `engine/adapters/polymarket/fok_ladder.py` depending on §10 open question #1.
- Keep: `__init__` (trimmed to ~30 LOC), `start`, `stop`, `on_market_state` (dispatches to the use case), `_check_rate_limit`, `_check_circuit_breaker`, `_on_order_error`, `_on_order_success` (these are rate-limit/circuit-breaker state, not decision logic).

**Exit criteria**:
- `wc -l engine/strategies/five_min_vpin.py` < 500
- `grep -r "import aiohttp" engine/strategies/ engine/use_cases/` returns zero matches
- `grep -r "write_window_snapshot\|write_evaluation" engine/strategies/ engine/use_cases/` returns zero matches (those calls live in `adapters/persistence/pg_signal_repo.py` now)
- All existing integration tests still pass
- Production deploy clean for 48h

**Rollback**: revert the merge. All deleted code is in git history.

**Estimated effort**: 1–2 days (the phase is mostly deletions).

### Phase 8 — Infrastructure cleanup

**Goal**: `engine/config/settings.py` moves to `engine/infrastructure/config/settings.py`, pydantic `BaseSettings` with `ENGINE_` env prefix consistently, composition-root code cleaned up, and any leftover mixing of layers gets a final pass.

**Entry criteria**:
- Phases 0–7 merged.
- `engine/infrastructure/main.py` from Phase 6 is the production entry point.

**Deliverables**:
- Move `engine/config/settings.py` → `engine/infrastructure/config/settings.py`
- Ensure `pydantic_settings.BaseSettings` with `env_prefix = "ENGINE_"` — any env var currently read as `FOO_BAR` becomes `ENGINE_FOO_BAR` (breaking change, noted explicitly in the PR body, with a one-week deprecation window where both prefixes are accepted)
- `grep -r "os\.environ\.get" engine/use_cases/ engine/domain/` returns zero matches (all config reads go through settings)
- Optional: add `import-linter` CI check enforcing the dependency rule (see §10 open question #5)
- Delete any now-orphaned files: if the legacy `Orchestrator` class has any dead methods still around, remove them.

**Exit criteria**:
- `engine/infrastructure/config/settings.py` exists; `engine/config/settings.py` is gone
- All env vars use the `ENGINE_` prefix
- `grep -rn "from engine\.domain" engine/adapters/` shows every file imports only from `engine.domain.ports` and `engine.domain.value_objects` (nothing from use_cases or infrastructure)
- `mypy engine/domain/ engine/use_cases/` clean
- Production stable for 1 week

**Rollback**: revert the merge. The env-var prefix change is the only operationally-visible thing here; the deprecation window gives a safety net.

**Estimated effort**: 1 day.

---

## 7. Risk matrix

Per phase: hot-path risk (chance of changing gate behaviour mid-migration), test-coverage gap (what needs new tests before the refactor), merge-conflict risk (how likely the main session is to step on the migration), and rollback plan.

| Phase | Hot-path risk | Test gap before start | Merge-conflict risk | Rollback plan |
|---|---|---|---|---|
| 0 Ports | **ZERO** — file unused | Minimal (mypy is enough) | Very low — new file only | Delete file |
| 1 Value objects | **ZERO** — file unused | VO-level unit tests (new) | Very low — new file only | Delete file |
| 2 Adapter shims | **ZERO** — adapters unused | Per-adapter unit tests with recorded fixtures; Tiingo adapter needs the `responses` library with a golden-master candle file | Low — adds files, does not modify `engine/strategies/` | Delete `engine/adapters/` tree |
| 3 EvaluateWindow UC | **HIGH** — first wiring of real decision logic | **Characterization tests** on `_evaluate_window` against a fixture set of 50+ historical windows. Shadow-write to `signal_evaluations_shadow` for 24h on staging. Parallel run for 48h on production behind feature flag. | HIGH — touches `five_min_vpin.py::_evaluate_window`. Any in-flight DQ-01 / DS-01 / V10.6 / V11 work must coordinate merge timing. | Flip feature flag off; legacy path still live. If code is broken, revert the merge. |
| 4 Immutable gates | MEDIUM — all gate signatures change at once | Add property test: "same input → same decision" on a fixture of gate-context snapshots. Add ordering-error regression test. | MEDIUM — touches `engine/signals/gates.py` directly. Coordinate with any in-flight gate-logic work (DS-01 activation, V10.6 grid). | Revert merge. GateContext returns to mutable. |
| 5 Window state repo | LOW — dual-write phase keeps legacy authoritative for a week | Dual-write reconciliation query (see §8). Characterization tests on manual-trade poller and reconciler still green. | LOW — mostly additive; reader-flip commits are one call site at a time. | Revert the last reader-flip commit; legacy in-memory sets take over again. |
| 6 Orchestrator split | MEDIUM — `Orchestrator.__init__` rewrite is large | Characterization tests on the full engine boot path (integration test: "engine starts, does one eval cycle, one reconcile, one heartbeat, shuts down cleanly"). | MEDIUM — touches `orchestrator.py` massively | Revert merge. Old orchestrator composition is last known good. |
| 7 God class shrink | LOW — deletions only; new paths have been live for 2+ weeks | None new — Phase 3's tests are sufficient | LOW — deletions rarely conflict | Revert merge (large diff but trivially reversible) |
| 8 Infrastructure cleanup | LOW — env-prefix change + file moves | None new | LOW — mostly file moves | Revert merge. Env-var prefix has a 1-week deprecation window that accepts both, so operator machines can roll back env vars independently |

**Risk budget**:
- Phases 0, 1, 2, 8 can ship back-to-back in a single day with zero runtime risk.
- Phase 3 is the single-biggest risk boundary — it must ship during a low-volume window (weekend, no macro calendar events in the next 24h) with active on-call watching.
- Phase 4 should ship at least 48h after Phase 3, so any silent Phase 3 drift has had time to surface.
- Phase 5 can run in parallel with Phase 4 (they touch different files) — but the dual-write observation window adds calendar time, not risk.
- Phase 6 must wait until Phases 3, 4, 5 are production-stable for a week.
- Phase 7 must wait 1 week after Phase 5's deletion step (step 4) — gives time for any subtle WR drift to become statistically visible.

**Merge-conflict risk against the main development stream**: this migration runs on a long-lived feature branch (`claude/docs/ca-clean-architect-migration-plan` for the planning doc; Phase 1+ get their own branches). The hot file is `engine/strategies/five_min_vpin.py`. Any in-flight audit task touching that file (DQ-01, DS-01, LT-03, V10.6 rollout) must coordinate. Strategy: land Phases 0–2 quickly (they don't touch the god class), then schedule Phase 3 during a 1-week feature freeze on the god class.

---

## 8. Testing strategy

The goal is **zero behaviour change** through Phase 7. Tests protect that invariant at three levels: characterization (lock existing behaviour), shadow (parallel-run both paths), and unit (fake ports, test logic in isolation).

### 8.1 Characterization tests (golden master)

Before any phase that mutates hot-path code, pin the existing behaviour to a test fixture. The test harness:

1. Captures 50–100 representative windows from production (mix of TRENDING, TRANSITION, CHOPPY, NORMAL, CASCADE regimes; mix of pass-trade and all-skip outcomes; mix of eval offsets T-60 to T-200).
2. For each window, captures the full input state: CL/TI/BIN deltas, VPIN, regime, CoinGlass snapshot, Gamma prices, eval offset, current price.
3. Runs `_evaluate_window` once and captures the observable outputs: passed/failed, failed_gate name, skip reason, decision direction, confidence tier, stake, SignalEvaluation row.
4. Stores the (input, output) pairs as a JSON fixture in `engine/tests/fixtures/characterization/windows_v1.json`.

Then, for every subsequent phase, a `test_characterization_v1.py` runs the same inputs through whatever the new code path is and asserts the same outputs. Phase 3's extraction and Phase 4's immutable-gate refactor each re-run this suite and must pass.

### 8.2 Parallel-run (shadow-write)

Phase 3 specifically requires a dual-write period where both the legacy `_evaluate_window` body and the new `EvaluateWindowUseCase` run on every window-close event. Strategy:

1. Add a `signal_evaluations_shadow` table with the same schema as `signal_evaluations`.
2. Under feature flag `ENGINE_USE_CLEAN_EVALUATE_WINDOW=shadow`:
   - Legacy path writes to `signal_evaluations` (the real table) as today.
   - New use case writes to `signal_evaluations_shadow`.
3. After each window, a background job diffs the two rows on `(asset, window_ts, eval_offset, passed, failed_gate, skip_reason, direction, confidence)` and flags any mismatch with a Telegram alert.
4. Run for 24h on staging, 48h on production. Target: zero mismatches.
5. Under flag `ENGINE_USE_CLEAN_EVALUATE_WINDOW=true`, only the use case runs and writes to the real table. Legacy body is dead code until Phase 7.

### 8.3 Per-use-case unit tests with fake ports

Steal the pattern from `margin_engine/tests/use_cases/test_open_position_macro_advisory.py`: each port is replaced with a test double (MagicMock for simple deps, hand-rolled fake for state-bearing deps like `WindowStateRepository`). The test hands the use case a concrete input VO and asserts the exact port calls and return value.

Each use case must have:
- At least one test per branch (happy path, each skip-reason, each failure mode)
- 80%+ line coverage
- A test for the "all feeds missing" edge case (returns None, no trade)
- A test for the "already traded" dedup check

### 8.4 Property tests for gate pipeline

Phase 4 adds a property test using `hypothesis`:
- Generator: random `GateContext` field values drawn from realistic ranges
- Invariant 1: **Same input → same decision.** Run the pipeline twice on the same context; assert identical `PipelineResult`.
- Invariant 2: **Deterministic ordering.** Run the pipeline on a shuffled copy of the gate list; assert that ordering dependencies are either enforced by `gate_requires.py` (test passes) or that the decision is unchanged (test passes). Any shuffle that produces a different decision AND is not caught by `gate_requires.py` is a bug (test fails).
- Invariant 3: **Frozen context stays frozen.** Attempting to mutate `GateContext` raises `FrozenInstanceError`. No gate can accidentally regress to mutable mutation.

### 8.5 Schema-migration tests (Phase 5)

The `window_states` table arrives in Phase 5. Before the dual-write step ships, add a test that:
- Applies `migrations/NNN_window_states.sql` to an empty PG
- Inserts a row via the new `PgWindowRepo.mark_traded(...)` call
- Reads it back via `was_traded(...)`
- Asserts idempotency (second `mark_traded` on the same key is a no-op, not an error)

### 8.6 Integration smoke test (Phase 6)

Phase 6's biggest risk is that the composition-root rewrite wires something incorrectly. A smoke test:
1. Launches `engine/infrastructure/main.py` against a test PG + mock Polymarket + paper mode
2. Injects a single fake `WindowClose` event into the feed adapter's queue
3. Asserts: one row written to `signal_evaluations`, one heartbeat row written, no uncaught exceptions
4. Sends SIGTERM; asserts clean shutdown within 5 seconds

---

## 9. Deferred items (NOT in scope)

Items listed here are intentionally excluded from this migration. Each deferred item has a one-line reason. If an item is tracked by another audit task, the task ID is cited.

- **DB schema migration**. Keep `signal_evaluations`, `clob_book_snapshots`, `manual_trades`, `windows`, `trades`, `system_state`, and friends exactly as-is. The only new table is `window_states` in Phase 5, and it's additive (no FK change on existing tables). Reason: the schema has 7+ days of continuous live data with FKs from monitoring and reporting tools; changing it doubles migration risk.
- **Gate logic changes**. The gates in `engine/signals/gates.py` are already clean and are the template for what "good" looks like. Phase 4 only changes their *return type* and their *contract with the pipeline* — the decision math inside each gate is bit-for-bit unchanged. Any gate-logic change (DS-01 activation, V10.6 full grid, DQ-01 Polymarket consensus) ships as its own PR and is not bundled into any Phase 0–8 PR.
- **Config migration (CFG-01)**. The runtime-config DB hot-reload path continues to work as-is. A future CFG-01 effort adds a `ConfigPort` (§4.8) and replaces `os.environ.get(...)` reads inside gates with port calls. Reason: CFG-01 is a separate plan with its own risk profile. This migration would double its surface area.
- **Telegram alerter rewrite**. The existing `TelegramAlerter` class stays. Phase 2 wraps it behind `AlerterPort`; the wrapper is a ~30-LOC delegation shim. Reason: message formatting templates are stable and well-tested.
- **Hardcoded IP `3.98.114.0` for ML model server**. Out of scope — separate item in `docs/CLEAN_ARCHITECTURE_REVIEW.md` §1. Can ship in parallel.
- **Hardcoded wallet address** at `orchestrator.py:~2015`. The fix (read from `settings.poly_funder_address`) can ship at any point in the migration; does not block any phase.
- **Mode-switch hot-flip** (`_heartbeat_loop` at line ~1755). Stays in the orchestrator because it currently mutates `self._poly_client.paper_mode` via attribute assignment — untangling that requires rethinking the `PolymarketClient` port to support "rebuild for new mode", which is a significant refactor and out of scope. Tracked as a follow-up.
- **Sub-dollar-arb and VPIN-cascade strategies**. These live alongside `FiveMinVPINStrategy` in `engine/strategies/`. This migration only touches `five_min_vpin.py`; the other strategies can be migrated later using the same pattern.
- **SQ-01 Sequoia v5.2 rename**. Cosmetic, orthogonal. The 4-PR rollout plan in `docs/AUDIT_PROGRESS.md` can ship before, during, or after this migration as long as none of its PRs touch the same lines as an in-flight CA-migration PR.
- **`DBClient` deletion**. The plan keeps `engine/persistence/db_client.py` as a low-level `asyncpg` wrapper after per-aggregate repositories arrive in `engine/adapters/persistence/`. Following the margin_engine convention: keep the pool helper, add repos next to it. Full `DBClient` deletion is a follow-up if the team wants to go further.

---

## 10. Open questions — decide before Phase 0 begins

These are the design decisions that need a user call before execution starts. My recommendation is included for each; the user's answer should be pasted into this section as each one is resolved.

1. **Do we delete `five_min_vpin.py` or shrink it to a thin adapter?**
   - **Option A**: Shrink to ~350 LOC (keep `FiveMinVPINStrategy` as a thin `BaseStrategy` subclass that delegates to the use case). Preserves the class name in DB-persisted logs and in any external tool that grep-searches for it.
   - **Option B**: Delete `FiveMinVPINStrategy` entirely. `engine/infrastructure/main.py` instantiates the use case directly and the `BaseStrategy` abstraction dies with it.
   - **Recommendation**: Option A. The class name shows up in `structlog` structured logs, in the Execution-HQ Live tab, and potentially in a dozen grep patterns in ops runbooks. Keeping the name as a thin wrapper costs 350 LOC and gives up zero clarity — the god class IS gone, the name is just a label.

2. **What happens to the `_recent_windows` ring buffer if the engine restarts mid-migration?**
   - The ring buffer is currently a process-local `list` of the most recent 50 windows, used by the LT-02 manual-trade fallback as the primary token-id lookup. On restart, it's empty until the first 10 windows come in.
   - **Option A**: The ring buffer moves to `WindowStateRepository` — every lookup is a PG query. Simpler, restart-safe, slightly slower (50-100μs per lookup vs in-memory).
   - **Option B**: The ring buffer stays process-local as an in-memory cache in front of `WindowStateRepository`. Fast but resets on restart; the LT-02 fallback to `market_data` DB kicks in.
   - **Recommendation**: Option A. Manual trades don't fire more than ~10/day; the PG lookup latency is irrelevant at that volume, and "restart-safe" is a genuine win for operator debuggability.

3. **Should the migration happen in `develop` directly, or in a long-running feature branch?**
   - `develop` is deployed to EC2 Montreal on every merge. That means a half-landed migration would be running in production.
   - **Option A**: All 8 phases directly on `develop`, one PR per phase. Zero long-lived branches.
   - **Option B**: Long-lived branch `feature/clean-arch-migration`. All phases land there; periodically rebased onto `develop`. Final merge after Phase 8.
   - **Recommendation**: Option A. Phases 0, 1, 2, 8 are pure-add (zero risk). Phases 3–7 have feature flags protecting them. Long-lived branches accumulate conflicts and make `main.py` parity hard to maintain. Ship incrementally.

4. **Does this migration block DQ-01 / DS-01 / V10.6 activation, or can they ship in parallel?**
   - **DQ-01** (Polymarket spot-only consensus): **shipped in PR #48 (2026-04-11)**, default OFF via `DQ_01_POLY_SPOT_ENABLED`. Lives as a new gate class in `engine/signals/gates.py`, not inside the `_evaluate_window` price-consensus region — so there is **no merge conflict** with Phase 3's extraction. Phase 3 treats it as one more gate in the pipeline; Phase 4's immutable refactor covers it automatically.
   - **DS-01** (V10.6 EvalOffsetBoundsGate): already landed as a gate class in `gates.py`. Activation flag flip is an environment-variable change, not a code change. No conflict with any phase.
   - **V10.6 full grid**: would add per-regime min-p threshold, UP/DOWN penalty asymmetry, proportional sizing, confidence haircut. All of these are gate-logic changes that go into `gates.py` and compose cleanly with Phase 4's immutable-context refactor.
   - **Recommendation**: DQ-01 is already in place — no blocker. DS-01 activation and V10.6 grid can ship in parallel — they're gate-internal changes.

5. **Should Phase 8 add `import-linter` CI enforcement of the dependency rule?**
   - **Option A**: Yes. Add `import-linter.yml` with rules like "`engine/domain/` must not import from `engine/adapters/`". CI gate. Prevents future regressions.
   - **Option B**: No. Code review enforces it. Less CI complexity.
   - **Recommendation**: Option A, but make it the last step of Phase 8 (not a blocker) and scope it to the dependency rule only (don't try to enforce use-case boundaries or adapter-port mappings — those are too subjective for a linter).

6. **Dual-write observation window duration for Phase 5.**
   - How long do we run with `mark_traded` writing to both the in-memory set AND the `window_states` table before flipping readers?
   - **Option A**: 1 week (~2,016 windows per asset × 6 assets = ~12K windows of data). High confidence, slow.
   - **Option B**: 24 hours (~288 windows). Fast, less confidence.
   - **Recommendation**: 48 hours. Splits the difference; catches most once-per-day glitches (cron jobs, nightly reconciliation) without delaying the phase by a full week.

7. **Test framework for new unit tests.**
   - `margin_engine/tests/` uses pytest + `unittest.mock.AsyncMock` / `MagicMock`. `engine/tests/` has a mix of pytest and a few plain `unittest` files.
   - **Recommendation**: all new tests are pytest, mirroring the margin_engine convention. Existing `unittest`-style files stay as-is; new tests do not add to that mix.

8. **Phase numbering vs task spec.**
   - The task spec describes 8 phases numbered 0 through 8 (9 total including Phase 0). This document uses that exact numbering. If the user prefers 1-indexed phases (Phase 1 = ports, etc.) to match existing audit-checklist conventions, a global rename is trivial before Phase 0 starts.
   - **Recommendation**: keep 0-indexed as written.

9. **Does Phase 8's `ENGINE_` env-prefix standardisation break deploy automation?**
   - The engine deploy workflow (`.github/workflows/deploy-engine.yml`) templates env vars into the EC2 host's `.env` file. Any prefix change must either: (a) update the workflow in the same PR, with both old and new prefixes accepted for the 1-week deprecation window; or (b) defer the prefix standardisation to a separate PR after Phase 8.
   - **Recommendation**: bundle the workflow update into the Phase 8 PR and ship with both prefixes accepted. Remove the old prefix acceptance 1 week later in a cleanup PR.

10. **Who owns `_execute_trade`'s FOK/CLOB retry logic post-shrink?**
    - `FiveMinVPINStrategy._execute_trade` is 482 LOC of FOK ladder + CLOB retry + Telegram alerts + OM callbacks. Phase 7 deletes it, but the logic has to go somewhere.
    - **Option A**: New use case `engine/use_cases/place_polymarket_order.py`. The retry policy is domain logic (we decide when to retry based on market conditions); belongs in a use case.
    - **Option B**: `engine/adapters/polymarket/fok_ladder.py`. The retry policy is infrastructure (we decide how to talk to the exchange); belongs in an adapter.
    - **Recommendation**: Option A. The retry decision reads circuit-breaker state and risk-manager state — those are domain concerns. The adapter just executes one FOK attempt.

---

**End of plan.** Phase 0 is the one-file, zero-runtime-change opening move. Ready to execute once the §10 questions are resolved with the user.
