/**
 * mock-data.js — Realistic mock data generators for Novakash TimesFM dashboard.
 * Used until the real TimesFM backend is wired up.
 */

// ── Seeded PRNG (same pattern as Dashboard.jsx) ───────────────────────────────
function seededRng(seed = 42) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) & 0xffffffff;
    return (s >>> 0) / 0xffffffff;
  };
}

// ── BTC price simulation ──────────────────────────────────────────────────────
const BASE_PRICE = 67_300;

/**
 * Generate realistic BTC OHLCV candlestick data (hourly).
 * Returns array of { time, open, high, low, close, volume }
 * where time is unix timestamp (seconds).
 */
export function generateBTCCandles(count = 72) {
  const rng = seededRng(1337);
  const candles = [];
  const now = Math.floor(Date.now() / 1000);
  const intervalSecs = 3600; // 1-hour candles

  let price = BASE_PRICE;

  for (let i = count; i >= 0; i--) {
    const time = now - i * intervalSecs;
    const open = price;

    // Random walk with slight mean reversion
    const drift = (BASE_PRICE - price) * 0.005;
    const shock = (rng() - 0.5) * 400;
    const close = Math.max(40_000, open + drift + shock);

    const range = Math.abs(close - open) * (1 + rng() * 0.5);
    const high = Math.max(open, close) + range * rng() * 0.4;
    const low = Math.min(open, close) - range * rng() * 0.4;
    const volume = 800 + rng() * 1200;

    candles.push({ time, open, high, low, close, volume });
    price = close;
  }

  return candles;
}

/**
 * Generate live-updating BTC tick (call repeatedly on interval).
 * Returns { price, change, changePct }
 */
export function generateBTCTick(prevPrice = BASE_PRICE) {
  const delta = (Math.random() - 0.5) * 80;
  const price = Math.max(40_000, prevPrice + delta);
  const change = price - BASE_PRICE;
  const changePct = (change / BASE_PRICE) * 100;
  return { price, change, changePct };
}

// ── TimesFM Forecast ──────────────────────────────────────────────────────────

/**
 * Generate a TimesFM forecast for the current window.
 * @param {number} windowOpenPrice - BTC price at window open
 * @param {'UP'|'DOWN'} forcedDirection - optional override
 */
export function generateForecast(windowOpenPrice = BASE_PRICE, forcedDirection = null) {
  const rng = seededRng(Date.now() % 10000);

  // Slight random walk for predicted close
  const predictedDelta = (rng() - 0.48) * 200; // slight upward bias
  const predictedClose = windowOpenPrice + predictedDelta;

  const direction = forcedDirection ?? (predictedClose >= windowOpenPrice ? 'UP' : 'DOWN');
  const magnitude = Math.abs(predictedDelta);

  // Confidence scales with magnitude
  const baseConfidence = 0.52 + Math.min(0.35, magnitude / 600);
  const confidence = baseConfidence + (rng() - 0.5) * 0.08;

  // Quantiles — spread around predicted close
  const spread = 150 + rng() * 200;
  return {
    direction,
    confidence: Math.min(0.95, Math.max(0.51, confidence)),
    predictedClose,
    windowOpenPrice,
    delta: predictedDelta,
    quantiles: {
      p10: predictedClose - spread * 1.8,
      p25: predictedClose - spread * 1.0,
      p50: predictedClose,
      p75: predictedClose + spread * 1.0,
      p90: predictedClose + spread * 1.8,
    },
    modelVersion: 'TimesFM-1.0-200m',
    inferenceLatencyMs: 120 + Math.floor(rng() * 80),
    lastUpdated: new Date().toISOString(),
  };
}

/**
 * Generate forecast line data for chart overlay.
 * Returns array of { time, value } extending from now into the future.
 */
export function generateForecastLine(windowOpenTime, windowCloseTime, predictedClose) {
  const points = [];
  const steps = 12;
  for (let i = 0; i <= steps; i++) {
    const t = Math.floor(windowOpenTime + (i / steps) * (windowCloseTime - windowOpenTime));
    // Smooth curve from open price towards predicted close
    const progress = i / steps;
    const noise = (Math.random() - 0.5) * 30 * (1 - progress);
    const value = BASE_PRICE + (predictedClose - BASE_PRICE) * progress + noise;
    points.push({ time: t, value });
  }
  return points;
}

// ── Gamma Prices ──────────────────────────────────────────────────────────────

/**
 * Generate Polymarket-style gamma token prices for UP/DOWN.
 * Prices sum to ~$0.97 (vig) or ~$1.00.
 */
export function generateGammaPrices(forecastDirection = 'UP', confidence = 0.62) {
  const vig = 0.97;
  const up = parseFloat((confidence * vig + (Math.random() - 0.5) * 0.02).toFixed(4));
  const down = parseFloat((vig - up).toFixed(4));
  return {
    up: Math.min(0.96, Math.max(0.04, up)),
    down: Math.min(0.96, Math.max(0.04, down)),
    combined: up + down,
    spread: Math.abs(up - down),
    timestamp: new Date().toISOString(),
  };
}

// ── VPIN ──────────────────────────────────────────────────────────────────────

let _vpinState = 0.45;

/**
 * Generate a live VPIN reading with realistic mean-reversion.
 */
export function generateVPIN() {
  const shock = (Math.random() - 0.5) * 0.06;
  const revert = (0.48 - _vpinState) * 0.1;
  _vpinState = Math.max(0.1, Math.min(0.95, _vpinState + shock + revert));

  return {
    value: _vpinState,
    informed: _vpinState >= 0.55,
    cascade: _vpinState >= 0.70,
    percentile: Math.round(_vpinState * 100),
    regime: _vpinState < 0.40 ? 'QUIET' : _vpinState < 0.55 ? 'NORMAL' : _vpinState < 0.70 ? 'INFORMED' : 'CASCADE',
  };
}

// ── TWAP Delta ────────────────────────────────────────────────────────────────

/**
 * Generate TWAP-Delta time series over the current window.
 * Returns array of { time, delta, twap, gammaUp, gammaDown }
 */
export function generateTWAPDeltaSeries(count = 60) {
  const rng = seededRng(9999);
  const series = [];
  const now = Date.now();
  const intervalMs = 60_000; // 1-minute buckets

  let cumDelta = 0;
  let gammaUp = 0.62;
  let gammaDown = 0.35;

  for (let i = count; i >= 0; i--) {
    const time = now - i * intervalMs;
    const tick = (rng() - 0.48) * 0.8; // slight upward bias
    cumDelta += tick;

    // Gamma prices drift slowly
    const gammaShock = (rng() - 0.5) * 0.01;
    gammaUp = Math.max(0.05, Math.min(0.95, gammaUp + gammaShock));
    gammaDown = Math.max(0.05, Math.min(0.95, 0.97 - gammaUp));

    series.push({
      time,
      timeLabel: new Date(time).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
      delta: parseFloat(cumDelta.toFixed(4)),
      twap: parseFloat((cumDelta / (count - i + 1)).toFixed(4)),
      gammaUp: parseFloat(gammaUp.toFixed(4)),
      gammaDown: parseFloat(gammaDown.toFixed(4)),
    });
  }

  return series;
}

// ── Window Info ───────────────────────────────────────────────────────────────

/**
 * Get current window info (simulates Polymarket BTC UpDown windows).
 * Each window is 1 hour, aligned to hour boundaries.
 */
export function getWindowInfo() {
  const now = Date.now();
  const windowMs = 3_600_000; // 1 hour
  const windowOpen = Math.floor(now / windowMs) * windowMs;
  const windowClose = windowOpen + windowMs;
  const elapsed = now - windowOpen;
  const remaining = windowClose - now;

  const remainingMins = Math.floor(remaining / 60_000);
  const remainingSecs = Math.floor((remaining % 60_000) / 1000);

  return {
    asset: 'BTC',
    timeframe: '1H',
    windowOpenTs: new Date(windowOpen).toISOString(),
    windowCloseTs: new Date(windowClose).toISOString(),
    windowOpenUnix: Math.floor(windowOpen / 1000),
    windowCloseUnix: Math.floor(windowClose / 1000),
    elapsed: elapsed / 1000,
    remaining: remaining / 1000,
    remainingStr: `${remainingMins}m ${remainingSecs.toString().padStart(2, '0')}s`,
    progressPct: (elapsed / windowMs) * 100,
  };
}

// ── Signal Ensemble ───────────────────────────────────────────────────────────

/**
 * Generate all trading signals for the ensemble panel.
 */
export function generateSignals(btcPrice = BASE_PRICE, vpin = 0.48) {
  const rng = seededRng(Date.now() % 5000);

  const timesfmDir = rng() > 0.48 ? 'UP' : 'DOWN';
  const timesfmConf = 0.55 + rng() * 0.30;

  const gammaDir = rng() > 0.45 ? 'UP' : 'DOWN';
  const gammaConf = 0.50 + rng() * 0.40;

  const twapDir = rng() > 0.50 ? 'UP' : 'DOWN';
  const twapConf = 0.45 + rng() * 0.35;

  const cgDir = rng() > 0.52 ? 'UP' : 'DOWN';
  const cgConf = 0.40 + rng() * 0.45;

  const vpinDir = vpin >= 0.55 ? (rng() > 0.4 ? 'UP' : 'DOWN') : 'NEUTRAL';
  const vpinConf = vpin >= 0.55 ? vpin : 0.5;

  const signals = [
    {
      id: 'timesfm',
      name: 'TimesFM',
      icon: '🔮',
      direction: timesfmDir,
      confidence: timesfmConf,
      weight: 0.35,
      source: 'ML Forecast',
      color: '#a855f7',
    },
    {
      id: 'gamma',
      name: 'Gamma',
      icon: '⚡',
      direction: gammaDir,
      confidence: gammaConf,
      weight: 0.25,
      source: 'Token Price',
      color: '#06b6d4',
    },
    {
      id: 'twap',
      name: 'TWAP-Delta',
      icon: '📊',
      direction: twapDir,
      confidence: twapConf,
      weight: 0.20,
      source: 'Order Flow',
      color: '#f59e0b',
    },
    {
      id: 'coinglass',
      name: 'CoinGlass',
      icon: '🔭',
      direction: cgDir,
      confidence: cgConf,
      weight: 0.12,
      source: 'OI + Funding',
      color: '#34d399',
    },
    {
      id: 'vpin',
      name: 'VPIN',
      icon: '🌊',
      direction: vpinDir,
      confidence: vpinConf,
      weight: 0.08,
      source: 'Volume Sync',
      color: '#f87171',
    },
  ];

  // Compute weighted score
  const upScore = signals
    .filter(s => s.direction === 'UP')
    .reduce((acc, s) => acc + s.confidence * s.weight, 0);
  const downScore = signals
    .filter(s => s.direction === 'DOWN')
    .reduce((acc, s) => acc + s.confidence * s.weight, 0);
  const neutralScore = signals
    .filter(s => s.direction === 'NEUTRAL')
    .reduce((acc, s) => acc + s.confidence * s.weight, 0);

  const totalWeight = signals.reduce((acc, s) => acc + s.weight, 0);
  const normalizedUp = upScore / totalWeight;
  const normalizedDown = downScore / totalWeight;

  const aggDir = normalizedUp > normalizedDown ? 'UP' : 'DOWN';
  const aggConf = Math.max(normalizedUp, normalizedDown);

  // Conflict: majority signals disagree with weighted direction
  const upCount = signals.filter(s => s.direction === 'UP').length;
  const downCount = signals.filter(s => s.direction === 'DOWN').length;
  const hasConflict = Math.abs(upCount - downCount) <= 1;

  const shouldTrade = aggConf >= 0.58 && !hasConflict;

  return {
    signals,
    aggregate: {
      direction: aggDir,
      confidence: aggConf,
      upScore: normalizedUp,
      downScore: normalizedDown,
      hasConflict,
      shouldTrade,
      recommendation: shouldTrade
        ? `TRADE ${aggDir} @ ${(aggConf * 100).toFixed(0)}%`
        : hasConflict
        ? 'SKIP — SIGNAL CONFLICT'
        : 'SKIP — LOW CONFIDENCE',
    },
    coinglassData: {
      takerBuyRatio: 0.48 + rng() * 0.10,
      fundingRate: (rng() - 0.5) * 0.02,
      openInterest: 8_200_000_000 + rng() * 1_000_000_000,
      oiChange24h: (rng() - 0.5) * 0.08,
    },
  };
}

// ── Forecast History ──────────────────────────────────────────────────────────

/**
 * Generate last N forecast results for accuracy tracking.
 */
export function generateForecastHistory(count = 10) {
  const rng = seededRng(2222);
  const history = [];

  for (let i = count; i >= 1; i--) {
    const windowOpen = BASE_PRICE + (rng() - 0.5) * 1000;
    const actualClose = windowOpen + (rng() - 0.5) * 300;
    const predictedDir = rng() > 0.45 ? 'UP' : 'DOWN';
    const actualDir = actualClose >= windowOpen ? 'UP' : 'DOWN';
    const correct = predictedDir === actualDir;
    const confidence = 0.53 + rng() * 0.35;

    history.push({
      id: i,
      timestamp: new Date(Date.now() - i * 3_600_000).toISOString(),
      predictedDirection: predictedDir,
      actualDirection: actualDir,
      confidence,
      windowOpenPrice: windowOpen,
      predictedClose: windowOpen + (predictedDir === 'UP' ? 1 : -1) * 50 * confidence,
      actualClose,
      correct,
      pnl: correct ? confidence * 0.05 : -0.05,
    });
  }

  return history;
}
