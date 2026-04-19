"""
Tests for the TimesFMV2Client dual-mode POST/GET fallback.

These tests spin up a real in-process aiohttp server and point the
client at it, so we exercise the actual HTTP layer — not mocks. The
cost is ~50ms per test; the benefit is that the fallback logic is
verified against real request/response cycles rather than our
assumption about what the client does.

Key invariants we're pinning:

  1. First POST on a fresh client tries push-mode.
  2. On 404/405/501, the client silently downgrades to GET for that
     SAME call, returns the GET response, and flips `_push_mode_supported`
     to False.
  3. Once flipped to False, all subsequent `score_with_features` calls
     skip POST entirely and go straight to GET — no retry.
  4. On 200, `_push_mode_supported` flips to True and stays there.
  5. On 5xx, the client propagates the error — NO silent fallback.
     Real outages must not be masked.
  6. Legacy `get_probability()` always uses GET and never touches the
     push-mode state machine.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from signals.timesfm_v2_client import TimesFMV2Client
from signals.v2_feature_body import V5FeatureBody, build_v5_feature_body


# ────────────────────────────────────────────────────────────────────
#  Test server builders
# ────────────────────────────────────────────────────────────────────


def _make_success_response(model_version: str = "test-sha") -> dict:
    """The minimum response shape the client expects."""
    return {
        "probability_up": 0.6,
        "probability_down": 0.4,
        "probability_raw": 0.58,
        "model_version": model_version,
        "delta_bucket": 60,
        "feature_freshness_ms": {},
        "timesfm": {},
        "timestamp": 0.0,
    }


class _Recorder:
    """Tracks per-method call counts on the test server.

    Instances are closed over by the request handlers so each test can
    assert how many POST vs GET requests actually hit the wire.
    """

    def __init__(self) -> None:
        self.post_count = 0
        self.get_count = 0
        self.last_post_body: dict | None = None
        self.last_get_params: dict[str, str] | None = None


async def _start_server(
    *,
    post_status: int,
    post_body: dict | None = None,
    get_status: int = 200,
    get_body: dict | None = None,
) -> tuple[TestServer, _Recorder]:
    """
    Spin up a test server that responds to POST and GET /v2/probability
    with the configured statuses and bodies. Returns the running server
    plus a _Recorder that tracks call counts for assertions.

    The body defaults use _make_success_response() so tests only have
    to specify what they care about.
    """
    rec = _Recorder()
    if post_body is None:
        post_body = _make_success_response("post-sha")
    if get_body is None:
        get_body = _make_success_response("get-sha")

    async def handle_post(request: web.Request) -> web.Response:
        rec.post_count += 1
        try:
            rec.last_post_body = await request.json()
        except Exception:
            rec.last_post_body = None
        return web.Response(
            status=post_status,
            body=json.dumps(post_body),
            content_type="application/json",
        )

    async def handle_get(request: web.Request) -> web.Response:
        rec.get_count += 1
        rec.last_get_params = dict(request.query)
        return web.Response(
            status=get_status,
            body=json.dumps(get_body),
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_post("/v2/probability", handle_post)
    app.router.add_get("/v2/probability", handle_get)

    server = TestServer(app)
    await server.start_server()
    return server, rec


async def _make_client(server: TestServer) -> TimesFMV2Client:
    base = f"http://{server.host}:{server.port}"
    return TimesFMV2Client(base_url=base, timeout=5.0)


# ────────────────────────────────────────────────────────────────────
#  Tests
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_success_flips_push_mode_to_true():
    """First successful POST locks in push-mode for the session."""
    server, rec = await _start_server(post_status=200)
    try:
        client = await _make_client(server)
        assert client._push_mode_supported is None  # untried

        features = build_v5_feature_body(eval_offset=60, vpin=0.5)
        result = await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )

        assert result["model_version"] == "post-sha"
        assert client._push_mode_supported is True
        assert rec.post_count == 1
        assert rec.get_count == 0
        # Verify the wire-level body actually contains the 25-field feature dict
        assert rec.last_post_body is not None
        assert rec.last_post_body["asset"] == "BTC"
        assert rec.last_post_body["seconds_to_close"] == 60
        assert "features" in rec.last_post_body
        assert len(rec.last_post_body["features"]) == 25

        await client.close()
    finally:
        await server.close()


@pytest.mark.parametrize("status", [404, 405, 501])
@pytest.mark.asyncio
async def test_post_unsupported_status_falls_back_to_get_and_flips_flag(status):
    """404/405/501 are the only statuses that trigger silent fallback."""
    server, rec = await _start_server(post_status=status)
    try:
        client = await _make_client(server)
        features = build_v5_feature_body(eval_offset=60)
        result = await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )

        # First call attempted POST, got an unsupported status, then
        # fell back to GET for the SAME call.
        assert result["model_version"] == "get-sha"
        assert rec.post_count == 1
        assert rec.get_count == 1
        assert client._push_mode_supported is False

        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_fallback_is_sticky_for_session():
    """Once flipped to False, subsequent calls skip POST entirely."""
    server, rec = await _start_server(post_status=405)
    try:
        client = await _make_client(server)
        features = build_v5_feature_body(eval_offset=60)

        # First call: attempts POST (fails 405), falls back to GET
        await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )
        assert rec.post_count == 1
        assert rec.get_count == 1

        # Second + third calls: should skip POST entirely
        await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )
        await client.score_with_features(
            asset="BTC", seconds_to_close=120, features=features,
        )

        # Critical: post_count stayed at 1 — we did NOT re-try POST.
        # get_count climbed to 3 (the original fallback + two direct GETs).
        assert rec.post_count == 1, "client retried POST after flipping to fallback mode"
        assert rec.get_count == 3

        await client.close()
    finally:
        await server.close()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.asyncio
async def test_5xx_does_not_trigger_silent_fallback(status):
    """Server-side errors MUST propagate — masking outages is dangerous."""
    server, rec = await _start_server(post_status=status)
    try:
        client = await _make_client(server)
        features = build_v5_feature_body(eval_offset=60)

        with pytest.raises(RuntimeError, match=f"{status}"):
            await client.score_with_features(
                asset="BTC", seconds_to_close=60, features=features,
            )

        # The client saw the 5xx, raised, and did NOT try GET.
        assert rec.post_count == 1
        assert rec.get_count == 0
        # Critically, push_mode_supported stays None — we didn't lock
        # in ANY mode because the server is flaking, not declaring its
        # capabilities.
        assert client._push_mode_supported is None

        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_legacy_get_probability_never_uses_push_state():
    """get_probability() is the pull-mode path — it should never touch
    the push-mode state machine, so the elm_prediction_recorder keeps
    working even after a push-mode fallback on another client."""
    server, rec = await _start_server(post_status=200)  # irrelevant
    try:
        client = await _make_client(server)
        result = await client.get_probability(
            asset="BTC", seconds_to_close=60,
        )

        assert result["model_version"] == "get-sha"
        assert rec.post_count == 0  # GET path never posts
        assert rec.get_count == 1
        assert client._push_mode_supported is None  # untouched

        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_body_contains_all_25_feature_keys():
    """Wire-level verification that the JSON body always has exactly
    the 25 fields the scorer will expect."""
    server, rec = await _start_server(post_status=200)
    try:
        client = await _make_client(server)
        # Deliberately sparse body — only 2 populated fields
        features = build_v5_feature_body(eval_offset=60, vpin=0.5)
        await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )

        assert rec.last_post_body is not None
        feature_dict = rec.last_post_body["features"]
        assert len(feature_dict) == 25
        # Populated fields
        assert feature_dict["eval_offset"] == 60.0
        assert feature_dict["vpin"] == 0.5
        # Unpopulated fields should be JSON null (→ Python None in the
        # parsed body), NOT missing and NOT 0.0
        assert feature_dict["delta_pct"] is None
        assert feature_dict["gate_vpin_passed"] is None
        assert feature_dict["regime_num"] is None
        assert feature_dict["v2_logit"] is None

        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_retries_on_transient_connection_reset():
    """POST that fails once with a connection reset retries and succeeds on
    the second attempt. Pins that transient transport failures (seen in prod
    as `v2.probability.push_connection_error`) are no longer fatal."""
    call_count = {"n": 0}

    async def handle_post(request: web.Request) -> web.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate Connection reset by peer by closing the transport.
            request.transport.close()
            return web.Response(status=500)
        return web.Response(
            status=200,
            body=json.dumps(_make_success_response("retry-sha")),
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_post("/v2/probability", handle_post)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        client = TimesFMV2Client(base_url=base, timeout=2.0, retries=2, retry_backoff=0.01)
        features = build_v5_feature_body(eval_offset=60)
        result = await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features,
        )
        assert result["model_version"] == "retry-sha"
        assert call_count["n"] == 2
        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_retries_exhausted_raises():
    """If transport keeps failing, the client gives up after `retries+1`
    total attempts and propagates. 5xx with a valid HTTP response is NOT
    a transport failure — that still propagates immediately without retry."""
    call_count = {"n": 0}

    async def handle_post(request: web.Request) -> web.Response:
        call_count["n"] += 1
        request.transport.close()
        return web.Response(status=500)

    app = web.Application()
    app.router.add_post("/v2/probability", handle_post)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        client = TimesFMV2Client(base_url=base, timeout=2.0, retries=1, retry_backoff=0.01)
        features = build_v5_feature_body(eval_offset=60)
        with pytest.raises(Exception):
            await client.score_with_features(
                asset="BTC", seconds_to_close=60, features=features,
            )
        # retries=1 => total 2 attempts.
        assert call_count["n"] == 2
        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_cedar_model_routes_to_cedar_url():
    """The model='cedar' / 'dune' path hits /v2/probability/cedar, not
    the base URL. We don't bother routing both in this test — just
    verify it doesn't hit the base path."""
    rec = _Recorder()

    async def handle_cedar(request: web.Request) -> web.Response:
        rec.post_count += 1
        return web.Response(
            status=200,
            body=json.dumps(_make_success_response("cedar-sha")),
            content_type="application/json",
        )

    async def handle_base(request: web.Request) -> web.Response:
        raise AssertionError("base /v2/probability should not have been hit")

    app = web.Application()
    app.router.add_post("/v2/probability/cedar", handle_cedar)
    app.router.add_post("/v2/probability", handle_base)
    server = TestServer(app)
    await server.start_server()
    try:
        client = await _make_client(server)
        features = build_v5_feature_body(eval_offset=60)
        result = await client.score_with_features(
            asset="BTC", seconds_to_close=60, features=features, model="cedar",
        )
        assert result["model_version"] == "cedar-sha"
        assert rec.post_count == 1
        await client.close()
    finally:
        await server.close()
