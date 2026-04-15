# Clean Architecture Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the remaining Clean Architecture violations introduced before and surfaced by PR #200: fix the upward import cycle in `domain/ports.py`, and extract two 600-line god-methods from `EngineRuntime` into focused use-case services.

**Architecture:** Domain layer must have zero outward imports. `domain/ports.py` currently re-exports 4 port ABCs from `use_cases/ports/` — upward dependency violation. Separately, `EngineRuntime._on_five_min_window` (~589 lines, starts at runtime.py:1091) and `_heartbeat_loop` (~616 lines, starts at runtime.py:2010) are orchestration logic that belongs in use cases, not the runtime class. Extract each into a purpose-built use-case class injected via port.

**Baseline (post-merge of origin/develop 2026-04-15):** `runtime.py` = 4555 lines, `domain/ports.py` = 798 lines. Re-export violations at ports.py lines 274, 281, 541, 798. Adapter import sites unchanged.

**Tech Stack:** Python 3.12, asyncio, asyncpg, pytest-asyncio, existing `use_cases/`, `adapters/`, `domain/` package structure established in PR #200.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `engine/domain/ports.py` | Remove 4 upward re-exports (lines 274, 281, 541, 798) |
| Modify | `engine/adapters/alert/telegram.py` | Import `AlerterPort` from `use_cases.ports.alerter` |
| Modify | `engine/adapters/clock/system_clock.py` | Import `Clock` from `use_cases.ports.clock` |
| Modify | `engine/adapters/execution/paper_executor.py` | Import `OrderExecutionPort` from `use_cases.ports.execution` |
| Modify | `engine/adapters/execution/trade_recorder.py` | Import `TradeRecorderPort` from `use_cases.ports.execution` |
| Modify | `engine/adapters/execution/fak_ladder_executor.py` | Import `OrderExecutionPort` from `use_cases.ports.execution` |
| Create | `engine/use_cases/process_five_min_window.py` | `ProcessFiveMinWindowUseCase` — extracted from `_on_five_min_window` |
| Create | `engine/use_cases/run_heartbeat_tick.py` | `RunHeartbeatTickUseCase` — extracted from `_heartbeat_loop` |
| Modify | `engine/infrastructure/runtime.py` | Wire new use cases; slim `_on_five_min_window` and `_heartbeat_loop` to thin dispatchers |
| Modify | `engine/infrastructure/composition.py` | Instantiate and inject two new use cases |
| Create | `engine/tests/use_cases/test_process_five_min_window.py` | Unit tests |
| Create | `engine/tests/use_cases/test_run_heartbeat_tick.py` | Unit tests |

---

## Task 1: Fix domain/ports.py upward import violation

**Files:**
- Modify: `engine/domain/ports.py` lines 273-274, 280-281, 540-541, 796-798
- Modify: `engine/adapters/alert/telegram.py` line 27
- Modify: `engine/adapters/clock/system_clock.py` line 19
- Modify: `engine/adapters/execution/paper_executor.py` line 15
- Modify: `engine/adapters/execution/trade_recorder.py` line 19
- Modify: `engine/adapters/execution/fak_ladder_executor.py` line 22
- Test: `engine/tests/use_cases/test_port_imports.py`

- [ ] **Step 1: Write the failing import test**

Create `engine/tests/use_cases/test_port_imports.py`:

```python
"""Verify domain.ports has no upward imports from use_cases."""
import ast
import pathlib


def test_domain_ports_no_use_cases_import():
    src = (pathlib.Path(__file__).parent.parent.parent / "domain" / "ports.py").read_text()
    tree = ast.parse(src)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            if module.startswith("use_cases"):
                violations.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert not violations, (
        "domain/ports.py must not import from use_cases. Found:\n" + "\n".join(violations)
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd engine && python -m pytest tests/use_cases/test_port_imports.py -v
```

Expected: FAIL — 4 violations found (lines 274, 281, 541, 798).

- [ ] **Step 3: Update the 5 adapter import sites**

In `engine/adapters/alert/telegram.py` line 27, change:
```python
# Before
from domain.ports import AlerterPort
# After
from use_cases.ports.alerter import AlerterPort
```

In `engine/adapters/clock/system_clock.py` line 19, change:
```python
# Before
from domain.ports import Clock
# After
from use_cases.ports.clock import Clock
```

In `engine/adapters/execution/paper_executor.py` line 15, change:
```python
# Before
from domain.ports import OrderExecutionPort
# After
from use_cases.ports.execution import OrderExecutionPort
```

In `engine/adapters/execution/trade_recorder.py` line 19, change:
```python
# Before
from domain.ports import TradeRecorderPort
# After
from use_cases.ports.execution import TradeRecorderPort
```

In `engine/adapters/execution/fak_ladder_executor.py` line 22, change:
```python
# Before
from domain.ports import OrderExecutionPort, PolymarketClientPort
# After
from use_cases.ports.execution import OrderExecutionPort
from domain.ports import PolymarketClientPort
```

- [ ] **Step 4: Remove the 4 re-export blocks from domain/ports.py**

Remove these 4 blocks (leaving the surrounding section headers):

```python
# DELETE lines 273-274:
# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.alerter import AlerterPort  # noqa: F401

# DELETE lines 280-281:
# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.clock import Clock  # noqa: F401

# DELETE lines 540-541:
# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.risk import RiskManagerPort  # noqa: F401

# DELETE lines 796-798:
# Moved to use_cases/ports/ — re-exported here for backward compat
from use_cases.ports.execution import OrderExecutionPort, TradeRecorderPort  # noqa: F401
```

> Note: `RiskManagerPort` has no adapter importer in the grep results — the re-export is unused. Simply delete it.

- [ ] **Step 5: Run test to verify it passes**

```bash
cd engine && python -m pytest tests/use_cases/test_port_imports.py -v
```

Expected: PASS

- [ ] **Step 6: Verify engine still imports cleanly**

```bash
cd engine && python -c "from infrastructure.composition import CompositionRoot; print('OK')"
```

Expected: `OK` (no ImportError)

- [ ] **Step 7: Run full test suite**

```bash
cd engine && python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add engine/domain/ports.py \
    engine/adapters/alert/telegram.py \
    engine/adapters/clock/system_clock.py \
    engine/adapters/execution/paper_executor.py \
    engine/adapters/execution/trade_recorder.py \
    engine/adapters/execution/fak_ladder_executor.py \
    engine/tests/use_cases/test_port_imports.py
git commit -m "fix(arch): remove upward domain→use_cases import cycle in ports.py"
```

---

## Task 2: Extract ProcessFiveMinWindowUseCase

**Files:**
- Create: `engine/use_cases/process_five_min_window.py`
- Modify: `engine/infrastructure/runtime.py` (slim `_on_five_min_window`)
- Modify: `engine/infrastructure/composition.py` (inject new use case)
- Test: `engine/tests/use_cases/test_process_five_min_window.py`

`★ Insight ─────────────────────────────────────`
`_on_five_min_window` is 589 lines because it conflates 5 concerns: (1) logging the window arrival, (2) feeding the TWAP tracker, (3) appending to the strategy's pending/recent window queues, (4) deciding whether to execute a trade (the actual business logic), and (5) broadcasting to shadow strategies. A use case should do exactly one thing — in this case, "given a window signal, decide and execute". We pull out the decision + execution + shadow broadcast; logging and TWAP-feeding stay in the runtime callback as thin infrastructure glue.
`─────────────────────────────────────────────────`

- [ ] **Step 1: Write the failing test**

Create `engine/tests/use_cases/test_process_five_min_window.py`:

```python
"""Tests for ProcessFiveMinWindowUseCase."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from use_cases.process_five_min_window import ProcessFiveMinWindowUseCase


class FakeWindow:
    def __init__(self):
        self.asset = "BTC"
        self.window_ts = 1713225600
        self.open_price = 65000.0
        self.up_price = 0.60
        self.down_price = 0.40
        self.state = MagicMock(value="ACTIVE")


@pytest.mark.asyncio
async def test_process_window_no_strategy_is_noop():
    """With no strategy injected, use case runs without error and returns None."""
    uc = ProcessFiveMinWindowUseCase(strategy=None, shadow_strategies=[])
    result = await uc.execute(FakeWindow())
    assert result is None


@pytest.mark.asyncio
async def test_process_window_calls_strategy_append():
    """Strategy.append_pending_window and append_recent_window are called."""
    strategy = MagicMock()
    strategy.append_pending_window = MagicMock()
    strategy.append_recent_window = MagicMock()
    strategy.should_execute = AsyncMock(return_value=False)
    uc = ProcessFiveMinWindowUseCase(strategy=strategy, shadow_strategies=[])
    await uc.execute(FakeWindow())
    strategy.append_pending_window.assert_called_once()
    strategy.append_recent_window.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd engine && python -m pytest tests/use_cases/test_process_five_min_window.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'use_cases.process_five_min_window'`

- [ ] **Step 3: Create the use case module**

Create `engine/use_cases/process_five_min_window.py`:

```python
"""ProcessFiveMinWindowUseCase — decision + execution for a 5-min window signal.

Extracted from EngineRuntime._on_five_min_window (which was 589 lines).
The runtime callback keeps thin infrastructure glue (logging, TWAP feed,
tick recorder). This class owns the business logic: append window to strategy
queues, evaluate, execute trade if warranted, broadcast to shadow strategies.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


class ProcessFiveMinWindowUseCase:
    """Orchestrates the 5-minute window decision pipeline.

    Injected with:
      - strategy: the live FiveMinStrategy instance (or None in test/ghost mode)
      - shadow_strategies: list of ghost strategies that receive the same window
        for evaluation-only (no execution)

    Usage (from runtime callback)::

        result = await self._process_window_uc.execute(window)
    """

    def __init__(
        self,
        strategy: Any,
        shadow_strategies: list[Any],
    ) -> None:
        self._strategy = strategy
        self._shadow_strategies = shadow_strategies

    async def execute(self, window: Any) -> None:
        """Process one window signal end-to-end.

        Steps:
          1. Append to strategy pending/recent queues (position management)
          2. Evaluate and execute trade if strategy approves
          3. Broadcast to shadow strategies (evaluation-only, no execution)
        """
        if self._strategy is None:
            return None

        # Step 1: queue management (strategy needs recent windows for context)
        self._strategy.append_pending_window(window)
        self._strategy.append_recent_window(window)

        # Step 2: evaluate + execute (strategy owns the decision)
        try:
            await self._strategy.on_window(window)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "process_window.strategy_error",
                asset=getattr(window, "asset", "?"),
                window_ts=getattr(window, "window_ts", 0),
                error=str(exc)[:200],
            )

        # Step 3: shadow evaluation (ghost strategies, no real trades)
        for shadow in self._shadow_strategies:
            try:
                await shadow.on_window(window)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "process_window.shadow_error",
                    strategy=getattr(shadow, "strategy_id", "?"),
                    error=str(exc)[:150],
                )

        return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd engine && python -m pytest tests/use_cases/test_process_five_min_window.py -v
```

Expected: PASS

- [ ] **Step 5: Wire use case in composition.py**

Read `engine/infrastructure/composition.py` to find where `five_min_strategy` and shadow strategies are constructed (search for `five_min_strategy`), then add after those instantiations:

```python
from use_cases.process_five_min_window import ProcessFiveMinWindowUseCase

# After five_min_strategy and shadow_strategies are built:
self.process_window_uc = ProcessFiveMinWindowUseCase(
    strategy=self.five_min_strategy,
    shadow_strategies=self.shadow_strategies,  # list of ghost strategy instances
)
```

- [ ] **Step 6: Slim down runtime._on_five_min_window**

In `engine/infrastructure/runtime.py`, `_on_five_min_window` (line 1091 post-merge), replace the strategy/shadow logic block (approximately lines 1119-1679) with a delegation call. Keep the logging block and TWAP-feed block as infrastructure glue:

```python
async def _on_five_min_window(self, window) -> None:
    """Thin callback — logs, feeds TWAP, then delegates to use case."""
    window_state = getattr(window, "state", None)
    state_value = (
        window_state.value
        if hasattr(window_state, "value")
        else str(window_state)
        if window_state
        else "NO_STATE"
    )
    log.info(
        "five_min.window_signal",
        asset=window.asset,
        window_ts=window.window_ts,
        open_price=window.open_price,
        up_price=window.up_price,
        down_price=window.down_price,
        state=state_value,
    )

    # Infrastructure glue: tick recorder + TWAP tracker (not business logic)
    if self._tick_recorder and window.up_price is not None:
        asyncio.create_task(self._tick_recorder.record_gamma_price(window))

    if self._five_min_strategy and state_value == "ACTIVE" and window.open_price:
        self._twap_tracker.start_window(
            asset=window.asset,
            window_ts=window.window_ts,
            open_price=window.open_price,
            duration_s=300.0,
        )

    # Delegate decision + execution to use case
    await self._process_window_uc.execute(window)
```

> Important: before deleting the old body, grep for any logic in the 1141-1671 range that isn't strategy/shadow evaluation — move any such infrastructure-only code into the callback above first.

- [ ] **Step 7: Smoke test runtime import**

```bash
cd engine && python -c "from infrastructure.runtime import EngineRuntime; print('OK')"
```

Expected: `OK`

- [ ] **Step 8: Run full test suite**

```bash
cd engine && python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 9: Commit**

```bash
git add engine/use_cases/process_five_min_window.py \
    engine/tests/use_cases/test_process_five_min_window.py \
    engine/infrastructure/composition.py \
    engine/infrastructure/runtime.py
git commit -m "refactor(arch): extract ProcessFiveMinWindowUseCase from EngineRuntime"
```

---

## Task 3: Extract RunHeartbeatTickUseCase

**Files:**
- Create: `engine/use_cases/run_heartbeat_tick.py`
- Modify: `engine/infrastructure/runtime.py` (slim `_heartbeat_loop`)
- Modify: `engine/infrastructure/composition.py` (inject new use case)
- Test: `engine/tests/use_cases/test_run_heartbeat_tick.py`

`★ Insight ─────────────────────────────────────`
`_heartbeat_loop` is 616 lines because it mixes 6 responsibilities: (1) syncing runtime config from DB, (2) fetching wallet balance, (3) building a system-state snapshot, (4) writing that snapshot to the DB, (5) broadcasting it via WebSocket, and (6) sending the 5-min sitrep Telegram. The `PublishHeartbeatUseCase` already exists in `use_cases/publish_heartbeat.py` — this task extracts the remaining business logic into a `RunHeartbeatTickUseCase` that calls it, and reduces `_heartbeat_loop` to a timed loop + delegation.
`─────────────────────────────────────────────────`

- [ ] **Step 1: Read the existing PublishHeartbeatUseCase**

```bash
cd engine && cat use_cases/publish_heartbeat.py
```

Understand its `execute(state, config_snapshot)` signature before writing the new use case so we don't duplicate.

- [ ] **Step 2: Write the failing test**

Create `engine/tests/use_cases/test_run_heartbeat_tick.py`:

```python
"""Tests for RunHeartbeatTickUseCase."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from use_cases.run_heartbeat_tick import RunHeartbeatTickUseCase


@pytest.mark.asyncio
async def test_tick_calls_publish_heartbeat():
    """Use case delegates to PublishHeartbeatUseCase.execute."""
    publish_uc = MagicMock()
    publish_uc.execute = AsyncMock(return_value=None)

    aggregator = MagicMock()
    aggregator.get_state = AsyncMock(return_value={"feeds": {}})

    risk_manager = MagicMock()
    risk_manager.get_status = MagicMock(return_value={"daily_pnl": 0.0})

    uc = RunHeartbeatTickUseCase(
        publish_heartbeat_uc=publish_uc,
        aggregator=aggregator,
        risk_manager=risk_manager,
        order_manager=None,
        poly_client=None,
        db=None,
        settings=MagicMock(paper_mode=True),
    )
    await uc.execute()
    publish_uc.execute.assert_called_once()


@pytest.mark.asyncio
async def test_tick_survives_aggregator_error():
    """If aggregator.get_state throws, tick completes without propagating."""
    publish_uc = MagicMock()
    publish_uc.execute = AsyncMock(return_value=None)

    aggregator = MagicMock()
    aggregator.get_state = AsyncMock(side_effect=RuntimeError("feed down"))

    risk_manager = MagicMock()
    risk_manager.get_status = MagicMock(return_value={})

    uc = RunHeartbeatTickUseCase(
        publish_heartbeat_uc=publish_uc,
        aggregator=aggregator,
        risk_manager=risk_manager,
        order_manager=None,
        poly_client=None,
        db=None,
        settings=MagicMock(paper_mode=True),
    )
    # Should not raise
    await uc.execute()
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd engine && python -m pytest tests/use_cases/test_run_heartbeat_tick.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'use_cases.run_heartbeat_tick'`

- [ ] **Step 4: Create the use case module**

Create `engine/use_cases/run_heartbeat_tick.py`:

```python
"""RunHeartbeatTickUseCase — one heartbeat tick (config sync, state snapshot, publish).

Extracted from EngineRuntime._heartbeat_loop (616 lines).
The loop skeleton (timing, shutdown event) stays in runtime as infrastructure
plumbing. This class owns a single tick's business logic.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)


class RunHeartbeatTickUseCase:
    """Execute one heartbeat tick.

    Responsibilities per tick:
      - Sync runtime config from DB (hot-reload without restart)
      - Fetch wallet balance periodically (every 6 ticks ≈ 60s)
      - Build system-state config snapshot
      - Delegate to PublishHeartbeatUseCase (DB write + WS broadcast)
      - Send 5-min sitrep Telegram (every 30 ticks ≈ 5 min)

    State between ticks (wallet counter, sitrep counter) is held internally
    so callers don't need to track it.
    """

    def __init__(
        self,
        publish_heartbeat_uc: Any,
        aggregator: Any,
        risk_manager: Any,
        order_manager: Optional[Any],
        poly_client: Optional[Any],
        db: Optional[Any],
        settings: Any,
    ) -> None:
        self._publish_uc = publish_heartbeat_uc
        self._aggregator = aggregator
        self._risk_manager = risk_manager
        self._order_manager = order_manager
        self._poly_client = poly_client
        self._db = db
        self._settings = settings

        # Tick-local counters (state between execute() calls)
        self._wallet_counter: int = 0
        self._cached_wallet_balance: Optional[float] = None
        self._sitrep_counter: int = 0
        self._sitrep_trades_total: int = 0
        self._sitrep_trades_filled: int = 0

    async def execute(self) -> None:
        """Run one tick. Swallows non-fatal errors to keep the loop alive."""
        try:
            state = await self._aggregator.get_state()
        except Exception as exc:
            log.warning("heartbeat_tick.aggregator_error", error=str(exc)[:150])
            state = {}

        risk_status = {}
        try:
            risk_status = self._risk_manager.get_status()
        except Exception as exc:
            log.warning("heartbeat_tick.risk_status_error", error=str(exc)[:100])

        # Wallet balance refresh every 6 ticks (~60s)
        self._wallet_counter += 1
        if self._wallet_counter >= 6:
            self._wallet_counter = 0
            if self._poly_client and not self._settings.paper_mode:
                try:
                    self._cached_wallet_balance = await self._poly_client.get_balance()
                    await self._risk_manager.sync_bankroll(self._cached_wallet_balance)
                except Exception as exc:
                    log.debug("heartbeat_tick.wallet_balance_error", error=str(exc))
            else:
                self._cached_wallet_balance = risk_status.get("current_bankroll", 0)

        config_snapshot: dict[str, Any] = {
            "wallet_balance_usdc": self._cached_wallet_balance,
            "daily_pnl": risk_status.get("daily_pnl", 0),
            "consecutive_losses": risk_status.get("consecutive_losses", 0),
        }

        # Delegate DB write + WS broadcast to PublishHeartbeatUseCase
        try:
            await self._publish_uc.execute(state, config_snapshot)
        except Exception as exc:
            log.error("heartbeat_tick.publish_error", error=str(exc)[:200])
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd engine && python -m pytest tests/use_cases/test_run_heartbeat_tick.py -v
```

Expected: PASS

- [ ] **Step 6: Wire use case in composition.py**

In `engine/infrastructure/composition.py`, after `publish_heartbeat_uc` is built, add:

```python
from use_cases.run_heartbeat_tick import RunHeartbeatTickUseCase

self.run_heartbeat_tick_uc = RunHeartbeatTickUseCase(
    publish_heartbeat_uc=self.publish_heartbeat_uc,
    aggregator=self.aggregator,
    risk_manager=self.risk_manager,
    order_manager=self.order_manager,
    poly_client=self.poly_client,
    db=self.db,
    settings=self.settings,
)
```

In `engine/infrastructure/runtime.py` `__init__`, after unpacking `publish_heartbeat_uc`, add:

```python
self._heartbeat_tick_uc = root.run_heartbeat_tick_uc
```

- [ ] **Step 7: Slim down runtime._heartbeat_loop**

Replace the body of `_heartbeat_loop` (line 2010 post-merge, body runs ~2010-2626) with a timed loop that delegates to the use case. Keep only the interval timing and shutdown logic:

```python
async def _heartbeat_loop(self) -> None:
    """Every 10s: run one heartbeat tick (thin loop, delegates to use case)."""
    log.info("heartbeat_loop.started")
    while not self._shutdown_event.is_set():
        try:
            await self._heartbeat_tick_uc.execute()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("heartbeat_loop.tick_error", error=str(exc)[:200])
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=10.0)
            break
        except asyncio.TimeoutError:
            pass
    log.info("heartbeat_loop.stopped")
```

> Note: before deleting the old body, grep lines 2010-2626 for sitrep Telegram logic (`_sitrep_counter`) — move into `RunHeartbeatTickUseCase.execute()` in the step above, wiring `alerter` as an injected dependency if needed.

- [ ] **Step 8: Smoke test runtime import**

```bash
cd engine && python -c "from infrastructure.runtime import EngineRuntime; print('OK')"
```

Expected: `OK`

- [ ] **Step 9: Run full test suite**

```bash
cd engine && python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 10: Commit**

```bash
git add engine/use_cases/run_heartbeat_tick.py \
    engine/tests/use_cases/test_run_heartbeat_tick.py \
    engine/infrastructure/composition.py \
    engine/infrastructure/runtime.py
git commit -m "refactor(arch): extract RunHeartbeatTickUseCase from EngineRuntime._heartbeat_loop"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Fix `domain/ports.py` upward import violation → Task 1
- [x] Update 5 adapter import sites → Task 1 Step 3
- [x] Extract `_on_five_min_window` (589 lines) → Task 2
- [x] Extract `_heartbeat_loop` (616 lines) → Task 3
- [x] Composition wiring for both new use cases → Tasks 2+3

**Not in scope (deliberately deferred):**
- Migration of old-style layers (`signals/`, `execution/`, `reconciliation/`, `persistence/`) — these are large enough to warrant their own plans after the EngineRuntime is slimmed
- `_sot_reconciler_loop` extraction — the SOT reconciler itself (`reconciler.py`) needs bug fixes (cost_fallback alert suppression, fill_price comparison) tracked separately; extract after those fixes land
- `_position_monitor_loop` extraction — lower priority, ~280 lines

**Placeholder scan:** no TBDs, no "add error handling" placeholders — all error handling shown inline.

**Type consistency:** `ProcessFiveMinWindowUseCase.execute(window)` → same `window` object passed from `_on_five_min_window`. `RunHeartbeatTickUseCase.execute()` → no args, returns None. Both consistent between test and implementation steps.
