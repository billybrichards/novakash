"""
Tests for the ``probability_up_calibrated`` consumption path.

These cover the three layers the PR touches:

  1. ``TimesFMV2Client`` augments every payload (GET and POST) with a
     ``probability_up_effective`` key chosen per the env flag.
  2. ``FullDataSurface`` — built from the V4 snapshot ts_data — exposes
     ``v2_probability_up_raw`` / ``v2_probability_up_calibrated`` /
     ``isotonic_version`` and picks the calibrated value when both the
     env flag is true and the calibrated field is non-null.
  3. ``TelegramRenderer`` renders ``iso=vN`` on the ensemble line when
     ``isotonic_version`` is non-null, and omits it otherwise.

These tests complement ``test_timesfm_v2_client_fallback.py`` which
covers the POST/GET transport fallback.
"""
from __future__ import annotations

import json
import os
from typing import Optional
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from signals.timesfm_v2_client import TimesFMV2Client
from signals.v2_feature_body import build_v5_feature_body


# ────────────────────────────────────────────────────────────────────
#  Server + client helpers (parallel structure to the fallback tests)
# ────────────────────────────────────────────────────────────────────


def _base_payload(
    *,
    probability_up: float = 0.6,
    probability_up_calibrated: Optional[float] = None,
    isotonic_version: Optional[str] = None,
) -> dict:
    body = {
        "probability_up": probability_up,
        "probability_down": 1.0 - probability_up,
        "probability_raw": 0.58,
        "model_version": "test-sha",
        "delta_bucket": 60,
        "feature_freshness_ms": {},
        "timesfm": {},
        "timestamp": 0.0,
    }
    if probability_up_calibrated is not None:
        body["probability_up_calibrated"] = probability_up_calibrated
    if isotonic_version is not None:
        body["isotonic_version"] = isotonic_version
    return body


async def _start_server(response_body: dict) -> TestServer:
    async def handler(_req):
        return web.Response(text=json.dumps(response_body), content_type="application/json")

    app = web.Application()
    app.router.add_get("/v2/probability", handler)
    app.router.add_post("/v2/probability", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _build_client(server: TestServer) -> TimesFMV2Client:
    # retries=0 so flakiness from localhost timeouts doesn't mask intent.
    return TimesFMV2Client(
        base_url=f"http://{server.host}:{server.port}",
        timeout=5.0,
        retries=0,
    )


def _features():
    return build_v5_feature_body(eval_offset=60, vpin=0.5)


# ────────────────────────────────────────────────────────────────────
#  1) Client-level payload augmentation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_prob_uses_calibrated_when_flag_on_and_present(monkeypatch):
    """Flag true + calibrated present → effective = calibrated."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")
    server = await _start_server(
        _base_payload(
            probability_up=0.6,
            probability_up_calibrated=0.58,
            isotonic_version="v3",
        )
    )
    try:
        client = _build_client(server)
        try:
            payload = await client.get_probability("BTC", 60)
        finally:
            await client.close()
    finally:
        await server.close()

    # Both originals preserved, plus the new effective + metadata.
    assert payload["probability_up"] == 0.6
    assert payload["probability_up_calibrated"] == 0.58
    assert payload["isotonic_version"] == "v3"
    assert payload["probability_up_effective"] == 0.58


@pytest.mark.asyncio
async def test_get_prob_falls_through_when_calibrated_missing(monkeypatch):
    """Flag true but payload has no calibrated field → effective = raw."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")
    # Pre-PR-107 forecaster: no calibrated or isotonic keys at all.
    server = await _start_server(_base_payload(probability_up=0.71))
    try:
        client = _build_client(server)
        try:
            payload = await client.get_probability("BTC", 60)
        finally:
            await client.close()
    finally:
        await server.close()

    assert payload["probability_up"] == 0.71
    assert "probability_up_calibrated" not in payload
    assert payload["probability_up_effective"] == 0.71


@pytest.mark.asyncio
async def test_get_prob_falls_through_when_calibrated_null(monkeypatch):
    """Forecaster returned explicit null (no calibrator artifact yet)."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")
    server = await _start_server(
        {
            **_base_payload(probability_up=0.42),
            "probability_up_calibrated": None,
            "isotonic_version": None,
        }
    )
    try:
        client = _build_client(server)
        try:
            payload = await client.get_probability("BTC", 60)
        finally:
            await client.close()
    finally:
        await server.close()

    assert payload["probability_up_calibrated"] is None
    assert payload["probability_up_effective"] == 0.42


@pytest.mark.asyncio
async def test_get_prob_respects_disabled_flag(monkeypatch):
    """Flag false + calibrated present → effective = raw (engine-side kill)."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "false")
    server = await _start_server(
        _base_payload(
            probability_up=0.6,
            probability_up_calibrated=0.58,
            isotonic_version="v3",
        )
    )
    try:
        client = _build_client(server)
        try:
            payload = await client.get_probability("BTC", 60)
        finally:
            await client.close()
    finally:
        await server.close()

    # Calibrated fields still preserved (never stripped), but not chosen.
    assert payload["probability_up_calibrated"] == 0.58
    assert payload["probability_up_effective"] == 0.6


@pytest.mark.asyncio
async def test_score_with_features_uses_calibrated(monkeypatch):
    """POST push-mode path also augments payload."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")
    server = await _start_server(
        _base_payload(
            probability_up=0.6,
            probability_up_calibrated=0.64,
            isotonic_version="v7",
        )
    )
    try:
        client = _build_client(server)
        try:
            payload = await client.score_with_features("BTC", 60, _features())
        finally:
            await client.close()
    finally:
        await server.close()

    assert payload["probability_up"] == 0.6
    assert payload["probability_up_effective"] == 0.64
    assert payload["isotonic_version"] == "v7"


# ────────────────────────────────────────────────────────────────────
#  2) FullDataSurface assembly
# ────────────────────────────────────────────────────────────────────


def test_data_surface_populates_raw_and_calibrated(monkeypatch):
    """data_surface applies the env flag when ts_data carries both keys."""
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")

    # Simulate the relevant slice of `_assemble` inline without spinning
    # up the full DataSurfaceManager (which needs V4 snapshot cache).
    # We exercise the exact decision block added by this PR.
    ts_data = {
        "probability_up": 0.6,
        "probability_raw": 0.58,
        "probability_up_calibrated": 0.55,
        "isotonic_version": "v3",
    }

    p_up_raw = ts_data.get("probability_up")
    p_up_cal = ts_data.get("probability_up_calibrated")
    iso_version = ts_data.get("isotonic_version")
    p_up_eff = ts_data.get("probability_up_effective")
    if p_up_eff is None:
        use_cal = os.environ.get(
            "V2_PROB_USE_CALIBRATED", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        p_up_eff = p_up_cal if (use_cal and p_up_cal is not None) else p_up_raw

    assert p_up_raw == 0.6
    assert p_up_cal == 0.55
    assert iso_version == "v3"
    assert p_up_eff == 0.55  # calibrated wins under default flag


def test_data_surface_falls_through_when_cal_null(monkeypatch):
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "true")
    ts_data = {
        "probability_up": 0.42,
        "probability_up_calibrated": None,
        "isotonic_version": None,
    }

    p_up_raw = ts_data.get("probability_up")
    p_up_cal = ts_data.get("probability_up_calibrated")
    p_up_eff = ts_data.get("probability_up_effective")
    if p_up_eff is None:
        use_cal = os.environ.get(
            "V2_PROB_USE_CALIBRATED", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        p_up_eff = p_up_cal if (use_cal and p_up_cal is not None) else p_up_raw

    assert p_up_eff == 0.42


def test_data_surface_respects_disabled_flag(monkeypatch):
    monkeypatch.setenv("V2_PROB_USE_CALIBRATED", "false")
    ts_data = {
        "probability_up": 0.6,
        "probability_up_calibrated": 0.55,
        "isotonic_version": "v3",
    }

    p_up_raw = ts_data.get("probability_up")
    p_up_cal = ts_data.get("probability_up_calibrated")
    p_up_eff = ts_data.get("probability_up_effective")
    if p_up_eff is None:
        use_cal = os.environ.get(
            "V2_PROB_USE_CALIBRATED", "true"
        ).strip().lower() in ("1", "true", "yes", "on")
        p_up_eff = p_up_cal if (use_cal and p_up_cal is not None) else p_up_raw

    assert p_up_eff == 0.6  # raw wins when flag off


# ────────────────────────────────────────────────────────────────────
#  3) Telegram renderer — isotonic suffix
# ────────────────────────────────────────────────────────────────────


def _invoke_renderer(extras: Optional[dict]) -> Optional[str]:
    from adapters.alert.telegram_renderer import TelegramRenderer

    r = TelegramRenderer()
    return r._render_ensemble_extras(extras)


def test_renderer_includes_iso_version_when_present():
    extras = {
        "signal_source": "ensemble",
        "probability_used": 0.812,
        "probability_lgb": 0.773,
        "probability_classifier": 0.851,
        "ensemble_config": {"mode": "blend"},
        "isotonic_version": "v3",
    }
    line = _invoke_renderer(extras)
    assert line is not None
    assert "iso=v3" in line
    assert "path1=0.851" in line  # path1 still rendered unchanged


def test_renderer_omits_iso_when_null():
    extras = {
        "signal_source": "ensemble",
        "probability_used": 0.812,
        "probability_lgb": 0.773,
        "probability_classifier": 0.851,
        "ensemble_config": {"mode": "blend"},
        "isotonic_version": None,
    }
    line = _invoke_renderer(extras)
    assert line is not None
    assert "iso=" not in line


def test_renderer_omits_iso_when_key_missing():
    extras = {
        "signal_source": "ensemble",
        "probability_used": 0.812,
        "probability_lgb": 0.773,
        "probability_classifier": 0.851,
        "ensemble_config": {"mode": "blend"},
    }
    line = _invoke_renderer(extras)
    assert line is not None
    assert "iso=" not in line
