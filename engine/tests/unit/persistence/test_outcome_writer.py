"""Forward-writer regression tests (hub note 297, 2026-04-30).

Background
----------
For ~3 weeks ``window_snapshots.outcome`` and ``signal_evaluations.outcome``
were being silently dropped or polluted by the engine on resolution:

  * ``order_manager.poll_resolutions`` called
    ``DBClient.update_window_outcome`` with the trade's ``WIN`` / ``LOSS``
    label in the slot meant for the directional UP / DOWN / FLAT label.
    The column took the wrong values (729 polluted rows) and every
    downstream analysis script silently filtered them out as ``IS NULL``.

  * No engine path ever wrote ``signal_evaluations.outcome`` — 100% NULL
    post-Apr-8. ``v_signal_comparison`` view returned 0 rows.

Fix (PR: forward-outcome-writer):

  * ``update_window_outcome`` coerces ``outcome`` to UP/DOWN/FLAT using
    ``poly_winner`` as source of truth. WIN/LOSS without a directional
    poly_winner is silently ignored for the column.
  * ``COALESCE`` everywhere so re-deliveries cannot overwrite a
    correctly-resolved value.
  * New ``update_signal_evaluations_outcome`` bulk-fills the column for
    every row tied to a window.

These tests pin the new semantics — they will fail loudly if a future
refactor brings back the WIN/LOSS pollution or removes the
signal_evaluations writer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from persistence.db_client import DBClient


# ─── Coercion (pure) ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "outcome, poly_winner, expected",
    [
        # poly_winner is the source of truth — wins over outcome
        ("WIN", "Up", "UP"),
        ("LOSS", "Down", "DOWN"),
        ("WIN", "down", "DOWN"),
        # Direct UP/DOWN/FLAT also accepted
        ("UP", None, "UP"),
        ("down", None, "DOWN"),
        ("FLAT", None, "FLAT"),
        # WIN/LOSS without a poly_winner = no directional info → None
        ("WIN", None, None),
        ("LOSS", None, None),
        # Garbage in → None out
        ("", None, None),
        (None, None, None),
        ("RANDOM", "garbage", None),
        # Whitespace is tolerated
        (" up ", None, "UP"),
        (None, " Down\n", "DOWN"),
    ],
)
def test_coerce_directional_outcome(outcome, poly_winner, expected):
    assert DBClient._coerce_directional_outcome(outcome, poly_winner) == expected


# ─── update_window_outcome — SQL contract ─────────────────────────────────

class _FakeConn:
    """Records the SQL + params an asyncpg connection would have run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return "UPDATE 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        # Return an async context manager that yields the conn.
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


def _stub_db_client() -> DBClient:
    """Build a DBClient with a fake pool — bypasses real connect()."""
    db = DBClient.__new__(DBClient)
    db._pool = _FakePool()
    return db


@pytest.mark.asyncio
async def test_update_window_outcome_writes_directional_from_poly_winner():
    """The 2026-04 regression: WIN/LOSS was being stored in outcome.

    With the fix, the directional label derived from poly_winner is
    written instead.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        outcome="WIN",        # legacy first arg — must NOT land in outcome col
        pnl_usd=12.34,
        poly_winner="Up",     # source of truth for direction
    )

    assert len(db._pool.conn.calls) == 1
    args = db._pool.conn.calls[0]["args"]
    # SQL is COALESCE(outcome, $1) — first positional is the directional label.
    assert args[0] == "UP", "must write UP, not WIN"
    assert args[1] == 12.34
    assert args[2] == "Up"
    assert args[3] == 1777617300
    assert args[4] == "BTC"
    assert args[5] == "5m"


@pytest.mark.asyncio
async def test_update_window_outcome_drops_non_directional_inputs():
    """If neither outcome nor poly_winner are directional, the
    coerced value is NULL — never WIN/LOSS — so COALESCE leaves the
    column untouched.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="WIN", pnl_usd=0.0, poly_winner=None,
    )
    args = db._pool.conn.calls[0]["args"]
    assert args[0] is None, "WIN without poly_winner must coerce to None"


@pytest.mark.asyncio
async def test_update_window_outcome_coalesces_every_column():
    """Idempotency contract: every UPDATEd column wraps in COALESCE so
    re-deliveries cannot overwrite a real resolution with stale data.
    """
    db = _stub_db_client()
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="UP", pnl_usd=1.0, poly_winner="Up",
    )
    sql = db._pool.conn.calls[0]["sql"]
    assert "COALESCE(outcome" in sql
    assert "COALESCE(pnl_usd" in sql
    assert "COALESCE(poly_winner" in sql


@pytest.mark.asyncio
async def test_update_window_outcome_no_pool_is_noop():
    db = DBClient.__new__(DBClient)
    db._pool = None
    # Must not raise.
    await db.update_window_outcome(
        window_ts=1, asset="BTC", timeframe="5m",
        outcome="UP", pnl_usd=1.0, poly_winner="Up",
    )


# ─── update_signal_evaluations_outcome ────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_evaluations_outcome_writes_when_directional():
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1777617300, asset="BTC", timeframe="5m", outcome="UP",
    )
    assert n == 1  # FakeConn.execute returned "UPDATE 1"
    assert len(db._pool.conn.calls) == 1
    args = db._pool.conn.calls[0]["args"]
    assert args[0] == "UP"
    assert args[1] == 1777617300
    assert args[2] == "BTC"
    assert args[3] == "5m"
    sql = db._pool.conn.calls[0]["sql"]
    assert "outcome IS NULL" in sql, "must be idempotent — fill NULL only"


@pytest.mark.asyncio
async def test_signal_evaluations_outcome_skips_non_directional():
    """``WIN`` / ``LOSS`` / garbage must NOT touch the table."""
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1, asset="BTC", timeframe="5m", outcome="WIN",
    )
    assert n == 0
    assert db._pool.conn.calls == []


@pytest.mark.asyncio
async def test_signal_evaluations_outcome_handles_lowercase():
    db = _stub_db_client()
    n = await db.update_signal_evaluations_outcome(
        window_ts=1, asset="BTC", timeframe="5m", outcome="down",
    )
    assert n == 1
    args = db._pool.conn.calls[0]["args"]
    assert args[0] == "DOWN"


# ─── update_shadow_resolution — outcome forward write ─────────────────────

@pytest.mark.asyncio
async def test_shadow_resolution_writes_outcome():
    """The shadow resolution loop must populate the canonical
    outcome column, not just oracle_outcome.

    Updated 2026-05-24 (RDS note #617): writer also stamps poly_winner,
    so the param order shifted to insert a new $5 = poly_winner slot.
    """
    db = _stub_db_client()
    await db.update_shadow_resolution(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        oracle_outcome="UP",
        shadow_pnl=1.50,
        shadow_would_win=True,
    )
    assert len(db._pool.conn.calls) == 1
    sql = db._pool.conn.calls[0]["sql"]
    args = db._pool.conn.calls[0]["args"]
    assert "outcome" in sql, "must write the canonical outcome column"
    assert "COALESCE(outcome" in sql
    assert "poly_winner" in sql, "audit #617: must write poly_winner too"
    assert "COALESCE(poly_winner" in sql
    # Param order in SQL: $1 oracle_outcome, $2 shadow_pnl, $3 shadow_would_win,
    #                    $4 directional outcome, $5 poly_winner, $6 ts,
    #                    $7 asset, $8 tf
    assert args[0] == "UP"            # oracle_outcome
    assert args[1] == 1.50            # shadow_pnl
    assert args[2] is True            # shadow_would_win
    assert args[3] == "UP"            # coerced directional outcome
    assert args[4] == "Up"            # poly_winner (capitalized)
    assert args[5] == 1777617300
    assert args[6] == "BTC"
    assert args[7] == "5m"


@pytest.mark.asyncio
async def test_shadow_resolution_writes_poly_winner_xrp():
    """RDS note #617: XRP-specific gap — shadow resolver must stamp
    poly_winner for any asset (XRP had 100% NULL because there were
    zero LIVE fires + the v8.1.2 fallback's ``oracle_winner IS NULL``
    filter excluded shadow-resolved rows).
    """
    db = _stub_db_client()
    await db.update_shadow_resolution(
        window_ts=1777617300,
        asset="XRP",
        timeframe="5m",
        oracle_outcome="DOWN",
        shadow_pnl=-2.10,
        shadow_would_win=False,
    )
    args = db._pool.conn.calls[0]["args"]
    assert args[4] == "Down"          # poly_winner capitalized
    assert args[6] == "XRP"           # asset


@pytest.mark.asyncio
async def test_shadow_resolution_poly_winner_none_when_outcome_unknown():
    """Unparseable outcome → directional and poly_winner both None;
    COALESCE protects the existing row.
    """
    db = _stub_db_client()
    await db.update_shadow_resolution(
        window_ts=1777617300,
        asset="BTC",
        timeframe="5m",
        oracle_outcome="WAT",   # garbage
        shadow_pnl=0.0,
        shadow_would_win=False,
    )
    args = db._pool.conn.calls[0]["args"]
    assert args[3] is None            # directional outcome
    assert args[4] is None            # poly_winner


# ─── populate_oracle_outcomes — bulk-path forward writer ──────────────────
#
# Follow-up to the original PR #439 fix (hub note 297, 2026-05-01):
# PR #439 patched the per-trade and shadow-loop paths but on Montreal
# they almost never fire. The bulk path that runs every ~2 min from
# reconcile_uc is ``PgWindowRepository.populate_oracle_outcomes`` — and
# that one was *not* patched, so ``window_snapshots.outcome`` still
# stayed NULL on 65% of rows for 16h post-deploy. These tests pin the
# bulk path's writer contract.


class _RecordingConn:
    """Like _FakeConn but threads each call through a per-window list and
    can stub asyncpg.Pool.fetch (rows from the 'find unresolved windows'
    query).
    """

    def __init__(self, fetch_rows: list) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fetch_rows = fetch_rows

    async def execute(self, sql: str, *args, **kwargs):
        self.calls.append({"sql": sql, "args": args, "kwargs": kwargs})
        return "UPDATE 1"

    async def fetch(self, sql: str, *args, **kwargs):
        return self._fetch_rows


class _RecordingPool:
    def __init__(self, fetch_rows: list) -> None:
        self.conn = _RecordingConn(fetch_rows)

    def acquire(self):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                return outer.conn

            async def __aexit__(self_inner, *exc):
                return None

        return _CM()


class _StubGammaResp:
    """Stubbed httpx response object — exposes ``status_code`` and ``json()``."""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _StubHttpxClient:
    """Async-context-manager replacing ``httpx.AsyncClient`` with deterministic
    Gamma responses. Each window_ts → a payload from ``responses``.
    """

    def __init__(self, responses):
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url: str, params=None, **_):
        slug = (params or {}).get("slug", "")
        return _StubGammaResp(200, self._responses.get(slug, []))


def _gamma_resolved_payload(slug: str, winner: str):
    """Build the Gamma /events shape that ``_fetch`` expects: list of events
    each with markets[]; closed=True; outcomes & outcomePrices arrays.
    """
    if winner == "UP":
        prices = ["1.0", "0.0"]
    else:
        prices = ["0.0", "1.0"]
    return [
        {
            "markets": [
                {
                    "slug": slug,
                    "closed": True,
                    "umaResolutionStatus": "resolved",
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": str(prices).replace("'", '"'),
                }
            ]
        }
    ]


@pytest.mark.asyncio
async def test_populate_oracle_outcomes_writes_outcome_column(monkeypatch):
    """Bulk oracle poll must stamp ``window_snapshots.outcome`` (not just
    oracle_outcome / poly_winner). This is the path that resolves ~99%
    of windows on Montreal — the per-trade and shadow paths cover <1%.

    Regression: 2026-05-01 — 65% of rows post-PR-#439 stayed NULL because
    PR #439 patched the per-trade and shadow paths only.
    """
    from adapters.persistence import pg_window_repo as repo_mod

    pool = _RecordingPool(fetch_rows=[{"window_ts": 1777617300}])
    repo = repo_mod.PgWindowRepository(pool)

    slug = f"{repo_mod._SLUG_PREFIX}1777617300"
    monkeypatch.setattr(
        repo_mod.httpx,
        "AsyncClient",
        lambda **_kw: _StubHttpxClient({slug: _gamma_resolved_payload(slug, "UP")}),
    )

    n = await repo.populate_oracle_outcomes()
    assert n == 1

    update_calls = [c for c in pool.conn.calls if c["sql"].lstrip().startswith("UPDATE")]
    snap_calls = [c for c in update_calls if "window_snapshots" in c["sql"]]
    se_calls = [c for c in update_calls if "signal_evaluations" in c["sql"]]

    assert len(snap_calls) == 1, "must update window_snapshots once per resolved window"
    snap_sql = snap_calls[0]["sql"]
    assert "outcome               = COALESCE(outcome" in snap_sql or \
           "outcome = COALESCE(outcome" in snap_sql, \
           "must stamp canonical outcome column with COALESCE"
    assert "oracle_outcome" in snap_sql
    assert "poly_winner" in snap_sql

    assert len(se_calls) == 1, (
        "must bulk-fill signal_evaluations.outcome — was 100% NULL pre-fix"
    )
    se_sql = se_calls[0]["sql"]
    assert "outcome IS NULL" in se_sql, "signal_evaluations writer must be idempotent"
    assert se_calls[0]["args"][0] == "UP"
    assert se_calls[0]["args"][1] == 1777617300


@pytest.mark.asyncio
async def test_populate_oracle_outcomes_covers_eth_5m(monkeypatch):
    """ETH 5m must be polled too. Pre-2026-05-20 the bulk writer was hardcoded
    BTC-only, so the 1,779+ signal_evaluations rows for ETH 5m that started
    flowing 2026-05-20 had no outcome column populated. Forward-writer now
    iterates ``_GAMMA_SLUG_PREFIXES`` and ETH 5m must be in there.
    """
    from adapters.persistence import pg_window_repo as repo_mod

    # _GAMMA_SLUG_PREFIXES is the contract: which pairs the bulk writer
    # actually polls. ETH 5m must be present.
    assert ("ETH", "5m") in repo_mod._GAMMA_SLUG_PREFIXES
    assert repo_mod._GAMMA_SLUG_PREFIXES[("ETH", "5m")] == "eth-updown-5m-"

    pool = _RecordingPool(fetch_rows=[{"window_ts": 1779299100}])
    repo = repo_mod.PgWindowRepository(pool)

    # Stub Gamma so only the ETH-prefixed slug resolves. If the writer is
    # still BTC-only it will ask for the btc- slug and find nothing →
    # the test will fail on snap_calls == 0.
    eth_slug = f"eth-updown-5m-1779299100"
    monkeypatch.setattr(
        repo_mod.httpx,
        "AsyncClient",
        lambda **_kw: _StubHttpxClient(
            {eth_slug: _gamma_resolved_payload(eth_slug, "DOWN")}
        ),
    )

    n = await repo.populate_oracle_outcomes()
    assert n >= 1, "ETH 5m window must be resolved + written"

    # The UPDATE for ETH must pass asset='ETH', timeframe='5m' as bind params.
    update_calls = [c for c in pool.conn.calls if c["sql"].lstrip().startswith("UPDATE")]
    snap_calls = [c for c in update_calls if "window_snapshots" in c["sql"]]
    eth_snap = [c for c in snap_calls if "ETH" in c["args"] and "5m" in c["args"]]
    assert eth_snap, "must UPDATE window_snapshots with asset=ETH timeframe=5m"

    se_calls = [c for c in update_calls if "signal_evaluations" in c["sql"]]
    eth_se = [c for c in se_calls if "ETH" in c["args"] and "5m" in c["args"]]
    assert eth_se, "must also bulk-fill signal_evaluations.outcome for ETH 5m"


@pytest.mark.asyncio
async def test_populate_oracle_outcomes_no_resolutions_no_writes(monkeypatch):
    """If Gamma returns no resolved markets, no UPDATE statements should fire."""
    from adapters.persistence import pg_window_repo as repo_mod

    pool = _RecordingPool(fetch_rows=[{"window_ts": 1777617300}])
    repo = repo_mod.PgWindowRepository(pool)

    monkeypatch.setattr(
        repo_mod.httpx,
        "AsyncClient",
        lambda **_kw: _StubHttpxClient({}),  # no responses → returns []
    )

    n = await repo.populate_oracle_outcomes()
    assert n == 0
    update_calls = [c for c in pool.conn.calls if c["sql"].lstrip().startswith("UPDATE")]
    assert update_calls == []
