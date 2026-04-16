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
  - `adapters/persistence/pg_execution_guard.py`

Run locally:

```bash
cd engine && pytest tests/unit tests/use_cases \
  --cov=use_cases.reconcile_positions \
  --cov=use_cases.execute_trade \
  --cov=use_cases.execute_manual_trade \
  --cov=use_cases.publish_heartbeat \
  --cov=use_cases.build_window_summary \
  --cov=adapters.persistence.pg_execution_guard \
  --cov-report=term-missing
```

## Quarantine rule (xfail)

Use `@pytest.mark.xfail(reason="HUB-TASK-<id>: ...", strict=True)` for tests that can't be fixed now.

- `strict=True` is mandatory — if the test starts passing unexpectedly, CI fails.
- Each xfail MUST have a hub task tracked in the Hub API so the debt is visible.

## Canonical imports

All engine code + tests use bare imports:

```python
# YES
from domain.value_objects import WindowState
from use_cases.execute_trade import ExecuteTradeUseCase

# NO — triggers dual-load isinstance bug
from engine.domain.value_objects import WindowState
```

CI enforces via grep in `engine-canonical-imports` job. Regression test at `tests/test_no_dual_import.py`.

## Running against a real Postgres

For integration tier:

```bash
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost/btc_trader_test"
cd engine && pytest tests/integration -v
```
