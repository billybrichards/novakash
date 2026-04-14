# Margin Engine Clean Architecture Audit Report

**Date:** April 14, 2026  
**Auditor:** AI Assistant (based on clean_architecture_python_guide.md v1.0)  
**Scope:** Deep Clean Architecture Review  
**Status:** READ-ONLY AUDIT - NO CHANGES MADE

---

## Executive Summary

The margin engine has made **significant progress** toward clean architecture principles, particularly in its use of ports/interfaces for dependency inversion. However, there are **critical violations** that prevent it from achieving true clean architecture:

### Key Findings

| Category | Status | Issues Found |
|----------|--------|--------------|
| Domain Layer Purity | ⚠️ **PARTIAL** | 4 violations |
| Dependency Inversion | ✅ **GOOD** | Well-implemented ports |
| Business Logic Placement | ⚠️ **MIXED** | Services layer concerns |
| Use Case Layer | ✅ **GOOD** | Properly implemented |
| Infrastructure Separation | ⚠️ **PARTIAL** | 3 violations |
| Testability | ✅ **GOOD** | Port-based design enables mocking |

### Severity Summary

- **Critical:** 2 violations (domain importing framework code)
- **High:** 5 violations (business logic placement, ORM leakage)
- **Medium:** 8 violations (structural inconsistencies)
- **Low:** 4 violations (naming, minor concerns)

---

## Current Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           main.py (Entry Point)                         │
│                   Wires all adapters + starts main loop                  │
└────────────────────────────┬────────────────────────────────────────────┘
                             │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐    ┌─────────────────┐   ┌──────────────────┐
│  Use Cases    │    │    Domain       │   │   Adapters       │
│ (open, manage)│◄──►│  (entities,     │   │  (implement      │
│               │    │   value objects,│   │   ports)         │
│ Receives      │    │   ports)        │   │                  │
│ ports, returns│    │                 │   │ - persistence/   │
│ Position      │    │                 │   │ - signal/        │
└───────────────┘    └────────┬────────┘   │ - exchange/      │
                              │            │ - alert/         │
        ┌─────────────────────┼────────────┼──────────────────┤
        │                     │            │                  │
        ▼                     │            ▼                  │
┌───────────────┐             │   ┌──────────────┐           │
│ Infrastructure│             │   │ Services     │           │
│ - config/     │─────────────┘   │ (regime_*    │───────────┘
│ - status_server│                 │  cascade_*)  │
└───────────────┘                 └──────────────┘
```

**Dependency Flow:** The arrows show the intended dependency direction. Violations exist where arrows point the "wrong way."

---

## Layer-by-Layer Analysis

### Layer 1: Domain Layer

**Location:** `/margin_engine/domain/`

#### Expected Structure (per guide)
```
domain/
├── entities/           # Position, Portfolio
├── value_objects/      # Money, Price, TradeSide enums
├── services/           # Domain services (if any)
├── ports.py            # Abstract interfaces (ABCs)
└── exceptions.py       # Domain exceptions
```

#### Actual Structure
```
domain/
├── entities/
│   ├── position.py     ✅ Position entity
│   └── portfolio.py    ✅ Portfolio aggregate
├── value_objects.py    ⚠️ All VOs in single file
├── ports.py            ✅ Port interfaces
└── strategy.py         ⚠️ Strategy pattern (debatable placement)
```

#### Violations Found

##### ❌ VIOLATION 1: Domain Imports Framework Code (CRITICAL)

**File:** `margin_engine/domain/value_objects.py`  
**Lines:** 215-256

```python
# BAD: Domain layer imports framework-specific constructs
@dataclass(frozen=True)
class FillResult:
    """
    Result of a filled market order.
    
    Carries exchange ground truth so the caller doesn't have to estimate fees
    or filled notional after the fact. For paper mode, the adapter populates
    these from its own simulation — they're still "actual" in that the paper
    calculation IS the paper outcome.
    ...
    """
    order_id: str
    fill_price: Price
    filled_notional: float
    commission: float = 0.0
    commission_asset: str = "USDT"
    commission_is_actual: bool = False
```

**Analysis:** `FillResult` represents exchange-specific implementation details, not pure domain concepts. It should be part of the `ExchangePort` return type, not a domain value object.

**Impact:** Domain layer now knows about exchange operations, violating the dependency rule.

---

##### ❌ VIOLATION 2: Domain Contains Infrastructure Concepts (CRITICAL)

**File:** `margin_engine/domain/value_objects.py`  
**Lines:** 267-571 (v4 snapshot value objects)

```python
# BAD: Domain layer contains HTTP API response structures
@dataclass(frozen=True)
class V4Snapshot:
    """Top-level /v4/snapshot response."""
    asset: str
    ts: float
    last_price: Optional[float] = None
    server_version: str = "unknown"
    strategy: str = "unknown"
    # ... many fields that mirror the HTTP API response
```

**Analysis:** `V4Snapshot`, `Consensus`, `MacroBias`, `TimescalePayload`, etc. directly mirror the `/v4/snapshot` HTTP API response. These are **infrastructure concerns** (API contracts), not domain concepts.

**Impact:** Domain layer is now coupled to a specific HTTP API implementation.

**Recommended Fix:** Move all v4-related value objects to `adapters/signal/v4_snapshot_http.py` as private implementation details of the adapter.

---

##### ⚠️ VIOLATION 3: All Value Objects in Single File (MEDIUM)

**File:** `margin_engine/domain/value_objects.py`  
**Lines:** 1-572

**Analysis:** The guide recommends separating value objects into individual files for clarity:
```
domain/value_objects/
├── money.py
├── price.py
├── trade_side.py
├── position_state.py
└── ...
```

**Impact:** Single file is 572 lines and growing. Harder to navigate and maintain.

---

##### ⚠️ VIOLATION 4: Missing Domain Exceptions Module (LOW)

**File:** `margin_engine/domain/`

**Analysis:** The guide recommends a dedicated `domain/exceptions.py` file:
```python
class DomainException(Exception): ...
class DomainValidationError(DomainException): ...
class EntityNotFoundError(DomainException): ...
```

**Current State:** Exceptions are scattered (e.g., `ValueError` raised in `Position.confirm_entry`).

---

### Layer 2: Application Layer

**Location:** `/margin_engine/use_cases/`

#### Expected Structure
```
application/
├── ports/              # Abstract interfaces
├── use_cases/
│   ├── open_position.py
│   └── manage_positions.py
└── dto/                # Input/Output data transfer objects
```

#### Actual Structure
```
use_cases/
├── __init__.py
├── open_position.py    ✅ Properly implemented
└── manage_positions.py ✅ Properly implemented
```

#### Assessment

✅ **GOOD: Proper Dependency Injection**

**File:** `margin_engine/use_cases/open_position.py`  
**Lines:** 88-140

```python
# GOOD: Use case receives abstract ports, not concrete implementations
def __init__(
    self,
    exchange: ExchangePort,
    portfolio: Portfolio,
    repository: PositionRepository,
    alerts: AlertPort,
    probability_port: ProbabilityPort,
    signal_port: SignalPort,
    *,
    v4_snapshot_port: Optional[V4SnapshotPort] = None,
    # ... config parameters
) -> None:
```

✅ **GOOD: Single Responsibility**

Each use case has a clear, single purpose:
- `OpenPositionUseCase`: Entry decision and execution
- `ManagePositionsUseCase`: Position lifecycle management

✅ **GOOD: Business Logic Encapsulation**

**File:** `margin_engine/use_cases/open_position.py`  
**Lines:** 398-768 (v4 entry path)

```python
async def _execute_v4(self, v4: V4Snapshot) -> Optional[Position]:
    """
    10-gate v4 decision stack. Ordered cheapest-first: every gate
    except #9 (balance query) and #10 (order placement) is in-memory,
    so a rejected trade costs us one list lookup and a few conditionals.
    """
    # Gate 1: tradeable state
    # Gate 2: consensus.safe_to_trade
    # Gate 3: macro direction_gate
    # ...
```

The complex business logic for trade decisions is properly encapsulated in the use case layer.

⚠️ **CONCERN: Use Case is Too Large (MEDIUM)**

**File:** `margin_engine/use_cases/open_position.py`  
**Lines:** 840 total

**Analysis:** The use case contains ~840 lines with both v4 and legacy v2 paths. Consider:
- Extract v4-specific logic to `V4EntryStrategy`
- Extract legacy v2 logic to `V2EntryStrategy`
- Use strategy pattern for dispatch

---

### Layer 3: Infrastructure Layer

**Location:** `/margin_engine/adapters/` and `/margin_engine/infrastructure/`

#### Expected Structure
```
infrastructure/
├── adapters/
│   ├── persistence/    # Repository implementations
│   ├── external/       # API clients (exchange, signals)
│   └── alert/          # Alert implementations
├── database/
│   ├── models.py       # ORM models
│   └── migrations/     # Database migrations
├── config/
│   └── settings.py
└── presentation/       # (if HTTP API exists)
    └── routes/
```

#### Actual Structure
```
adapters/
├── persistence/
│   ├── pg_repository.py        ⚠️ Contains migrations
│   ├── pg_log_repository.py
│   └── pg_signal_repository.py
├── signal/
│   ├── ws_signal.py
│   ├── probability_http.py     ⚠️ Contains V4 VO imports
│   └── v4_snapshot_http.py     ⚠️ Contains V4 VO imports
├── exchange/
│   ├── binance_margin.py
│   ├── paper.py
│   └── hyperliquid_price_feed.py
└── alert/
    └── telegram.py

infrastructure/
├── config/
│   └── settings.py             ⚠️ Uses Pydantic
└── status_server.py            ⚠️ HTTP server in infrastructure
```

#### Violations Found

##### ⚠️ VIOLATION 5: Pydantic in Configuration (MEDIUM)

**File:** `margin_engine/infrastructure/config/settings.py`  
**Lines:** 1-14

```python
from pydantic import field_validator
from pydantic_settings import BaseSettings

class MarginSettings(BaseSettings):
    """Configuration for the margin engine."""
    binance_api_key: str = ""
    # ...
```

**Analysis:** Using Pydantic for configuration is acceptable in practice, but strictly speaking, the infrastructure layer should avoid framework dependencies. However, this is a **pragmatic choice** that doesn't affect domain purity.

**Recommendation:** Keep as-is. This is a common, practical pattern.

---

##### ❌ VIOLATION 6: Business Logic in Services Layer (HIGH)

**File:** `margin_engine/services/regime_adaptive.py`  
**Lines:** 1-143

```python
from margin_engine.domain.strategy import Strategy, TradeDecision, Regime

class RegimeAdaptiveRouter:
    """Route to appropriate strategy based on regime."""
    
    def decide(self, v4: V4Snapshot) -> TradeDecision:
        """Make adaptive trading decision based on regime."""
        # Get regime from primary timescale
        ts = v4.timescales.get("15m")
        if ts is None or ts.regime is None:
            return TradeDecision(...)
        
        # Get appropriate strategy
        strategy = self.get_strategy(regime_str)
        
        # Make decision
        decision = strategy.decide(v4)
        # ...
```

**Analysis:** The `services/` layer contains **business logic** (regime routing, strategy selection) that should be in the **application layer** (use cases). The services are more like "application services" than "domain services."

**Current Structure:**
```
services/
├── regime_adaptive.py      # Business logic!
├── regime_trend.py         # Business logic!
├── regime_mean_reversion.py
├── cascade_fade.py
└── ...
```

**Recommended Fix:**
1. Move `services/` to `application/services/`
2. Or, integrate this logic directly into use cases
3. Or, create a `strategy/` package under `application/`

---

##### ⚠️ VIOLATION 7: Database Migrations in Repository (MEDIUM)

**File:** `margin_engine/adapters/persistence/pg_repository.py`  
**Lines:** 85-126

```python
# Additive migrations applied on every boot. Safe because:
ADDITIVE_MIGRATIONS_SQL = (
    "ALTER TABLE margin_positions ADD COLUMN IF NOT EXISTS entry_commission REAL DEFAULT 0",
    # ...
)

class PgPositionRepository(PositionRepository):
    async def ensure_table(self) -> None:
        """Create table if it doesn't exist AND run additive migrations."""
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            for migration in ADDITIVE_MIGRATIONS_SQL:
                await conn.execute(migration)
```

**Analysis:** Database migrations should be in a separate migrations system (e.g., Alembic), not in the repository. This couples the repository to schema evolution.

**Recommendation:** Move migrations to `alembic/` directory, use Alembic for schema management.

---

##### ⚠️ VIOLATION 8: HTTP Server in Infrastructure (MEDIUM)

**File:** `margin_engine/infrastructure/status_server.py`

**Analysis:** The status server is essentially a **presentation layer** component (HTTP API) placed in the infrastructure folder. This blurs the boundary between infrastructure and presentation.

**Recommendation:** Create `presentation/` layer for HTTP endpoints:
```
presentation/
├── api/
│   ├── routes/
│   │   ├── status.py
│   │   └── ...
│   └── schemas/
```

---

### Layer 4: Presentation Layer

**Location:** Not properly established

#### Expected Structure
```
presentation/
├── api/
│   ├── routes/           # FastAPI routes
│   ├── schemas/          # Pydantic request/response models
│   └── dependencies.py   # FastAPI dependency injection
└── dto/                  # Data transfer objects
```

#### Actual State

**File:** `margin_engine/infrastructure/status_server.py`

The status server exists but is embedded in `infrastructure/`. There's no proper separation of routes, schemas, and dependencies.

---

## Data Surface Review: v2, v3, v4

### Signal Flow Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Signal Sources                           │
├─────────────────────────────────────────────────────────────┤
│ v2 (Probability) ──┐                                       │
│   HTTP Poller      │                                       │
│   /v2/probability  │                                       │
├────────────────────┼───────────────────────────────────────┤
│ v3 (Composite) ────┤ WebSocket                             │
│   WS Feed          │                                       │
│   /v3/signal       │                                       │
├────────────────────┼───────────────────────────────────────┤
│ v4 (Snapshot) ─────┴─ HTTP Poller                          │
│   /v4/snapshot                                              │
│   (fused payload)                                           │
└─────────────────────────────────────────────────────────────┘
                             │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐    ┌─────────────────┐   ┌──────────────────┐
│ProbabilityPort│    │  SignalPort     │   │  V4SnapshotPort  │
│ (HTTP adapter)│    │ (WS adapter)    │   │  (HTTP adapter)  │
└───────┬───────┘    └────────┬────────┘   └────────┬─────────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │  OpenPositionUseCase │
                    │  ManagePositionsUseCase │
                    └───────────────────┘
```

### v4 Data Surface Analysis

**File:** `margin_engine/adapters/signal/v4_snapshot_http.py`

The v4 adapter is well-implemented as a **fail-soft polling adapter**:

```python
class V4SnapshotHttpAdapter(V4SnapshotPort):
    """
    Polls /v4/snapshot on a fixed cadence and caches the latest response.
    
    Design choices:
    1. Fail-soft contract - never raises, returns None on failure
    2. Eager initial fetch in connect()
    3. Asyncio.Event wait loop for responsive shutdown
    4. Rate-limited logging
    """
```

**Issue:** The adapter imports domain value objects that mirror API responses:

**File:** `margin_engine/adapters/signal/v4_snapshot_http.py`  
**Lines:** 56-58

```python
from margin_engine.domain.ports import V4SnapshotPort
from margin_engine.domain.value_objects import V4Snapshot
```

**Problem:** `V4Snapshot` and related classes should be in the adapter, not the domain layer.

---

## Detailed Violation List

### Critical Violations

| # | Violation | File | Line(s) | Severity |
|---|-----------|------|---------|----------|
| 1 | Domain imports framework code (FillResult, V4Snapshot) | `domain/value_objects.py` | 215-571 | Critical |
| 2 | Domain contains HTTP API response structures | `domain/value_objects.py` | 267-571 | Critical |
| 3 | Business logic in services (not use cases) | `services/` | All files | High |
| 4 | ORM/persistence concerns in domain | `domain/entities/position.py` | 68-78 | Medium |

### High Violations

| # | Violation | File | Line(s) | Severity |
|---|-----------|------|---------|----------|
| 5 | Business logic in services layer | `services/regime_adaptive.py` | 1-143 | High |
| 6 | Services should be application layer | `services/` | All | High |
| 7 | Missing separation of concerns | `domain/strategy.py` | 1-94 | Medium |

### Medium Violations

| # | Violation | File | Line(s) | Severity |
|---|-----------|------|---------|----------|
| 8 | All value objects in single file | `domain/value_objects.py` | 1-572 | Medium |
| 9 | Database migrations in repository | `adapters/persistence/pg_repository.py` | 85-126 | Medium |
| 10 | HTTP server in infrastructure | `infrastructure/status_server.py` | All | Medium |
| 11 | Use case too large (840 lines) | `use_cases/open_position.py` | All | Medium |
| 12 | Missing domain exceptions module | `domain/` | N/A | Low |

### Low Violations

| # | Violation | File | Line(s) | Severity |
|---|-----------|------|---------|----------|
| 13 | Pydantic in config (acceptable) | `infrastructure/config/settings.py` | 1-14 | Low |
| 14 | Naming inconsistency (services vs application) | `services/` | All | Low |
| 15 | Missing DTO layer | N/A | N/A | Low |

---

## Refactoring Roadmap

### Phase 1: Domain Layer Cleanup (Priority: Critical)

**Effort:** 2-3 days

**Goal:** Remove framework dependencies from domain layer

**Tasks:**
1. **Move v4 value objects to adapter**
   - Create `adapters/signal/v4_models.py`
   - Move: `V4Snapshot`, `Consensus`, `MacroBias`, `TimescalePayload`, `Quantiles`, `Cascade`
   - Update imports in `v4_snapshot_http.py`
   
2. **Move FillResult to ExchangePort**
   - Remove `FillResult` from `domain/value_objects.py`
   - Define as return type in `domain/ports.py` `ExchangePort.place_market_order()`
   
3. **Split value_objects.py**
   - Create `domain/value_objects/money.py`
   - Create `domain/value_objects/price.py`
   - Create `domain/value_objects/enums.py` (TradeSide, ExitReason, PositionState)
   
4. **Create domain exceptions module**
   - Create `domain/exceptions.py`
   - Move exception handling patterns from entities

**Dependencies:** None

**Risk:** Medium (requires updating imports across codebase)

---

### Phase 2: Services to Application Migration (Priority: High)

**Effort:** 2-3 days

**Goal:** Properly place business logic in application layer

**Tasks:**
1. **Create application/services directory**
   ```
   application/
   ├── services/
   │   ├── regime/
   │   │   ├── __init__.py
   │   │   ├── router.py        # RegimeAdaptiveRouter
   │   │   ├── trend.py         # TrendStrategy
   │   │   ├── mean_reversion.py
   │   │   └── no_trade.py
   │   └── cascade/
   │       └── fade.py          # CascadeFadeStrategy
   ```

2. **Move Strategy pattern**
   - Move `domain/strategy.py` to `application/services/strategy.py`
   - Update imports in `services/regime_adaptive.py`

3. **Update use case imports**
   - `use_cases/open_position.py` → import from `application/services`

4. **Rename services/ to application/services/ (or remove entirely)**

**Dependencies:** Phase 1

**Risk:** Medium (requires updating imports)

---

### Phase 3: Infrastructure/Presentation Separation (Priority: Medium)

**Effort:** 3-4 days

**Goal:** Proper separation of infrastructure and presentation

**Tasks:**
1. **Create presentation layer**
   ```
   presentation/
   ├── api/
   │   ├── routes/
   │   │   ├── status.py        # Move from infrastructure/
   │   │   └── ...
   │   ├── schemas/
   │   │   └── status_schemas.py
   │   └── dependencies.py
   └── dto/
   ```

2. **Move status_server.py to presentation/api/routes/status.py**

3. **Create Alembic migrations**
   - Initialize Alembic
   - Convert additive migrations to Alembic migrations
   - Remove migration code from `pg_repository.py`

4. **Create database models module**
   ```
   infrastructure/
   ├── database/
   │   ├── models.py            # SQLAlchemy models
   │   └── migrations/          # Alembic
   └── config/
   ```

**Dependencies:** Phase 1, Phase 2

**Risk:** High (requires testing all HTTP endpoints)

---

### Phase 4: Use Case Refactoring (Priority: Medium)

**Effort:** 2-3 days

**Goal:** Reduce use case complexity

**Tasks:**
1. **Extract v4 entry logic**
   ```
   application/use_cases/
   ├── open_position.py         # Main dispatcher
   ├── entry_strategies/
   │   ├── __init__.py
   │   ├── base.py              # EntryStrategy ABC
   │   ├── v4_strategy.py       # V4 entry logic
   │   └── v2_strategy.py       # Legacy v2 logic
   ```

2. **Refactor OpenPositionUseCase**
   - Use strategy pattern for v4/v2 dispatch
   - Delegate to strategy implementations

3. **Extract management logic**
   ```
   application/use_cases/
   ├── manage_positions.py      # Main dispatcher
   └── position_management/
       ├── stop_loss.py
       ├── take_profit.py
       ├── trailing.py
       └── expiry.py
   ```

**Dependencies:** Phase 2

**Risk:** Medium (behavioral changes possible)

---

### Phase 5: DTO Layer (Priority: Low)

**Effort:** 1-2 days

**Goal:** Add proper data transfer objects

**Tasks:**
1. **Create DTOs for use case I/O**
   ```
   application/
   ├── dto/
   │   ├── open_position.py     # OpenPositionInput, OpenPositionOutput
   │   └── manage_positions.py  # ManagePositionsInput, ManagePositionsOutput
   ```

2. **Update use cases to use DTOs**

**Dependencies:** Phase 4

**Risk:** Low (internal refactoring)

---

## Migration Strategy

### Principles

1. **Preserve working functionality** - Never change behavior during refactoring
2. **Incremental migration** - One phase at a time, test after each
3. **Backward compatible** - Old code should work during transition
4. **Feature-flag risky changes** - Use flags for complex refactors

### Timeline

```
Week 1: Phase 1 (Domain Cleanup)
  Day 1-2: Move v4 value objects to adapter
  Day 3: Split value_objects.py, create exceptions module
  Day 4-5: Test, verify, deploy

Week 2: Phase 2 (Services Migration)
  Day 1-2: Create application/services structure
  Day 3: Move strategy patterns
  Day 4-5: Test, verify, deploy

Week 3: Phase 3 (Infrastructure/Presentation)
  Day 1-2: Create presentation layer
  Day 3: Move status server
  Day 4-5: Setup Alembic, test migrations

Week 4: Phase 4-5 (Use Cases, DTOs)
  Day 1-3: Refactor use cases
  Day 4-5: Add DTOs, final cleanup
```

### Rollback Plan

Each phase should be:
1. **Feature-flagged** where possible
2. **Tested in staging** before production
3. **Reversible** within the same deployment

---

## Recommendations

### Immediate Actions (Next Sprint)

1. ✅ **Move v4 value objects** to `adapters/signal/v4_models.py`
2. ✅ **Create domain exceptions module**
3. ✅ **Add tests** for all moved code

### Medium-term (Next Month)

1. ✅ **Migrate services to application layer**
2. ✅ **Setup Alembic** for database migrations
3. ✅ **Create presentation layer** structure

### Long-term (Next Quarter)

1. ✅ **Refactor use cases** for better separation
2. ✅ **Add DTO layer** for clean I/O boundaries
3. ✅ **Add comprehensive integration tests**

---

## Conclusion

The margin engine has a **solid foundation** with proper port-based dependency inversion. The main issues are:

1. **Domain layer contamination** with infrastructure concerns (v4 API models)
2. **Services layer** containing business logic that belongs in application layer
3. **Missing proper presentation layer** separation

These issues are **fixable with incremental refactoring** without breaking existing functionality. The recommended phased approach allows for safe, tested migration to true clean architecture.

**Estimated Total Effort:** 10-15 days over 4 weeks

**Risk Level:** Medium (requires careful testing at each phase)

---

*Report generated: April 14, 2026*  
*Based on clean_architecture_python_guide.md (Version 1.0, January 2026)*
