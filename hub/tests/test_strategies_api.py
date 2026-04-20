"""Tests for hub/api/strategies.py — audit task #216.

`GET /api/strategies` scans engine/strategies/configs/*.yaml and returns a
map of strategy_id → {timeframe, yaml, runtime}. FE consumes the bare map
(no envelope) so useApiLoader's array-unwrap logic doesn't interfere.

Covers:
  1. Real YAML configs load end-to-end — smoke test against the actual
     engine/strategies/configs directory that ships with the repo.
  2. Bare-map shape (not {"strategies": {...}}) — FE contract.
  3. Timeframe normalisation (timescale→timeframe, fallback to "5m").
  4. Malformed YAML is skipped with a structured log, never crashes.
  5. Missing config dir returns empty map, never 500s.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unittest.mock import AsyncMock, MagicMock

from api.strategies import (
    router,
    _normalise_timeframe,
    _load_strategy_yaml,
    _pick_config_dir,
    _load_registry_from_db,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# ─── End-to-end: real YAML configs ────────────────────────────────────────────


def test_lists_all_shipping_strategies(client: TestClient) -> None:
    """Smoke: hits real engine/strategies/configs dir, confirms the
    strategies the engine ships with are all discoverable."""
    resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()

    # Bare map shape — NOT {"strategies": {...}}. FE relies on this.
    assert isinstance(data, dict)
    assert "strategies" not in data, (
        "Response must be bare map, not wrapped — FE useApiLoader "
        "only unwraps rows/trades/decisions/items."
    )

    # Every shipping strategy referenced in notes/audit tasks is present.
    for expected in ("v4_fusion", "v4_up_basic", "v15m_fusion"):
        assert expected in data, f"{expected} missing from registry"
        entry = data[expected]
        assert set(entry.keys()) == {"timeframe", "yaml", "runtime"}
        assert entry["timeframe"] in ("5m", "15m", "1h")
        assert isinstance(entry["yaml"], dict)
        assert entry["runtime"] == {}, "runtime reserved; should be empty"


def test_v4_fusion_yaml_round_trips(client: TestClient) -> None:
    """The YAML for v4_fusion is the spine of the live system; verify the
    fields FE renders (name/version/mode/sizing) survive the parse."""
    data = client.get("/api/strategies").json()
    y = data["v4_fusion"]["yaml"]
    assert y.get("name") == "v4_fusion"
    assert y.get("asset") == "BTC"
    assert y.get("timescale") == "5m"
    assert "sizing" in y and isinstance(y["sizing"], dict)


# ─── Unit: helpers ────────────────────────────────────────────────────────────


def test_normalise_timeframe_prefers_timescale() -> None:
    assert _normalise_timeframe({"timescale": "15m"}) == "15m"


def test_normalise_timeframe_falls_back_to_timeframe_field() -> None:
    assert _normalise_timeframe({"timeframe": "1h"}) == "1h"


def test_normalise_timeframe_default_when_missing() -> None:
    assert _normalise_timeframe({}) == "5m"


def test_load_strategy_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n")
    assert _load_strategy_yaml(str(bad), "bad") is None


def test_load_strategy_yaml_handles_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("key: : : invalid\n  indent")
    assert _load_strategy_yaml(str(bad), "broken") is None


def test_load_strategy_yaml_handles_missing_file() -> None:
    assert _load_strategy_yaml("/nonexistent/path.yaml", "missing") is None


# ─── Degraded paths ───────────────────────────────────────────────────────────


def test_missing_config_dir_returns_empty_map(client: TestClient) -> None:
    """If engine/strategies/configs doesn't exist (e.g. hub deployed
    without engine dir), endpoint returns {} not 500."""
    with patch("api.strategies._CONFIG_DIR", "/nonexistent/configs/path"):
        resp = client.get("/api/strategies")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_ignores_non_yaml_files(client: TestClient, tmp_path: Path) -> None:
    """Stray .txt / .py / README files in the config dir must not crash
    or leak into the registry."""
    (tmp_path / "v4_fusion.yaml").write_text(
        "name: v4_fusion\ntimescale: 5m\nmode: LIVE\n"
    )
    (tmp_path / "README.md").write_text("docs")
    (tmp_path / "v4_fusion.py").write_text("# hooks file, not config")

    with patch("api.strategies._CONFIG_DIR", str(tmp_path)):
        resp = client.get("/api/strategies")
    data = resp.json()
    assert list(data.keys()) == ["v4_fusion"]


def test_malformed_yaml_skipped_rest_still_load(
    client: TestClient, tmp_path: Path
) -> None:
    """One broken YAML must not kill the whole registry."""
    (tmp_path / "good.yaml").write_text(
        "name: good\ntimescale: 5m\nmode: GHOST\n"
    )
    (tmp_path / "bad.yaml").write_text("key: : : invalid\n  indent")

    with patch("api.strategies._CONFIG_DIR", str(tmp_path)):
        resp = client.get("/api/strategies")
    data = resp.json()
    assert "good" in data
    assert "bad" not in data


# ─── Config-dir resolver (2026-04-17 deploy-path hardening) ────────────────────


def test_pick_config_dir_respects_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRATEGY_CONFIGS_DIR env wins over all filesystem probes."""
    monkeypatch.setenv("STRATEGY_CONFIGS_DIR", str(tmp_path))
    assert _pick_config_dir() == str(tmp_path)


def test_pick_config_dir_prefers_hub_local_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given both candidates exist, hub/strategy_configs wins. This is the
    AWS-deploy layout; local repo-root layout is the fallback."""
    monkeypatch.delenv("STRATEGY_CONFIGS_DIR", raising=False)
    hub_local = tmp_path / "hub" / "strategy_configs"
    repo_engine = tmp_path / "engine" / "strategies" / "configs"
    hub_local.mkdir(parents=True)
    repo_engine.mkdir(parents=True)

    fake_module_file = tmp_path / "hub" / "api" / "strategies.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.write_text("")

    with patch("api.strategies.__file__", str(fake_module_file)):
        assert _pick_config_dir() == str(hub_local)


def test_pick_config_dir_falls_back_to_engine_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If hub/strategy_configs is absent but engine/strategies/configs
    exists (local dev / docker-compose), use the repo-root path."""
    monkeypatch.delenv("STRATEGY_CONFIGS_DIR", raising=False)
    repo_engine = tmp_path / "engine" / "strategies" / "configs"
    repo_engine.mkdir(parents=True)

    fake_module_file = tmp_path / "hub" / "api" / "strategies.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.write_text("")

    with patch("api.strategies.__file__", str(fake_module_file)):
        assert _pick_config_dir() == str(repo_engine)


def test_pick_config_dir_returns_aws_default_when_none_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning the AWS-deploy path when nothing exists makes the
    warning log in list_strategies point operators at the correct
    fix location."""
    monkeypatch.delenv("STRATEGY_CONFIGS_DIR", raising=False)

    fake_module_file = tmp_path / "hub" / "api" / "strategies.py"
    fake_module_file.parent.mkdir(parents=True, exist_ok=True)
    fake_module_file.write_text("")

    with patch("api.strategies.__file__", str(fake_module_file)):
        picked = _pick_config_dir()

    assert picked == str(tmp_path / "hub" / "strategy_configs")


# ─── DB-first registry (Phase 2 Option C.1) ──────────────────────────────────


def _mock_session_with_rows(rows: list[dict]):
    """Build a session that yields `rows` from ``result.mappings().all()``.

    Shape mirrors the SQLAlchemy async API used in ``_load_registry_from_db``.
    """
    mappings = MagicMock()
    mappings.all.return_value = rows
    result = MagicMock()
    result.mappings.return_value = mappings
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    async def gen():
        yield session

    return gen


def test_db_registry_preferred_over_filesystem(client: TestClient) -> None:
    """When the DB returns strategy rows, the endpoint serves them and
    never touches the filesystem."""
    db_rows = [
        {
            "strategy_id": "v4_fusion",
            "version": "4.5.0",
            "mode": "LIVE",
            "asset": "BTC",
            "timescale": "5m",
            "config_yaml": "name: v4_fusion\nversion: 4.5.0\nmode: LIVE\ntimescale: 5m\n",
            "gates_json": None,
            "sizing_json": None,
            "hooks_file": None,
            "updated_at": None,
        },
        {
            "strategy_id": "v15m_fusion",
            "version": "1.1.0",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "15m",
            "config_yaml": "name: v15m_fusion\nversion: 1.1.0\nmode: GHOST\ntimescale: 15m\n",
            "gates_json": None,
            "sizing_json": None,
            "hooks_file": None,
            "updated_at": None,
        },
    ]

    with patch("api.strategies._get_db_session", _mock_session_with_rows(db_rows)):
        with patch("api.strategies._load_registry_from_fs") as fs_mock:
            resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"v4_fusion", "v15m_fusion"}
    assert data["v4_fusion"]["yaml"]["version"] == "4.5.0"
    assert data["v15m_fusion"]["timeframe"] == "15m"
    # DB hit → filesystem must NOT be called
    fs_mock.assert_not_called()


def test_db_empty_falls_back_to_filesystem(client: TestClient) -> None:
    """Fresh cluster / engine hasn't seeded yet → DB returns [] rows,
    endpoint falls back to the filesystem resolver."""
    with patch("api.strategies._get_db_session", _mock_session_with_rows([])):
        resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    # Real shipping strategies must still show up via FS fallback
    assert "v4_fusion" in data
    assert len(data) >= 10


def test_db_read_error_falls_back_to_filesystem(client: TestClient) -> None:
    """If the DB session raises (table missing, connection dropped),
    the endpoint still serves from the filesystem instead of 500ing."""

    async def broken_session():
        raise RuntimeError("simulated DB outage")
        yield  # pragma: no cover — keeps the generator shape

    with patch("api.strategies._get_db_session", broken_session):
        resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert "v4_fusion" in data


def test_db_row_with_malformed_yaml_is_skipped(client: TestClient) -> None:
    """One corrupt ``config_yaml`` in the DB must not kill the whole
    DB-backed response."""
    db_rows = [
        {
            "strategy_id": "v4_fusion",
            "version": "4.5.0",
            "mode": "LIVE",
            "asset": "BTC",
            "timescale": "5m",
            "config_yaml": "name: v4_fusion\nversion: 4.5.0\nmode: LIVE\ntimescale: 5m\n",
            "gates_json": None,
            "sizing_json": None,
            "hooks_file": None,
            "updated_at": None,
        },
        {
            "strategy_id": "corrupt_row",
            "version": "0.0.1",
            "mode": "GHOST",
            "asset": "BTC",
            "timescale": "5m",
            "config_yaml": "key: : : invalid\n  indent",
            "gates_json": None,
            "sizing_json": None,
            "hooks_file": None,
            "updated_at": None,
        },
    ]
    with patch("api.strategies._get_db_session", _mock_session_with_rows(db_rows)):
        resp = client.get("/api/strategies")
    data = resp.json()
    assert "v4_fusion" in data
    assert "corrupt_row" not in data


def test_db_session_module_unavailable_still_serves_fs(client: TestClient) -> None:
    """If the hub DB session import was never wired (e.g. in a stripped-
    down unit harness), the endpoint must still serve the filesystem
    resolver rather than 500ing on a None attribute."""
    with patch("api.strategies._get_db_session", None):
        resp = client.get("/api/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert "v4_fusion" in data
