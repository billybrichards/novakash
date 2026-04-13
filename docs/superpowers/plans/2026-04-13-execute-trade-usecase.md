# ExecuteTradeUseCase -- Clean Architecture Design

**Date:** 2026-04-13
**Status:** Design
**Audit item:** SP-06 / CA-01 Phase 4
**Branch:** clean-arch-polymarket

---

## 1. Current Execution Path Analysis

### What lives in `five_min_vpin.py::_execute_trade` (lines 3178-3714, ~536 LOC)

The god-class method does **nine distinct responsibilities** in a single function:

| # | Responsibility | Lines | Coupling |
|---|---|---|---|
| 1 | **Stake calculation** | 3191 | `_calculate_stake()` reads `_rm.get_status()`, `runtime.bet_fraction`, env vars |
| 2 | **Risk check** | 3194-3219 | `_check_risk()` hits risk manager + alerter |
| 3 | **Token ID selection** | 3222-3231 | Maps UP/DOWN to YES/NO + window token IDs |
| 4 | **Guardrails** (geoblock, circuit breaker, rate limit) | 3274-3285 | Mutable state on self |
| 5 | **FOK ladder execution** | 3287-3344 | Imports `FOKLadder`, delegates to poly client |
| 6 | **GTC fallback with multi-source pricing** | 3346-3492 | CLOB DB, Gamma API, window price fallback, RFQ, then GTC |
| 7 | **DB writes** (window_state, order_manager) | 3500-3600 | Multiple DB clients, fire-and-forget tasks |
| 8 | **Post-trade verification** (FOK instant vs GTC polling) | 3615-3714 | 60s polling loop for GTC fills |
| 9 | **Telegram alerts** (filled, exhausted, entry) | Scattered | `_alerter.send_order_filled()`, `send_fok_exhausted()`, `send_entry_alert()` |

### Key business rules buried in the method

1. **Price cascade**: FOK at cap -> FOK at cap + pi cents -> GTC at cap (with pi bonus if FOK exhausted)
2. **Price sourcing**: CLOB DB best ask -> Gamma API -> window Gamma price (3-level fallback)
3. **Floor/cap**: `PRICE_FLOOR=0.30`, `PRICE_CAP` dynamic per offset (from `_get_v81_cap`)
4. **Fee calculation**: `0.072 * price * (1 - price) * stake` (Polymarket binary options fee)
5. **Stake sizing**: `bankroll * bet_fraction * price_multiplier` where price_multiplier = `(1 - token_price) / 0.50`, clamped [0.5, 1.5]
6. **GTC fill polling**: 5s intervals, 60s max wait, tracks size_matched
7. **Pi bonus**: 3.14 cents added to GTC limit if FOK exhausted

### What the V4/registry path currently does

`EvaluateStrategiesUseCase` returns `EvaluateStrategiesResult.live_decision` (a `StrategyDecision`). The orchestrator then calls `five_min_vpin._sp_trade_decision()` which tunnels back into `_execute_trade()`. This is the coupling SP-06 identifies.

---

## 2. Polymarket Order Types

### FAK (Fill-and-Kill)
- Matches against resting orders at the specified price or better
- Unfilled remainder is immediately cancelled
- Used for aggressive entry: "give me whatever exists at this price"
- Polymarket CLOB API: `order_type: "FAK"` in the signed order payload

### GTC (Good-Til-Cancelled)
- Rests on the order book until filled or manually cancelled
- Market makers often fill these via RFQ (request-for-quote)
- Our system uses GTD (Good-Til-Date) variant with expiry at window close + 120s
- Used as fallback when FAK ladder exhausts without fills

### FOK (Fill-or-Kill)
- All-or-nothing: either fills the entire requested size or cancels immediately
- More restrictive than FAK (which allows partial fills)
- Configurable via `ORDER_TYPE` env var (default: FAK since v9.0)

### Current ladder flow (from `fok_ladder.py`)
1. Check CLOB book for best ask
2. If best_ask < floor ($0.30) -> abort
3. FAK at cap (e.g., $0.65) -> if zero fill, wait 2s
4. FAK at cap + pi ($0.6814) -> if zero fill, return exhausted
5. Strategy falls back to GTC at cap price

### RFQ path (from `_execute_trade`)
Between FOK and GTC, there is an RFQ attempt:
- `place_rfq_order(token_id, direction, price, size, max_price)`
- Market makers respond with a fill quote
- If RFQ fills, use that. If not, fall through to GTC.

---

## 3. Clean Architecture Design

### 3.1 New Value Objects

```python
# engine/domain/value_objects.py -- additions

@dataclass(frozen=True)
class ExecutionRequest:
    """Input to ExecuteTradeUseCase. Built from StrategyDecision + market state."""
    # Identity
    asset: str
    window_ts: int
    timeframe: str

    # Decision (from strategy)
    strategy_id: str
    strategy_version: str
    direction: str              # "UP" | "DOWN"
    confidence: str             # "DECISIVE" | "HIGH" | "MODERATE" | "LOW"
    confidence_score: float     # 0.0-1.0
    entry_reason: str           # Human-readable audit trail

    # Pricing
    entry_cap: float            # Max acceptable CLOB price (e.g., 0.65)
    price_floor: float          # Min acceptable price (e.g., 0.30)

    # Sizing (from strategy decision)
    collateral_pct: float       # Fraction of bankroll

    # Market context (for DB record)
    current_btc_price: float
    open_price: float
    delta_pct: float
    vpin: float

    # Gate audit trail
    gate_results: list          # [{gate, passed, reason}]
    metadata: dict              # Strategy-specific metadata


@dataclass(frozen=True)
class ExecutionResult:
    """Output of ExecuteTradeUseCase."""
    success: bool
    order_id: Optional[str]
    fill_price: Optional[float]
    fill_size: Optional[float]
    stake_usd: float
    fee_usd: float

    # Execution metadata
    execution_mode: str         # "fok" | "rfq" | "gtc" | "paper"
    fok_attempts: int
    fok_prices: list[float]

    # Failure info
    failure_reason: Optional[str]

    # Token used
    token_id: str
    market_slug: str

    # Timing
    execution_start: float      # Unix epoch
    execution_end: float        # Unix epoch


@dataclass(frozen=True)
class StakeCalculation:
    """Result of stake sizing calculation."""
    base_stake: float
    price_multiplier: float
    adjusted_stake: float
    bankroll: float
    bet_fraction: float
    hard_cap: float
```

### 3.2 New Port: OrderExecutionPort

```python
# engine/domain/ports.py -- addition

class OrderExecutionPort(abc.ABC):
    """Abstracts the order execution strategy (FAK ladder, GTC, paper).

    Different from PolymarketClientPort which is the raw CLOB API.
    This port encapsulates the multi-step execution logic:
    FAK ladder -> RFQ -> GTC fallback.
    """

    @abc.abstractmethod
    async def execute_order(
        self,
        token_id: str,
        side: str,             # "YES" | "NO"
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        """Execute a single order using the configured strategy.

        Implementations:
          - FAKLadderExecutor: FAK ladder -> RFQ -> GTC fallback
          - PaperExecutor: Simulate fill at mid price
          - GTCOnlyExecutor: Single GTC at cap (future)
        """
        ...
```

### 3.3 ExecuteTradeUseCase

```python
# engine/use_cases/execute_trade.py

class ExecuteTradeUseCase:
    """Execute a strategy decision on Polymarket CLOB.

    Single responsibility: take a StrategyDecision, validate it,
    size the position, execute the order, record the trade, alert.

    Does NOT evaluate strategies -- that is EvaluateStrategiesUseCase.
    Does NOT resolve positions -- that is ReconcilePositionsUseCase.

    Port dependencies:
      - PolymarketClientPort: get_window_market (token ID lookup)
      - OrderExecutionPort: execute_order (FAK/GTC/paper)
      - RiskManagerPort: get_status (bankroll, risk checks)
      - WindowStateRepository: was_traded, mark_traded (dedup)
      - AlerterPort: send_trade_alert
      - TradeRepository: record trade to DB
      - Clock: deterministic time for testing
    """

    def __init__(
        self,
        polymarket: PolymarketClientPort,
        order_executor: OrderExecutionPort,
        risk_manager: RiskManagerPort,
        window_state: WindowStateRepository,
        alerter: AlerterPort,
        trade_recorder: TradeRecorderPort,
        clock: Clock,
        *,
        paper_mode: bool = True,
    ):
        self._polymarket = polymarket
        self._executor = order_executor
        self._risk = risk_manager
        self._window_state = window_state
        self._alerter = alerter
        self._recorder = trade_recorder
        self._clock = clock
        self._paper_mode = paper_mode

        # Guardrails (stateful -- same as current five_min_vpin)
        self._order_timestamps: list[float] = []
        self._last_order_time: float = 0.0
        self._consecutive_errors: int = 0
        self._circuit_break_until: float = 0.0

    async def execute(
        self,
        decision: StrategyDecision,
        window_market: WindowMarket,
        current_btc_price: float,
        open_price: float,
    ) -> ExecutionResult:
        """Execute a trade from a strategy decision.

        Steps:
          1. Dedup check (was this window already traded?)
          2. Calculate stake from risk manager state + decision sizing
          3. Risk approval (drawdown, daily loss, exposure)
          4. Guardrails (rate limit, circuit breaker)
          5. Resolve token ID from direction + window market
          6. Execute order via OrderExecutionPort
          7. Record trade to DB
          8. Mark window as traded
          9. Send Telegram alert
          10. Return ExecutionResult
        """
        window_key = WindowKey(
            asset=window_market.market_slug.split("-")[0].upper(),
            window_ts=_extract_window_ts(window_market.market_slug),
        )

        # Step 1: Dedup
        if await self._window_state.was_traded(window_key):
            return ExecutionResult(
                success=False,
                failure_reason="already_traded",
                ...
            )

        # Step 2: Stake calculation
        stake = self._calculate_stake(decision, current_btc_price)

        # Step 3: Risk check
        risk_status = self._risk.get_status()
        approved, reason = self._check_risk(risk_status, stake)
        if not approved:
            await self._alerter.send_system_alert(
                f"Trade BLOCKED -- {decision.strategy_id}\n"
                f"Stake: ${stake.adjusted_stake:.2f}\n"
                f"Reason: {reason}"
            )
            return ExecutionResult(success=False, failure_reason=reason, ...)

        # Step 4: Guardrails
        ok, reason = self._check_guardrails()
        if not ok:
            return ExecutionResult(success=False, failure_reason=reason, ...)

        # Step 5: Token ID
        token_id = (
            window_market.down_token_id
            if decision.direction == "DOWN"
            else window_market.up_token_id
        )
        side = "NO" if decision.direction == "DOWN" else "YES"

        # Step 6: Execute
        result = await self._executor.execute_order(
            token_id=token_id,
            side=side,
            stake_usd=stake.adjusted_stake,
            entry_cap=decision.entry_cap or 0.65,
            price_floor=0.30,
        )

        # Step 7: Record trade
        await self._recorder.record_trade(decision, result, stake)

        # Step 8: Mark traded
        await self._window_state.mark_traded(
            window_key, result.order_id or "unknown"
        )

        # Step 9: Alert
        await self._alerter.send_trade_alert(window_key, ...)

        # Step 10: Record guardrail success
        self._record_order_placed()
        self._on_order_success()

        return result

    def _calculate_stake(
        self,
        decision: StrategyDecision,
        current_btc_price: float,
    ) -> StakeCalculation:
        """Calculate stake from risk status and decision sizing.

        Extracted from five_min_vpin._calculate_stake.
        Formula: bankroll * bet_fraction * price_multiplier
        """
        risk = self._risk.get_status()
        bankroll = risk.current_bankroll
        bet_fraction = decision.collateral_pct or 0.025

        base_stake = bankroll * bet_fraction

        # Token price estimate for R/R scaling
        tp = max(0.30, min(0.65, decision.entry_cap or 0.50))
        price_multiplier = (1.0 - tp) / 0.50
        price_multiplier = max(0.5, min(1.5, price_multiplier))

        adjusted = base_stake * price_multiplier

        # Hard caps
        hard_cap = min(50.0, bankroll * bet_fraction * 0.95)
        adjusted = min(adjusted, hard_cap)
        adjusted = round(adjusted, 2)

        return StakeCalculation(
            base_stake=base_stake,
            price_multiplier=price_multiplier,
            adjusted_stake=adjusted,
            bankroll=bankroll,
            bet_fraction=bet_fraction,
            hard_cap=hard_cap,
        )

    def _check_risk(
        self, status: RiskStatus, stake: StakeCalculation,
    ) -> tuple[bool, str]:
        """Validate trade against risk limits."""
        if status.kill_switch_active:
            return False, "kill_switch_active"
        if status.drawdown_pct > 0.45:
            return False, f"drawdown {status.drawdown_pct:.1%} > 45%"
        if stake.adjusted_stake < 2.0:
            return False, f"stake ${stake.adjusted_stake:.2f} < $2 minimum"
        return True, ""

    def _check_guardrails(self) -> tuple[bool, str]:
        """Rate limit + circuit breaker checks."""
        now = self._clock.now()

        # Circuit breaker
        if self._circuit_break_until > now:
            remaining = self._circuit_break_until - now
            return False, f"circuit_breaker.active: {remaining:.0f}s remaining"

        # Rate limit: min interval between orders
        if self._last_order_time > 0:
            elapsed = now - self._last_order_time
            if elapsed < 30:  # min_order_interval_seconds
                return False, f"rate_limit.too_fast: {elapsed:.1f}s"

        # Hourly cap
        cutoff = now - 3600.0
        self._order_timestamps = [
            ts for ts in self._order_timestamps if ts > cutoff
        ]
        if len(self._order_timestamps) >= 20:  # max_orders_per_hour
            return False, f"rate_limit.hourly_cap: {len(self._order_timestamps)}"

        return True, ""
```

### 3.4 OrderExecutionPort Adapters

#### FAKLadderExecutor (Live)

```python
# engine/adapters/execution/fak_ladder_executor.py

class FAKLadderExecutor(OrderExecutionPort):
    """Live execution: FAK ladder -> RFQ -> GTC fallback.

    Wraps the existing FOKLadder + PolymarketClient.place_rfq_order +
    PolymarketClient.place_order into a single execute_order() call.

    This adapter owns the multi-step execution strategy. The use case
    just calls execute_order() and gets back an ExecutionResult.
    """

    def __init__(
        self,
        poly_client: PolymarketClientPort,
        *,
        pi_bonus_cents: float = 0.0314,
        gtc_poll_interval: int = 5,
        gtc_max_wait: int = 60,
    ):
        self._poly = poly_client
        self._pi_bonus = pi_bonus_cents
        self._gtc_poll_interval = gtc_poll_interval
        self._gtc_max_wait = gtc_max_wait

    async def execute_order(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        """
        Three-phase execution:
          Phase 1: FAK at cap -> FAK at cap + pi (2 attempts)
          Phase 2: RFQ at CLOB best ask (if Phase 1 missed)
          Phase 3: GTC at cap (resting order, poll for fill)
        """
        start = time.time()
        market_slug = ""  # Built from token context

        # Phase 1: FAK ladder
        ladder = FOKLadder(self._poly)
        fok_result = await ladder.execute(
            token_id=token_id,
            direction="BUY",
            stake_usd=stake_usd,
            max_price=entry_cap,
            min_price=price_floor,
        )

        if fok_result.filled:
            return ExecutionResult(
                success=True,
                order_id=fok_result.order_id,
                fill_price=fok_result.fill_price,
                fill_size=fok_result.shares,
                stake_usd=stake_usd,
                fee_usd=self._calc_fee(fok_result.fill_price, stake_usd),
                execution_mode="fok",
                fok_attempts=fok_result.attempts,
                fok_prices=fok_result.attempted_prices,
                failure_reason=None,
                token_id=token_id,
                market_slug=market_slug,
                execution_start=start,
                execution_end=time.time(),
            )

        # Phase 2: RFQ
        rfq_result = await self._try_rfq(
            token_id, side, stake_usd, entry_cap, price_floor
        )
        if rfq_result and rfq_result.success:
            return rfq_result

        # Phase 3: GTC
        gtc_result = await self._try_gtc(
            token_id, side, stake_usd, entry_cap, market_slug, start
        )
        return gtc_result

    @staticmethod
    def _calc_fee(price: float, stake: float) -> float:
        """Polymarket binary options fee: 7.2% * p * (1-p) * stake."""
        return 0.072 * price * (1.0 - price) * stake
```

#### PaperExecutor

```python
# engine/adapters/execution/paper_executor.py

class PaperExecutor(OrderExecutionPort):
    """Paper mode: simulate fill at entry_cap with small random slippage."""

    async def execute_order(
        self,
        token_id: str,
        side: str,
        stake_usd: float,
        entry_cap: float,
        price_floor: float,
    ) -> ExecutionResult:
        slippage = random.uniform(-0.005, 0.005)
        fill_price = max(price_floor, min(0.99, entry_cap + slippage))
        shares = stake_usd / fill_price
        order_id = f"paper-{uuid.uuid4().hex[:12]}"

        return ExecutionResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            fill_size=shares,
            stake_usd=stake_usd,
            fee_usd=0.072 * fill_price * (1 - fill_price) * stake_usd,
            execution_mode="paper",
            fok_attempts=0,
            fok_prices=[],
            failure_reason=None,
            token_id=token_id,
            market_slug="",
            execution_start=time.time(),
            execution_end=time.time(),
        )
```

### 3.5 New Port: TradeRecorderPort

```python
# engine/domain/ports.py -- addition

class TradeRecorderPort(abc.ABC):
    """Records executed trades to the trades table + window_snapshots.

    Extracted from the scattered DB writes in _execute_trade.
    Consolidates: order_manager.register_order, db.update_window_trade_placed,
    and the metadata dict construction.
    """

    @abc.abstractmethod
    async def record_trade(
        self,
        decision: StrategyDecision,
        result: ExecutionResult,
        stake: StakeCalculation,
    ) -> None:
        """Persist a completed trade to the trades table."""
        ...
```

---

## 4. File Structure

```
engine/
├── domain/
│   ├── ports.py                          # +OrderExecutionPort, +TradeRecorderPort
│   └── value_objects.py                  # +ExecutionRequest, +ExecutionResult, +StakeCalculation
│
├── use_cases/
│   └── execute_trade.py                  # NEW: ExecuteTradeUseCase (core)
│
├── adapters/
│   └── execution/
│       ├── __init__.py
│       ├── fak_ladder_executor.py        # NEW: FAKLadderExecutor (OrderExecutionPort)
│       ├── paper_executor.py             # NEW: PaperExecutor (OrderExecutionPort)
│       └── trade_recorder.py             # NEW: DBTradeRecorder (TradeRecorderPort)
│
└── strategies/
    └── registry.py                       # MODIFIED: wire execute_trade after LIVE decisions
```

---

## 5. Integration with StrategyRegistry

### Current flow (to preserve)

```
Orchestrator._evaluate_window()
  -> EvaluateStrategiesUseCase.execute()
    -> returns EvaluateStrategiesResult with live_decision
  -> if live_decision.action == "TRADE":
       five_min_vpin._sp_trade_decision(live_decision)  # GOD CLASS TUNNEL
         -> five_min_vpin._execute_trade(state, signal)  # 536 lines
```

### New flow

```
Orchestrator._evaluate_window()
  -> EvaluateStrategiesUseCase.execute()
    -> returns EvaluateStrategiesResult with live_decision
  -> if live_decision.action == "TRADE" and ENGINE_REGISTRY_EXECUTE:
       ExecuteTradeUseCase.execute(live_decision, window_market, ...)
  -> else if live_decision.action == "TRADE":
       five_min_vpin._sp_trade_decision(live_decision)  # OLD PATH (fallback)
```

### Registry v2 integration

```python
# In orchestrator or a new RegistryOrchestrator:

async def _process_registry_decisions(self, window, state):
    """Evaluate all strategies via registry, execute LIVE decisions."""
    decisions = await self._registry.evaluate_all(window, state)

    for decision in decisions:
        config = self._registry.configs.get(decision.strategy_id)

        if config.mode == "GHOST":
            # Log only -- no execution
            log.info("registry.ghost_decision",
                strategy=decision.strategy_id,
                action=decision.action,
                direction=decision.direction)
            continue

        if config.mode == "LIVE" and decision.action == "TRADE":
            # Look up window market (token IDs)
            window_market = await self._polymarket.get_window_market(
                asset=window.asset,
                window_ts=window.window_ts,
            )
            if not window_market:
                log.error("registry.no_market", window_ts=window.window_ts)
                continue

            # Execute via clean use case
            result = await self._execute_trade_uc.execute(
                decision=decision,
                window_market=window_market,
                current_btc_price=state.btc_price,
                open_price=window.open_price,
            )

            log.info("registry.executed",
                strategy=decision.strategy_id,
                success=result.success,
                order_id=result.order_id,
                fill_price=result.fill_price)
```

### Feature flags

| Flag | Default | Purpose |
|---|---|---|
| `ENGINE_REGISTRY_EXECUTE` | `false` | Enable ExecuteTradeUseCase (vs old _sp_trade_decision) |
| `ENGINE_USE_STRATEGY_REGISTRY` | `false` | Enable registry v2 evaluate path |
| `ENGINE_PARALLEL_EXECUTE` | `false` | Run both old and new, compare results |

---

## 6. Telegram Alert Format

### Trade placed alert

```
TRADE v4_down_only v2.0.0
Direction: DOWN (NO)
Confidence: DECISIVE (0.87)

Gates passed:
  timing: T-120 in [90, 150]
  direction: DOWN = DOWN
  confidence: dist 0.37 >= 0.10
  trade_advised: true

Execution: FAK filled at $0.55
  Attempts: 1/2
  Shares: 18.18
  Stake: $10.00
  Fee: $0.18

Entry reason: v4_down_only_T120_DOWN_clob_sized
BTC: $84,231 -> delta -0.12%
VPIN: 0.42 (NORMAL)
```

### Trade blocked alert

```
BLOCKED v4_down_only v2.0.0
Direction: DOWN
Stake: $10.00
Reason: drawdown 46.2% > 45%
```

### FOK exhausted -> GTC fallback alert

```
FOK EXHAUSTED v4_down_only
Attempted: [$0.55, $0.58]
Best ask was above cap
Falling back to GTC at $0.55
```

### Key design decision: strategy name always in subject line

The current system's alerts do not consistently include which strategy triggered the trade. The new format always starts with the strategy name + version so the operator can immediately identify the source.

---

## 7. Migration Strategy

### Phase A: Build in parallel (no production risk)

1. Add `ExecutionRequest`, `ExecutionResult`, `StakeCalculation` to `domain/value_objects.py`
2. Add `OrderExecutionPort`, `TradeRecorderPort` to `domain/ports.py`
3. Create `engine/use_cases/execute_trade.py`
4. Create `engine/adapters/execution/fak_ladder_executor.py`
5. Create `engine/adapters/execution/paper_executor.py`
6. Create `engine/adapters/execution/trade_recorder.py`
7. Write tests for ExecuteTradeUseCase with mock ports

### Phase B: Shadow execution (low risk)

1. Wire `ExecuteTradeUseCase` into orchestrator alongside existing path
2. Feature flag `ENGINE_PARALLEL_EXECUTE=true`:
   - Old path executes as before
   - New path runs in shadow mode: same inputs, logs result, does NOT place real orders
   - Compare: did both paths agree on stake, token, direction?
3. Alert on any mismatch between old and new

### Phase C: Cutover

1. After 1 week of zero mismatches in shadow mode:
2. Set `ENGINE_REGISTRY_EXECUTE=true`
3. Old `_sp_trade_decision` path becomes the fallback
4. Monitor for 48 hours

### Phase D: Cleanup

1. Remove `_sp_trade_decision` shortcut from five_min_vpin.py
2. Remove `_execute_trade` from five_min_vpin.py (536 LOC deleted)
3. Remove `_calculate_stake` from five_min_vpin.py
4. Mark SP-06 as RESOLVED in audit checklist

### Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Stake calculation differs (rounding) | Medium | Low | Shadow mode catches this before cutover |
| FOK ladder behavior differs | Low | Medium | Same FOKLadder class, just called from different location |
| GTC fallback pricing differs | Medium | Medium | Shadow mode compares GTC limit price |
| Window dedup race condition | Low | High | Same WindowStateRepository, atomic check |
| Telegram alert format change confuses operator | Low | Low | Add strategy name, keep rest of format |

---

## 8. Test Plan

### Unit tests (domain layer, no mocks needed)

```python
# test_stake_calculation.py

def test_stake_at_50_cent_token():
    """50c token -> 1.0x multiplier -> base stake unchanged."""

def test_stake_at_40_cent_token():
    """40c token -> 1.2x multiplier -> 20% bigger bet (better R/R)."""

def test_stake_at_65_cent_token():
    """65c token -> 0.7x multiplier -> 30% smaller bet."""

def test_stake_hard_cap():
    """Stake never exceeds $50 hard cap."""

def test_stake_minimum():
    """Stake below $2 is rejected by risk check."""

def test_fee_calculation():
    """Fee = 0.072 * p * (1-p) * stake."""
```

### Use case tests (mock ports)

```python
# test_execute_trade.py

async def test_happy_path_fok_fill():
    """FOK fills -> record trade -> mark traded -> alert."""
    mock_executor = MockOrderExecutor(fill_price=0.55)
    uc = ExecuteTradeUseCase(executor=mock_executor, ...)
    result = await uc.execute(decision, window_market, ...)
    assert result.success
    assert result.execution_mode == "fok"

async def test_already_traded_dedup():
    """Window already traded -> skip, no execution."""
    mock_window_state = MockWindowState(traded=True)
    uc = ExecuteTradeUseCase(window_state=mock_window_state, ...)
    result = await uc.execute(...)
    assert not result.success
    assert result.failure_reason == "already_traded"

async def test_risk_blocked():
    """Kill switch active -> blocked, alert sent."""
    mock_risk = MockRiskManager(kill_switch_active=True)
    uc = ExecuteTradeUseCase(risk_manager=mock_risk, ...)
    result = await uc.execute(...)
    assert not result.success
    assert "kill_switch" in result.failure_reason

async def test_rate_limit_blocked():
    """Too many orders in 1 hour -> blocked."""

async def test_circuit_breaker_active():
    """After 3 consecutive errors -> 3-minute circuit break."""

async def test_paper_mode():
    """Paper executor simulates fill, no real CLOB calls."""

async def test_fok_exhausted_falls_to_gtc():
    """FOK gets zero fill -> GTC placed at cap."""

async def test_no_token_id():
    """Direction maps to None token_id -> fail gracefully."""
```

### Integration tests (real DB, test adapters)

```python
# test_fak_ladder_executor_integration.py

async def test_fak_ladder_with_mock_clob():
    """Full FAK ladder flow with a mock CLOB server."""

async def test_gtc_poll_timeout():
    """GTC order not filled within 60s -> result shows unfilled."""

async def test_trade_recorder_writes_to_db():
    """Trade recorded correctly in trades table with all metadata."""
```

### Shadow mode comparison test

```python
# test_shadow_comparison.py

async def test_old_vs_new_same_result():
    """Given identical inputs, old _execute_trade and new use case
    produce the same stake, token selection, and order parameters."""
```

---

## 9. Composition Root Wiring

```python
# In engine/main.py or engine/infrastructure/di.py

def build_execute_trade_use_case(
    poly_client: PolymarketClientPort,
    risk_manager: RiskManagerPort,
    window_state: WindowStateRepository,
    alerter: AlerterPort,
    db_client: Any,
    clock: Clock,
    paper_mode: bool,
) -> ExecuteTradeUseCase:
    """Wire up the ExecuteTradeUseCase with all dependencies."""

    # Select executor based on mode
    if paper_mode:
        executor = PaperExecutor()
    else:
        executor = FAKLadderExecutor(poly_client)

    recorder = DBTradeRecorder(db_client)

    return ExecuteTradeUseCase(
        polymarket=poly_client,
        order_executor=executor,
        risk_manager=risk_manager,
        window_state=window_state,
        alerter=alerter,
        trade_recorder=recorder,
        clock=clock,
        paper_mode=paper_mode,
    )
```

---

## 10. Dependency Graph

```
ExecuteTradeUseCase
├── depends on (domain ports only):
│   ├── PolymarketClientPort     (get_window_market for token IDs)
│   ├── OrderExecutionPort       (execute_order -- NEW)
│   ├── RiskManagerPort          (get_status for risk checks)
│   ├── WindowStateRepository    (was_traded / mark_traded)
│   ├── AlerterPort              (send_trade_alert / send_system_alert)
│   ├── TradeRecorderPort        (record_trade -- NEW)
│   └── Clock                    (deterministic time)
│
├── value objects (domain layer):
│   ├── StrategyDecision         (input from strategy)
│   ├── WindowMarket             (token IDs from Gamma)
│   ├── ExecutionResult          (output -- NEW)
│   ├── StakeCalculation         (intermediate -- NEW)
│   ├── WindowKey                (dedup identity)
│   └── RiskStatus               (risk manager snapshot)
│
└── NO framework imports, NO DB imports, NO HTTP imports
```

All external dependencies (Polymarket CLOB, DB, Telegram) are behind ports. The use case is testable with pure mocks.
