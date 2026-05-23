"""Tests for engine.config.startup_validators.

Covers the FIVE_MIN_ASSETS / FIFTEEN_MIN_ASSETS alignment warning added
after PR #577 (XRP silently dropped from 5m feed for ~24h).

structlog has its own pipeline that doesn't route through python logging
by default in this repo's config, so we attach a capturing processor
directly for the duration of each test.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from config.startup_validators import (
    _parse_asset_env,
    validate_asset_enum_alignment,
)


# ── _parse_asset_env ────────────────────────────────────────────────────


def test_parse_none() -> None:
    assert _parse_asset_env(None) == set()


def test_parse_empty_string() -> None:
    assert _parse_asset_env("") == set()


def test_parse_single_asset() -> None:
    assert _parse_asset_env("BTC") == {"BTC"}


def test_parse_multi_assets_normalised() -> None:
    # Whitespace stripped, lowercase upper-cased, blanks dropped.
    assert _parse_asset_env(" btc, eth ,,sol,") == {"BTC", "ETH", "SOL"}


def test_parse_order_irrelevant() -> None:
    # Set semantics: order doesn't matter.
    assert _parse_asset_env("BTC,ETH") == _parse_asset_env("ETH,BTC")


# ── validate_asset_enum_alignment ───────────────────────────────────────
#
# structlog's log_capture is the canonical way to assert against structlog
# events without depending on caplog routing or the global config.


@pytest.fixture()
def captured_logs() -> Any:
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    yield cap
    # Reset to defaults so other tests see normal output.
    structlog.reset_defaults()


def test_match_returns_true_no_warning(captured_logs: Any) -> None:
    ok = validate_asset_enum_alignment(
        five_min_assets_env="BTC,ETH,SOL,XRP",
        fifteen_min_assets_env="XRP,SOL,ETH,BTC",  # order-insensitive
    )
    assert ok is True
    events = captured_logs.entries
    assert any(e["event"] == "engine.startup.asset_enum_aligned" for e in events)
    assert not any(
        e["event"] == "engine.startup.asset_enum_mismatch" for e in events
    )


def test_mismatch_returns_false_warns(captured_logs: Any) -> None:
    ok = validate_asset_enum_alignment(
        five_min_assets_env="BTC,ETH,SOL",
        fifteen_min_assets_env="BTC,ETH,SOL,XRP",  # XRP only in 15m
    )
    assert ok is False
    events = captured_logs.entries
    mismatch = [e for e in events if e["event"] == "engine.startup.asset_enum_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["log_level"] == "warning"


def test_both_empty_counts_as_aligned() -> None:
    # Edge: both unset → empty == empty → no warning.
    # No fixture needed — we only assert the return value here.
    assert validate_asset_enum_alignment(
        five_min_assets_env="",
        fifteen_min_assets_env="",
    ) is True


def test_only_in_five_only_in_fifteen_partitioned(captured_logs: Any) -> None:
    # The warning payload should list assets unique to each side so the
    # operator can immediately see what drifted.
    ok = validate_asset_enum_alignment(
        five_min_assets_env="BTC,DOGE",
        fifteen_min_assets_env="BTC,XRP",
    )
    assert ok is False
    mismatch = [
        e for e in captured_logs.entries
        if e["event"] == "engine.startup.asset_enum_mismatch"
    ]
    assert len(mismatch) == 1
    payload = mismatch[0]
    assert payload["only_in_five_min"] == ["DOGE"]
    assert payload["only_in_fifteen_min"] == ["XRP"]
    assert payload["five_min"] == ["BTC", "DOGE"]
    assert payload["fifteen_min"] == ["BTC", "XRP"]
    assert "PR #577" in payload["hint"]


def test_pr577_scenario_warns(captured_logs: Any) -> None:
    # Exact regression scenario: 5m had BTC,ETH,SOL (XRP dropped) while
    # 15m kept BTC,ETH,SOL,XRP. The validator must flag this.
    ok = validate_asset_enum_alignment(
        five_min_assets_env="BTC,ETH,SOL",
        fifteen_min_assets_env="BTC,ETH,SOL,XRP",
    )
    assert ok is False
    mismatch = [
        e for e in captured_logs.entries
        if e["event"] == "engine.startup.asset_enum_mismatch"
    ]
    assert len(mismatch) == 1
    assert mismatch[0]["only_in_fifteen_min"] == ["XRP"]
    assert mismatch[0]["only_in_five_min"] == []


def test_reads_from_env_when_no_args(
    captured_logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If no args are passed, the validator should read os.environ.
    monkeypatch.setenv("FIVE_MIN_ASSETS", "BTC")
    monkeypatch.setenv("FIFTEEN_MIN_ASSETS", "BTC,ETH")
    ok = validate_asset_enum_alignment()
    assert ok is False
    assert any(
        e["event"] == "engine.startup.asset_enum_mismatch"
        for e in captured_logs.entries
    )
