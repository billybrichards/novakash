# novakash-timesfm

**TimesFM 2.5 BTC Price Forecast Microservice**

Zero-shot BTC price forecasting using [TimesFM 2.5 200M](https://huggingface.co/google/timesfm-2.5-200m-pytorch) (Google Research) — pre-trained time series foundation model. Designed for Polymarket BTC UpDown 5-minute windows.

---

## Features

- 📡 **Live Binance feed** — WebSocket BTC/USDT trades, 1-second aggregated ticks
- 🔮 **TimesFM 2.5** — zero-shot 60-step forecast with quantile intervals (P10–P90)
- ⚡ **Cached forecasts** — refreshed every 10 seconds, sub-ms response for cached
- 🌐 **REST + WebSocket** — dashboard-ready, CORS open for Vercel
- 🐳 **Docker-ready** — CPU-only, single container

---

## Quick Start

### Docker (recommended)

```bash
docker-compose up --build
```

Service starts on `http://localhost:8080`. First startup downloads the model (~800MB) from Hugging Face — subsequent starts use the `hf_cache` volume.

### Local Development

```bash
# Install deps
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# Run
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

---

## API Reference

### `GET /health`

```json
{
  "status": "ok",
  "model_loaded": true,
  "price_feed_connected": true,
  "buffer_size": 1024,
  "uptime_seconds": 42.1
}
```

### `GET /forecast`

Returns the latest cached forecast (updated every 10s from live Binance data).

```json
{
  "point_forecast": [65012.3, 65018.7, ...],
  "quantiles": {
    "p10": [...], "p25": [...], "p50": [...], "p75": [...], "p90": [...]
  },
  "predicted_close": 65089.2,
  "direction": "UP",
  "confidence": 0.87,
  "spread": 312.4,
  "horizon": 60,
  "input_length": 512,
  "timestamp": 1712263200.0
}
```

### `POST /forecast`

Custom price array forecast.

**Request:**
```json
{
  "prices": [64800.0, 64823.5, ...],
  "horizon": 60
}
```

**Response:** Same as `GET /forecast`.

### `WebSocket /ws/forecast`

Connects and receives live forecast updates every 10 seconds. Send `"ping"` to receive `"pong"`.

### `WebSocket /ws/prices`

Streams live BTC price buffer every second:
```json
{
  "prices": [64800.0, ...],
  "last_price": 64912.5,
  "buffer_size": 512,
  "timestamp": 1712263200.0
}
```

---

## Test Script

```bash
# Install requests if needed
pip install requests

# Run test (service must be running)
python test_forecast.py

# Custom host/port
python test_forecast.py --host localhost --port 8080 --n 256
```

---

## Architecture

```
Binance WS (btcusdt@trade)
    │
    ▼
PriceFeed (price_feed.py)
  • 1-second aggregated ticks
  • Rolling buffer: 2048 ticks
    │
    ├──▶ GET /forecast (cached, every 10s)
    │
    └──▶ TimesFMForecaster (forecaster.py)
           • TimesFM 2.5 200M PyTorch
           • Zero-shot, context=1024, horizon=60
           • Quantiles: P10/25/50/75/90
           │
           └──▶ WS /ws/forecast (push every 10s)
```

---

## Model Details

| | |
|---|---|
| **Model** | `google/timesfm-2.5-200m-pytorch` |
| **Parameters** | 200M |
| **Backend** | PyTorch (CPU) |
| **Context window** | 1024 ticks |
| **Horizon** | 60 steps |
| **Quantile head** | Continuous (P10–P90) |
| **Frequency** | 0 (high-frequency / sub-daily) |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HF_HOME` | `/app/.cache/huggingface` | Hugging Face model cache dir |
| `PYTHONUNBUFFERED` | `1` | Unbuffered stdout for Docker logs |

---

## Notes

- First start downloads ~800MB model from HuggingFace. Use the Docker volume to persist across restarts.
- CPU inference for a 60-step forecast takes ~1-3s on a modern CPU.
- For production, consider pinning the model cache to a persistent volume or pre-baking it into the image.
