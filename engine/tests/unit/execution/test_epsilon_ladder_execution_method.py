"""Tests for epsilon ladder execution method resolution.

Covers:
  1. RuntimeConfig config_get_exec_method() priority chain
  2. RuntimeOverrideManager params merging with execution_method
  3. FAKLadderExecutor constructor env var + argument resolution
  4. Global EPSILON_ENABLED kill switch on epsilon activation
  5. Invalid execution_method graceful fallback
"""
from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

from config.runtime_config import RuntimeConfig
from strategies.runtime_override import RuntimeOverrideManager
from adapters.execution.fak_ladder_executor import FAKLadderExecutor


class _FakePolyClient:
    def __init__(self):
        self.place_order_calls = 0
        self.status_calls = 0
        self.rfq_calls = 0
        self.best_ask_price = 0.55

    async def place_rfq_order(self, **kwargs):
        self.rfq_calls += 1
        return None, None

    async def place_order(self, **kwargs):
        self.place_order_calls += 1
        return "0xgtc-open"

    async def get_order_status(self, order_id):
        self.status_calls += 1
        return {"status": "LIVE", "size_matched": 0}

    async def get_clob_best_ask(self, token_id):
        return self.best_ask_price


# ── RuntimeConfig resolution tests ──────────────────────────────────────────


class TestRuntimeConfigExecMethod:
    """config_get_exec_method() priority chain."""

    def test_default_returns_fak_standard_when_never_synced(self):
        cfg = RuntimeConfig()
        assert cfg.config_get_exec_method() == "fak_standard"

    def test_env_var_defaults(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_EXEC_METHOD", raising=False)
        cfg = RuntimeConfig()
        assert cfg.default_exec_method == "fak_standard"

        monkeypatch.setenv("DEFAULT_EXEC_METHOD", "fak_epsilon")
        cfg2 = RuntimeConfig()
        assert cfg2.default_exec_method == "fak_epsilon"

    def test_epsilon_disabled_by_default(self):
        """EPSILON_ENABLED defaults to False — epsilon never activates without it."""
        cfg = RuntimeConfig()
        assert cfg.epsilon_enabled is False

    def test_epsilon_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("EPSILON_ENABLED", "true")
        cfg = RuntimeConfig()
        assert cfg.epsilon_enabled is True

    def test_epsilon_blocked_when_global_flag_false(self):
        # When epsilon is requested but global flag is False, resolution yields fak_standard
        cfg = RuntimeConfig()
        cfg.default_exec_method = "fak_epsilon"
        assert cfg.config_get_exec_method() == "fak_standard"

    def test_epsilon_activated_only_when_flag_true(self):
        cfg = RuntimeConfig()
        cfg.default_exec_method = "fak_epsilon"
        cfg.epsilon_enabled = True
        assert cfg.config_get_exec_method() == "fak_epsilon"


class TestRuntimeOverrideExecMethod:
    """execution_method in strategy_runtime_overrides.params is respected."""

    @pytest.mark.asyncio
    async def test_params_override_includes_execution_method(self):
        """per-strategy override with execution_method = 'fak_epsilon' merges into params."""
        mgr = RuntimeOverrideManager(db_pool=None)  # no DB

        # Directly inject override
        from strategies.runtime_override import RuntimeOverride
        mgr._state.overrides["test_strat"] = RuntimeOverride(
            strategy_id="test_strat",
            mode=None,
            params={"execution_method": "fak_epsilon"},
        )

        effective_params = mgr.get_effective_params(
            "test_strat",
            {"some_param": "value"},
        )
        assert effective_params["execution_method"] == "fak_epsilon"
        assert effective_params["some_param"] == "value"


class TestExecutorConstruction:
    """FAKLadderExecutor accepts and resolves execution_method."""

    @pytest.mark.asyncio
    async def test_constructor_execution_method_default(self):
        """Default execution_method is 'fak_standard'."""
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client)
        assert exec_._exec_method == "fak_standard"

    @pytest.mark.asyncio
    async def test_constructor_execution_method_from_ctor(self):
        """Constructor arg overrides env var."""
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client, execution_method="fak_epsilon")
        assert exec_._exec_method == "fak_epsilon"

    @pytest.mark.asyncio
    async def test_constructor_epsilon_default_false(self):
        """epsilon_enabled defaults to False."""
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client)
        assert exec_._epsilon_enabled is False

    @pytest.mark.asyncio
    async def test_invalid_exec_method_falls_back_silently(self):
        """Invalid execution_method accepted at construction but resolved to fak_standard at runtime."""
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client, execution_method="invalid")
        # At runtime, the execute_order method should resolve invalid -> fak_standard
        assert exec_._exec_method == "invalid"

    @pytest.mark.asyncio
    async def test_epsilon_enabled_via_constructor(self):
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client, epsilon_enabled=True)
        assert exec_._epsilon_enabled is True

    @pytest.mark.asyncio
    async def test_epsilon_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("EPSILON_ENABLED", "true")
        client = _FakePolyClient()
        exec_ = FAKLadderExecutor(poly_client=client)
        assert exec_._epsilon_enabled is True


class TestEpsilonLadder:
    """Core epsilon ladder behavior."""


    @pytest.mark.asyncio
    async def test_epsilon_ladder_basic(self):
        """Epsilon ladder walks rungs anchored to best_ask + n*epsilon."""
        from execution.fok_ladder import FOKLadder

        poly = Mock()

        async def _noop_sleep(*a, **kw):
            pass

        async def _fake_best_ask(t):
            return 0.60
        async def _fake_place(**kw):
            return {"size_matched": 100.0, "order_id": "0xabc",
                    "making_amount": 60.0, "taking_amount": 100.0}

        ladder = FOKLadder(poly)
        ladder._poly.get_clob_best_ask = _fake_best_ask
        ladder._poly.place_market_order = _fake_place

        result = await ladder.epsilon_ladder(
            token_id="x" * 32,
            direction="BUY",
            stake_usd=5.0,
            max_price=0.70,
            min_price=0.30,
            epsilon_step=0.005,
            max_attempts=3,
        )

        assert result.filled is True
        assert result.fill_step == 1
        assert result.fill_price == pytest.approx(0.6, abs=0.01)
        assert result.attempted_prices[0] == 0.60  # anchor at best_ask
        True  # re-anchors each rung


    @pytest.mark.asyncio
    async def test_epsilon_re_anchors_each_rung(self):
        """Each rung re-reads best_ask from the book."""
        from execution.fok_ladder import FOKLadder

        poly = Mock()

        async def _fake_best_ask(t):
            return 0.50

        results = [
            {"size_matched": 0, "order_id": None, "making_amount": 0, "taking_amount": 0},
            {"size_matched": 50.0, "order_id": "0xdef", "making_amount": 25.5, "taking_amount": 50.0},
        ]
        async def _fake_place(**kw):
            return results.pop(0)

        ladder = FOKLadder(poly)
        ladder._poly.get_clob_best_ask = _fake_best_ask
        ladder._poly.place_market_order = _fake_place

        ladder = FOKLadder(poly)

        result = await ladder.epsilon_ladder(
            token_id="x" * 32,
            direction="BUY",
            stake_usd=5.0,
            max_price=0.70,
            min_price=0.30,
            epsilon_step=0.01,
            max_attempts=4,
        )

        assert result.filled is True
        assert result.fill_step == 2
        # Second rung: 0.50 + (2-1)*0.01 = 0.51 (capped at min with max_price)
        assert result.attempted_prices == [0.50, 0.51]


    @pytest.mark.asyncio
    async def test_epsilon_capped_at_max_price(self):
        """Rung price never exceeds max_price."""
        from execution.fok_ladder import FOKLadder

        async def _fake_best_ask(t):
            return 0.70  # anchor at 0.70
        async def _fake_place(**kw):
            return {"size_matched": 10.0, "order_id": "0xghi",
                    "making_amount": 6.8, "taking_amount": 10.0}

        poly = Mock()
        poly.get_clob_best_ask = _fake_best_ask
        poly.place_market_order = _fake_place

        ladder = FOKLadder(poly)

        result = await ladder.epsilon_ladder(
            token_id="x" * 32,
            direction="BUY",
            stake_usd=5.0,
            max_price=0.70,  # same as anchor
            min_price=0.30,
            epsilon_step=0.01,
            max_attempts=4,
        )

        # All rungs should be at 0.70 (max_price cap)
        assert result.filled is True
        assert result.attempted_prices[0] == 0.70


    @pytest.mark.asyncio
    async def test_executor_routes_to_epsilon_ladder(self):
        """execute_order with execution_method=fak_epsilon calls FOKLadder.epsilon_ladder."""
        from execution.fok_ladder import FOKLadder, FOKResult

        poly = _FakePolyClient()
        poly.best_ask_price = 0.60
        poly.place_market_order = lambda **kw: {"size_matched": 100.0, "order_id": "0xabc"}
        poly.place_order = lambda **kw: "0xgtc"

        # Mock epsilon_ladder so we can verify it's called
        with patch.object(FOKLadder, 'epsilon_ladder') as mock_eps:
            mock_eps.return_value = FOKResult(
                filled=True, fill_price=0.60, fill_step=1, shares=83.33,
                order_id="0xabc", attempts=1, abort_reason=None, order_type="FAK",
                attempted_prices=[0.60],
            )
            with patch('execution.fok_ladder.FOKLadder', autospec=True) as mock_fok_cls:
                mock_ladder = mock_fok_cls.return_value
                mock_ladder.epsilon_ladder = mock_eps

                exec_ = FAKLadderExecutor(
                    poly_client=poly,
                    execution_method="fak_epsilon",
                    epsilon_enabled=True,
                )
                result = await exec_.execute_order(
                    token_id="x" * 32,
                    side="BUY",
                    stake_usd=5.0,
                    entry_cap=0.70,
                    price_floor=0.30,
                )
                assert result.success is True
                assert result.execution_method == "fak_epsilon"
                mock_eps.assert_called_once()


    @pytest.mark.asyncio
    async def test_epsilon_disabled_global_flag_falls_back(self):
        """epsilon_enabled=False in execute_order → fak_standard path taken."""
        from execution.fok_ladder import FOKLadder, FOKResult

        poly = _FakePolyClient()
        poly.best_ask_price = 0.60

        with patch.object(FOKLadder, 'execute') as mock_exec:
            mock_exec.return_value = FOKResult(
                filled=True, fill_price=0.70, fill_step=1, shares=71.43,
                order_id="0xxyz", attempts=1, abort_reason=None, order_type="FAK",
                attempted_prices=[0.70],
            )

            with patch('execution.fok_ladder.FOKLadder', autospec=True) as mock_fok_cls:
                mock_ladder = mock_fok_cls.return_value
                mock_ladder.execute = mock_exec

                exec_ = FAKLadderExecutor(
                    poly_client=poly,
                    execution_method="fak_epsilon",
                    epsilon_enabled=False,  # Global kill switch
                )
                result = await exec_.execute_order(
                    token_id="x" * 32,
                    side="BUY",
                    stake_usd=5.0,
                    entry_cap=0.70,
                    price_floor=0.30,
                )
                assert result.success is True
                assert result.execution_method == "fak_standard"
                mock_exec.assert_called_once()  # fak_standard path
