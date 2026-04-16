# Engine Test Infrastructure — Design

**Date:** 2026-04-15
**Author:** Billy + Claude
**Status:** Approved, ready for implementation plan

## Problem

Engine test suite is not enforced at PR time. CI runs only import-smoke jobs (see `.github/workflows/ci.yml`). Full `pytest engine/tests/` produces:

- **14 collection errors** — missing deps (`yaml`, `structlog`) and pydantic `database_url` required in `Settings`.
- **135 failing assertions** (stub env vars applied) across 8 clusters. Pre-existing, not introduced by #206/#207/#208. Includes:
  - `test_reconcile_positions.py::test_no_match_returns_none` (1)
  - `test_value_objects.py::*` (13)
  - `test_v4_fusion_strategy.py::*` (5)
  - `test_evaluate_window.py::*` (2)
  - `test_execute_manual_trade.py::test_live_mode_places_real_order` (1)
  - `test_publish_heartbeat.py::test_tick_writes_heartbeat` + `test_sitrep_sent_at_interval` (2 — dual-import `isinstance` quirk)
  - `test_data_surface.py::test_primary_delta_tiingo_first` (1)
  - Remaining ~110 in unaccounted clusters
- **No coverage signal** — no gate, no report.

Gap is material: #206/#207 were live-execution bugs (silent stale-import dropped TRADEs; dict-vs-object crash on heartbeat). Unit tests existed and pass in isolation, but no CI enforcement caught them before prod.

## Goals

1. 311+ passing tests must stay green at PR time (enforced in CI).
2. Execution-path use cases (reconcile, execute_trade, execute_manual_trade, publish_heartbeat, build_window_summary) get hard coverage floor ≥90%.
3. Local test run must work from fresh clone without `.env` — `pip install` + `pytest` and go.
4. Dual-import bug class eliminated (canonical imports + lint rule + regression test).
5. 135 failures triaged per cluster: fix, xfail-with-hub-task, or delete.
6. Nightly full-suite run on `develop` catches slow/integration drift.

## Non-Goals

- Paper-mode engine end-to-end tests. Requires Montreal-only APIs (CLOB, Gamma, Opinion). Deferred.
- Hard global coverage gate. Forces fake tests on glue code. Soft report only.
- Matrixed Python versions / multi-OS. Prod runs single Python, single OS.
- Backfilling tests for every untested module. Scope is fix-what's-broken + guard execution path.

## Decisions (captured from brainstorm)

| # | Question | Decision |
|---|---|---|
| Q1 | Triage philosophy | **Per-cluster:** delete obsolete, fix real bugs, xfail stale |
| Q2 | Test infra scope | **Medium:** CI pytest job + fix 135 + conftest fixtures + markers + coverage |
| Q3 | Cluster priority | **Execution-path first** (reconcile, execute_manual_trade live, publish_heartbeat) |
| Q4 | Coverage enforcement | **Soft global + hard gate on execution-path modules (≥90%)** |
| Q5 | Dual-import fix | **Root cause:** canonical imports + conftest sys.path + lint rule |
| Q6 | Fixture strategy | **Layered conftest + explicit fixtures package** |
| Q7 | Local settings friction | **Settings split** — prod strict, TestSettings with defaults, DI boundary |
| Q8 | CI shape | **Fast PR subset + nightly full on develop** via pytest-xdist |
| Q9 | Quarantine rule | **xfail(strict=True) + hub task** via `/api/audit-tasks` |

## Architecture

Three-tier test pyramid aligned with clean-arch layers.

```
engine/tests/
├── conftest.py                    # sys.path canon + autouse TestSettings env
├── fixtures/                      # entity/VO builders (explicit import)
│   ├── __init__.py
│   ├── domain.py                  # make_window_state(), make_trade(), ...
│   ├── ports.py                   # fake_risk_manager(), in_memory_repo()
│   └── infra.py                   # test_settings(), sqlite_session()
│
├── unit/                          # pure domain + VO — zero mocks, zero I/O
│   ├── conftest.py                # domain-layer fixtures
│   ├── domain/test_value_objects.py
│   └── signals/test_gates.py
│
├── use_cases/                     # use-case tests w/ mocked ports only
│   ├── conftest.py                # port mocks, async mode
│   ├── test_reconcile_positions.py
│   ├── test_execute_trade.py
│   └── test_publish_heartbeat.py
│
└── integration/                   # real SQLite + stub adapters (nightly only)
    ├── conftest.py                # real DB session fixture
    └── test_execution_path_e2e.py
```

Markers: `@pytest.mark.unit`, `@pytest.mark.use_case`, `@pytest.mark.integration`, `@pytest.mark.slow`.

Dependency rule: domain tests import ZERO adapters. Use-case tests mock ONLY ports. Integration tests wire real adapters. Lint can enforce (Phase 5 follow-up).

## Component Design

### 1. Settings split (unblocks 14 collection errors)

Problem: `Settings(BaseSettings)` has `database_url: str` required → ImportError chain in tests.

Fix per clean-arch guide §Application (settings belong in `infrastructure/config/`, injected):

```python
# engine/infrastructure/config/settings.py
class Settings(BaseSettings):
    """Production — strict, all required."""
    database_url: str
    binance_api_key: str
    coinglass_api_key: str
    # ...

class TestSettings(Settings):
    """Test — defaults for everything."""
    database_url: str = "sqlite+aiosqlite:///:memory:"
    binance_api_key: str = "test"
    coinglass_api_key: str = "test"
    paper_mode: bool = True
    # ...

def get_settings() -> Settings: ...
def get_test_settings() -> TestSettings: ...
```

Injection pattern: use cases receive `settings` as constructor arg (same pattern as `IUserRepository` port in clean-arch guide). No use case calls `get_settings()` directly.

Root `tests/conftest.py` autouses `get_test_settings()` and writes env vars before any import.

Missing deps: new `engine/requirements-test.txt` with `yaml`, `structlog`, `pytest-xdist`, `pytest-cov`, `pytest-asyncio`, `pytest-env`, `aiosqlite`. Separate from prod reqs so prod image stays minimal.

### 2. Canonical imports (kills dual-load `isinstance` bug)

Root cause of `test_publish_heartbeat` dual-import quirk: same class loaded twice under different module paths (`engine.domain.x` vs `domain.x`) → `isinstance(instance, Class)` false even though attribute access works.

Fix:

1. **One convention:** `from domain.ports import X` (no `engine.` prefix). Sweep any stragglers.
2. **Conftest enforcement** — `engine/tests/conftest.py` adds `engine/` to `sys.path[0]` AND removes parent (which would cause dual-load via `engine.domain.x`).
3. **Lint rule** in CI before pytest:
   ```bash
   ! grep -rn "from engine\." engine/ --include="*.py"
   ! grep -rn "import engine\." engine/ --include="*.py"
   ```
4. **Regression test** — `test_no_dual_import.py` asserts `sys.modules` has only canonical path.

### 3. Execution-path hard coverage gate

Modules under gate (≥90% line coverage) — 6 modules:

- `use_cases/reconcile_positions.py`
- `use_cases/execute_trade.py`
- `use_cases/execute_manual_trade.py`
- `use_cases/publish_heartbeat.py`
- `use_cases/build_window_summary.py`
- `adapters/pg_execution_guard.py`

(`adapters/fak_ladder_executor.py` candidate — only 1.1K file, covered opportunistically, not gated. Promote in follow-up if grows.)

Test discipline per module:

1. **Happy path** — full use-case `execute()` with stub ports. Assert persistence called, alerts fired, return-shape correct.
2. **Port contract** — each port method asserted called with expected args via `AsyncMock.assert_called_once_with(...)`.
3. **Failure modes** — one test per `raise` path.
4. **Regression tests** — one test per prod incident (#206/#207/#208). Keep, do not delete.

CI block:

```yaml
- name: Execution-path coverage gate
  run: |
    cd engine
    pytest tests/use_cases/ \
      --cov=use_cases.reconcile_positions \
      --cov=use_cases.execute_trade \
      --cov=use_cases.execute_manual_trade \
      --cov=use_cases.publish_heartbeat \
      --cov=use_cases.build_window_summary \
      --cov=adapters.pg_execution_guard \
      --cov-fail-under=90 \
      --cov-report=term-missing
```

### 4. Per-cluster triage (135 failures)

| Cluster | Count | Action |
|---|---:|---|
| `test_publish_heartbeat` (dual-import) | 2 | FIX via §canonical imports |
| `test_reconcile_positions::test_no_match_returns_none` | 1 | FIX — real assertion on `alerts.send_system_alert` post-#207 |
| `test_execute_manual_trade::test_live_mode_places_real_order` | 1 | FIX — live-path, highest risk |
| `test_evaluate_window::*` | 2 | FIX — adjacent to execution |
| `test_value_objects::*` | 13 | FIX — domain foundation, likely stale after #204/#208 additive fields |
| `test_v4_fusion_strategy::*` | 5 | TRIAGE — fix or xfail+hub-task |
| `test_data_surface::test_primary_delta_tiingo_first` | 1 | FIX — feed ordering |
| Collection errors | 14 | FIX via §settings split + requirements-test.txt |
| Remaining ~96 | ~96 | AUDIT — subagent sweep, categorize per rules below |

Per-test classification:

1. Run test → read failure.
2. Classify: `real bug` | `stale fixture` | `obsolete feature` | `infra-blocked`.
3. **Real bug** → fix code, test stays.
4. **Stale fixture** → update test to match current domain contract.
5. **Obsolete feature** → `git rm` test, log in cleanup note.
6. **Infra-blocked** → `@pytest.mark.xfail(reason="…", strict=True)` + create hub task via `POST /api/audit-tasks`.

`strict=True` is mandatory — if a quarantined test unexpectedly passes, CI fails (catches "bug got fixed by unrelated change" silently).

### 5. CI shape

**PR job** (`ci.yml` extended, runs on `develop` + `main` PRs):

```yaml
engine-tests-fast:
  name: Engine - fast tests
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5
      with: { python-version: "3.12", cache: pip }
    - run: pip install -r engine/requirements.txt -r engine/requirements-test.txt
    - name: Canonical-imports lint
      run: |
        ! grep -rn "from engine\." engine/ --include="*.py"
        ! grep -rn "import engine\." engine/ --include="*.py"
    - name: Fast tests + execution coverage gate
      run: |
        cd engine
        pytest tests/unit tests/use_cases \
          -m "not slow and not integration" \
          -n auto --maxfail=5 \
          --cov=use_cases.reconcile_positions \
          --cov=use_cases.execute_trade \
          --cov=use_cases.execute_manual_trade \
          --cov=use_cases.publish_heartbeat \
          --cov=use_cases.build_window_summary \
          --cov=adapters.pg_execution_guard \
          --cov-fail-under=90 \
          --cov-report=term-missing
```

Target runtime <2min. `-n auto` = pytest-xdist parallel.

**Nightly job** (`.github/workflows/nightly.yml`):

```yaml
on:
  schedule: [{ cron: "0 6 * * *" }]   # 06:00 UTC
  workflow_dispatch:

jobs:
  engine-full:
    runs-on: ubuntu-latest
    steps:
      - ...setup...
      - run: |
          cd engine
          pytest tests/ --cov=engine --cov-report=xml
      - uses: codecov/codecov-action@v4  # soft global coverage report
      - if: failure()
        run: gh issue create --title "Nightly test failure $(date +%F)" ...
```

Full suite incl. integration tier. Soft global coverage report. Opens GH issue on failure.

**Dev workflow** (documented in `docs/engine-testing.md`):

```bash
cd engine
pip install -r requirements.txt -r requirements-test.txt
pytest tests/unit tests/use_cases -n auto         # fast local
pytest tests/ -m integration                      # integration on demand
pytest tests/ --cov=engine --cov-report=html      # full coverage report
```

## Data flow

No new runtime components. All changes are test-harness + CI. Production code changes limited to:

- `infrastructure/config/settings.py` — `TestSettings` subclass (new, never imported in prod path)
- Settings-injection refactor on any use case that currently calls `get_settings()` directly (audited in Phase 1)

## Error handling

Test harness errors:

- **Collection error** → CI fails fast. No tests run. Lint rule output tells user which import broke.
- **Dual-import regression** → `test_no_dual_import` fails. CI red with clear message.
- **Coverage drop below 90%** → CI red with `--cov-report=term-missing` showing uncovered lines.
- **xfail test unexpectedly passes** → CI red via `strict=True`. Signals to remove the xfail.

Production runtime untouched.

## Testing strategy

Recursive: how do we test the test infra?

1. **Phase 1 verify** — `pytest engine/tests/ --collect-only` returns zero errors.
2. **Phase 2 verify** — `test_no_dual_import` passes. Lint grep returns empty.
3. **Phase 3 verify** — coverage report shows ≥90% on 6 execution-path modules (5 use cases + `pg_execution_guard`). Regression tests for #206/#207/#208 pass.
4. **Phase 4 verify** — `pytest engine/tests/` returns 0 failures (any xfails tracked in hub). Before/after failure count logged in PR description.
5. **Phase 5 verify** — nightly job runs once manually via `workflow_dispatch`. Coverage reports upload to Codecov.

## Delivery phases

Ships as 5 separate PRs against `develop`, mergeable independently, in order.

### Phase 1 — Unblock (foundation)
- `engine/requirements-test.txt` with test deps
- `TestSettings` subclass in `infrastructure/config/settings.py`
- `engine/tests/conftest.py` — sys.path canonicalize + autouse `TestSettings` env
- `engine/tests/fixtures/` scaffold (empty builders)
- Audit: any use case calling `get_settings()` directly → refactor to inject
- **Exit criteria:** `pytest engine/tests/ --collect-only` has 0 errors

### Phase 2 — Canonical imports
- Grep sweep + rewrite `engine.*` → bare imports
- Conftest sibling-path removal
- CI lint rule `grep -rn "from engine\."`
- `test_no_dual_import.py` regression test
- Fix `test_publish_heartbeat` 2 failures
- **Exit criteria:** lint passes, heartbeat tests green

### Phase 3 — Execution-path hard gate
- Fix `test_reconcile_positions::test_no_match_returns_none`
- Fix `test_execute_manual_trade::test_live_mode_places_real_order`
- Fix `test_evaluate_window` 2 failures
- Add port-contract tests for reconcile + execute_trade + execute_manual_trade (three-level discipline)
- CI coverage gate `--cov-fail-under=90` on 6 modules (5 use cases + `pg_execution_guard`)
- Regression tests for #206/#207/#208 retained (heartbeat regression lands in Phase 2)
- **Exit criteria:** CI coverage gate green, 4 execution-path failures fixed (reconcile×1, execute_manual_trade×1, evaluate_window×2)

### Phase 4 — Triage remaining clusters
- Subagent sweep of `test_value_objects` (13), `test_v4_fusion_strategy` (5), `test_data_surface` (1), unaccounted ~96
- Per-test: fix / xfail+hub-task / delete
- Hub-task creation via `POST /api/audit-tasks` for each xfail
- **Deliverable:** `docs/superpowers/plans/2026-04-15-engine-test-triage.md` logged with per-file decisions
- **Exit criteria:** `pytest engine/tests/` 0 failures (xfails tracked)

### Phase 5 — Nightly + coverage reporting
- `.github/workflows/nightly.yml` cron on develop
- Codecov integration (soft global coverage)
- `docs/engine-testing.md` dev workflow + markers + fixtures guide
- Coverage trend baseline captured
- **Exit criteria:** nightly runs successfully, baseline coverage reported

## Open questions

None blocking. Some follow-ups worth considering after Phase 5:

- Promote canonical-imports lint to `ruff` rule instead of grep.
- Add domain-layer dependency lint (no adapter imports from domain tests).
- Consider `hypothesis` property tests for value objects once stable.
- Paper-mode integration tier deferred — revisit when/if Montreal APIs become reachable from GH runners.

## References

- Clean Architecture guide: `/Users/billyrichards/Downloads/clean_architecture_python_guide.md`
- Recent execution-path bugs: #206, #207, #208
- Existing CI: `.github/workflows/ci.yml`
- Existing conftest: `engine/tests/conftest.py`
