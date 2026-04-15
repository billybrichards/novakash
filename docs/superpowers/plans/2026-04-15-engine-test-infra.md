# Engine Test Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 135 pre-existing pytest failures + 14 collection errors in `engine/tests/`, add CI enforcement with hard coverage gate on execution-path use cases, and prevent dual-import regressions — all without touching production runtime behavior.

**Architecture:** Three-tier test pyramid aligned with clean-arch layers. Root cause fixes: (1) Settings subclass split so `Settings()` no longer crashes on import in test env; (2) canonical imports (no `engine.` prefix) to eliminate dual-load `isinstance` bugs; (3) port-contract tests on the 5 execution-path use cases to guard the class of bugs that shipped in #206/#207/#208.

**Tech Stack:** pytest 8 + pytest-asyncio + pytest-xdist + pytest-cov + pytest-env + aiosqlite. GitHub Actions CI. Pydantic v2 BaseSettings. Python 3.12.

**Ships as 5 independently-mergeable PRs against `develop`** (primary branch per Billy's convention).

---

## Ground rules

- Base branch: `develop` (not `main`). Primary is `develop`.
- Each phase = one PR. Merge in order 1→2→3→4→5.
- Worktree already isolated: `/Users/billyrichards/Code/novakash/.claude/worktrees/nice-germain`. Stay here.
- Tests run from `engine/` cwd: `cd engine && pytest ...`. Conftest assumes this.
- No production runtime changes except `engine/config/settings.py` (add `TestSettings`, stop eager `Settings()` instantiation) + the 5 call sites that read the module-level `settings` singleton.
- Commit after every passing step with a scoped message. Never squash across tasks.
- Tests in `engine/tests/` currently use `from engine.domain.x import Y` (with `engine.` prefix). Production code uses `from domain.x import Y` (bare). That mismatch IS the dual-import bug class — Phase 2 rewrites all test imports to bare.

---

## File structure

Files created or modified across all phases:

```
engine/
├── config/
│   └── settings.py                         # MODIFY: add TestSettings, remove eager Settings()
├── main.py                                 # MODIFY: use get_settings()
├── alerts/telegram.py                      # MODIFY: use get_settings()
├── persistence/db_client.py                # MODIFY: use get_settings()
├── infrastructure/runtime.py               # MODIFY: use get_settings()
├── infrastructure/composition.py           # MODIFY: use get_settings()
├── requirements-test.txt                   # CREATE
└── tests/
    ├── conftest.py                         # MODIFY: autouse TestSettings env + sibling path removal
    ├── fixtures/
    │   ├── __init__.py                     # CREATE
    │   ├── domain.py                       # CREATE
    │   ├── ports.py                        # CREATE
    │   └── infra.py                        # CREATE
    ├── test_no_dual_import.py              # CREATE (Phase 2 regression)
    ├── unit/                               # MODIFY imports (strip engine. prefix)
    ├── use_cases/                          # MODIFY imports (strip engine. prefix)
    └── integration/                        # CREATE tier (Phase 5 scaffolding)

.github/workflows/
├── ci.yml                                  # MODIFY: add engine-tests-fast job + canonical-imports lint
└── nightly.yml                             # CREATE (Phase 5)

docs/
├── engine-testing.md                       # CREATE (Phase 5)
└── superpowers/plans/
    └── 2026-04-15-engine-test-triage.md    # CREATE (Phase 4 meta-log of triage decisions)
```

---

# Phase 1 — Unblock local + test settings split

**Goal:** `pytest engine/tests/ --collect-only` returns 0 errors from a fresh clone with `pip install -r engine/requirements.txt -r engine/requirements-test.txt`. Production `Settings` stays strict. Tests get `TestSettings` with safe defaults.

**PR title:** `test(engine): split Settings + add requirements-test.txt — unblock pytest collection`

---

### Task 1.1: Create `engine/requirements-test.txt`

**Files:**
- Create: `engine/requirements-test.txt`

- [ ] **Step 1: Write the file**

```txt
# engine/requirements-test.txt
# Test-only dependencies. Kept separate from requirements.txt so the
# production container image stays minimal.

pytest>=8.0.0
pytest-asyncio>=0.23.0
pytest-xdist>=3.6.0
pytest-cov>=5.0.0
pytest-env>=1.1.0
aiosqlite>=0.20.0
pyyaml>=6.0              # Used by strategy config loader; missing causes collection error
structlog>=24.1.0        # Used by engine logging; missing causes collection error
freezegun>=1.5.0         # Time-freezing for heartbeat/reconcile tests
```

- [ ] **Step 2: Install and verify**

Run:
```bash
pip install -r engine/requirements.txt -r engine/requirements-test.txt
python -c "import yaml, structlog, aiosqlite, freezegun; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add engine/requirements-test.txt
git commit -m "test(engine): add requirements-test.txt with yaml/structlog/aiosqlite deps"
```

---

### Task 1.2: Add `TestSettings` subclass + kill eager `Settings()` instantiation

**Files:**
- Modify: `engine/config/settings.py`

**Context:** Line 134 of `engine/config/settings.py` has `settings = Settings()` at module load, with `database_url: str = Field(...)` required. Importing anything that transitively imports `config.settings` crashes in test envs without `.env`. Fix: lazy-load via `get_settings()`, add `TestSettings` subclass with safe defaults, keep `settings` as a lazy `LazyProxy` that calls `get_settings()` on first attribute access so existing 5 call sites don't break mid-PR.

- [ ] **Step 1: Write the failing test**

**File:** `engine/tests/test_settings_split.py` (CREATE)

```python
"""Regression test for Phase 1 Settings split.

TestSettings MUST have defaults for every required field in Settings so
importing it produces no ValidationError even when no env vars are set.
"""
from __future__ import annotations

import os


def test_test_settings_instantiates_without_env(monkeypatch):
    """TestSettings() must not raise in a bare env."""
    # Strip every env var that Settings might read
    for key in list(os.environ):
        if key.startswith(("DATABASE_", "POLY_", "BINANCE_", "COINGLASS_",
                           "TIINGO_", "POLYGON_", "TELEGRAM_", "OPINION_",
                           "OPENROUTER_", "PAPER_", "STARTING_")):
            monkeypatch.delenv(key, raising=False)

    from config.settings import TestSettings
    s = TestSettings()
    assert s.database_url.startswith("sqlite")
    assert s.paper_mode is True


def test_get_settings_returns_settings_instance():
    """get_settings() returns a Settings (not TestSettings) in prod mode."""
    from config.settings import Settings, get_settings
    # Set DATABASE_URL so prod Settings() validates
    import os
    os.environ["DATABASE_URL"] = "postgresql://test"
    try:
        s = get_settings()
        assert isinstance(s, Settings)
    finally:
        del os.environ["DATABASE_URL"]


def test_module_level_settings_is_lazy():
    """Importing config.settings must NOT instantiate Settings eagerly."""
    # If the module-level `settings = Settings()` still exists and env is
    # unset, this import would raise. We only import here; no attribute access.
    import config.settings  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd engine && pytest tests/test_settings_split.py -v`

Expected: FAIL — `ImportError: cannot import name 'TestSettings'` OR `ValidationError: database_url required`.

- [ ] **Step 3: Modify `engine/config/settings.py`**

Replace the bottom of the file (the `settings = Settings()` line and anything after it). Show the NEW tail of the file:

```python
# ... (all existing field definitions above stay unchanged) ...


class TestSettings(Settings):
    """Test defaults — NEVER instantiate in production paths.

    Subclasses Settings so every required field gets a safe default.
    Imported only by the test suite (tests/conftest.py and fixtures/).
    """
    database_url: str = "sqlite+aiosqlite:///:memory:"
    poly_private_key: str = "test"
    poly_api_key: str = "test"
    poly_api_secret: str = "test"
    poly_api_passphrase: str = "test"
    poly_funder_address: str = "0x0000000000000000000000000000000000000000"
    opinion_api_key: str = "test"
    opinion_wallet_key: str = "test"
    binance_api_key: str = "test"
    binance_api_secret: str = "test"
    coinglass_api_key: str = "test"
    openrouter_api_key: str = "test"
    tiingo_api_key: str = "test"
    polygon_rpc_url: str = "https://test.invalid"
    telegram_bot_token: str = "test"
    telegram_chat_id: str = "0"
    starting_bankroll: float = 500.0
    paper_mode: bool = True


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the singleton Settings instance, loading lazily."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _reset_settings_for_tests() -> None:
    """Test helper — clears the cached singleton. Not for prod use."""
    global _settings
    _settings = None


class _LazySettingsProxy:
    """Backwards-compat shim for `from config.settings import settings`.

    Forwards attribute access to the lazily-loaded real Settings object.
    Existing call sites that do `settings.database_url` keep working without
    needing the refactor in Phase 1 Task 1.3 — that task is cleanup.
    """
    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(get_settings(), name, value)


settings = _LazySettingsProxy()  # type: ignore[assignment]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd engine && pytest tests/test_settings_split.py -v`

Expected: PASS (3 tests).

- [ ] **Step 5: Verify existing call sites still work via the proxy**

Run:
```bash
cd engine && DATABASE_URL=postgresql://test python -c "from config.settings import settings; print(settings.paper_mode)"
```

Expected: prints `True` (default) with no crash.

- [ ] **Step 6: Commit**

```bash
git add engine/config/settings.py engine/tests/test_settings_split.py
git commit -m "test(config): add TestSettings subclass + lazy settings proxy

Module-level Settings() instantiation crashed test collection when
database_url env var was unset. TestSettings subclass has defaults for
all required fields; _LazySettingsProxy preserves existing call-site
ergonomics until Task 1.3 sweeps them to get_settings()."
```

---

### Task 1.3: Sweep `settings` call sites to `get_settings()`

**Files:**
- Modify: `engine/main.py`
- Modify: `engine/alerts/telegram.py`
- Modify: `engine/persistence/db_client.py`
- Modify: `engine/infrastructure/runtime.py`
- Modify: `engine/infrastructure/composition.py`

**Context:** Proxy works but imports coupling test and prod settings is sloppy. Each consumer calls `get_settings()` once at module init (or inside functions where appropriate), gets a real `Settings` instance, uses it locally. Clean-arch DI pattern.

- [ ] **Step 1: Grep for existing uses**

Run:
```bash
cd engine && grep -rn "from config.settings import settings\|from config.settings import.*settings" . --include="*.py"
```

Expected output:
```
main.py:18:from config.settings import settings
alerts/telegram.py:200:                from config.settings import settings
persistence/db_client.py:21:from config.settings import Settings
infrastructure/runtime.py:23:from config.settings import Settings  # noqa: F401
infrastructure/composition.py:22:from config.settings import Settings
```

- [ ] **Step 2: Update `engine/main.py`**

Change line 18 (and any usage downstream):

```python
# BEFORE
from config.settings import settings

# AFTER
from config.settings import get_settings
settings = get_settings()
```

- [ ] **Step 3: Update `engine/alerts/telegram.py`**

Change the inline import on line 200:

```python
# BEFORE
                from config.settings import settings

# AFTER
                from config.settings import get_settings
                settings = get_settings()
```

- [ ] **Step 4: Verify the other three files already import `Settings` (the class, not `settings` singleton)**

Run:
```bash
cd engine && grep -n "Settings" persistence/db_client.py infrastructure/runtime.py infrastructure/composition.py | head -10
```

Expected: they import the class `Settings` and accept it as a parameter (DI pattern already in place). No changes needed in these three. Confirm no module-level call to `Settings()` that'd crash without env:

```bash
cd engine && grep -n "Settings()" persistence/db_client.py infrastructure/runtime.py infrastructure/composition.py
```

Expected: no hits (or only inside function bodies — those are fine, they run at runtime with env set).

- [ ] **Step 5: Remove the `_LazySettingsProxy` shim**

The proxy exists only to avoid breaking Steps 2–4 during this phase. Now that call sites use `get_settings()`, we can drop the `settings` module attribute entirely.

In `engine/config/settings.py`, delete these lines at the bottom:

```python
class _LazySettingsProxy:
    ...  # delete entire class

settings = _LazySettingsProxy()  # type: ignore[assignment]  # delete
```

Keep `get_settings()`, `_reset_settings_for_tests()`, and `TestSettings`.

- [ ] **Step 6: Verify with import smoke**

Run:
```bash
cd engine && DATABASE_URL=postgresql://test python -c "
from config.settings import get_settings, TestSettings
s = get_settings()
print('prod:', type(s).__name__)
t = TestSettings()
print('test:', type(t).__name__, t.database_url)
"
```

Expected:
```
prod: Settings
test: TestSettings sqlite+aiosqlite:///:memory:
```

- [ ] **Step 7: Run the Task 1.2 test to confirm nothing broke**

Run: `cd engine && pytest tests/test_settings_split.py -v`

Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add engine/main.py engine/alerts/telegram.py engine/config/settings.py
git commit -m "refactor(config): use get_settings() at call sites, drop lazy proxy

Completes Phase 1 Settings split. Production code now reaches Settings
via explicit get_settings() call rather than a module-level singleton
instantiated at import time. Eliminates import-time ValidationError in
test environments without a full .env."
```

---

### Task 1.4: Update `engine/tests/conftest.py` — autouse TestSettings env

**Files:**
- Modify: `engine/tests/conftest.py`

- [ ] **Step 1: Replace the conftest**

```python
"""Engine test conftest — sys.path + TestSettings env bootstrap.

Engine modules use absolute bare imports (`from domain.ports import X`,
`from use_cases.reconcile_positions import Y`) so the test process needs
`engine/` on sys.path. The PARENT of engine/ must NOT be on sys.path —
that would enable `from engine.domain.ports import X` style imports,
causing the same symbols to load twice under different module paths
(dual-load). The `engine.X`-vs-`X` dual-load is the root cause of the
`test_publish_heartbeat` isinstance quirk; Phase 2 sweeps test imports
to the canonical bare form and adds a regression test.

Run pytest from the engine/ directory:
    cd engine && pytest tests/
or with explicit path:
    pytest engine/tests/
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- sys.path canonicalization --------------------------------------------
ENGINE_ROOT = Path(__file__).resolve().parent.parent  # engine/
REPO_ROOT = ENGINE_ROOT.parent                         # repo root

# Insert engine/ first so `from domain.x import Y` resolves.
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

# Remove repo root if present — it enables `from engine.domain.x import Y`
# which collides with the bare form and creates dual-loaded module copies.
# Phase 2 enforces the bare form via CI grep; this is defense in depth.
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))

# --- TestSettings env bootstrap -------------------------------------------
# Set env vars BEFORE any engine module imports. Settings() still uses
# env-var lookup as primary source, so this satisfies required fields even
# for code paths that call Settings() directly (pre-sweep leftovers) and
# for the prod get_settings() path when a test explicitly opts out of
# TestSettings.
_DEFAULTS = {
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "POLY_PRIVATE_KEY": "test",
    "POLY_API_KEY": "test",
    "POLY_API_SECRET": "test",
    "POLY_API_PASSPHRASE": "test",
    "POLY_FUNDER_ADDRESS": "0x0000000000000000000000000000000000000000",
    "OPINION_API_KEY": "test",
    "OPINION_WALLET_KEY": "test",
    "BINANCE_API_KEY": "test",
    "BINANCE_API_SECRET": "test",
    "COINGLASS_API_KEY": "test",
    "OPENROUTER_API_KEY": "test",
    "TIINGO_API_KEY": "test",
    "POLYGON_RPC_URL": "https://test.invalid",
    "TELEGRAM_BOT_TOKEN": "test",
    "TELEGRAM_CHAT_ID": "0",
    "PAPER_MODE": "true",
}
for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)

# --- pytest-asyncio mode --------------------------------------------------
# Set default asyncio mode to auto for backward compat with tests that use
# plain `async def test_*` without explicit @pytest.mark.asyncio decoration.
# New tests SHOULD decorate explicitly; this is tolerance for legacy code.
import pytest  # noqa: E402  (after sys.path munging)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers + pytest-asyncio mode."""
    config.addinivalue_line("markers", "unit: pure domain/VO test, no I/O")
    config.addinivalue_line("markers", "use_case: use case test with mocked ports")
    config.addinivalue_line(
        "markers",
        "integration: real DB or real adapter — nightly only",
    )
    config.addinivalue_line("markers", "slow: >1s runtime — excluded from PR fast subset")
```

- [ ] **Step 2: Sanity check the conftest loads**

Run:
```bash
cd engine && python -c "
import sys
sys.path.insert(0, 'tests')
# Trigger conftest.py execution via pytest
" && pytest tests/test_settings_split.py --collect-only -q
```

Expected:
```
3 tests collected in 0.XXs
```

- [ ] **Step 3: Commit**

```bash
git add engine/tests/conftest.py
git commit -m "test(engine): conftest bootstraps TestSettings env + removes repo-root from sys.path

sibling-path removal is defense in depth against dual-load: Phase 2 will
enforce via CI grep. Env defaults satisfy any Settings() call that slips
through the get_settings() sweep. Markers registered for the three-tier
pyramid (unit / use_case / integration)."
```

---

### Task 1.5: Create `engine/tests/fixtures/` scaffold

**Files:**
- Create: `engine/tests/fixtures/__init__.py`
- Create: `engine/tests/fixtures/domain.py`
- Create: `engine/tests/fixtures/ports.py`
- Create: `engine/tests/fixtures/infra.py`

**Context:** Fixtures package holds explicit builders. Tests opt in with `from tests.fixtures.domain import make_window_state`. Keeps per-test mocks DRY, lets reviewers see exactly what's being built.

- [ ] **Step 1: Create `__init__.py`**

```python
"""Test fixture builders — explicit, importable.

Pattern: entity/VO builders live in domain.py, port fakes in ports.py,
infrastructure stubs (sessions, settings) in infra.py. Tests import
explicitly — no pytest magic discovery for these.
"""
```

- [ ] **Step 2: Create `domain.py` — entity/VO builders**

```python
"""Domain-layer test builders.

One factory per entity/value object. Kwargs override defaults. Builders
must NOT import adapters or infrastructure — they only produce domain
objects.
"""
from __future__ import annotations

import time
from typing import Any


def make_window_state(**overrides: Any) -> dict:
    """Build a minimal WindowState payload for tests.

    Returns a dict (not the dataclass) to stay framework-agnostic — callers
    wrap in the actual domain dataclass if needed. This mirrors the
    clean-arch guide's Result/DTO pattern.
    """
    defaults = {
        "asset": "BTC",
        "window_ts": int(time.time()),
        "eval_offset": 120,
        "direction": "UP",
        "confidence": 0.75,
    }
    defaults.update(overrides)
    return defaults


def make_trade(**overrides: Any) -> dict:
    """Build a minimal Trade payload for tests."""
    defaults = {
        "trade_id": "test-trade-1",
        "strategy_id": "v4_fusion",
        "asset": "BTC",
        "direction": "UP",
        "size_usd": 10.0,
        "entry_price": 0.50,
        "mode": "PAPER",
        "status": "OPEN",
        "opened_ts": int(time.time()),
    }
    defaults.update(overrides)
    return defaults


def make_strategy_decision(**overrides: Any) -> dict:
    """Build a minimal StrategyDecision payload."""
    defaults = {
        "strategy_id": "test",
        "version": "1.0",
        "action": "TRADE",
        "direction": "UP",
        "mode": "LIVE",
        "size_usd": 10.0,
        "reason": "test",
    }
    defaults.update(overrides)
    return defaults
```

- [ ] **Step 3: Create `ports.py` — port fakes**

```python
"""Port (interface) fakes for use-case tests.

Pattern from clean-arch guide §Testing Strategy: "Use cases with mocks"
means mocking ports, not concrete infrastructure. Each fake implements
the minimal interface required to satisfy the port contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock


def fake_trade_repository() -> AsyncMock:
    """Return an AsyncMock conforming to ITradeRepository."""
    mock = AsyncMock()
    mock.save.return_value = None
    mock.find_by_id.return_value = None
    mock.find_unresolved_paper_trades.return_value = []
    mock.fetch_trades.return_value = []
    mock.manual_trades_joined_poly_fills.return_value = []
    return mock


def fake_window_repository() -> AsyncMock:
    """Return an AsyncMock conforming to IWindowRepository."""
    mock = AsyncMock()
    mock.get_actual_direction.return_value = None
    mock.save_window_state.return_value = None
    return mock


def fake_risk_manager(*, paper_mode: bool = True) -> AsyncMock:
    """Return a fake RiskManager that approves by default."""
    mock = AsyncMock()
    mock.approve_bet.return_value = True
    mock.get_status.return_value = {
        "paper_mode": paper_mode,
        "bankroll": 500.0,
        "drawdown": 0.0,
        "kill_switch_active": False,
    }
    return mock


def fake_alerts_gateway() -> AsyncMock:
    """Return a fake telegram-alerts gateway."""
    mock = AsyncMock()
    mock.send_system_alert.return_value = True
    mock.send_trade_alert.return_value = True
    return mock


def fake_execution_guard() -> AsyncMock:
    """Return a fake PgWindowExecutionGuard that permits all executions."""
    mock = AsyncMock()
    mock.try_claim.return_value = True
    mock.is_claimed.return_value = False
    return mock
```

- [ ] **Step 4: Create `infra.py` — infra stubs**

```python
"""Infrastructure test stubs — settings, sessions.

For `integration` tier tests, real SQLite sessions live here. For fast
tests, callers should prefer `ports.py` fakes over real DB.
"""
from __future__ import annotations

from typing import AsyncIterator

from config.settings import TestSettings


def test_settings(**overrides) -> TestSettings:
    """Return a TestSettings instance with optional field overrides."""
    return TestSettings(**overrides)


async def sqlite_session() -> AsyncIterator:
    """Create an in-memory SQLite async session for integration tests.

    Usage:
        async for session in sqlite_session():
            repo = SQLTradeRepository(session)
            ...
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
```

- [ ] **Step 5: Smoke-test the fixtures import cleanly**

Run:
```bash
cd engine && python -c "
from tests.fixtures.domain import make_window_state, make_trade, make_strategy_decision
from tests.fixtures.ports import fake_trade_repository, fake_risk_manager
from tests.fixtures.infra import test_settings
print('domain:', make_window_state())
print('ports:', fake_risk_manager().get_status())
print('infra:', type(test_settings()).__name__)
"
```

Expected: prints each without import errors.

- [ ] **Step 6: Commit**

```bash
git add engine/tests/fixtures/
git commit -m "test(engine): add fixtures/ package — domain builders + port fakes + infra stubs

Three-file split: domain.py for entity/VO builders, ports.py for
AsyncMock port fakes, infra.py for settings + sqlite session. Explicit
import pattern — no pytest magic discovery."
```

---

### Task 1.6: Verify Phase 1 exit criteria — pytest --collect-only clean

- [ ] **Step 1: Run full collection**

Run:
```bash
cd engine && pytest tests/ --collect-only -q 2>&1 | tail -20
```

- [ ] **Step 2: Inspect for errors**

Expected: the last line reads `<N> tests collected` with zero `ERROR` entries. If any collection errors remain, they are either:
- Missing test deps → add to `requirements-test.txt` (revisit Task 1.1)
- Import-time Settings() calls → grep and fix (revisit Task 1.3)
- Something unrelated → log in plan as Task 1.6.1 follow-up

- [ ] **Step 3: Run the test suite with `-x --co` to ensure NO import-time exceptions**

Run:
```bash
cd engine && pytest tests/ -x --co --tb=line 2>&1 | grep -E "^(ERROR|FAIL|collected)" | head -20
```

Expected: only `collected N items` (no ERROR/FAIL during collection).

- [ ] **Step 4: Commit any residual fixes from Step 2**

If Step 2 found import-time issues, address them, then:

```bash
git add <files>
git commit -m "test(engine): fix residual collection errors — <details>"
```

If zero residual issues: skip this step.

- [ ] **Step 5: Log Phase 1 result in plan**

Add a line to `docs/superpowers/plans/2026-04-15-engine-test-infra.md` under Phase 1:

```markdown
**Phase 1 result (YYYY-MM-DD):** N tests collected, 0 collection errors.
```

Commit:
```bash
git add docs/superpowers/plans/2026-04-15-engine-test-infra.md
git commit -m "docs(plan): record Phase 1 collection result"
```

---

### Task 1.7: Open Phase 1 PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin claude/nice-germain
```

- [ ] **Step 2: Create PR against `develop`**

```bash
gh pr create --base develop --title "test(engine): Phase 1 — Settings split + requirements-test.txt + fixtures scaffold" --body "$(cat <<'EOF'
## Summary

Phase 1 of the engine test infra plan ([spec](docs/superpowers/specs/2026-04-15-engine-test-infra-design.md), [plan](docs/superpowers/plans/2026-04-15-engine-test-infra.md)).

- `engine/requirements-test.txt` with pytest-xdist, pytest-cov, pytest-env, aiosqlite, yaml, structlog, freezegun
- `TestSettings` subclass in `engine/config/settings.py` with defaults for every required field
- `get_settings()` lazy accessor replaces module-level `Settings()` instantiation (which crashed test collection when DATABASE_URL was unset)
- `engine/tests/conftest.py` autouses TestSettings env + registers `unit` / `use_case` / `integration` / `slow` markers
- `engine/tests/fixtures/` package with `domain.py` (entity builders), `ports.py` (port fakes), `infra.py` (settings/session stubs)

**Before:** `pytest engine/tests/ --collect-only` fails with 14 import errors.
**After:** 0 collection errors.

Production runtime untouched — only `engine/config/settings.py` and 5 call sites change, and they go through the same Settings class at runtime.

## Test plan

- [ ] `pytest engine/tests/test_settings_split.py -v` → 3 pass
- [ ] `pytest engine/tests/ --collect-only` → 0 errors
- [ ] `python -c "from config.settings import get_settings; print(get_settings())"` (with real `.env`) → prints Settings instance
- [ ] Spot-check existing engine boots against the change (`python -m engine.main --help` or local dev start)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Wait for CI green**

CI currently does only import-smoke for hub/margin-engine/frontend. Those should pass unchanged. Manually verify no regressions.

- [ ] **Step 4: Merge to develop**

Only after review + CI green:

```bash
gh pr merge --squash
```

---

# Phase 2 — Canonical imports + dual-load kill

**Goal:** Every test in `engine/tests/` uses bare imports (`from domain.x`, NOT `from engine.domain.x`). CI grep enforces. `test_publish_heartbeat` 2 failures resolved.

**PR title:** `test(engine): Phase 2 — canonical imports, kill dual-load isinstance bug`

---

### Task 2.1: Grep test imports using the `engine.` prefix

- [ ] **Step 1: Enumerate offenders**

Run:
```bash
cd engine && grep -rn "from engine\." tests/ --include="*.py" | wc -l
grep -rn "from engine\." tests/ --include="*.py" | head -30
```

Expected: non-zero count. Record the exact count. Typical offenders: `from engine.domain.value_objects import ...`.

- [ ] **Step 2: Check for `import engine.X` form (less common)**

Run:
```bash
cd engine && grep -rn "import engine\." tests/ --include="*.py"
```

Expected: usually empty. If non-empty, flag for Task 2.2.

- [ ] **Step 3: Record baseline count in plan**

Append to Phase 2 section of `docs/superpowers/plans/2026-04-15-engine-test-infra.md`:

```markdown
**Phase 2 baseline:** N `from engine.` imports in tests/ as of commit <sha>.
```

Commit:
```bash
git add docs/superpowers/plans/2026-04-15-engine-test-infra.md
git commit -m "docs(plan): Phase 2 import sweep baseline"
```

---

### Task 2.2: Rewrite test imports — strip `engine.` prefix

**Files:**
- Modify: every file in `engine/tests/` matching the grep (enumerated in Task 2.1).

- [ ] **Step 1: Run scripted rewrite**

The rewrite is mechanical. Use `sed` on each matching file — but one at a time so each change is greppable in git log. DO NOT use `sed -i` blindly across the whole tree.

For each file found in Task 2.1, apply:

```bash
cd engine
# Example for one file:
FILE="tests/unit/strategies/test_v10_gate_strategy.py"
sed -i.bak 's|from engine\.|from |g; s|import engine\.|import |g' "$FILE"
rm "$FILE.bak"
```

Repeat per file. Commit after each handful of files:

```bash
git add tests/unit/strategies/test_v10_gate_strategy.py
git commit -m "test(engine): strip engine. prefix from test_v10_gate_strategy imports"
```

Alternative batched approach (one commit for the whole sweep — fine if confident):

```bash
cd engine
grep -rl "from engine\.\|import engine\." tests/ --include="*.py" | \
  xargs sed -i.bak 's|from engine\.|from |g; s|import engine\.|import |g'
find tests -name "*.bak" -delete
```

- [ ] **Step 2: Verify sweep is complete**

Run:
```bash
cd engine && grep -rn "from engine\.\|import engine\." tests/ --include="*.py"
```

Expected: no output (empty).

- [ ] **Step 3: Also strip from production code if any leaked**

Run:
```bash
cd engine && grep -rn "from engine\.\|import engine\." --include="*.py" \
  | grep -v "tests/"
```

Expected: empty. If non-empty, apply same sed to those files and commit separately:

```bash
git add <files>
git commit -m "refactor(engine): strip stray engine. prefix from prod imports"
```

- [ ] **Step 4: Run collection to make sure rewrite didn't break imports**

Run:
```bash
cd engine && pytest tests/ --collect-only -q 2>&1 | tail -5
```

Expected: same `N tests collected` as Phase 1 exit, 0 errors. If errors appear, it means some module was genuinely only reachable via `engine.X` — revisit the offending import manually.

- [ ] **Step 5: Commit the sweep**

```bash
git add engine/tests/
git commit -m "test(engine): canonical imports — strip engine. prefix across test suite

Dual-load cause: tests imported symbols via \`engine.domain.X\` while prod
imported via \`domain.X\`, loading the same class twice under different
module paths. isinstance() returns False across the duplicate, leading
to the test_publish_heartbeat \`isinstance(x, RiskStatus)\` failure."
```

---

### Task 2.3: Add `test_no_dual_import.py` regression

**Files:**
- Create: `engine/tests/test_no_dual_import.py`

- [ ] **Step 1: Write the test**

```python
"""Regression guard: canonical imports only.

Dual-load happens when both \`domain.x\` and \`engine.domain.x\` appear in
sys.modules — Python treats them as distinct modules, making isinstance()
false across the duplicate. This test asserts we have exactly one copy
loaded, with the canonical bare name.
"""
from __future__ import annotations

import sys

import pytest


# Modules that MUST only appear under the bare name in sys.modules.
_CANONICAL_MODULES = [
    "domain.value_objects",
    "domain.ports",
    "use_cases.reconcile_positions",
    "use_cases.execute_trade",
    "use_cases.publish_heartbeat",
    "config.settings",
]


@pytest.mark.parametrize("mod_name", _CANONICAL_MODULES)
def test_no_engine_prefixed_duplicate(mod_name: str):
    """\`engine.{mod_name}\` must NOT be in sys.modules after importing the canonical form."""
    # Ensure the canonical module is loaded
    __import__(mod_name)
    duplicate = f"engine.{mod_name}"
    assert duplicate not in sys.modules, (
        f"Dual-load detected: both \`{mod_name}\` and \`{duplicate}\` are in "
        f"sys.modules. This breaks isinstance() checks. "
        f"Check sys.path for the engine/ parent directory."
    )


def test_sys_path_does_not_contain_engine_parent():
    """engine/ parent (repo root) must NOT be on sys.path.

    Conftest removes it; if something puts it back, this test fires.
    """
    from pathlib import Path

    import config.settings  # trigger a known canonical import
    engine_root = Path(config.settings.__file__).parent.parent  # engine/
    repo_root = engine_root.parent

    assert str(repo_root) not in sys.path, (
        f"Repo root {repo_root} is on sys.path — enables \`from engine.X\` "
        f"imports that dual-load canonical modules."
    )
```

- [ ] **Step 2: Run the test**

Run:
```bash
cd engine && pytest tests/test_no_dual_import.py -v
```

Expected: all parametrized cases PASS + `test_sys_path_does_not_contain_engine_parent` PASS.

- [ ] **Step 3: Commit**

```bash
git add engine/tests/test_no_dual_import.py
git commit -m "test(engine): add dual-load regression guard

Asserts \`engine.X\` never appears alongside canonical \`X\` in sys.modules,
and repo root is not on sys.path. Catches any future reintroduction of
engine.-prefixed imports that'd break isinstance() checks."
```

---

### Task 2.4: Fix `test_publish_heartbeat` 2 failures

**Files:**
- Read: `engine/tests/use_cases/test_publish_heartbeat.py`
- Potentially modify: same

**Context:** The 2 failures were caused by `isinstance(x, RiskStatus)` returning False when test-imported `RiskStatus` via `engine.domain.value_objects` while use-case-imported via `domain.value_objects`. After Task 2.2 sweep, the test-side import is now canonical. Likely the test just passes now. Verify.

- [ ] **Step 1: Run the two failing tests**

Run:
```bash
cd engine && pytest tests/use_cases/test_publish_heartbeat.py::test_tick_writes_heartbeat tests/use_cases/test_publish_heartbeat.py::test_sitrep_sent_at_interval -v
```

Expected: both PASS.

- [ ] **Step 2: If still failing, read the failure**

Run:
```bash
cd engine && pytest tests/use_cases/test_publish_heartbeat.py -v --tb=long 2>&1 | tail -60
```

Analyze the traceback. If the failure mode has shifted (different assertion, different exception), the dual-load fix wasn't the root cause. Fix the actual assertion — e.g. if the test mocks `RiskManager.get_status()` to return an object but the use case now expects a dict (per #206), update the test to return a dict.

Example fix (hypothetical — verify actual file content first):

```python
# BEFORE
mock_risk.get_status.return_value = RiskStatus(
    paper_mode=True, bankroll=500.0, drawdown=0.0, kill_switch_active=False
)

# AFTER — align with #206's change accepting dict
mock_risk.get_status.return_value = {
    "paper_mode": True,
    "bankroll": 500.0,
    "drawdown": 0.0,
    "kill_switch_active": False,
}
```

- [ ] **Step 3: Re-run and verify PASS**

Run:
```bash
cd engine && pytest tests/use_cases/test_publish_heartbeat.py -v
```

Expected: all tests in the file PASS.

- [ ] **Step 4: Commit**

If no code changes needed (Step 1 passed directly):

```bash
git commit --allow-empty -m "test(heartbeat): verify dual-load fix resolved both heartbeat failures"
```

If code changes were needed:

```bash
git add engine/tests/use_cases/test_publish_heartbeat.py
git commit -m "test(heartbeat): align fixture with #206 dict-shaped RiskManager.get_status"
```

---

### Task 2.5: Add CI canonical-imports lint step

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Append a new job to `ci.yml`**

Read current `ci.yml`, then add this job under `jobs:`:

```yaml
  engine-canonical-imports:
    name: Engine - canonical imports lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Fail if any file imports via engine. prefix
        run: |
          set -e
          offenders=$(grep -rn "from engine\.\|import engine\." engine/ --include="*.py" || true)
          if [ -n "$offenders" ]; then
            echo "::error::Found imports via 'engine.' prefix. Use bare imports (from domain.X, not from engine.domain.X)."
            echo "$offenders"
            exit 1
          fi
          echo "Canonical imports OK."
```

- [ ] **Step 2: Verify YAML validity**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no output (valid YAML).

- [ ] **Step 3: Verify the grep in isolation**

Run locally:
```bash
grep -rn "from engine\.\|import engine\." engine/ --include="*.py" || echo "OK - no offenders"
```

Expected: `OK - no offenders`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add engine canonical-imports lint job

Fails PR if any file under engine/ uses \`from engine.X\` or
\`import engine.X\` form. Enforces single-path imports to prevent
dual-load isinstance regression (see test_no_dual_import.py)."
```

---

### Task 2.6: Open Phase 2 PR

- [ ] **Step 1: Push**

```bash
git push
```

- [ ] **Step 2: PR**

```bash
gh pr create --base develop --title "test(engine): Phase 2 — canonical imports, kill dual-load isinstance bug" --body "$(cat <<'EOF'
## Summary

Phase 2. Strips \`engine.\` prefix from all test imports, adds CI lint to keep it that way, and lands a regression test for dual-load.

- Test suite import sweep: \`from engine.X\` → \`from X\` across ~N files (see diff)
- \`engine/tests/test_no_dual_import.py\` regression asserts no \`engine.X\` duplicate in sys.modules
- \`engine/tests/conftest.py\` already removes repo-root from sys.path (Phase 1)
- \`.github/workflows/ci.yml\` fails PR if anything imports via \`engine.\` prefix
- \`test_publish_heartbeat\` 2 failures resolved by the sweep (no code changes to heartbeat logic)

## Test plan

- [ ] \`pytest engine/tests/test_no_dual_import.py -v\` → PASS
- [ ] \`pytest engine/tests/use_cases/test_publish_heartbeat.py -v\` → PASS
- [ ] CI canonical-imports lint green
- [ ] \`grep -rn "from engine\\." engine/ --include="*.py"\` → empty

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Merge on green**

---

# Phase 3 — Execution-path hard gate

**Goal:** 6 execution-path modules (5 use cases + `pg_execution_guard`) at ≥90% line coverage. 4 specific failing tests fixed. Port-contract tests added. CI `--cov-fail-under=90` enforces.

**PR title:** `test(engine): Phase 3 — execution-path coverage gate + port contracts`

---

### Task 3.1: Fix `test_reconcile_positions::test_no_match_returns_none`

**Files:**
- Read: `engine/tests/use_cases/test_reconcile_positions.py`
- Read: `engine/use_cases/reconcile_positions.py`

**Context:** Per Billy's diagnosis, this test asserts `alerts.send_system_alert.assert_called_once()`. Likely the reconcile use case was refactored in #207 (stale-import fix) and the test fixture now needs to seed the right pre-condition.

- [ ] **Step 1: Read the failing test**

```bash
cd engine && pytest tests/use_cases/test_reconcile_positions.py::test_no_match_returns_none -v --tb=long 2>&1 | tail -30
```

Read the exact assertion and line number of the failure. Record:

- Expected: \_\_\_
- Actual: \_\_\_
- Line in test: \_\_\_

- [ ] **Step 2: Read `engine/use_cases/reconcile_positions.py` around the no-match path**

```bash
cd engine && grep -n "send_system_alert\|no.*match\|alerts" use_cases/reconcile_positions.py | head -20
```

Find where the no-match case is handled. Is `send_system_alert` called there? If yes, figure out why the test's mock isn't seeing the call. If no, the test is stale — it asserted an alert that the use case no longer fires.

- [ ] **Step 3: Decide fix direction**

Two options — pick based on the reconcile_positions.py logic:

**Option A — Test is stale:** The use case legitimately no longer calls `send_system_alert` on no-match (by design). Update the test assertion to match current behavior.

**Option B — Regression in use case:** The use case SHOULD call `send_system_alert` on no-match but a recent change broke it. Fix the use case to fire the alert again.

- [ ] **Step 4: Apply the fix**

**If Option A:** Replace the assertion:

```python
# BEFORE
alerts.send_system_alert.assert_called_once()

# AFTER
alerts.send_system_alert.assert_not_called()  # no-match is silent by design
```

**If Option B:** Add the alert call back into `reconcile_positions.py` in the no-match branch. Code must match the pattern already used for other alert calls in that file — read for context before editing.

- [ ] **Step 5: Run test**

```bash
cd engine && pytest tests/use_cases/test_reconcile_positions.py::test_no_match_returns_none -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/tests/use_cases/test_reconcile_positions.py [engine/use_cases/reconcile_positions.py]
git commit -m "test(reconcile): fix test_no_match_returns_none — <brief reason>"
```

---

### Task 3.2: Fix `test_execute_manual_trade::test_live_mode_places_real_order`

**Files:**
- Read: `engine/tests/use_cases/test_execute_manual_trade.py`
- Read: `engine/use_cases/execute_manual_trade.py`

**Context:** Live-mode path is the highest-risk spot in the codebase. If this test is failing for a real reason, it's critical. If it's a stale mock, we still need to make it bulletproof because Phase 3's coverage gate locks it in.

- [ ] **Step 1: Run the test**

```bash
cd engine && pytest tests/use_cases/test_execute_manual_trade.py::test_live_mode_places_real_order -v --tb=long 2>&1 | tail -40
```

Record the failure mode.

- [ ] **Step 2: Read the production path**

```bash
cd engine && sed -n '1,50p' use_cases/execute_manual_trade.py
cd engine && grep -n "live\|LIVE\|paper_mode\|place_order\|execute" use_cases/execute_manual_trade.py | head -20
```

Understand the live-mode execution branch. What port does it call? With what arguments?

- [ ] **Step 3: Align test mock with production contract**

Update the test's port fakes so the mock returns what the use case expects. Use `tests/fixtures/ports.py` if a suitable fake exists; if not, inline the mock with `AsyncMock()`.

Example (adjust to actual file):

```python
# Import fakes from fixtures
from tests.fixtures.ports import fake_trade_repository, fake_risk_manager


async def test_live_mode_places_real_order():
    repo = fake_trade_repository()
    risk = fake_risk_manager(paper_mode=False)
    executor = AsyncMock()
    executor.place_order.return_value = {"order_id": "abc", "status": "FILLED"}

    uc = ExecuteManualTradeUseCase(
        trade_repository=repo,
        risk_manager=risk,
        executor=executor,
    )

    result = await uc.execute(
        asset="BTC",
        direction="UP",
        size_usd=10.0,
        mode="LIVE",
    )

    # Contract: live mode calls executor.place_order exactly once
    executor.place_order.assert_called_once()
    # Contract: trade is persisted
    repo.save.assert_called_once()
    assert result.status == "FILLED"
```

- [ ] **Step 4: Run test**

```bash
cd engine && pytest tests/use_cases/test_execute_manual_trade.py::test_live_mode_places_real_order -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/tests/use_cases/test_execute_manual_trade.py
git commit -m "test(manual_trade): fix test_live_mode_places_real_order — use port-contract asserts"
```

---

### Task 3.3: Fix `test_evaluate_window` 2 failures

**Files:**
- Read: `engine/tests/use_cases/test_evaluate_window.py`
- Read: `engine/use_cases/evaluate_window.py`

- [ ] **Step 1: Enumerate the 2 failing tests**

```bash
cd engine && pytest tests/use_cases/test_evaluate_window.py -v --tb=no 2>&1 | grep -E "FAILED|PASSED" | head -20
```

Record the 2 FAILED test names.

- [ ] **Step 2: For each failure, read the traceback**

```bash
cd engine && pytest tests/use_cases/test_evaluate_window.py::<failing_test_name> -v --tb=long 2>&1 | tail -30
```

- [ ] **Step 3: Classify**

For each of the 2 failures:
- `stale fixture` → update test
- `real bug` → fix use case
- `contract drift` → update port mock return shape

- [ ] **Step 4: Apply fix per classification**

Code the fix. Commit each fix separately:

```bash
git add <file>
git commit -m "test(evaluate_window): fix <test_name> — <reason>"
```

- [ ] **Step 5: Re-run full file**

```bash
cd engine && pytest tests/use_cases/test_evaluate_window.py -v
```

Expected: all tests PASS.

---

### Task 3.4: Add port-contract tests — reconcile_positions

**Files:**
- Modify: `engine/tests/use_cases/test_reconcile_positions.py`

**Context:** Port-contract tests assert "use case calls port method X with args Y". These would have caught #207 (stale import silently dropped TRADEs — no port call, no test failure on mocks that didn't assert calls). Add 3 per module: happy-path contract, failure-mode contract, regression.

- [ ] **Step 1: Read the existing reconcile_positions use case signature**

```bash
cd engine && grep -n "def __init__\|async def" use_cases/reconcile_positions.py | head -20
```

Note ports injected via `__init__` and the public method(s).

- [ ] **Step 2: Write new port-contract tests**

Append to `engine/tests/use_cases/test_reconcile_positions.py`:

```python
# ============ Port-contract tests (Phase 3) ============


import pytest
from tests.fixtures.ports import (
    fake_trade_repository,
    fake_window_repository,
    fake_alerts_gateway,
)
from tests.fixtures.domain import make_trade


@pytest.mark.use_case
class TestReconcilePositionsPortContract:
    """Assert each port method is called exactly when/how the use case contract says."""

    @pytest.mark.asyncio
    async def test_live_trade_reaches_repository(self):
        """Regression #207: LIVE-mode TRADE decisions must hit find_by_id + save."""
        repo = fake_trade_repository()
        window_repo = fake_window_repository()
        alerts = fake_alerts_gateway()
        window_repo.get_actual_direction.return_value = "UP"
        repo.find_unresolved_paper_trades.return_value = []
        repo.fetch_trades.return_value = [
            make_trade(trade_id="t1", mode="LIVE", status="OPEN", direction="UP"),
        ]

        from use_cases.reconcile_positions import ReconcilePositionsUseCase
        uc = ReconcilePositionsUseCase(
            trade_repository=repo,
            window_repository=window_repo,
            alerts=alerts,
        )

        await uc.execute()

        # Contract: live trades are fetched
        repo.fetch_trades.assert_called()
        # Contract: actual direction is queried per window
        window_repo.get_actual_direction.assert_called()

    @pytest.mark.asyncio
    async def test_zero_trades_no_persistence_calls(self):
        """No trades to reconcile → no save() calls."""
        repo = fake_trade_repository()
        window_repo = fake_window_repository()
        alerts = fake_alerts_gateway()
        repo.find_unresolved_paper_trades.return_value = []
        repo.fetch_trades.return_value = []

        from use_cases.reconcile_positions import ReconcilePositionsUseCase
        uc = ReconcilePositionsUseCase(
            trade_repository=repo,
            window_repository=window_repo,
            alerts=alerts,
        )

        await uc.execute()

        repo.save.assert_not_called()
```

- [ ] **Step 3: Run the new tests**

```bash
cd engine && pytest tests/use_cases/test_reconcile_positions.py::TestReconcilePositionsPortContract -v
```

Expected: PASS. If FAIL, inspect — the test contract may need tuning to match actual use-case signature. Adjust accordingly.

- [ ] **Step 4: Commit**

```bash
git add engine/tests/use_cases/test_reconcile_positions.py
git commit -m "test(reconcile): add port-contract tests — regression guard for #207"
```

---

### Task 3.5: Add port-contract tests — execute_trade + execute_manual_trade + publish_heartbeat + build_window_summary

**Files:**
- Modify: `engine/tests/use_cases/test_execute_trade.py`
- Modify: `engine/tests/use_cases/test_execute_manual_trade.py`
- Modify: `engine/tests/use_cases/test_publish_heartbeat.py`
- Modify: `engine/tests/use_cases/test_build_window_summary.py`

**Context:** Same pattern as Task 3.4. For each module:
1. Happy-path contract — primary port method called with expected args.
2. Failure-mode contract — when use case raises/returns error, persistence NOT called.
3. Regression test — one per prod incident (#206 for heartbeat, #208 for build_window_summary).

- [ ] **Step 1: execute_trade — add `TestExecuteTradePortContract`**

Append to `engine/tests/use_cases/test_execute_trade.py`:

```python
# ============ Port-contract tests (Phase 3) ============

import pytest
from tests.fixtures.ports import (
    fake_trade_repository,
    fake_risk_manager,
    fake_alerts_gateway,
    fake_execution_guard,
)
from tests.fixtures.domain import make_strategy_decision
from unittest.mock import AsyncMock


@pytest.mark.use_case
class TestExecuteTradePortContract:
    @pytest.mark.asyncio
    async def test_approved_decision_reaches_executor_once(self):
        repo = fake_trade_repository()
        risk = fake_risk_manager(paper_mode=False)
        alerts = fake_alerts_gateway()
        guard = fake_execution_guard()
        executor = AsyncMock()
        executor.place_order.return_value = {"order_id": "x", "status": "FILLED"}

        from use_cases.execute_trade import ExecuteTradeUseCase
        uc = ExecuteTradeUseCase(
            trade_repository=repo,
            risk_manager=risk,
            executor=executor,
            alerts=alerts,
            execution_guard=guard,
        )
        decision = make_strategy_decision(mode="LIVE", action="TRADE")

        await uc.execute(decision=decision)

        guard.try_claim.assert_called_once()
        executor.place_order.assert_called_once()
        repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_risk_rejected_decision_skips_executor(self):
        repo = fake_trade_repository()
        risk = fake_risk_manager()
        risk.approve_bet.return_value = False
        executor = AsyncMock()

        from use_cases.execute_trade import ExecuteTradeUseCase
        uc = ExecuteTradeUseCase(
            trade_repository=repo,
            risk_manager=risk,
            executor=executor,
            alerts=fake_alerts_gateway(),
            execution_guard=fake_execution_guard(),
        )
        decision = make_strategy_decision(mode="LIVE", action="TRADE")

        await uc.execute(decision=decision)

        executor.place_order.assert_not_called()
        repo.save.assert_not_called()
```

- [ ] **Step 2: execute_manual_trade — add contracts**

Append to `engine/tests/use_cases/test_execute_manual_trade.py`:

```python
# ============ Port-contract tests (Phase 3) ============

import pytest
from tests.fixtures.ports import fake_trade_repository, fake_risk_manager
from unittest.mock import AsyncMock


@pytest.mark.use_case
class TestExecuteManualTradePortContract:
    @pytest.mark.asyncio
    async def test_paper_mode_skips_executor(self):
        repo = fake_trade_repository()
        risk = fake_risk_manager(paper_mode=True)
        executor = AsyncMock()

        from use_cases.execute_manual_trade import ExecuteManualTradeUseCase
        uc = ExecuteManualTradeUseCase(
            trade_repository=repo, risk_manager=risk, executor=executor,
        )
        await uc.execute(asset="BTC", direction="UP", size_usd=5.0, mode="PAPER")

        executor.place_order.assert_not_called()
        repo.save.assert_called_once()  # paper trade still persists
```

- [ ] **Step 3: publish_heartbeat — add regression for #206**

Append to `engine/tests/use_cases/test_publish_heartbeat.py`:

```python
# ============ Regression: #206 (dict-shaped RiskManager.get_status) ============

import pytest
from tests.fixtures.ports import fake_risk_manager
from unittest.mock import AsyncMock


@pytest.mark.use_case
class TestPublishHeartbeatRiskStatusShape:
    @pytest.mark.asyncio
    async def test_accepts_dict_shaped_risk_status(self):
        """Regression #206 — RiskManager.get_status() returns dict (not object)."""
        risk = fake_risk_manager()  # returns dict per fixture
        assert isinstance(risk.get_status.return_value, dict)

        hb_repo = AsyncMock()
        hb_repo.save.return_value = None

        from use_cases.publish_heartbeat import PublishHeartbeatUseCase
        uc = PublishHeartbeatUseCase(heartbeat_repository=hb_repo, risk_manager=risk)

        await uc.tick()  # must not raise TypeError/AttributeError

        hb_repo.save.assert_called_once()
```

- [ ] **Step 4: build_window_summary — add regression for #208**

Append to `engine/tests/use_cases/test_build_window_summary.py`:

```python
# ============ Regression: #208 (window-expired blocker framing) ============

import pytest
from tests.fixtures.domain import make_window_state


@pytest.mark.use_case
class TestBuildWindowSummaryAlertFraming:
    def test_window_expired_reframes_final_offset_too_late(self):
        """#208 — final-offset blocker is re-labelled as 'window expired'."""
        from use_cases.build_window_summary import build_window_summary

        window = make_window_state(eval_offset=0)  # final offset
        summary = build_window_summary(
            window=window,
            in_window_blocker="some_gate_fail",
            final_offset_blocker="too_late",
        )
        # Contract: the assembled summary labels too-late as "window expired"
        # (not "too late") and surfaces the in-window blocker.
        assert "window expired" in summary.message.lower() or \
               summary.reason_code == "WINDOW_EXPIRED"
        assert summary.in_window_blocker == "some_gate_fail"
```

- [ ] **Step 5: Run all four files**

```bash
cd engine && pytest \
  tests/use_cases/test_execute_trade.py \
  tests/use_cases/test_execute_manual_trade.py \
  tests/use_cases/test_publish_heartbeat.py \
  tests/use_cases/test_build_window_summary.py \
  -v
```

Expected: all new tests PASS. Any FAIL means the test contract doesn't match actual use case signature — inspect and adjust (the fixtures and use case args must align exactly).

- [ ] **Step 6: Commit**

```bash
git add engine/tests/use_cases/
git commit -m "test(use_cases): add port-contract tests + regressions for #206 and #208

- execute_trade: happy-path + risk-rejection contracts
- execute_manual_trade: paper-mode skip contract
- publish_heartbeat: dict-shaped RiskStatus regression (#206)
- build_window_summary: window-expired framing regression (#208)

Each use case now has an assert_called_once() on its primary port,
guarding against silent drops like #207."
```

---

### Task 3.6: Add CI `engine-tests-fast` job with `--cov-fail-under=90`

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the job**

Append to `jobs:` in `.github/workflows/ci.yml`:

```yaml
  engine-tests-fast:
    name: Engine - fast tests + execution coverage gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: |
            engine/requirements.txt
            engine/requirements-test.txt

      - name: Install dependencies
        run: pip install -r engine/requirements.txt -r engine/requirements-test.txt

      - name: Fast tests + execution-path coverage gate
        working-directory: engine
        run: |
          pytest tests/unit tests/use_cases \
            -m "not slow and not integration" \
            -n auto \
            --maxfail=5 \
            --cov=use_cases.reconcile_positions \
            --cov=use_cases.execute_trade \
            --cov=use_cases.execute_manual_trade \
            --cov=use_cases.publish_heartbeat \
            --cov=use_cases.build_window_summary \
            --cov=adapters.pg_execution_guard \
            --cov-fail-under=90 \
            --cov-report=term-missing
```

- [ ] **Step 2: Verify YAML**

Run:
```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no output.

- [ ] **Step 3: Local smoke — run the same command**

```bash
cd engine && pytest tests/unit tests/use_cases \
  -m "not slow and not integration" \
  -n auto --maxfail=5 \
  --cov=use_cases.reconcile_positions \
  --cov=use_cases.execute_trade \
  --cov=use_cases.execute_manual_trade \
  --cov=use_cases.publish_heartbeat \
  --cov=use_cases.build_window_summary \
  --cov=adapters.pg_execution_guard \
  --cov-fail-under=90 \
  --cov-report=term-missing 2>&1 | tail -30
```

Expected: all tests pass, coverage ≥90% for each listed module. If coverage is below 90% on any module, identify the missing branches in the `term-missing` report and add targeted tests to cover them (repeat Task 3.4/3.5 pattern).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add engine-tests-fast job with 90% coverage gate on execution path

Gate modules: reconcile_positions, execute_trade, execute_manual_trade,
publish_heartbeat, build_window_summary, pg_execution_guard. These are
the 6 modules where silent failures cost real money. Gate applies on
PRs to develop + main."
```

---

### Task 3.7: Open Phase 3 PR

- [ ] **Step 1: Push + PR**

```bash
git push
gh pr create --base develop --title "test(engine): Phase 3 — execution-path coverage gate + port contracts" --body "$(cat <<'EOF'
## Summary

Phase 3. Fixes 4 execution-path test failures and adds port-contract tests that would have caught #206, #207, #208. CI \`--cov-fail-under=90\` enforces on 6 modules.

**Failures fixed:**
- \`test_reconcile_positions::test_no_match_returns_none\` (1)
- \`test_execute_manual_trade::test_live_mode_places_real_order\` (1)
- \`test_evaluate_window\` (2)

**Port-contract tests added:**
- reconcile_positions: live-trade-reaches-repository, zero-trades-no-save, regression for #207
- execute_trade: approved-reaches-executor, risk-rejected-skips-executor
- execute_manual_trade: paper-mode-skips-executor
- publish_heartbeat: dict-shaped-RiskStatus regression (#206)
- build_window_summary: window-expired-framing regression (#208)

**CI:**
- \`engine-tests-fast\` job runs \`pytest tests/unit tests/use_cases -n auto\` with \`--cov-fail-under=90\` on 6 modules

## Test plan

- [ ] \`cd engine && pytest tests/use_cases -v\` → 0 failures in test_reconcile_positions, test_execute_manual_trade, test_evaluate_window
- [ ] Coverage report shows ≥90% on each of 6 gated modules
- [ ] CI \`engine-tests-fast\` green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Merge on green**

---

# Phase 4 — Triage remaining ~125 failures

**Goal:** After Phases 1–3, ~125 failures remain (135 − 2 heartbeat − 4 exec-path − 4 already counted). Triage each to fix / xfail+hub-task / delete. Final `pytest engine/tests/` = 0 failures (xfails tracked).

**PR title:** `test(engine): Phase 4 — triage remaining 125 failures`

---

### Task 4.1: Enumerate remaining failures + cluster

- [ ] **Step 1: Run full suite and capture failure list**

```bash
cd engine && pytest tests/ --tb=no -q 2>&1 | tee /tmp/phase4_failures.txt | tail -40
```

- [ ] **Step 2: Extract failure names into a sortable list**

```bash
grep "^FAILED" /tmp/phase4_failures.txt | awk '{print $2}' | sort > /tmp/phase4_failures_sorted.txt
wc -l /tmp/phase4_failures_sorted.txt
head -30 /tmp/phase4_failures_sorted.txt
```

Record count in plan doc.

- [ ] **Step 3: Start triage log**

Create `docs/superpowers/plans/2026-04-15-engine-test-triage.md`:

```markdown
# Engine Test Triage Log — Phase 4

**Baseline:** N failures (from commit <sha>, <date>).

Per-test decision record. Each row: test id, classification, action, notes.

| Test | Cluster | Classification | Action | Notes |
|---|---|---|---|---|
| tests/unit/domain/test_value_objects.py::test_X | value_objects | stale fixture | FIX | #208 added field, update factory |
| ... | ... | ... | ... | ... |

**Legend:**
- Classification: `real-bug` / `stale-fixture` / `obsolete-feature` / `infra-blocked`
- Action: `FIX` (code or test change) / `XFAIL` (mark xfail + create hub task) / `DELETE` (rm test)
```

- [ ] **Step 4: Commit the triage log skeleton**

```bash
git add docs/superpowers/plans/2026-04-15-engine-test-triage.md
git commit -m "docs(plan): initialize Phase 4 triage log"
```

---

### Task 4.2: Dispatch subagent to classify value_objects + strategy clusters

- [ ] **Step 1: Dispatch an Explore subagent**

Use the `Agent` tool with `subagent_type: "Explore"` and this prompt (self-contained — subagent has no context):

```
Classify the following failing tests in engine/tests/. For each test:
1. Run it to see the failure mode.
2. Read the test code and the production code it tests.
3. Classify as: real-bug / stale-fixture / obsolete-feature / infra-blocked.
4. Report a table per cluster.

Tests to classify:
- engine/tests/unit/domain/test_value_objects.py (13 failures)
- engine/tests/unit/strategies/test_v4_fusion_strategy.py (5 failures)
- engine/tests/unit/signals/test_data_surface.py::test_primary_delta_tiingo_first (1 failure)

For each failure, output:
| test_id | failure_mode | classification | suggested_action |

Do NOT change any code. Just report.
```

- [ ] **Step 2: Receive the subagent report**

Paste subagent output into `docs/superpowers/plans/2026-04-15-engine-test-triage.md` under the relevant clusters.

- [ ] **Step 3: Commit the classification**

```bash
git add docs/superpowers/plans/2026-04-15-engine-test-triage.md
git commit -m "docs(triage): record classifications for value_objects + strategy + data_surface clusters"
```

---

### Task 4.3: Apply fixes + xfails + deletions for classified clusters

**Files:**
- Modify: `engine/tests/unit/domain/test_value_objects.py`
- Modify: `engine/tests/unit/strategies/test_v4_fusion_strategy.py`
- Modify: `engine/tests/unit/signals/test_data_surface.py`

**Context:** For each test classified in Task 4.2, apply the action from the triage log.

- [ ] **Step 1: For each `FIX` row in triage log**

Apply the concrete code change noted in the "Notes" column. Run the test to confirm it passes. Commit per-cluster (one commit per cluster, not per test):

```bash
git add engine/tests/unit/domain/test_value_objects.py
git commit -m "test(value_objects): fix 13 failures — align fixtures with #208 additive field"
```

- [ ] **Step 2: For each `XFAIL` row**

Apply the marker + open a hub task via the Hub API:

```python
# Before the failing test function:
@pytest.mark.xfail(
    reason="HUB-TASK-<id>: <short reason>. See audit-tasks for details.",
    strict=True,
)
def test_something():
    ...
```

Create the hub task (per Billy's `reference_hub_api.md` memory):

```bash
curl -X POST "http://16.54.141.121:8091/api/audit-tasks" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix xfailed test: <test_id>",
    "priority": "low",
    "category": "test-debt",
    "description": "Phase 4 triage marked as xfail(strict=True). Classification: <class>. Original failure: <mode>. Needs: <what>."
  }'
```

Record the returned task ID in the xfail `reason` and in the triage log.

- [ ] **Step 3: For each `DELETE` row**

```bash
git rm <test_file_or_test_function_file>
git commit -m "test: remove obsolete test <test_id> — feature removed in <commit/pr>"
```

If deleting just a single test within a larger file, use `sed` or edit directly, then commit:

```bash
git add <file>
git commit -m "test: remove obsolete test_X from test_Y.py — <reason>"
```

- [ ] **Step 4: Re-run cluster and verify green**

```bash
cd engine && pytest tests/unit/domain/test_value_objects.py tests/unit/strategies/test_v4_fusion_strategy.py tests/unit/signals/test_data_surface.py -v
```

Expected: 0 F (failures) / 0 E (errors). Some `x` (xfail) entries are OK.

---

### Task 4.4: Triage remaining unaccounted ~96 failures

- [ ] **Step 1: Identify what's left**

After Task 4.3, re-run:

```bash
cd engine && pytest tests/ --tb=no -q 2>&1 | grep "^FAILED" | sort > /tmp/phase4_remaining.txt
wc -l /tmp/phase4_remaining.txt
```

If count is 0: skip to Task 4.5.
If count > 0: continue.

- [ ] **Step 2: Dispatch a second Explore subagent for the remaining set**

Subagent prompt (self-contained):

```
Classify these remaining failing tests in engine/tests/. For each test:
1. Run it.
2. Classify as: real-bug / stale-fixture / obsolete-feature / infra-blocked.
3. Report the same table format as before.

Failing tests:
<paste contents of /tmp/phase4_remaining.txt>

Do NOT change any code. Just report.
```

- [ ] **Step 3: Apply the subagent's recommendations**

Same FIX/XFAIL/DELETE loop as Task 4.3. Commit per cluster. Update triage log per action.

- [ ] **Step 4: Final verify**

```bash
cd engine && pytest tests/ --tb=no -q 2>&1 | tail -5
```

Expected: `N passed, M xfailed, 0 failed`. (Non-zero `xpassed` means a `strict=True` xfail started passing — investigate per Task 4.3 Step 2 conventions.)

---

### Task 4.5: Open Phase 4 PR

- [ ] **Step 1: Push + PR**

```bash
git push
gh pr create --base develop --title "test(engine): Phase 4 — triage 125 remaining failures (fix / xfail / delete)" --body "$(cat <<'EOF'
## Summary

Phase 4 of the engine test infra plan. Classifies + resolves the 125 failures surviving after Phases 1–3.

**Actions applied:**
- FIX: N tests (real bug in test or code, aligned to current contract)
- XFAIL: M tests (strict=True, tracked via hub /api/audit-tasks tasks)
- DELETE: K tests (obsolete feature, no regression value)

**Triage log:** [docs/superpowers/plans/2026-04-15-engine-test-triage.md](docs/superpowers/plans/2026-04-15-engine-test-triage.md) has per-test classification + action.

**Exit criteria met:**
- \`pytest engine/tests/\` → \`N passed, M xfailed, 0 failed\`
- All xfails have \`strict=True\` (regression if they start passing silently)
- All xfails linked to hub tasks

## Test plan

- [ ] \`cd engine && pytest tests/\` → 0 failures
- [ ] Hub tasks created for every xfail — spot-check 3 via \`/api/audit-tasks\`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Merge on green**

---

# Phase 5 — Nightly + coverage reporting + dev docs

**Goal:** `.github/workflows/nightly.yml` runs full suite on develop daily. Codecov reports global coverage (soft). `docs/engine-testing.md` gives devs a quickstart.

**PR title:** `test(engine): Phase 5 — nightly full-suite + coverage reporting + dev docs`

---

### Task 5.1: Create `.github/workflows/nightly.yml`

**Files:**
- Create: `.github/workflows/nightly.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: Nightly - engine full suite

on:
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC daily (targets develop HEAD)
  workflow_dispatch:      # also runnable manually

jobs:
  engine-full:
    name: Engine - full test suite incl. integration
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: develop

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: |
            engine/requirements.txt
            engine/requirements-test.txt

      - name: Install dependencies
        run: pip install -r engine/requirements.txt -r engine/requirements-test.txt

      - name: Full suite + coverage (soft — reported, not gated)
        working-directory: engine
        run: |
          pytest tests/ \
            -n auto \
            --cov=. \
            --cov-report=xml \
            --cov-report=term \
            --junitxml=junit.xml

      - name: Upload coverage to Codecov
        uses: codecov/codecov-action@v4
        with:
          files: engine/coverage.xml
          flags: engine-nightly
          fail_ci_if_error: false

      - name: Open issue on failure
        if: failure()
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh issue create \
            --title "Nightly engine test failure $(date -u +%Y-%m-%d)" \
            --body "Nightly run failed on develop. See [workflow run](${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }})." \
            --label "ci-failure,test-debt"
```

- [ ] **Step 2: Verify YAML**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/nightly.yml'))"
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/nightly.yml
git commit -m "ci: add nightly full-suite run on develop @ 06:00 UTC

Runs full pytest (incl. integration tier) against develop HEAD every day.
Reports coverage to Codecov (soft — not gated). Opens a GH issue on
failure so regressions don't sit silently."
```

---

### Task 5.2: Add Codecov config (optional but recommended)

**Files:**
- Create: `codecov.yml` (repo root)

- [ ] **Step 1: Write the config**

```yaml
# codecov.yml
coverage:
  status:
    project:
      default:
        target: auto
        threshold: 2%            # allow 2% drop without failing
        base: auto
    patch:
      default:
        target: 80%              # new code should be ≥80% covered

flags:
  engine-nightly:
    paths:
      - engine/

ignore:
  - "engine/tests/"
  - "engine/scripts/"
  - "**/__init__.py"
```

- [ ] **Step 2: Commit**

```bash
git add codecov.yml
git commit -m "ci: add codecov config — soft 2% threshold, new-patch 80% target"
```

---

### Task 5.3: Write `docs/engine-testing.md`

**Files:**
- Create: `docs/engine-testing.md`

- [ ] **Step 1: Write the doc**

```markdown
# Engine testing guide

## Quickstart

```bash
cd engine
pip install -r requirements.txt -r requirements-test.txt
pytest tests/ -n auto            # full suite in parallel
pytest tests/unit tests/use_cases -n auto  # fast subset (CI PR gate)
pytest tests/ -m integration     # nightly / on-demand integration tier
```

## Layout

```
engine/tests/
├── conftest.py           # sys.path + TestSettings env bootstrap
├── fixtures/             # explicit builders — import what you need
├── unit/                 # domain + VO — zero mocks, zero I/O
├── use_cases/            # use-case tests with mocked ports
└── integration/          # real SQLite + stub adapters (nightly only)
```

## Markers

| Marker | Meaning |
|---|---|
| `@pytest.mark.unit` | Pure domain/VO, no I/O |
| `@pytest.mark.use_case` | Use-case test with mocked ports |
| `@pytest.mark.integration` | Real DB or adapter — nightly |
| `@pytest.mark.slow` | >1s runtime — excluded from PR fast subset |

Default PR subset: `-m "not slow and not integration"`.

## Writing a new use-case test

1. Import a builder from `tests/fixtures/domain.py`.
2. Import port fakes from `tests/fixtures/ports.py`.
3. Instantiate the use case with the fakes.
4. Assert port-contract calls with `assert_called_once_with(...)`.

Example:

```python
import pytest
from tests.fixtures.ports import fake_trade_repository, fake_risk_manager
from tests.fixtures.domain import make_strategy_decision


@pytest.mark.use_case
async def test_rejected_decision_does_not_persist():
    repo = fake_trade_repository()
    risk = fake_risk_manager()
    risk.approve_bet.return_value = False

    from use_cases.execute_trade import ExecuteTradeUseCase
    uc = ExecuteTradeUseCase(trade_repository=repo, risk_manager=risk, ...)

    await uc.execute(decision=make_strategy_decision())

    repo.save.assert_not_called()
```

## Coverage gates

- **Global:** soft (reported to Codecov, not gated).
- **Execution path:** hard gate `--cov-fail-under=90` on 6 modules:
  - `use_cases/reconcile_positions.py`
  - `use_cases/execute_trade.py`
  - `use_cases/execute_manual_trade.py`
  - `use_cases/publish_heartbeat.py`
  - `use_cases/build_window_summary.py`
  - `adapters/pg_execution_guard.py`

Run locally:

```bash
cd engine && pytest tests/use_cases \
  --cov=use_cases.reconcile_positions \
  --cov=use_cases.execute_trade \
  --cov-report=term-missing
```

## Quarantine rule (xfail)

Use `@pytest.mark.xfail(reason="HUB-TASK-<id>: ...", strict=True)` for tests that can't be fixed now.

- `strict=True` is mandatory — if the test starts passing unexpectedly, CI fails.
- Each xfail MUST have a hub task (see `reference_hub_api.md`) so the debt is tracked.

## Canonical imports

All engine code + tests use bare imports:

```python
# YES
from domain.value_objects import WindowState
from use_cases.execute_trade import ExecuteTradeUseCase

# NO — triggers dual-load isinstance bug
from engine.domain.value_objects import WindowState
```

CI enforces via grep. Regression test at `tests/test_no_dual_import.py`.

## Running against a real Postgres

For integration tier:

```bash
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost/btc_trader_test"
cd engine && pytest tests/integration -v
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/engine-testing.md
git commit -m "docs: engine testing guide — quickstart + layout + markers + conventions"
```

---

### Task 5.4: Manual nightly smoke via workflow_dispatch

- [ ] **Step 1: After Phase 5 PR merges to develop**

```bash
gh workflow run nightly.yml --ref develop
```

- [ ] **Step 2: Watch the run**

```bash
gh run watch
```

Expected: green.

- [ ] **Step 3: Verify Codecov upload**

Check `codecov.io/gh/<org>/novakash` for the new coverage report.

- [ ] **Step 4: Record first coverage baseline in plan**

Append to plan doc:

```markdown
**Phase 5 first coverage baseline (YYYY-MM-DD):**
- engine/: XX%
- engine/use_cases/: XX%
- engine/domain/: XX%
```

Commit:

```bash
git add docs/superpowers/plans/2026-04-15-engine-test-infra.md
git commit -m "docs(plan): record Phase 5 first coverage baseline"
```

---

### Task 5.5: Open Phase 5 PR

- [ ] **Step 1: Push + PR**

```bash
git push
gh pr create --base develop --title "test(engine): Phase 5 — nightly full-suite + coverage reporting + dev docs" --body "$(cat <<'EOF'
## Summary

Phase 5 — last phase of the engine test infra plan. Catches what PR fast subset skips.

- \`.github/workflows/nightly.yml\` runs full suite on develop @ 06:00 UTC daily
- \`codecov.yml\` — soft global coverage, 2% drop threshold, 80% target for new patches
- \`docs/engine-testing.md\` — quickstart + layout + markers + conventions

**Workflow_dispatch-able:** \`gh workflow run nightly.yml\` runs on demand.

## Test plan

- [ ] \`gh workflow run nightly.yml --ref develop\` → green
- [ ] Codecov receives engine coverage report (visible at codecov.io/gh/...)
- [ ] \`docs/engine-testing.md\` renders correctly on GitHub

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Merge on green**

---

# Rollback notes

Each phase is mergeable + revertable independently:

- **Phase 1 rollback:** revert the PR — Settings goes back to eager singleton, tests break again. Harmless to prod.
- **Phase 2 rollback:** revert — test imports back to `engine.` prefix, dual-load returns. Harmless to prod.
- **Phase 3 rollback:** revert — coverage gate removed, 4 test failures return. Port contracts deleted. Harmless to prod.
- **Phase 4 rollback:** revert — xfails become failures again, triage log reverts. Harmless to prod.
- **Phase 5 rollback:** revert — no nightly, no Codecov, no doc. Harmless to prod.

None of the phases touch runtime trade execution, heartbeat logic, or persistence behavior. Production engine is untouched.

---

# Appendix — pytest markers cheat sheet

```python
@pytest.mark.unit           # No I/O, no async
@pytest.mark.use_case       # Async, mocked ports
@pytest.mark.integration    # Real DB/adapter — nightly only
@pytest.mark.slow           # >1s — excluded from PR subset
@pytest.mark.xfail(reason="HUB-TASK-NNN: ...", strict=True)
```

# Appendix — command recipe card

```bash
# Install
cd engine && pip install -r requirements.txt -r requirements-test.txt

# Fast subset (PR gate)
pytest tests/unit tests/use_cases -n auto -m "not slow and not integration"

# Full suite (local debug)
pytest tests/ -n auto

# Execution-path coverage report
pytest tests/use_cases \
  --cov=use_cases.reconcile_positions \
  --cov=use_cases.execute_trade \
  --cov=use_cases.execute_manual_trade \
  --cov=use_cases.publish_heartbeat \
  --cov=use_cases.build_window_summary \
  --cov=adapters.pg_execution_guard \
  --cov-report=term-missing

# Integration tier (on demand)
pytest tests/integration -m integration

# Canonical-imports lint
grep -rn "from engine\.\|import engine\." engine/ --include="*.py" && echo "OFFENDERS" || echo "OK"
```
