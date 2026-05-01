"""SQL contract tests for ``PgDecisionsQueryRepo``.

Pin the SQL shape so the audit #340 regression family cannot recur:

1. Outcome must be read from ``signal_evaluations.outcome`` first
   (~99.7% populated, PR #441 wired the writer + index
   ``idx_se_v12_outcome``), with ``window_snapshots.outcome`` as a
   fallback (~23% populated overall, but used for any pre-#441 rows).
   The pre-#454 query relied on ``ws.close_price > ws.open_price``
   but those columns are ~0.02% populated on RDS, so n_wins / n_losses
   / wr_pct / real_net_pnl_usd were 100% NULL despite the scheduler
   running. Audit-task #340.
2. The join must be one-row-per-(asset, window_ts) on each side, not
   fan out across every eval_offset row in either table. Pre-#454
   the plain LEFT JOIN multiplied each strategy_decision by ~171
   (one row per eval_offset), inflating n_fires by the same factor.
"""

from __future__ import annotations

from adapters.persistence.pg_decisions_query_repo import _SQL


def test_actual_outcome_prefers_signal_evaluations():
    """signal_evaluations.outcome is 99.7% populated for trade windows
    (canonical column wired by PR #441). window_snapshots.outcome was
    polluted by an old WIN/LOSS writer (audit #339) and is only ~23%
    populated overall. Prefer se, fall back to ws."""
    assert "COALESCE(se.outcome, ws.outcome)" in _SQL, (
        "must coalesce signal_evaluations.outcome (primary) with "
        "window_snapshots.outcome (fallback) — see audit #340"
    )
    assert "FROM signal_evaluations" in _SQL


def test_actual_outcome_does_not_use_close_open_inference():
    """close_price / open_price are <0.1% populated on RDS — they cannot
    be used to infer outcome. The 100%-NULL regression that triggered
    audit #340 came from this inference path; it must never return."""
    assert "close_price > ws.open_price" not in _SQL
    assert "ws.close_price" not in _SQL
    assert "ws.open_price" not in _SQL


def test_signal_evaluations_lateral_filters_to_resolved():
    """The se lateral must only return rows with outcome set, otherwise
    COALESCE picks NULL and the ws fallback never fires for windows
    where signal_evaluations has both NULL and resolved offsets."""
    se_block = _SQL.split("FROM signal_evaluations", 1)[1].split(") se ON TRUE", 1)[0]
    assert "outcome IS NOT NULL" in se_block, (
        "signal_evaluations LATERAL must filter to outcome IS NOT NULL "
        "so COALESCE falls through to window_snapshots when se has only "
        "NULL rows for that window"
    )
    assert "LIMIT 1" in se_block


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


def test_stake_comes_from_trades_table_not_metadata():
    """strategy_decisions.metadata_json never carries stake_usd
    (verified live: 0/69 v12_lgb_combo trades have it). Stake lives
    on the ``trades`` table joined by ``order_id``."""
    assert "LEFT JOIN trades t ON t.order_id = sd.order_id" in _SQL
    assert "t.stake_usd" in _SQL
    assert "metadata_json->>'stake_usd'" not in _SQL, (
        "metadata_json never has stake_usd — must read from trades.stake_usd"
    )


def test_fill_price_falls_back_to_trades():
    """sd.fill_price is sometimes NULL on shadow paths; trades.fill_price
    is the avg-fill source of truth (PR #447)."""
    assert "COALESCE(sd.fill_price, t.fill_price)" in _SQL
