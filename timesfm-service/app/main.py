"""
TimesFM BTC Forecast Service

FastAPI microservice that:
- Connects to Binance WebSocket for live BTC prices
- Runs TimesFM 2.5 200M PyTorch model for zero-shot forecasting
- Exposes REST + WebSocket endpoints for the trading dashboard
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.forecaster import TimesFMForecaster
from app.models import ForecastRequest, ForecastResponse, HealthResponse, QuantileForecasts
from app.price_feed import PriceFeed

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Global state ────────────────────────────────────────────────────────────

_start_time = time.time()
_price_feed = PriceFeed(buffer_size=2048)
_forecaster = TimesFMForecaster(
    model_id="google/timesfm-2.5-200m-pytorch",
    max_context=2048,  # v5.8: Use full buffer — 34 min of 1s ticks for better pattern detection
    max_horizon=300,
    normalize_inputs=True,
    use_continuous_quantile_head=True,
)
_forecast_cache: Optional[dict] = None
_forecast_lock = asyncio.Lock()
_price_feed_task: Optional[asyncio.Task] = None
_forecast_refresh_task: Optional[asyncio.Task] = None

# Connected WebSocket clients
_forecast_ws_clients: set[WebSocket] = set()
_price_ws_clients: set[WebSocket] = set()


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model and start background tasks on startup."""
    global _price_feed_task, _forecast_refresh_task

    # Load model in thread pool (blocking IO)
    logger.info("Loading TimesFM model...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _forecaster.load)
    logger.info("TimesFM model ready.")

    # Start Binance price feed
    _price_feed_task = asyncio.create_task(_price_feed.start())
    logger.info("Price feed task started.")

    # Start forecast refresh loop
    _forecast_refresh_task = asyncio.create_task(_forecast_refresh_loop())
    logger.info("Forecast refresh loop started.")

    yield

    # ─── Shutdown ───
    logger.info("Shutting down...")

    if _forecast_refresh_task:
        _forecast_refresh_task.cancel()
        try:
            await _forecast_refresh_task
        except asyncio.CancelledError:
            pass

    await _price_feed.stop()

    if _price_feed_task:
        _price_feed_task.cancel()
        try:
            await _price_feed_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutdown complete.")


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="TimesFM BTC Forecast Service",
    description="Zero-shot BTC price forecasting using TimesFM 2.5 200M",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Background tasks ────────────────────────────────────────────────────────

async def _forecast_refresh_loop() -> None:
    """Refresh the forecast cache every 1 second and broadcast to WS clients."""
    global _forecast_cache, _forecast_ws_clients

    while True:
        try:
            await asyncio.sleep(1)

            prices = _price_feed.get_prices()
            if len(prices) < 10:
                logger.debug(f"Not enough prices yet ({len(prices)}), skipping forecast.")
                continue

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: _forecaster.forecast(prices, horizon=300)
            )

            async with _forecast_lock:
                _forecast_cache = result

            logger.info(
                f"Forecast updated: {result['direction']} @ {result['predicted_close']:.2f} "
                f"(confidence={result['confidence']:.2f})"
            )

            # Broadcast to connected WebSocket clients
            if _forecast_ws_clients:
                payload = json.dumps(result)
                dead = set()
                for ws in list(_forecast_ws_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                _forecast_ws_clients -= dead

        except asyncio.CancelledError:
            logger.info("Forecast refresh loop cancelled.")
            break
        except Exception as e:
            logger.error(f"Forecast refresh error: {e}", exc_info=True)
            await asyncio.sleep(5)  # back off on error


async def _price_broadcast_loop(websocket: WebSocket) -> None:
    """Stream live prices to a single WebSocket client every second."""
    try:
        while True:
            await asyncio.sleep(1)
            prices = _price_feed.get_prices(n=100)
            last_price = _price_feed.last_price
            payload = json.dumps({
                "prices": prices,
                "last_price": last_price,
                "buffer_size": _price_feed.buffer_size,
                "timestamp": time.time(),
            })
            await websocket.send_text(payload)
    except (WebSocketDisconnect, Exception):
        pass


# ─── REST Endpoints ──────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check — returns model and feed status."""
    return HealthResponse(
        status="ok",
        model_loaded=_forecaster.is_loaded,
        price_feed_connected=_price_feed.is_connected,
        buffer_size=_price_feed.buffer_size,
        uptime_seconds=round(time.time() - _start_time, 1),
    )


@app.get("/forecast", response_model=ForecastResponse)
async def get_forecast(horizon: int = 0) -> ForecastResponse:
    """
    Returns the latest forecast.

    Args:
        horizon: Forecast horizon in seconds/steps. If 0 (default), returns
                 the 1s background cache (horizon=300, full window).
                 If >0, runs a fresh inference with that exact horizon.
                 Use this to predict price at a specific window close:
                   horizon = window_close_ts - now_ts
    """
    global _forecast_cache

    # Custom horizon: run fresh inference (not cached)
    if horizon > 0:
        prices = _price_feed.get_prices()
        if len(prices) < 10:
            raise HTTPException(
                status_code=503,
                detail=f"Not enough price data yet. Buffer has {len(prices)} ticks (need 10+).",
            )
        h = max(1, min(horizon, 600))  # clamp to 1-600 steps
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: _forecaster.forecast(prices, horizon=h)
        )
        return _build_forecast_response(result)

    # Default: return background cache
    async with _forecast_lock:
        cached = _forecast_cache

    if cached is None:
        prices = _price_feed.get_prices()
        if len(prices) < 10:
            raise HTTPException(
                status_code=503,
                detail=f"Not enough price data yet. Buffer has {len(prices)} ticks (need 10+).",
            )
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: _forecaster.forecast(prices, horizon=300)
        )
        async with _forecast_lock:
            _forecast_cache = result
        cached = result

    return _build_forecast_response(cached)


@app.post("/forecast", response_model=ForecastResponse)
async def post_forecast(request: ForecastRequest) -> ForecastResponse:
    """
    Run a forecast on a custom price array.
    Useful for backtesting or dashboard replays.
    """
    if not _forecaster.is_loaded:
        raise HTTPException(status_code=503, detail="Model not yet loaded.")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _forecaster.forecast(request.prices, horizon=request.horizon),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _build_forecast_response(result)


def _build_forecast_response(result: dict) -> ForecastResponse:
    """Convert raw forecaster dict to ForecastResponse."""
    return ForecastResponse(
        point_forecast=result["point_forecast"],
        quantiles=QuantileForecasts(
            p10=result["quantiles"]["p10"],
            p25=result["quantiles"]["p25"],
            p50=result["quantiles"]["p50"],
            p75=result["quantiles"]["p75"],
            p90=result["quantiles"]["p90"],
        ),
        predicted_close=result["predicted_close"],
        direction=result["direction"],
        confidence=result["confidence"],
        spread=result["spread"],
        horizon=result["horizon"],
        input_length=result["input_length"],
        timestamp=result.get("timestamp"),
    )


# ─── WebSocket Endpoints ─────────────────────────────────────────────────────

@app.websocket("/ws/forecast")
async def ws_forecast(websocket: WebSocket) -> None:
    """
    WebSocket: streams the latest forecast every 10 seconds.
    Sends the cached value immediately on connect, then updates.
    """
    global _forecast_cache, _forecast_ws_clients
    await websocket.accept()
    _forecast_ws_clients.add(websocket)
    logger.info(f"WS /forecast client connected. Total: {len(_forecast_ws_clients)}")

    try:
        # Send current cache immediately
        async with _forecast_lock:
            cached = _forecast_cache
        if cached:
            await websocket.send_text(json.dumps(cached))

        # Keep connection alive, listen for pings
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # Echo pings back as pongs
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send heartbeat
                await websocket.send_text(json.dumps({"type": "heartbeat", "timestamp": time.time()}))

    except WebSocketDisconnect:
        logger.info("WS /forecast client disconnected.")
    except Exception as e:
        logger.error(f"WS /forecast error: {e}")
    finally:
        _forecast_ws_clients.discard(websocket)


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket) -> None:
    """
    WebSocket: streams raw BTC prices every second.
    Sends last 100 prices + current price on each tick.
    """
    await websocket.accept()
    _price_ws_clients.add(websocket)
    logger.info(f"WS /prices client connected. Total: {len(_price_ws_clients)}")

    broadcast_task = asyncio.create_task(_price_broadcast_loop(websocket))

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                pass

    except WebSocketDisconnect:
        logger.info("WS /prices client disconnected.")
    except Exception as e:
        logger.error(f"WS /prices error: {e}")
    finally:
        broadcast_task.cancel()
        _price_ws_clients.discard(websocket)
