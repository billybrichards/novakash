"""SQL contract tests for ``PgDecisionsQueryRepo``.

Pin the SQL shape so two regressions cannot recur:

1. Outcome must be read from ``window_snapshots.outcome`` (canonical
   UP/DOWN/FLAT label, ~23% populated). The pre-fix query relied on
   ``ws.close_price > ws.open_price`` but those columns are ~0.02%
   populated on RDS, so n_wins/n_losses/wr_pct/real_net_pnl_usd were
   100% NULL despite the scheduler running. Audit-task #340.
2. The join must be one-row-per-(asset, window_ts), not fan out
   across every eval_offset row in window_snapshots. Pre-fix the
   plain LEFT JOIN multiplied each strategy_decision by ~171 (one
   row per eval_offset), inflating n_fires by the same factor.
"""

from __future__ import annotations

from adapters.persistence.pg_decisions_query_repo import _SQL


def test_actual_outcome_reads_from_ws_outcome():
    assert "ws.outcome" in _SQL, "must read canonical outcome column"
    assert "ws.close_price > ws.open_price" not in _SQL, (
        "close_price/open_price are ~0% populated on RDS — never use them as the outcome source"
    )
    assert "ws.outcome" in _SQL.split("AS actual_outcome")[0]


def test_join_is_lateral_with_one_row_per_window():
    assert "LEFT JOIN LATERAL" in _SQL, (
        "plain LEFT JOIN multiplies trades by eval_offset count — must be LATERAL+LIMIT 1"
    )
    assert "LIMIT 1" in _SQL


def test_lateral_prefers_outcome_resolved_row():
    """When multiple eval_offset rows exist, prefer one with outcome set
    so resolved trades aren't tagged pending just because the LATERAL
    happened to pick an unresolved offset."""
    assert "outcome IS NOT NULL) DESC" in _SQL


def test_select_columns_unchanged():
    """The use case reads strategy_id, asset, timeframe, window_ts,
    eval_offset, direction, regime, fill_price, stake_usd,
    actual_outcome, evaluated_at — pin the projection."""
    for col in [
        "sd.strategy_id",
        "sd.asset",
        "sd.timeframe",
        "sd.window_ts",
        "sd.eval_offset",
        "sd.direction",
        "ws.regime",
        "sd.fill_price",
        "stake_usd",
        "actual_outcome",
        "sd.evaluated_at",
    ]:
        assert col in _SQL, f"missing projection: {col}"


def test_filters_to_executed_trades():
    assert "sd.action      = 'TRADE'" in _SQL
    assert "sd.executed    = TRUE" in _SQL
    assert "sd.evaluated_at >= $1" in _SQL
    assert "sd.evaluated_at <  $2" in _SQL
