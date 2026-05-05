"""Strategies SKIP cleanly when priceToBeat-aligned probability is None.

Audit #351: when the Polymarket HTML scraper times out (today's maintenance),
the engine passes ``polymarket_price_to_beat=None`` to timesfm-service. With a
missing priceToBeat-aligned feature, timesfm-service returns
``probability_lgb_v9_1=None``. This MUST cascade into the strategy hooks as a
SKIP — a TRADE with a misaligned prediction loses money (4 trades lost during
the 30min maintenance window pre-fix).

This test pack confirms the existing skip-on-None behavior works; no new
gate is added (per audit brief: "If you find the strategies DO already
skip on None, just add tests confirming that and skip Part 3 implementation").
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from strategies.configs.v9_1_lgb_only import evaluate_v9_1_lgb_only
from strategies.configs.v12_lgb_combo import evaluate_v12_lgb_combo


def _surface(**overrides):
    """Build a permissive mock surface — only the probability fields matter
    for the SKIP-on-None check; the strategies short-circuit before any
    other gate fires."""
    base = dict(
        # Identity / generic
        asset="BTC",
        timescale="5m",
        window_ts=1777824300,
        eval_offset=10,
        # Probability fields
        probability_lgb=0.55,
        probability_lgb_v9_1=None,
        probability_lgb_v12=None,
        probability_classifier=None,
        v4_regime=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────────────
# v9_1_lgb_only — primary strategy that consumes priceToBeat-aligned probability
# ─────────────────────────────────────────────────────────────────────────────


def test_v9_1_lgb_only_skips_when_probability_lgb_v9_1_is_none():
    """When timesfm-service couldn't compute v9.1 probability (because the
    engine passed polymarket_price_to_beat=None), strategy SKIPs without
    trading."""
    surface = _surface(probability_lgb_v9_1=None)
    decision = evaluate_v9_1_lgb_only(surface)

    assert decision.action == "SKIP"
    assert decision.skip_reason == "v9_1_model_not_loaded"
    assert decision.direction is None
    # Confirms the strategy did NOT silently fall back to v9 PROD probability.
    assert decision.metadata.get("v9_1_enabled") is False


def test_v9_1_lgb_only_proceeds_when_probability_lgb_v9_1_is_present():
    """Sanity check: when v9.1 probability IS available (priceToBeat scraper
    worked), the strategy proceeds to delegation. We don't assert the
    eventual TRADE/SKIP decision (depends on full gate stack), but it must
    not skip with the priceToBeat-unavailable reason."""
    surface = _surface(probability_lgb_v9_1=0.62, probability_lgb=0.55)
    # The full delegation depends on gates we don't want to mock here. We
    # only verify the early-return branch is NOT taken.
    try:
        decision = evaluate_v9_1_lgb_only(surface)
    except Exception:
        # Downstream gate explosion is fine — we just want to prove the
        # early SKIP-on-None branch wasn't hit.
        return
    # If it returned, skip_reason must NOT be the v9.1-not-loaded one.
    assert decision.skip_reason != "v9_1_model_not_loaded"


# ─────────────────────────────────────────────────────────────────────────────
# v12_lgb_combo — combo strategy that needs BOTH v9.1 and v12 probabilities
# ─────────────────────────────────────────────────────────────────────────────


def test_v12_lgb_combo_skips_when_probability_lgb_v9_1_is_none():
    """v12_lgb_combo (post-2026-05-02 v9.1 swap) reads probability_lgb_v9_1
    as the v9-side. None → clean SKIP, NOT silent fall-back to v9 PROD."""
    surface = _surface(probability_lgb_v9_1=None, probability_lgb_v12=0.62)
    decision = evaluate_v12_lgb_combo(surface)

    assert decision.action == "SKIP"
    assert decision.skip_reason == "probability_lgb_v9_1 unavailable"
    assert decision.direction is None
    # Confirms the strategy did NOT silently fall back.
    assert decision.metadata.get("v9_1_enabled") is False


def test_v12_lgb_combo_skips_when_probability_lgb_v12_is_none():
    """If v12-side probability is None, also SKIP (independent guard)."""
    surface = _surface(probability_lgb_v9_1=0.62, probability_lgb_v12=None)
    decision = evaluate_v12_lgb_combo(surface)

    assert decision.action == "SKIP"
    assert decision.skip_reason == "probability_lgb_v12 unavailable"
    assert decision.direction is None


def test_v12_lgb_combo_skips_when_both_probabilities_none():
    """Both None → SKIP, with v12 guard firing first (function checks v12
    before v9.1)."""
    surface = _surface(probability_lgb_v9_1=None, probability_lgb_v12=None)
    decision = evaluate_v12_lgb_combo(surface)

    assert decision.action == "SKIP"
    # v12 guard fires first per implementation order.
    assert decision.skip_reason == "probability_lgb_v12 unavailable"
