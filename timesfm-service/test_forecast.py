#!/usr/bin/env python3
"""
Test script for the TimesFM BTC Forecast Service.

Generates fake sinusoidal BTC price data, calls the forecast endpoint,
and prints the prediction result.

Usage:
    python test_forecast.py [--host localhost] [--port 8080]
"""

import argparse
import json
import math
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)


def generate_fake_btc_prices(
    n: int = 512,
    base_price: float = 65000.0,
    amplitude: float = 500.0,
    period: float = 120.0,
    noise_std: float = 50.0,
    seed: int = 42,
) -> list[float]:
    """
    Generate fake BTC prices with a sinusoidal trend + Gaussian noise.
    
    Args:
        n: Number of data points (seconds)
        base_price: Base BTC price (USD)
        amplitude: Sine wave amplitude (USD)
        period: Sine wave period (seconds)
        noise_std: Gaussian noise standard deviation
        seed: Random seed for reproducibility
    
    Returns:
        List of fake BTC prices
    """
    import random
    rng = random.Random(seed)
    
    prices = []
    for i in range(n):
        sine_component = amplitude * math.sin(2 * math.pi * i / period)
        noise = rng.gauss(0, noise_std)
        price = base_price + sine_component + noise
        # Clamp to realistic range
        price = max(base_price * 0.8, min(base_price * 1.2, price))
        prices.append(round(price, 2))
    
    return prices


def print_forecast_result(result: dict) -> None:
    """Pretty-print a forecast result dict."""
    print("\n" + "=" * 60)
    print("  🔮 TimesFM BTC Forecast Result")
    print("=" * 60)
    
    print(f"\n  Direction:       {result['direction']} {'🟢' if result['direction'] == 'UP' else '🔴'}")
    print(f"  Predicted Close: ${result['predicted_close']:,.2f}")
    print(f"  Confidence:      {result['confidence']:.1%}")
    print(f"  Spread (P90-P10): ${result['spread']:,.2f}")
    print(f"  Horizon:         {result['horizon']} steps")
    print(f"  Input Length:    {result['input_length']} ticks")
    
    if result.get("timestamp"):
        ts = result["timestamp"]
        print(f"  Generated At:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}")
    
    pf = result.get("point_forecast", [])
    if pf:
        print(f"\n  Point Forecast (first 5): {[f'${p:,.2f}' for p in pf[:5]]}")
        print(f"  Point Forecast (last 5):  {[f'${p:,.2f}' for p in pf[-5:]]}")
    
    q = result.get("quantiles", {})
    if q:
        print("\n  Final-step Quantiles:")
        for level, key in [("P10", "p10"), ("P25", "p25"), ("P50", "p50"), ("P75", "p75"), ("P90", "p90")]:
            vals = q.get(key, [])
            if vals:
                print(f"    {level}: ${vals[-1]:,.2f}")
    
    print("\n" + "=" * 60)


def test_health(base_url: str) -> bool:
    """Check service health."""
    try:
        resp = requests.get(f"{base_url}/health", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        print(f"\n  ✅ Health check passed")
        print(f"     Model loaded:     {data.get('model_loaded')}")
        print(f"     Feed connected:   {data.get('price_feed_connected')}")
        print(f"     Buffer size:      {data.get('buffer_size')} ticks")
        print(f"     Uptime:           {data.get('uptime_seconds')}s")
        return data.get("model_loaded", False)
    except requests.exceptions.ConnectionError:
        print(f"\n  ❌ Cannot connect to {base_url}")
        print("     Is the service running? Try: docker-compose up")
        return False
    except Exception as e:
        print(f"\n  ❌ Health check failed: {e}")
        return False


def test_post_forecast(base_url: str, prices: list[float]) -> dict | None:
    """Call POST /forecast with fake price data."""
    print(f"\n  📤 Calling POST /forecast with {len(prices)} price points...")
    
    payload = {
        "prices": prices,
        "horizon": 60,
    }
    
    try:
        start = time.time()
        resp = requests.post(
            f"{base_url}/forecast",
            json=payload,
            timeout=120,  # model inference can take a moment on CPU
        )
        elapsed = time.time() - start
        
        if resp.status_code == 200:
            print(f"  ✅ Forecast received in {elapsed:.2f}s")
            return resp.json()
        else:
            print(f"  ❌ Forecast failed: {resp.status_code} - {resp.text}")
            return None
    except requests.exceptions.Timeout:
        print("  ❌ Request timed out. Model may still be loading (wait 60s and retry).")
        return None
    except Exception as e:
        print(f"  ❌ Request failed: {e}")
        return None


def test_get_forecast(base_url: str) -> dict | None:
    """Call GET /forecast to get the live cached forecast."""
    print(f"\n  📥 Calling GET /forecast (live cached forecast)...")
    
    try:
        resp = requests.get(f"{base_url}/forecast", timeout=120)
        if resp.status_code == 200:
            print("  ✅ Cached forecast received")
            return resp.json()
        elif resp.status_code == 503:
            detail = resp.json().get("detail", "Not enough data")
            print(f"  ⏳ Service not ready yet: {detail}")
            return None
        else:
            print(f"  ❌ Failed: {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        print(f"  ❌ Request failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Test TimesFM BTC Forecast Service")
    parser.add_argument("--host", default="localhost", help="Service host")
    parser.add_argument("--port", default=8080, type=int, help="Service port")
    parser.add_argument("--n", default=512, type=int, help="Number of fake prices")
    parser.add_argument("--no-live", action="store_true", help="Skip live GET /forecast test")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    
    print(f"\n🚀 TimesFM BTC Forecast Service — Test Suite")
    print(f"   Target: {base_url}")
    print(f"   Fake prices: {args.n} data points")

    # ── 1. Health check ───────────────────────────────────────────────────────
    print("\n[1/3] Health Check")
    healthy = test_health(base_url)
    if not healthy:
        print("\n  ⚠️  Model not loaded yet. Continuing anyway...")

    # ── 2. Generate fake data ──────────────────────────────────────────────────
    print("\n[2/3] Generating Fake BTC Prices")
    prices = generate_fake_btc_prices(n=args.n)
    print(f"  Generated {len(prices)} data points")
    print(f"  Range: ${min(prices):,.2f} – ${max(prices):,.2f}")
    print(f"  First 5: {prices[:5]}")

    # ── 3. POST /forecast ──────────────────────────────────────────────────────
    print("\n[3/3] Running Forecast")
    result = test_post_forecast(base_url, prices)
    
    if result:
        print_forecast_result(result)
    
    # ── Optional: GET /forecast (live) ────────────────────────────────────────
    if not args.no_live:
        print("\n[bonus] Live Cached Forecast")
        live_result = test_get_forecast(base_url)
        if live_result:
            print_forecast_result(live_result)

    print("\n✅ Test complete.\n")


if __name__ == "__main__":
    main()
