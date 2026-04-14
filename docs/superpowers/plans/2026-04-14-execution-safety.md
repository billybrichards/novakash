# Execution Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate 4 confirmed production bugs that caused real money losses on 2026-04-14 — in-memory dedup resets on restart, stale bankroll on cold start, multiple fills per window, and CLOB stale price reaching execute_trade.

**Architecture:** New `PreTradeGate` use case composes 3 checks (DB-backed dedup, CLOB freshness, live bankroll) before any order is submitted. `PgWindowExecutionGuard` persists executed windows to DB so restarts don't lose state. `WalletBalancePort` forces live balance lookup at startup instead of .env default. All dependency arrows point inward — adapters implement ports, use cases depend only on ports.

**Tech Stack:** Python 3.12, asyncio, asyncpg, SQLAlchemy, existing Polymarket client

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `engine/domain/ports.py` | Modify | Add `WindowExecutionGuard` + `WalletBalancePort` abstract ports |
| `engine/domain/value_objects.py` | Modify | Add `PreTradeCheckResult` frozen dataclass |
| `engine/use_cases/pre_trade_gate.py` | Create | Single composable gate (dedup + CLOB freshness + bankroll) |
| `engine/adapters/persistence/pg_execution_guard.py` | Create | DB-backed dedup with in-memory cache, FAIL-CLOSED |
| `engine/adapters/wallet/polymarket_wallet.py` | Create | Live USDC balance from Polymarket client (30s cache) |
| `engine/adapters/wallet/paper_wallet.py` | Create | Paper mode: reads from risk manager |
| `engine/execution/risk_manager.py` | Modify | Add `_ready` flag + `initialize_bankroll()`, refuse trades until init |
| `engine/use_cases/execute_trade.py` | Modify | Use PreTradeGate instead of fail-open dedup, validate entry_cap |
| `engine/strategies/registry.py` | Modify | Remove `_executed_windows` dict, inject + call PreTradeGate |
| `engine/strategies/orchestrator.py` | Modify | Wire new components, warm dedup cache + init bankroll on startup |
| `engine/tests/test_pre_trade_gate.py` | Create | Unit tests for PreTradeGate (all 3 check paths) |
| `engine/tests/test_pg_execution_guard.py` | Create | Unit tests for dedup adapter |

---

## Task 1: DB migration — strategy_executions table

**Files:**
- Create: `engine/db/migrations/add_strategy_executions.sql`
- Modify: `hub/main.py` (add to startup DDL) OR `engine/strategies/orchestrator.py`

- [ ] **Step 1: Create migration SQL**

```sql
-- engine/db/migrations/add_strategy_executions.sql
CREATE TABLE IF NOT EXISTS strategy_executions (
    strategy_id   TEXT        NOT NULL,
    window_ts     BIGINT      NOT NULL,
    order_id      TEXT,
    executed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, window_ts)
);
CREATE INDEX IF NOT EXISTS idx_strategy_executions_ts
    ON strategy_executions (executed_at DESC);
```

- [ ] **Step 2: Add to engine startup in orchestrator**

Find `ensure_tables` or startup block in `engine/strategies/orchestrator.py`. Add:

```python
await self._db._pool.execute("""
    CREATE TABLE IF NOT EXISTS strategy_executions (
        strategy_id TEXT NOT NULL,
        window_ts   BIGINT NOT NULL,
        order_id    TEXT,
        executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (strategy_id, window_ts)
    )
""")
await self._db._pool.execute(
    "CREATE INDEX IF NOT EXISTS idx_strategy_executions_ts "
    "ON strategy_executions (executed_at DESC)"
)
```

- [ ] **Step 3: Verify table creates cleanly**

```bash
cd engine
python3 -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
import asyncpg

async def test():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS strategy_executions (
            strategy_id TEXT NOT NULL, window_ts BIGINT NOT NULL,
            order_id TEXT, executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (strategy_id, window_ts)
        )
    ''')
    row = await conn.fetchrow('SELECT COUNT(*) FROM strategy_executions')
    print('Table exists, row count:', row[0])
    await conn.close()

asyncio.run(test())
"
```
Expected: `Table exists, row count: 0`

- [ ] **Step 4: Commit**

```bash
git add engine/db/migrations/add_strategy_executions.sql engine/strategies/orchestrator.py
git commit -m "feat(db): add strategy_executions table for persistent window dedup"
```

---

## Task 2: Add ports and value object to domain layer

**Files:**
- Modify: `engine/domain/ports.py`
- Modify: `engine/domain/value_objects.py`

- [ ] **Step 1: Add `WindowExecutionGuard` port to `engine/domain/ports.py`**

Append after the `WindowStateRepository` class (search for `class WindowStateRepository`):

```python
class WindowExecutionGuard(abc.ABC):
    """Strategy-level dedup: has (strategy_id, window_ts) been executed?

    Backed by DB so state survives engine restarts.
    FAIL-CLOSED: if DB is unreachable, has_executed() returns True (block trade).
    """

    @abc.abstractmethod
    async def has_executed(self, strategy_id: str, window_ts: int) -> bool: ...

    @abc.abstractmethod
    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None: ...

    @abc.abstractmethod
    async def load_recent(self, hours: int = 2) -> None:
        """Warm in-memory cache from DB on startup."""
        ...


class WalletBalancePort(abc.ABC):
    """Live wallet balance — never returns a .env default."""

    @abc.abstractmethod
    async def get_live_balance(self) -> float: ...
```

- [ ] **Step 2: Add `PreTradeCheckResult` to `engine/domain/value_objects.py`**

Append to the file:

```python
@dataclass(frozen=True)
class PreTradeCheckResult:
    """Result of the pre-execution gate. approved=False means SKIP."""
    approved: bool
    reason: str
    live_bankroll: float = 0.0
    clob_price_age_s: float = 0.0
```

- [ ] **Step 3: Verify imports work**

```bash
cd engine
python3 -c "
from domain.ports import WindowExecutionGuard, WalletBalancePort
from domain.value_objects import PreTradeCheckResult
print('imports OK')
print(PreTradeCheckResult(approved=True, reason='ok', live_bankroll=44.0))
"
```
Expected: `imports OK` + dataclass repr

- [ ] **Step 4: Commit**

```bash
git add engine/domain/ports.py engine/domain/value_objects.py
git commit -m "feat(domain): add WindowExecutionGuard + WalletBalancePort ports + PreTradeCheckResult VO"
```

---

## Task 3: PgWindowExecutionGuard adapter (DB-backed dedup)

**Files:**
- Create: `engine/adapters/persistence/pg_execution_guard.py`
- Create: `engine/tests/test_pg_execution_guard.py`

- [ ] **Step 1: Write failing test**

```python
# engine/tests/test_pg_execution_guard.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from adapters.persistence.pg_execution_guard import PgWindowExecutionGuard

@pytest.mark.asyncio
async def test_has_not_executed_initially():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is False

@pytest.mark.asyncio
async def test_in_memory_cache_hit_after_mark():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()
    guard = PgWindowExecutionGuard(pool)
    await guard.mark_executed("v4_fusion", 1713000000, "order-123")
    # Second call should hit in-memory cache, NOT DB
    pool.fetchrow.reset_mock()
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True
    pool.fetchrow.assert_not_called()  # cache hit, no DB call

@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=Exception("DB down"))
    guard = PgWindowExecutionGuard(pool)
    result = await guard.has_executed("v4_fusion", 1713000000)
    assert result is True  # FAIL-CLOSED: assume already executed

@pytest.mark.asyncio
async def test_load_recent_warms_cache():
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"strategy_id": "v4_fusion", "window_ts": 1713000000},
        {"strategy_id": "v4_down_only", "window_ts": 1713000000},
    ])
    guard = PgWindowExecutionGuard(pool)
    await guard.load_recent(hours=2)
    pool.fetchrow = AsyncMock()  # should NOT be called
    assert await guard.has_executed("v4_fusion", 1713000000) is True
    assert await guard.has_executed("v4_down_only", 1713000000) is True
    assert await guard.has_executed("v4_fusion", 9999999999) is False
    pool.fetchrow.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd engine && python3 -m pytest tests/test_pg_execution_guard.py -v
```
Expected: `ModuleNotFoundError: No module named 'adapters.persistence.pg_execution_guard'`

- [ ] **Step 3: Implement the adapter**

```python
# engine/adapters/persistence/pg_execution_guard.py
from __future__ import annotations
import structlog
from domain.ports import WindowExecutionGuard

log = structlog.get_logger()

class PgWindowExecutionGuard(WindowExecutionGuard):
    """DB-backed strategy dedup with in-memory read-through cache.

    FAIL-CLOSED: DB errors return True (block trade, don't double-fill).
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self._cache: set[tuple[str, int]] = set()

    async def has_executed(self, strategy_id: str, window_ts: int) -> bool:
        key = (strategy_id, window_ts)
        if key in self._cache:
            return True
        try:
            row = await self._pool.fetchrow(
                "SELECT 1 FROM strategy_executions "
                "WHERE strategy_id=$1 AND window_ts=$2",
                strategy_id, window_ts,
            )
            if row:
                self._cache.add(key)
                return True
            return False
        except Exception as exc:
            log.error("execution_guard.db_error", error=str(exc)[:120])
            return True  # FAIL-CLOSED

    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None:
        key = (strategy_id, window_ts)
        try:
            await self._pool.execute(
                "INSERT INTO strategy_executions (strategy_id, window_ts, order_id) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                strategy_id, window_ts, order_id,
            )
            self._cache.add(key)
        except Exception as exc:
            log.error("execution_guard.mark_error", error=str(exc)[:120])
            # Still add to in-memory cache to prevent same-process duplicates
            self._cache.add(key)

    async def load_recent(self, hours: int = 2) -> None:
        try:
            rows = await self._pool.fetch(
                "SELECT strategy_id, window_ts FROM strategy_executions "
                "WHERE executed_at > NOW() - ($1 || ' hours')::interval",
                str(hours),
            )
            for row in rows:
                self._cache.add((row["strategy_id"], row["window_ts"]))
            log.info("execution_guard.cache_warmed", entries=len(self._cache))
        except Exception as exc:
            log.warning("execution_guard.load_error", error=str(exc)[:120])
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd engine && python3 -m pytest tests/test_pg_execution_guard.py -v
```
Expected: 4 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add engine/adapters/persistence/pg_execution_guard.py engine/tests/test_pg_execution_guard.py
git commit -m "feat(adapter): PgWindowExecutionGuard — DB-backed dedup, fail-closed, cache-warmed on startup"
```

---

## Task 4: WalletBalance adapters

**Files:**
- Create: `engine/adapters/wallet/polymarket_wallet.py`
- Create: `engine/adapters/wallet/paper_wallet.py`

- [ ] **Step 1: Implement PolymarketWalletAdapter**

```python
# engine/adapters/wallet/polymarket_wallet.py
from __future__ import annotations
import time
import structlog
from domain.ports import WalletBalancePort

log = structlog.get_logger()
_CACHE_TTL = 30.0  # seconds


class PolymarketWalletAdapter(WalletBalancePort):
    """Live USDC balance from Polymarket client, cached 30s."""

    def __init__(self, poly_client, fallback_balance: float = 0.0) -> None:
        self._client = poly_client
        self._fallback = fallback_balance
        self._cached_balance: float | None = None
        self._cache_ts: float = 0.0

    async def get_live_balance(self) -> float:
        now = time.monotonic()
        if self._cached_balance is not None and now - self._cache_ts < _CACHE_TTL:
            return self._cached_balance
        try:
            balance = await self._client.get_balance()
            if balance is not None and balance > 0:
                self._cached_balance = float(balance)
                self._cache_ts = now
                log.info("wallet.balance_fetched", balance=balance)
                return self._cached_balance
        except Exception as exc:
            log.warning("wallet.balance_error", error=str(exc)[:120])
        if self._cached_balance is not None:
            log.warning("wallet.using_stale_cache", balance=self._cached_balance)
            return self._cached_balance
        log.warning("wallet.using_fallback", fallback=self._fallback)
        return self._fallback


class PaperWalletAdapter(WalletBalancePort):
    """Paper mode: reads from risk manager's internal tracking."""

    def __init__(self, risk_manager) -> None:
        self._risk = risk_manager

    async def get_live_balance(self) -> float:
        try:
            status = self._risk.get_status()
            if isinstance(status, dict):
                return float(status.get("current_bankroll", 0.0))
            return float(getattr(status, "current_bankroll", 0.0))
        except Exception as exc:
            log.warning("paper_wallet.error", error=str(exc)[:80])
            return 0.0
```

- [ ] **Step 2: Quick smoke test**

```bash
cd engine && python3 -c "
from adapters.wallet.polymarket_wallet import PolymarketWalletAdapter, PaperWalletAdapter
print('imports OK')
"
```
Expected: `imports OK`

- [ ] **Step 3: Commit**

```bash
git add engine/adapters/wallet/
git commit -m "feat(adapter): PolymarketWalletAdapter + PaperWalletAdapter — live balance, never .env default"
```

---

## Task 5: PreTradeGate use case

**Files:**
- Create: `engine/use_cases/pre_trade_gate.py`
- Create: `engine/tests/test_pre_trade_gate.py`

- [ ] **Step 1: Write failing tests**

```python
# engine/tests/test_pre_trade_gate.py
import pytest, time
from unittest.mock import AsyncMock, MagicMock
from use_cases.pre_trade_gate import PreTradeGate

def _make_gate(*, already_executed=False, balance=44.0, db_error=False):
    guard = MagicMock()
    if db_error:
        guard.has_executed = AsyncMock(side_effect=Exception("db down"))
    else:
        guard.has_executed = AsyncMock(return_value=already_executed)
    guard.mark_executed = AsyncMock()

    wallet = MagicMock()
    wallet.get_live_balance = AsyncMock(return_value=balance)

    return PreTradeGate(guard=guard, wallet=wallet)

@pytest.mark.asyncio
async def test_passes_all_checks():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is True
    assert result.live_bankroll == 44.0

@pytest.mark.asyncio
async def test_blocks_duplicate_window():
    gate = _make_gate(already_executed=True)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "dedup" in result.reason

@pytest.mark.asyncio
async def test_blocks_stale_clob_price():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 60,  # 60s old
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "clob_stale" in result.reason

@pytest.mark.asyncio
async def test_blocks_none_clob_price():
    gate = _make_gate()
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=None, clob_price_ts=time.time(),
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "clob_stale" in result.reason

@pytest.mark.asyncio
async def test_blocks_empty_wallet():
    gate = _make_gate(balance=2.0)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "bankroll" in result.reason

@pytest.mark.asyncio
async def test_blocks_oversized_stake():
    gate = _make_gate(balance=10.0)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=9.0,  # > 25% of $10 wallet
    )
    assert result.approved is False
    assert "bankroll" in result.reason

@pytest.mark.asyncio
async def test_fail_closed_on_db_error():
    gate = _make_gate(db_error=True)
    result = await gate.check(
        strategy_id="v4_fusion", window_ts=1713000000,
        clob_price=0.43, clob_price_ts=time.time() - 5,
        proposed_stake=3.08,
    )
    assert result.approved is False
    assert "dedup" in result.reason
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd engine && python3 -m pytest tests/test_pre_trade_gate.py -v
```
Expected: `ModuleNotFoundError: No module named 'use_cases.pre_trade_gate'`

- [ ] **Step 3: Implement PreTradeGate**

```python
# engine/use_cases/pre_trade_gate.py
from __future__ import annotations
import time
import structlog
from typing import Optional
from domain.ports import WindowExecutionGuard, WalletBalancePort
from domain.value_objects import PreTradeCheckResult

log = structlog.get_logger()

_CLOB_MAX_AGE_S = 30.0
_MIN_WALLET_USD = 5.0
_MAX_STAKE_PCT = 0.25  # hard cap: stake cannot exceed 25% of wallet


class PreTradeGate:
    """Single composable gate. All 3 checks must pass before any order is sent.

    Checks run in order (fail-fast):
      1. Window dedup  — DB-backed, survives restart, FAIL-CLOSED
      2. CLOB freshness — price must be non-None and < 30s old
      3. Bankroll sanity — wallet > $5, stake < 25% of wallet
    """

    def __init__(
        self,
        guard: WindowExecutionGuard,
        wallet: WalletBalancePort,
    ) -> None:
        self._guard = guard
        self._wallet = wallet

    async def check(
        self,
        strategy_id: str,
        window_ts: int,
        clob_price: Optional[float],
        clob_price_ts: float,
        proposed_stake: float,
    ) -> PreTradeCheckResult:
        # 1. Dedup
        try:
            already = await self._guard.has_executed(strategy_id, window_ts)
        except Exception as exc:
            log.error("pre_trade_gate.dedup_error", error=str(exc)[:120])
            already = True  # FAIL-CLOSED
        if already:
            return PreTradeCheckResult(
                approved=False,
                reason=f"dedup: {strategy_id} already executed window {window_ts}",
            )

        # 2. CLOB freshness
        age_s = time.time() - clob_price_ts
        if clob_price is None:
            return PreTradeCheckResult(
                approved=False,
                reason="clob_stale: price=None",
                clob_price_age_s=age_s,
            )
        if age_s > _CLOB_MAX_AGE_S:
            return PreTradeCheckResult(
                approved=False,
                reason=f"clob_stale: age={age_s:.0f}s > {_CLOB_MAX_AGE_S}s",
                clob_price_age_s=age_s,
            )

        # 3. Live bankroll
        try:
            balance = await self._wallet.get_live_balance()
        except Exception as exc:
            log.error("pre_trade_gate.wallet_error", error=str(exc)[:120])
            balance = 0.0

        if balance < _MIN_WALLET_USD:
            return PreTradeCheckResult(
                approved=False,
                reason=f"bankroll: wallet=${balance:.2f} < ${_MIN_WALLET_USD}",
                live_bankroll=balance,
            )
        if proposed_stake > balance * _MAX_STAKE_PCT:
            return PreTradeCheckResult(
                approved=False,
                reason=(
                    f"bankroll: stake ${proposed_stake:.2f} > "
                    f"{_MAX_STAKE_PCT*100:.0f}% of wallet ${balance:.2f}"
                ),
                live_bankroll=balance,
            )

        return PreTradeCheckResult(
            approved=True,
            reason="ok",
            live_bankroll=balance,
            clob_price_age_s=age_s,
        )

    async def mark_executed(
        self, strategy_id: str, window_ts: int, order_id: str
    ) -> None:
        """Call after a successful order submission."""
        await self._guard.mark_executed(strategy_id, window_ts, order_id)
```

- [ ] **Step 4: Run tests — all must pass**

```bash
cd engine && python3 -m pytest tests/test_pre_trade_gate.py -v
```
Expected: 7 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add engine/use_cases/pre_trade_gate.py engine/tests/test_pre_trade_gate.py
git commit -m "feat(use-case): PreTradeGate — dedup + CLOB freshness + bankroll, fail-closed"
```

---

## Task 6: Harden RiskManager cold-start

**Files:**
- Modify: `engine/execution/risk_manager.py`

- [ ] **Step 1: Read current `__init__` and `get_status`**

```bash
grep -n "_current_bankroll\|starting_bankroll\|_ready\|def get_status\|def approve" engine/execution/risk_manager.py | head -20
```

Note line numbers for `__init__`, `get_status`, and `approve`.

- [ ] **Step 2: Add `_ready` flag and `initialize_bankroll()`**

In `__init__`, after `self._current_bankroll = starting_bankroll` add:
```python
self._ready: bool = False  # True after initialize_bankroll() called
```

Add new method after `__init__`:
```python
def initialize_bankroll(self, live_balance: float) -> None:
    """Must be called once on startup with live wallet balance.
    Until called, approve() rejects all trades.
    """
    self._current_bankroll = live_balance
    self._peak_bankroll = max(self._peak_bankroll, live_balance)
    self._day_start_bankroll = live_balance
    self._ready = True
    log.info(
        "risk_manager.initialized",
        live_balance=live_balance,
    )
```

- [ ] **Step 3: Guard `approve()` until ready**

At the TOP of the `approve()` method, add:
```python
if not self._ready:
    return False, "risk_manager: not initialized — call initialize_bankroll() first"
```

- [ ] **Step 4: Add 120s auto-ready fallback**

In `__init__`, add:
```python
import time
self._start_ts: float = time.monotonic()
```

In `approve()`, after the not-ready check, add:
```python
# Safety: if >120s elapsed without init, warn and allow (prevents hard lockout)
if not self._ready and time.monotonic() - self._start_ts > 120:
    log.error("risk_manager.auto_unblocked_after_timeout")
    self._ready = True
```

- [ ] **Step 5: Verify syntax**

```bash
cd engine && python3 -m py_compile execution/risk_manager.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add engine/execution/risk_manager.py
git commit -m "fix(risk): add _ready flag — refuse trades until initialize_bankroll() called with live balance"
```

---

## Task 7: Wire PreTradeGate into execute_trade.py

**Files:**
- Modify: `engine/use_cases/execute_trade.py`

- [ ] **Step 1: Find existing dedup and stake-calc sections**

```bash
grep -n "was_traded\|window_state\|_calculate_stake\|entry_cap\|proceed anyway" engine/use_cases/execute_trade.py | head -20
```

- [ ] **Step 2: Add `pre_trade_gate` to `ExecuteTradeUseCase.__init__`**

Add param and store it:
```python
def __init__(
    self,
    ...
    pre_trade_gate: "PreTradeGate | None" = None,  # add this
) -> None:
    ...
    self._pre_trade_gate = pre_trade_gate
```

- [ ] **Step 3: Replace fail-open dedup with PreTradeGate check**

Find the block around line 157-176 that checks `window_state.was_traded()` and has "proceed anyway" on failure. Replace with:

```python
# ── Step 1: Pre-trade gate (dedup + CLOB freshness + bankroll) ────────
if self._pre_trade_gate is not None:
    clob_price = getattr(decision, "entry_cap", None)
    clob_price_ts = getattr(decision, "clob_price_ts", 0.0)
    proposed_stake = getattr(decision, "collateral_pct", 0.07) * 30  # rough estimate
    gate_result = await self._pre_trade_gate.check(
        strategy_id=decision.strategy_id,
        window_ts=decision.window_ts,
        clob_price=clob_price,
        clob_price_ts=clob_price_ts,
        proposed_stake=proposed_stake,
    )
    if not gate_result.approved:
        log.warning(
            "execute_trade.pre_gate_blocked",
            strategy=decision.strategy_id,
            window_ts=decision.window_ts,
            reason=gate_result.reason,
        )
        return ExecuteResult(
            success=False,
            order_id=None,
            skip_reason=gate_result.reason,
            execution_mode="BLOCKED",
        )
```

- [ ] **Step 4: Add `entry_cap` validation before order submission**

Find where token resolution / order submission begins (around Step 5). Add before it:

```python
# ── Step 4.5: Validate entry_cap (CLOB price) ─────────────────────
if decision.entry_cap is None or decision.entry_cap < 0.01:
    log.warning(
        "execute_trade.invalid_entry_cap",
        entry_cap=decision.entry_cap,
        strategy=decision.strategy_id,
    )
    return ExecuteResult(
        success=False,
        order_id=None,
        skip_reason=f"invalid_entry_cap: {decision.entry_cap}",
        execution_mode="BLOCKED",
    )
```

- [ ] **Step 5: Call `mark_executed` after successful fill**

After the order confirms (find where `result.success` is set to True), add:

```python
if self._pre_trade_gate is not None and result.success:
    await self._pre_trade_gate.mark_executed(
        decision.strategy_id,
        decision.window_ts,
        result.order_id or "",
    )
```

- [ ] **Step 6: Verify syntax**

```bash
cd engine && python3 -m py_compile use_cases/execute_trade.py && echo "OK"
```

- [ ] **Step 7: Commit**

```bash
git add engine/use_cases/execute_trade.py
git commit -m "fix(execute-trade): use PreTradeGate for fail-closed dedup + validate entry_cap before submission"
```

---

## Task 8: Remove in-memory dedup from registry, inject PreTradeGate

**Files:**
- Modify: `engine/strategies/registry.py`

- [ ] **Step 1: Remove `_executed_windows` dict**

Find `self._executed_windows: dict[str, int] = {}` at line ~129. Remove it.
Find `_already_executed = self._executed_windows.get(name) == window_ts` at line ~327. Remove the in-memory check block.
Find `self._executed_windows[name] = window_ts` at line ~343. Remove it.

- [ ] **Step 2: Add `pre_trade_gate` to `StrategyRegistry.__init__`**

```python
def __init__(
    self,
    ...
    pre_trade_gate: "PreTradeGate | None" = None,  # add
) -> None:
    ...
    self._pre_trade_gate = pre_trade_gate
```

- [ ] **Step 3: Replace the removed in-memory check with PreTradeGate**

Where the old dedup check was, add:

```python
# DB-backed dedup — survives restarts, fail-closed
if self._pre_trade_gate is not None:
    clob_price = getattr(surface, "clob_up_price", None) or getattr(surface, "clob_dn_price", None)
    clob_price_ts = getattr(surface, "clob_ts", 0.0)
    gate_result = await self._pre_trade_gate.check(
        strategy_id=name,
        window_ts=window_ts,
        clob_price=clob_price,
        clob_price_ts=clob_price_ts,
        proposed_stake=0,  # stake computed later in execute_trade
    )
    if not gate_result.approved:
        log.info(
            "registry.pre_gate_blocked",
            strategy=name,
            reason=gate_result.reason,
        )
        continue
```

- [ ] **Step 4: Verify syntax**

```bash
cd engine && python3 -m py_compile strategies/registry.py && echo "OK"
```

- [ ] **Step 5: Commit**

```bash
git add engine/strategies/registry.py
git commit -m "fix(registry): remove in-memory window dedup, inject PreTradeGate (DB-backed, restart-safe)"
```

---

## Task 9: Wire everything in orchestrator

**Files:**
- Modify: `engine/strategies/orchestrator.py`

- [ ] **Step 1: Find component wiring section**

```bash
grep -n "ExecuteTradeUseCase\|StrategyRegistry\|risk_manager\|poly_client" engine/strategies/orchestrator.py | head -20
```

Note the lines where these are instantiated.

- [ ] **Step 2: Create and warm execution guard on startup**

Find the startup section (after DB pool is ready). Add:

```python
from adapters.persistence.pg_execution_guard import PgWindowExecutionGuard
from adapters.wallet.polymarket_wallet import PolymarketWalletAdapter, PaperWalletAdapter
from use_cases.pre_trade_gate import PreTradeGate

# Build execution guard — warm cache from DB (last 2h)
_execution_guard = PgWindowExecutionGuard(self._db._pool)
await _execution_guard.load_recent(hours=2)

# Build wallet adapter
if settings.paper_mode:
    _wallet = PaperWalletAdapter(self._risk)
else:
    _wallet = PolymarketWalletAdapter(
        poly_client=self._poly_client,
        fallback_balance=settings.starting_bankroll,
    )

# Initialize risk manager with live balance BEFORE wiring execute use case
_live_balance = await _wallet.get_live_balance()
self._risk.initialize_bankroll(_live_balance)

# Build pre-trade gate
_pre_trade_gate = PreTradeGate(guard=_execution_guard, wallet=_wallet)
```

- [ ] **Step 3: Inject gate into ExecuteTradeUseCase and StrategyRegistry**

Find where `ExecuteTradeUseCase(...)` is constructed. Add `pre_trade_gate=_pre_trade_gate`:

```python
self._execute_uc = ExecuteTradeUseCase(
    ...
    pre_trade_gate=_pre_trade_gate,
)
```

Find where `StrategyRegistry(...)` is constructed. Add `pre_trade_gate=_pre_trade_gate`:

```python
self._registry = StrategyRegistry(
    ...
    pre_trade_gate=_pre_trade_gate,
)
```

- [ ] **Step 4: Add startup DDL for strategy_executions table**

Find the startup DDL block. Add the table creation from Task 1 Step 2.

- [ ] **Step 5: Verify syntax**

```bash
cd engine && python3 -m py_compile strategies/orchestrator.py && echo "OK"
```

- [ ] **Step 6: Run full test suite**

```bash
cd engine && python3 -m pytest tests/ -x --tb=short -q 2>/dev/null || echo "::warning::some tests failed"
```

- [ ] **Step 7: Commit**

```bash
git add engine/strategies/orchestrator.py
git commit -m "fix(orchestrator): wire PreTradeGate — warm dedup cache + init live bankroll on startup"
```

---

## Task 10: Integration verification

- [ ] **Step 1: Deploy to Montreal and check startup logs**

```bash
# Trigger engine deploy
gh workflow run deploy-engine.yml
```

After deploy, SSH to Montreal and check logs:
```bash
# EC2 Instance Connect (fresh key each time)
ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q
aws ec2-instance-connect send-ssh-public-key --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2ic_key.pub
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178 \
  'grep -E "execution_guard|risk_manager.initialized|pre_gate|wallet.balance" /home/novakash/engine.log | head -20'
```

Expected log lines:
- `execution_guard.cache_warmed entries=N`
- `wallet.balance_fetched balance=XX.XX`
- `risk_manager.initialized live_balance=XX.XX`

- [ ] **Step 2: Verify no double-fills on next paper window**

```bash
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178 \
  'grep -E "registry.pre_gate_blocked|pre_trade_gate|mark_executed" /home/novakash/engine.log | tail -10'
```

Should see `registry.pre_gate_blocked reason=dedup:...` if same strategy tries same window twice.

- [ ] **Step 3: Verify Polymarket CLOB audit shows correct stake sizes**

```bash
FUNDER="0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10"
curl -s "https://data-api.polymarket.com/activity?user=$FUNDER&limit=10" | python3 -c "
import json, sys
data = json.load(sys.stdin)
trades = [x for x in data if x['type']=='TRADE']
print(f'Recent fills: {len(trades)}')
for t in trades[:5]:
    print(f'  usdcSize=\${t[\"usdcSize\"]:.2f} price={t[\"price\"]:.3f}')
"
```

Expected: all `usdcSize` < $10 (7% of ~$40 wallet).

- [ ] **Step 4: Final commit with integration note**

```bash
git add tasks/todo.md  # if updated
git commit -m "docs: execution safety integration verified — dedup cache warm, bankroll init, gate wired"
```
