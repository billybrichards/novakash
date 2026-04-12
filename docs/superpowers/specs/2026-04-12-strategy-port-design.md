# Strategy Port Design -- Pluggable Multi-Strategy Architecture

**Date:** 2026-04-12
**Author:** Claude Opus 4.6
**Status:** DRAFT
**Audit IDs:** SP-01 (StrategyPort), SP-02 (V10 Adapter), SP-03 (V4 Adapter), SP-04 (EvaluateStrategies UC), SP-05 (Persistence)

---

## 1. Current State Analysis

### What exists today

The engine has a single monolithic strategy path:

```
Orchestrator._on_five_min_window()
  -> _execution_queue.put(window)
  -> _process_execution_queue()
    -> FiveMinVPINStrategy.evaluate_window(window, state)
      -> _evaluate_window(window, state)
        -> [if ENGINE_USE_CLEAN_EVALUATE_WINDOW] EvaluateWindowUseCase.execute()
        -> [else] inline 600-line evaluation with gate pipeline
      -> _execute_trade(state, signal)
```

### Violations

1. **Single-strategy assumption** -- `FiveMinVPINStrategy` is hardwired in the orchestrator. There is no abstraction for "a strategy that evaluates a window and returns a decision."
2. **Evaluation and execution are coupled** -- `_evaluate_window` decides whether to trade, then immediately calls `_execute_trade`. There is no separation between "what would this strategy do?" and "actually do it."
3. **No ghost/shadow path** -- Every evaluation either trades or skips. No recording of "this strategy would have traded X but was in GHOST mode."
4. **The `BaseStrategy` ABC is not used by the window path** -- `FiveMinVPINStrategy.evaluate_window()` bypasses the `evaluate(state) -> execute(state, signal)` flow from `BaseStrategy`. The base class's `on_market_state` hook is a dead path for the 5-min strategy.
5. **`EvaluateWindowUseCase` (PR #103) is a partial extraction** -- It extracted the evaluation logic but still lives inside the single-strategy assumption. It returns `EvaluateWindowResult` with `signal: Optional[FiveMinSignal]`, which is V10-specific.

### What we can reuse

- **`GatePipeline` + `GateContext` + 8 gates** (`engine/signals/gates.py`) -- Production-tested, immutable context support (CA-03). This becomes the V10 strategy's internal evaluator.
- **`EvaluateWindowUseCase`** (PR #103) -- The inner evaluation loop. We will refactor this to become the V10 adapter's implementation rather than a standalone use case.
- **Domain ports** (`engine/domain/ports.py`) -- 12 ports already defined. We add `StrategyPort` as #13.
- **Value objects** (`engine/domain/value_objects.py`) -- 22 VOs. We add `StrategyDecision`, `StrategyContext`, `StrategyRegistration`.

---

## 2. Port Protocol Definition

### 2.1 StrategyPort

```python
# engine/domain/ports.py  (addition)

class StrategyPort(abc.ABC):
    """Evaluates a window and returns a structured decision.

    Each implementation encapsulates one trading strategy's decision
    logic.  The port is PURELY EVALUATIVE -- it never places orders.
    Execution is the caller's responsibility (EvaluateStrategiesUseCase).

    Implementations:
      - V10GateStrategy   (wraps GatePipeline from signals/gates.py)
      - V4FusionStrategy  (wraps /v4/snapshot from timesfm service)
    """

    @property
    @abc.abstractmethod
    def strategy_id(self) -> str:
        """Unique identifier, e.g. 'v10_gate', 'v4_fusion'."""
        ...

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Semantic version string for audit trail, e.g. '10.5.3'."""
        ...

    @abc.abstractmethod
    async def evaluate(
        self,
        ctx: "StrategyContext",
    ) -> "StrategyDecision":
        """Evaluate the window and return a decision.

        MUST be side-effect-free (no DB writes, no HTTP calls that
        mutate state).  Network reads (fetching V4 snapshot) are
        allowed because they are idempotent.

        MUST NOT raise -- implementation swallows all exceptions and
        returns a StrategyDecision with action='ERROR' and the
        exception message in skip_reason.

        Timeout: caller enforces a 5-second asyncio.wait_for around
        this call.  If the strategy needs longer (V4 HTTP), it should
        use its own internal timeout and return ERROR on timeout.
        """
        ...
```

### 2.2 V4SnapshotPort

```python
# engine/domain/ports.py  (addition)

class V4SnapshotPort(abc.ABC):
    """Fetches a V4 fusion snapshot from the timesfm service.

    Separated from StrategyPort because multiple strategies or analysis
    tools might consume V4 data.  The adapter wraps HTTP to /v4/snapshot.
    """

    @abc.abstractmethod
    async def get_snapshot(
        self,
        asset: str,
        timescale: str,
    ) -> Optional["V4Snapshot"]:
        """Fetch the latest V4 snapshot for (asset, timescale).

        Returns None on timeout, HTTP error, or missing data.
        MUST NOT raise.
        """
        ...
```

### 2.3 StrategyDecisionRepository

```python
# engine/domain/ports.py  (addition)

class StrategyDecisionRepository(abc.ABC):
    """Persists strategy decisions for the Strategy Lab.

    One row per (strategy_id, window_key, eval_offset) tuple.
    Both LIVE and GHOST decisions are written.
    """

    @abc.abstractmethod
    async def write_decision(self, decision: "StrategyDecisionRecord") -> None:
        """Persist one strategy decision row.

        Idempotent by (strategy_id, asset, window_ts, eval_offset).
        """
        ...

    @abc.abstractmethod
    async def get_decisions_for_window(
        self,
        asset: str,
        window_ts: int,
    ) -> list["StrategyDecisionRecord"]:
        """Read all strategy decisions for a window (for Strategy Lab)."""
        ...
```

---

## 3. Value Object Definitions

### 3.1 StrategyContext (input to all strategies)

```python
# engine/domain/value_objects.py  (addition)

@dataclass(frozen=True)
class StrategyContext:
    """Immutable snapshot of all data a strategy might need.

    Constructed once per (window, eval_offset) by the
    EvaluateStrategiesUseCase.  Strategies pick the fields they need;
    unused fields are None.  This is the SINGLE input contract --
    strategies never reach outside this object for data.
    """
    # Identity
    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]

    # Price deltas (from ConsensusPricePort)
    delta_chainlink: Optional[float]
    delta_tiingo: Optional[float]
    delta_binance: Optional[float]
    delta_pct: float                    # Primary delta (source-selected)
    delta_source: str                   # "tiingo_rest_candle" | "chainlink" | etc.

    # Market state
    current_price: float
    open_price: float
    vpin: float
    regime: str                         # "CALM" | "NORMAL" | "TRANSITION" | "CASCADE"

    # CoinGlass
    cg_snapshot: Optional[object]       # CoinGlassEnhancedFeed.snapshot

    # TWAP
    twap_delta: Optional[float]

    # Tiingo detail
    tiingo_close: Optional[float]

    # Gamma / CLOB prices
    gamma_up_price: Optional[float]
    gamma_down_price: Optional[float]
    clob_up_bid: Optional[float]
    clob_up_ask: Optional[float]
    clob_down_bid: Optional[float]
    clob_down_ask: Optional[float]

    # V4 fusion snapshot (populated by V4SnapshotPort before strategies run)
    v4_snapshot: Optional["V4Snapshot"] = None

    # Prior DUNE probability (for v2_logit feature)
    prev_dune_probability_up: Optional[float] = None
```

### 3.2 StrategyDecision (output from all strategies)

```python
@dataclass(frozen=True)
class StrategyDecision:
    """What a strategy decided for this window evaluation.

    Every strategy returns exactly one of these per evaluate() call.
    The use case uses `action` to determine what happens next.
    """
    action: str                         # "TRADE" | "SKIP" | "ERROR"
    direction: Optional[str]            # "UP" | "DOWN" | None (if SKIP/ERROR)
    confidence: Optional[str]           # "DECISIVE" | "HIGH" | "MODERATE" | "LOW" | None
    confidence_score: Optional[float]   # 0.0-1.0 numeric confidence

    # Entry pricing
    entry_cap: Optional[float]          # Max acceptable CLOB price (e.g. 0.65)
    collateral_pct: Optional[float]     # Fraction of bankroll to risk (V4 sizing)

    # Audit trail
    strategy_id: str
    strategy_version: str
    entry_reason: str                   # Human-readable, e.g. "v10_DUNE_TRANSITION_T120_FAK"
    skip_reason: Optional[str]          # Why SKIP/ERROR, None if TRADE

    # Strategy-specific metadata (JSON-serializable dict)
    metadata: dict = field(default_factory=dict)
    # Examples:
    #   V10: {"gate_results": [...], "dune_p": 0.72, "spread_pct": 0.08}
    #   V4:  {"probability_up": 0.68, "conviction": "HIGH", "regime": "calm_trend",
    #          "recommended_action": {...}, "sub_signals": {...}}
```

### 3.3 V4Snapshot

```python
@dataclass(frozen=True)
class V4Snapshot:
    """Parsed response from /v4/snapshot endpoint.

    Immutable -- V4FusionStrategy reads fields but never mutates.
    """
    probability_up: float
    conviction: str                     # "NONE" | "LOW" | "MEDIUM" | "HIGH"
    conviction_score: float             # 0.0-1.0
    regime: str                         # "calm_trend" | "volatile_trend" | "chop" | "risk_off"
    regime_confidence: float
    regime_persistence: float
    regime_transition: Optional[dict]   # Transition probability matrix

    # Recommended action
    recommended_side: Optional[str]     # "UP" | "DOWN" | None
    recommended_collateral_pct: Optional[float]
    recommended_sl_pct: Optional[float]
    recommended_tp_pct: Optional[float]
    recommended_reason: Optional[str]
    recommended_conviction_score: Optional[float]

    # Sub-signals
    sub_signals: dict                   # 7 sub-signal values
    consensus: dict                     # 6 source consensus + safe_to_trade
    macro: dict                         # Qwen/LightGBM bias, direction_gate, size_modifier
    quantiles: dict                     # p10-p90

    # Metadata
    timescale: str                      # "5m" | "15m"
    timestamp: float                    # When snapshot was generated
```

### 3.4 StrategyRegistration

```python
@dataclass(frozen=True)
class StrategyRegistration:
    """Runtime config for a registered strategy.

    The orchestrator holds a list of these.  Exactly one has
    mode='LIVE'; the rest are 'GHOST'.  Switchable at runtime
    via ConfigPort or env var.
    """
    strategy_id: str
    mode: str                           # "LIVE" | "GHOST"
    enabled: bool                       # False = don't even evaluate
    priority: int                       # Tie-breaking for display order
```

### 3.5 StrategyDecisionRecord (persistence)

```python
@dataclass(frozen=True)
class StrategyDecisionRecord:
    """One row in the strategy_decisions table.

    Written for EVERY evaluation (TRADE, SKIP, ERROR) of EVERY
    enabled strategy (LIVE and GHOST).  This is the Strategy Lab's
    source of truth.
    """
    # Identity
    strategy_id: str
    strategy_version: str
    asset: str
    window_ts: int
    timeframe: str
    eval_offset: Optional[int]
    mode: str                           # "LIVE" | "GHOST"

    # Decision
    action: str                         # "TRADE" | "SKIP" | "ERROR"
    direction: Optional[str]
    confidence: Optional[str]
    confidence_score: Optional[float]
    entry_cap: Optional[float]
    collateral_pct: Optional[float]
    entry_reason: str
    skip_reason: Optional[str]

    # Execution outcome (filled ONLY for LIVE + TRADE, after execution)
    executed: bool = False
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None

    # Audit
    metadata_json: str = "{}"           # JSON-serialized strategy metadata
    evaluated_at: float = 0.0           # Unix epoch
```

---

## 4. Use Case: EvaluateStrategiesUseCase

### 4.1 Signature

```python
# engine/use_cases/evaluate_strategies.py

class EvaluateStrategiesUseCase:
    """Runs ALL registered strategies for a window evaluation.

    Replaces the single-strategy path in the orchestrator's
    _process_execution_queue.

    Flow:
      1. Build StrategyContext from window + market state + feeds
      2. Run each enabled strategy in parallel (asyncio.gather)
      3. Record ALL decisions (LIVE + GHOST) to StrategyDecisionRepository
      4. Return the LIVE strategy's decision for execution
    """

    def __init__(
        self,
        *,
        strategies: list[tuple[StrategyRegistration, StrategyPort]],
        consensus_price: ConsensusPricePort,
        signal_repo: SignalRepository,
        decision_repo: StrategyDecisionRepository,
        window_state: WindowStateRepository,
        clock: Clock,
        # Data sources for building StrategyContext
        vpin_calculator,            # VPINCalculator instance
        cg_feeds: dict,             # Per-asset CoinGlass feeds
        twap_tracker,               # TWAPTracker instance
        v4_snapshot_port: Optional[V4SnapshotPort],
        db_client,                  # For CLOB prices, macro signal
    ):
        ...

    async def execute(
        self,
        window: WindowInfo,
        state: MarketState,
    ) -> "EvaluateStrategiesResult":
        """
        Returns:
          EvaluateStrategiesResult with:
            - live_decision: Optional[StrategyDecision]  (the LIVE strategy's output)
            - all_decisions: list[StrategyDecision]       (all strategies, for audit)
            - context: StrategyContext                     (the shared input)
        """
        ...
```

### 4.2 Flow Diagram

```
Window signal arrives (T-offset)
        |
        v
EvaluateStrategiesUseCase.execute(window, state)
        |
        v
  [1] Check WindowStateRepository.was_traded(key)
      If yes -> return SKIP for all strategies
        |
        v
  [2] Build StrategyContext
      - ConsensusPricePort.get_deltas() -> DeltaSet
      - VPIN from calculator
      - CoinGlass snapshot
      - TWAP result
      - CLOB prices from DB
      - V4SnapshotPort.get_snapshot() (if V4 strategy enabled)
        |
        v
  [3] Fan out to all enabled strategies (asyncio.gather with timeout)
      +--> V10GateStrategy.evaluate(ctx) ---> StrategyDecision
      +--> V4FusionStrategy.evaluate(ctx) --> StrategyDecision
      +--> [future strategies...]
        |
        v
  [4] Record ALL decisions to StrategyDecisionRepository
      (parallel writes, fire-and-forget with error logging)
        |
        v
  [5] Find the LIVE strategy's decision
      - If action == "TRADE": return it for execution
      - If action == "SKIP"/"ERROR": return None
        |
        v
  [6] Also write legacy signal_evaluations + gate_audit
      (backward compat with existing dashboard)
        |
        v
  Return EvaluateStrategiesResult
```

### 4.3 Result Type

```python
@dataclass
class EvaluateStrategiesResult:
    """Output of EvaluateStrategiesUseCase.execute()."""
    live_decision: Optional[StrategyDecision]   # None if LIVE strategy skipped
    all_decisions: list[StrategyDecision]        # All strategies' outputs
    context: StrategyContext                     # Shared input (for audit)
    window_key: str                             # "{asset}-{window_ts}"
    already_traded: bool                        # True if was_traded check hit
```

---

## 5. Strategy Adapter Implementations

### 5.1 V10GateStrategy (wraps existing gate pipeline)

```
File: engine/adapters/strategies/v10_gate_strategy.py
```

**Design principle:** This is a THIN ADAPTER. It translates `StrategyContext` into `GateContext`, calls the existing `GatePipeline`, and translates `PipelineResult` into `StrategyDecision`. Zero business logic rewrite.

```python
class V10GateStrategy:
    """StrategyPort implementation wrapping the V10 gate pipeline.

    Maps StrategyContext -> GateContext -> GatePipeline -> PipelineResult -> StrategyDecision.
    """
    strategy_id = "v10_gate"
    version = "10.5.3"

    def __init__(self, *, dune_client=None):
        self._pipeline = GatePipeline([
            EvalOffsetBoundsGate(),
            SourceAgreementGate(),
            DeltaMagnitudeGate(),
            TakerFlowGate(),
            CGConfirmationGate(),
            DuneConfidenceGate(dune_client=dune_client),
            SpreadGate(),
            DynamicCapGate(),
        ])

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        # 1. Map StrategyContext -> GateContext
        gate_ctx = self._build_gate_context(ctx)

        # 2. Run pipeline
        result: PipelineResult = await self._pipeline.evaluate(gate_ctx)

        # 3. Map PipelineResult -> StrategyDecision
        if result.passed:
            return StrategyDecision(
                action="TRADE",
                direction=result.direction,
                confidence=self._classify_confidence(gate_ctx, result),
                confidence_score=result.dune_p,
                entry_cap=result.cap or 0.65,
                collateral_pct=None,  # V10 uses fixed Kelly sizing
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                entry_reason=self._build_entry_reason(gate_ctx, result),
                skip_reason=None,
                metadata={
                    "gate_results": [...],
                    "dune_p": result.dune_p,
                    "cg_modifier": gate_ctx.cg_threshold_modifier,
                },
            )
        else:
            return StrategyDecision(
                action="SKIP",
                direction=None,
                confidence=None,
                confidence_score=None,
                entry_cap=None,
                collateral_pct=None,
                strategy_id=self.strategy_id,
                strategy_version=self.version,
                entry_reason="",
                skip_reason=result.skip_reason or result.failed_gate or "v10 gate failed",
                metadata={"failed_gate": result.failed_gate},
            )

    def _build_gate_context(self, ctx: StrategyContext) -> GateContext:
        """Direct field mapping -- no logic, just translation."""
        from signals.v2_feature_body import build_v5_feature_body
        v5 = build_v5_feature_body(
            eval_offset=ctx.eval_offset,
            vpin=ctx.vpin,
            delta_pct=ctx.delta_pct,
            twap_delta=ctx.twap_delta,
            binance_price=ctx.current_price,
            tiingo_close=ctx.tiingo_close,
            delta_binance=ctx.delta_binance,
            delta_chainlink=ctx.delta_chainlink,
            delta_tiingo=ctx.delta_tiingo,
            regime=ctx.regime,
            delta_source=ctx.delta_source,
            prev_v2_probability_up=ctx.prev_dune_probability_up,
        )
        return GateContext(
            delta_chainlink=ctx.delta_chainlink,
            delta_tiingo=ctx.delta_tiingo,
            delta_binance=ctx.delta_binance,
            delta_pct=ctx.delta_pct,
            vpin=ctx.vpin,
            regime=ctx.regime,
            asset=ctx.asset,
            eval_offset=ctx.eval_offset,
            window_ts=ctx.window_ts,
            cg_snapshot=ctx.cg_snapshot,
            twap_delta=ctx.twap_delta,
            tiingo_close=ctx.tiingo_close,
            current_price=ctx.current_price,
            delta_source=ctx.delta_source,
            prev_v2_probability_up=ctx.prev_dune_probability_up,
            v5_features=v5,
        )
```

**Key point:** The 8 gates in `signals/gates.py` are NOT touched. The adapter delegates to them exactly as the current `EvaluateWindowUseCase._run_v10_pipeline()` does. This is a structural refactor, not a behavioral one.

### 5.2 V4FusionStrategy

```
File: engine/adapters/strategies/v4_fusion_strategy.py
```

```python
class V4FusionStrategy:
    """StrategyPort implementation using the V4 fusion surface.

    Consumes V4Snapshot (pre-fetched and attached to StrategyContext.v4_snapshot)
    and applies conviction + regime rules to decide trade/skip.

    Dynamic entry timing: The EvaluateStrategiesUseCase calls this at every
    eval_offset (T-180 to T-5).  The strategy itself decides whether conditions
    at this offset are good enough to trade.  It can return SKIP at T-180 and
    TRADE at T-120 if conditions improve.  The dedup in
    WindowStateRepository.was_traded() prevents double execution.
    """
    strategy_id = "v4_fusion"
    version = "4.0.0"

    # Conviction -> minimum probability_up distance from 0.5
    _CONVICTION_THRESHOLDS = {
        "HIGH":   0.12,    # P(UP) >= 0.62 or <= 0.38
        "MEDIUM": 0.15,    # P(UP) >= 0.65 or <= 0.35
        "LOW":    0.20,    # P(UP) >= 0.70 or <= 0.30
        "NONE":   1.0,     # Never trade
    }

    # Regime gating: which regimes are tradeable
    _TRADEABLE_REGIMES = {"calm_trend", "volatile_trend"}

    async def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        snap = ctx.v4_snapshot
        if snap is None:
            return self._error("v4_snapshot_missing")

        # Gate 1: Regime must be tradeable
        if snap.regime not in self._TRADEABLE_REGIMES:
            return self._skip(f"regime={snap.regime} not tradeable")

        # Gate 2: Consensus safe_to_trade
        if not snap.consensus.get("safe_to_trade", False):
            return self._skip("consensus not safe_to_trade")

        # Gate 3: Conviction threshold
        p_up = snap.probability_up
        distance = abs(p_up - 0.5)
        min_distance = self._CONVICTION_THRESHOLDS.get(snap.conviction, 1.0)
        if distance < min_distance:
            return self._skip(
                f"conviction={snap.conviction} requires distance={min_distance:.2f}, "
                f"got {distance:.2f} (p_up={p_up:.3f})"
            )

        # Gate 4: Direction from recommended_action
        direction = snap.recommended_side
        if direction is None:
            direction = "UP" if p_up > 0.5 else "DOWN"

        # Gate 5: Macro direction_gate
        macro_gate = snap.macro.get("direction_gate")
        if macro_gate is not None and macro_gate != direction:
            return self._skip(f"macro direction_gate={macro_gate} vs {direction}")

        # Sizing from V4 recommendation
        collateral_pct = snap.recommended_collateral_pct
        if collateral_pct is not None:
            # Scale by conviction
            size_modifier = snap.macro.get("size_modifier", 1.0)
            collateral_pct = collateral_pct * size_modifier

        return StrategyDecision(
            action="TRADE",
            direction=direction,
            confidence=snap.conviction,
            confidence_score=snap.conviction_score,
            entry_cap=None,             # V4 uses its own sizing, not V10 caps
            collateral_pct=collateral_pct,
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            entry_reason=self._build_reason(snap, ctx),
            skip_reason=None,
            metadata={
                "probability_up": p_up,
                "conviction": snap.conviction,
                "conviction_score": snap.conviction_score,
                "regime": snap.regime,
                "regime_confidence": snap.regime_confidence,
                "recommended_action": {
                    "side": snap.recommended_side,
                    "collateral_pct": snap.recommended_collateral_pct,
                    "sl_pct": snap.recommended_sl_pct,
                    "tp_pct": snap.recommended_tp_pct,
                    "reason": snap.recommended_reason,
                },
                "sub_signals": snap.sub_signals,
                "macro": snap.macro,
                "quantiles": snap.quantiles,
            },
        )
```

### 5.3 V4SnapshotAdapter

```
File: engine/adapters/external/v4_snapshot_adapter.py
```

```python
class V4SnapshotHttpAdapter:
    """V4SnapshotPort implementation -- HTTP client for /v4/snapshot.

    Timeout: 3 seconds (V4 service runs on the same AWS region).
    Caching: None -- each call is fresh.  The service caches internally.
    """
    async def get_snapshot(self, asset: str, timescale: str) -> Optional[V4Snapshot]:
        # GET {base_url}/v4/snapshot?asset={asset}&timescale={timescale}
        # Parse response into V4Snapshot VO
        # Return None on any error
        ...
```

---

## 6. Persistence: strategy_decisions Table

### 6.1 Schema

```sql
CREATE TABLE IF NOT EXISTS strategy_decisions (
    id              BIGSERIAL PRIMARY KEY,
    strategy_id     TEXT NOT NULL,           -- 'v10_gate', 'v4_fusion'
    strategy_version TEXT NOT NULL,          -- '10.5.3'
    asset           TEXT NOT NULL,           -- 'BTC'
    window_ts       BIGINT NOT NULL,         -- Unix epoch of window open
    timeframe       TEXT NOT NULL DEFAULT '5m',
    eval_offset     INTEGER,                 -- Seconds to close at evaluation time
    mode            TEXT NOT NULL,           -- 'LIVE' | 'GHOST'

    -- Decision
    action          TEXT NOT NULL,           -- 'TRADE' | 'SKIP' | 'ERROR'
    direction       TEXT,                    -- 'UP' | 'DOWN'
    confidence      TEXT,
    confidence_score DOUBLE PRECISION,
    entry_cap       DOUBLE PRECISION,
    collateral_pct  DOUBLE PRECISION,
    entry_reason    TEXT NOT NULL DEFAULT '',
    skip_reason     TEXT,

    -- Execution (filled post-trade for LIVE+TRADE only)
    executed        BOOLEAN NOT NULL DEFAULT false,
    order_id        TEXT,
    fill_price      DOUBLE PRECISION,
    fill_size       DOUBLE PRECISION,

    -- Audit
    metadata_json   JSONB NOT NULL DEFAULT '{}',
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Dedup
    UNIQUE (strategy_id, asset, window_ts, eval_offset)
);

CREATE INDEX idx_sd_window ON strategy_decisions (asset, window_ts);
CREATE INDEX idx_sd_strategy ON strategy_decisions (strategy_id, evaluated_at);
```

### 6.2 Why a new table (not extending signal_evaluations)?

1. **signal_evaluations** is V10-specific -- columns like `gate_passed`, `gate_failed`, `v2_probability_up`, `v2_direction` are V10 concepts.
2. **strategy_decisions** is strategy-agnostic -- the `metadata_json` JSONB column holds strategy-specific data without schema changes per strategy.
3. **The existing signal_evaluations table continues to be written** by V10GateStrategy for backward compat with the existing dashboard.  The new table is the Strategy Lab's source of truth.

---

## 7. Integration with Existing Code

### 7.1 Orchestrator Changes

The orchestrator's `_process_execution_queue` currently calls:
```python
await self._five_min_strategy.evaluate_window(w, state)
```

After this design, it becomes:
```python
result = await self._evaluate_strategies_uc.execute(w, state)
if result.live_decision and result.live_decision.action == "TRADE":
    await self._execute_trade(state, result.live_decision, result.context)
```

The `_execute_trade` method is extracted from `FiveMinVPINStrategy._execute_trade()` and moved to the orchestrator (or a new `ExecuteStrategyTradeUseCase`).  It takes a `StrategyDecision` instead of a `FiveMinSignal`.

### 7.2 Existing EvaluateWindowUseCase Fate

`EvaluateWindowUseCase` (PR #103) has two concerns mixed together:
1. **Context building** -- delta calculation, Tiingo/Chainlink fetching, VPIN, regime classification
2. **V10 gate evaluation** -- `_run_v10_pipeline()`, CLOB price gates

Under this design:
- Concern 1 moves into `EvaluateStrategiesUseCase._build_context()` (shared by all strategies)
- Concern 2 moves into `V10GateStrategy.evaluate()` (adapter)
- `EvaluateWindowUseCase` itself is **retired** -- the feature flag `ENGINE_USE_CLEAN_EVALUATE_WINDOW` is no longer needed because the new use case subsumes it

### 7.3 FiveMinVPINStrategy Fate

The god class `FiveMinVPINStrategy` (2900+ lines) is progressively hollowed:
- `_evaluate_window()` -> replaced by `EvaluateStrategiesUseCase`
- `_run_v10_pipeline()` -> replaced by `V10GateStrategy`
- `_execute_trade()` -> extracted to orchestrator or dedicated use case
- Dedup (`_traded_windows`) -> replaced by `WindowStateRepository`

The class remains as a shell during migration (delegates to the new use case when the feature flag is on), then is deleted.

### 7.4 Execution Path for V4 Sizing

V10 uses fixed Kelly sizing (`BET_FRACTION * bankroll`).  V4 provides `collateral_pct` from its recommendation engine.

The execution path checks `StrategyDecision.collateral_pct`:
- If `None` -> use V10 Kelly sizing (existing `_calculate_stake`)
- If set -> use `collateral_pct * current_bankroll` as stake

This means the RiskManager still approves the final stake -- the strategy only *recommends* sizing.

---

## 8. File Structure

### New files to create

```
engine/
  domain/
    ports.py                            # ADD: StrategyPort, V4SnapshotPort, StrategyDecisionRepository
    value_objects.py                     # ADD: StrategyContext, StrategyDecision, V4Snapshot,
                                        #       StrategyRegistration, StrategyDecisionRecord,
                                        #       EvaluateStrategiesResult
  use_cases/
    evaluate_strategies.py              # NEW: EvaluateStrategiesUseCase (SP-04)
  adapters/
    strategies/
      __init__.py                       # NEW
      v10_gate_strategy.py              # NEW: V10GateStrategy adapter (SP-02)
      v4_fusion_strategy.py             # NEW: V4FusionStrategy adapter (SP-03)
    external/
      v4_snapshot_adapter.py            # NEW: V4SnapshotHttpAdapter
    persistence/
      pg_strategy_decision_repo.py      # NEW: PostgreSQL StrategyDecisionRepository (SP-05)
```

### Files to modify

```
engine/
  domain/
    ports.py                            # Add 3 new port protocols
    value_objects.py                     # Add 6 new VOs
  strategies/
    orchestrator.py                     # Wire EvaluateStrategiesUseCase, strategy registry
    five_min_vpin.py                    # Feature flag: delegate to new use case
  use_cases/
    evaluate_window.py                  # Eventually retired (context-building extracted)
```

### Files NOT modified

```
engine/signals/gates.py                 # Untouched -- V10GateStrategy wraps it
engine/signals/v2_feature_body.py       # Untouched -- called from V10GateStrategy adapter
engine/adapters/persistence/pg_window_repo.py  # Untouched
engine/adapters/polymarket/*            # Untouched
```

---

## 9. Migration Path

### Phase A: Foundation (SP-01) -- Port + VOs

1. Add `StrategyPort`, `V4SnapshotPort`, `StrategyDecisionRepository` to `ports.py`
2. Add all new VOs to `value_objects.py`
3. Create `strategy_decisions` migration
4. **Zero behavior change.** No wiring, no feature flags.

### Phase B: V10 Adapter (SP-02)

1. Create `V10GateStrategy` in `adapters/strategies/`
2. Unit test: given a `StrategyContext`, verify it produces identical `StrategyDecision` to what the current pipeline would produce
3. **Zero behavior change.** Adapter exists but is not wired.

### Phase C: V4 Adapter + Snapshot Port (SP-03)

1. Create `V4SnapshotHttpAdapter`
2. Create `V4FusionStrategy`
3. Unit test with mocked V4 snapshot responses
4. **Zero behavior change.**

### Phase D: Use Case + Wiring (SP-04)

1. Create `EvaluateStrategiesUseCase`
2. Extract context-building from `EvaluateWindowUseCase` into the new use case
3. Wire into orchestrator behind feature flag: `ENGINE_USE_STRATEGY_PORT=true`
4. Strategy registry:
   ```python
   strategies = [
       (StrategyRegistration("v10_gate", mode="LIVE", enabled=True, priority=1), v10_strategy),
       (StrategyRegistration("v4_fusion", mode="GHOST", enabled=True, priority=2), v4_strategy),
   ]
   ```
5. **Feature flag OFF = zero behavior change.** Flag ON = new path runs.

### Phase E: Persistence Adapter (SP-05)

1. Create `PgStrategyDecisionRepository`
2. Wire into `EvaluateStrategiesUseCase`
3. Strategy Lab frontend reads from `strategy_decisions` table
4. **Additive only -- writes new table, reads old tables unchanged.**

### Phase F: Shadow Validation

1. Turn on `ENGINE_USE_STRATEGY_PORT=true` on Montreal
2. V10 runs as LIVE, V4 runs as GHOST
3. Verify:
   - V10 LIVE decisions match legacy path exactly (diff signal_evaluations vs strategy_decisions)
   - V4 GHOST decisions are recorded correctly
   - No performance regression (add timing to use case)
4. Run for 48h minimum before considering V4 LIVE

### Phase G: V4 Promotion (future)

1. Flip V4 to LIVE, V10 to GHOST via config change
2. Monitor Strategy Lab for 24h
3. If V4 underperforms, flip back (instant rollback, no deploy needed)

---

## 10. Feature Flag Strategy

| Flag | Default | Purpose |
|------|---------|---------|
| `ENGINE_USE_STRATEGY_PORT` | `false` | Master switch: use EvaluateStrategiesUseCase instead of legacy path |
| `V4_FUSION_ENABLED` | `false` | Enable V4FusionStrategy (GHOST by default) |
| `V4_FUSION_MODE` | `GHOST` | `GHOST` or `LIVE` -- which mode V4 runs in |
| `V10_GATE_MODE` | `LIVE` | `GHOST` or `LIVE` -- which mode V10 runs in |
| `STRATEGY_DECISION_WRITES` | `true` | Write to strategy_decisions table (disable to reduce DB load during testing) |

**Rollback path:** Set `ENGINE_USE_STRATEGY_PORT=false` and restart. The legacy `FiveMinVPINStrategy._evaluate_window()` takes over immediately. Zero code changes needed.

**Promotion path:** Set `V4_FUSION_MODE=LIVE` + `V10_GATE_MODE=GHOST`. Both strategies still evaluate every window; only the execution target changes.

---

## 11. Open Questions

1. **V4 snapshot caching** -- Should we cache V4 snapshots for a window across eval_offsets (T-180 to T-5), or fetch fresh each time? Fresh gives better data but costs 6-10 HTTP calls per window. Recommendation: fetch fresh, with 5s TTL cache to collapse rapid re-evaluations.

2. **Multi-asset V4** -- The V4 service currently serves BTC only. When ETH/SOL are added, the `V4SnapshotPort.get_snapshot(asset, timescale)` signature already supports it.

3. **Execution adapter for V4 sizing** -- V4 provides `collateral_pct` and `sl_pct/tp_pct`. The current execution path does not support stop-loss or take-profit on Polymarket (binary outcome markets). These fields are recorded in metadata for analysis but not acted on initially.

4. **Backward compatibility of Telegram alerts** -- The existing alert pipeline reads `FiveMinSignal` fields. Need to create a `StrategyDecision -> FiveMinSignal` bridge or update the alert formatting to read `StrategyDecision` directly.

5. **Strategy Lab frontend** -- What queries does the frontend need? Suggested: `GET /api/strategy-lab/decisions?window_ts=X` returns all strategy decisions for a window, and `GET /api/strategy-lab/comparison?start=X&end=Y` returns aggregated win rates per strategy over a time range.
