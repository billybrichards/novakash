# v10.0 15m Trading System — Implementation Plan

**Date:** 2026-04-10 14:55 UTC  
**Author:** Novakash  
**Target:** Live deployment by Day 13  
**Success Criteria:** 93%+ WR over 30 trades or 7 days shadow mode

---

## Executive Summary

| Phase | Duration | Deliverable | Go/No-Go Gate |
|-------|----------|-------------|---------------|
| **Phase 1: Infrastructure** | Day 1-2 | 5m filter + 15m VPIN thresholds | All tests pass |
| **Phase 2: Model Training** | Day 3-5 | 15m TimesFM model + calibration | Validation WR >= 80% |
| **Phase 3: Shadow Mode** | Day 6-12 | 50-70 shadow trades | WR >= 93% |
| **Phase 4: Live** | Day 13+ | Production 15m trading | Monitor daily |

**Total Time to Live:** 12 days  
**Risk Level:** Medium (N=93 historical, need 30 live validation)

---

## Phase 1: Infrastructure (Day 1-2)

### Day 1: 5m Candle Confirmation Filter

**Objective:** Track 5m closes within 15m windows, detect intrawindow reversals

#### Task 1.1: Add 5m Window Tracking to Orchestrator

**File:** `engine/strategies/orchestrator.py`

```python
# Add to Orchestrator.__init__:
self._five_min_windows = {}  # window_ts -> {open, closes: [5m_closes]}

# Add method:
def _track_5m_closes(self, window_ts: int, close_price: float, timestamp: int):
    """Record 5m candle close within 15m window."""
    if window_ts not in self._five_min_windows:
        self._five_min_windows[window_ts] = {'open': None, 'closes': []}
    
    if self._five_min_windows[window_ts]['open'] is None:
        self._five_min_windows[window_ts]['open'] = close_price
    
    self._five_min_windows[window_ts]['closes'].append({
        'price': close_price,
        'timestamp': timestamp
    })

# Add method:
def _check_5m_confirmation(self, window_ts: int, direction: str) -> bool:
    """Check if 5m candles confirm 15m direction.
    
    Returns True if:
    - No 5m closes yet (first 5m window)
    - At least one 5m close agrees with 15m direction
    
    Returns False if:
    - 5m closes show reversal (e.g., 15m UP but all 5m closes DOWN)
    """
    if window_ts not in self._five_min_windows:
        return True  # No data yet
    
    data = self._five_min_windows[window_ts]
    if not data['closes']:
        return True  # No closes yet
    
    # Check if most 5m closes agree with 15m direction
    window_open = data['open']
    if window_open is None:
        return True
    
    recent_closes = data['closes'][-3:]  # Last 3 5m candles
    agreements = 0
    for close in recent_closes:
        if (direction == 'UP' and close['price'] > window_open) or \
           (direction == 'DOWN' and close['price'] < window_open):
            agreements += 1
    
    return agreements >= len(recent_closes) * 0.5  # 50% agreement
```

#### Task 1.2: Wire 5m Tick Recording

**File:** `engine/persistence/tick_recorder.py`

```python
# Add method:
async def record_5m_candle_close(self, window_ts: int, close_price: float, timestamp: int):
    """Record 5m candle close for 15m window tracking."""
    # Notify orchestrator via event
    await self._event_bus.emit('5m_candle_close', {
        'window_ts': window_ts,
        'close_price': close_price,
        'timestamp': timestamp
    })
    
    # Also log to DB for audit
    await self._db.execute("""
        INSERT INTO five_min_candle_closes (window_ts, close_price, timestamp)
        VALUES (%s, %s, %s)
        ON CONFLICT (window_ts, timestamp) DO NOTHING
    """, (window_ts, close_price, timestamp))
```

#### Task 1.3: Add DB Schema

**File:** `migrations/add_5m_candle_closes.sql`

```sql
-- Track 5m candle closes within 15m windows
CREATE TABLE IF NOT EXISTS five_min_candle_closes (
    id SERIAL PRIMARY KEY,
    window_ts BIGINT NOT NULL,           -- Parent 15m window timestamp
    five_min_ts BIGINT NOT NULL,         -- 5m candle timestamp
    close_price DECIMAL(18, 8) NOT NULL, -- 5m close price
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(window_ts, five_min_ts)
);

CREATE INDEX idx_5m_closes_window ON five_min_candle_closes(window_ts);
CREATE INDEX idx_5m_closes_timestamp ON five_min_candle_closes(five_min_ts);
```

**Run migration:**
```bash
psql -h <railway-host> -U <user> -d <database> -f migrations/add_5m_candle_closes.sql
```

#### Task 1.4: Integration Tests

**File:** `tests/test_5m_confirmation_filter.py`

```python
import pytest
from engine.strategies.orchestrator import Orchestrator

class TestFiveMinConfirmationFilter:
    
    def test_no_5m_data_yet(self):
        """Should pass filter if no 5m closes recorded."""
        orch = Orchestrator()
        assert orch._check_5m_confirmation(1775250000, 'UP') == True
    
    def test_all_5m_agree(self):
        """Should pass if all 5m closes agree with 15m direction."""
        orch = Orchestrator()
        orch._five_min_windows[1775250000] = {
            'open': 66931.96,
            'closes': [
                {'price': 66900.3, 'timestamp': 1775250300},  # DOWN
                {'price': 66897.26, 'timestamp': 1775250600}, # DOWN
                {'price': 66880.0, 'timestamp': 1775250900},  # DOWN
            ]
        }
        assert orch._check_5m_confirmation(1775250000, 'DOWN') == True
    
    def test_5m_reversal(self):
        """Should fail if 5m closes show reversal."""
        orch = Orchestrator()
        orch._five_min_windows[1775250000] = {
            'open': 66931.96,
            'closes': [
                {'price': 66950.0, 'timestamp': 1775250300},  # UP
                {'price': 66960.0, 'timestamp': 1775250600},  # UP
                {'price': 66970.0, 'timestamp': 1775250900},  # UP
            ]
        }
        assert orch._check_5m_confirmation(1775250000, 'DOWN') == False
```

**Run tests:**
```bash
cd engine && pytest tests/test_5m_confirmation_filter.py -v
```

---

### Day 2: 15m VPIN Thresholds + Env Vars

#### Task 2.1: Add 15m-Specific VPIN Config

**File:** `engine/config/runtime_config.py`

```python
# Add to RuntimeConfig:
@field_validator('five_min_vpin_gate', mode='before')
def _validate_vpin(cls, v):
    return float(v)

# Add new fields:
fifteen_min_vpin_gate: float = Field(
    default=0.40,
    description="VPIN floor for 15m windows (lower than 5m's 0.45)"
)
fifteen_min_cascade_threshold: float = Field(
    default=0.65,
    description="VPIN threshold for CASCADE regime in 15m windows"
)
```

#### Task 2.2: Update VPIN Gate Logic

**File:** `engine/signals/vpin.py`

```python
def classify_regime(self, vpin: float, timeframe: str = '5m') -> str:
    """Classify VPIN regime with timeframe-specific thresholds.
    
    5m windows:
    - NORMAL: VPIN < 0.45
    - TRANSITION: 0.45 <= VPIN < 0.55
    - CASCADE: VPIN >= 0.55
    
    15m windows:
    - NORMAL: VPIN < 0.40 (100% WR historically)
    - TRANSITION: 0.40 <= VPIN < 0.55
    - CASCADE: VPIN >= 0.65
    """
    if timeframe == '15m':
        if vpin >= self._runtime.fifteen_min_cascade_threshold:
            return 'CASCADE'
        elif vpin >= self._runtime.fifteen_min_vpin_gate:
            return 'TRANSITION'
        else:
            return 'NORMAL'  # 100% WR on 15m!
    else:
        # Original 5m logic
        if vpin >= self._runtime.vpin_cascade_direction_threshold:
            return 'CASCADE'
        elif vpin >= self._runtime.five_min_vpin_gate:
            return 'TRANSITION'
        else:
            return 'NORMAL'
```

#### Task 2.3: Update Strategy to Pass Timeframe

**File:** `engine/strategies/five_min_vpin.py`

```python
def _evaluate_window(self, window: WindowSnapshot) -> Optional[TradeDecision]:
    # ... existing code ...
    
    # Get VPIN regime with timeframe awareness
    timeframe = '15m' if window.duration_secs == 900 else '5m'
    regime = self._vpin_classifier.classify_regime(vpin, timeframe)
    
    # Apply timeframe-specific VPIN gate
    if timeframe == '15m':
        vpin_gate = self._runtime.fifteen_min_vpin_gate
    else:
        vpin_gate = self._runtime.five_min_vpin_gate
    
    if vpin < vpin_gate:
        return None  # SKIP
    
    # ... rest of evaluation ...
```

#### Task 2.4: Update .env Template

**File:** `.env.example`

```bash
# 15m Window Settings
FIFTEEN_MIN_WINDOW_DURATION=900
FIFTEEN_MIN_MAX_ENTRY_PRICE=0.80
FIFTEEN_MIN_EVAL_START_OFFSET=180

# VPIN Thresholds (15m-optimized)
FIFTEEN_MIN_VPIN_GATE=0.40
FIFTEEN_MIN_CASCADE_THRESHOLD=0.65

# v10.0 Feature Flags
V10_15M_ENABLED=true
V10_5M_CONFIRMATION_FILTER=true
V10_SHADOW_MODE=false
```

#### Task 2.5: Integration Tests

**File:** `tests/test_15m_vpin_thresholds.py`

```python
import pytest
from engine.signals.vpin import VPINClassifier
from engine.config.runtime_config import RuntimeConfig

class Test15mVPINThresholds:
    
    def test_15m_cascade(self):
        """15m CASCADE at VPIN >= 0.65."""
        config = RuntimeConfig()
        classifier = VPINClassifier(config)
        
        assert classifier.classify_regime(0.70, '15m') == 'CASCADE'
        assert classifier.classify_regime(0.65, '15m') == 'CASCADE'
        assert classifier.classify_regime(0.64, '15m') == 'TRANSITION'
    
    def test_15m_normal(self):
        """15m NORMAL at VPIN < 0.40 (should still trade at 100% WR)."""
        config = RuntimeConfig()
        classifier = VPINClassifier(config)
        
        assert classifier.classify_regime(0.39, '15m') == 'NORMAL'
        assert classifier.classify_regime(0.40, '15m') == 'TRANSITION'
    
    def test_5m_unchanged(self):
        """5m thresholds unchanged."""
        config = RuntimeConfig()
        classifier = VPINClassifier(config)
        
        assert classifier.classify_regime(0.54, '5m') == 'NORMAL'
        assert classifier.classify_regime(0.45, '5m') == 'TRANSITION'
        assert classifier.classify_regime(0.55, '5m') == 'CASCADE'
```

**Run tests:**
```bash
cd engine && pytest tests/test_15m_vpin_thresholds.py -v
```

---

## Phase 2: Model Training (Day 3-5)

### Day 3: Data Preparation

#### Task 3.1: Resample 1s Ticks to 15m Candles

**File:** `scripts/resample_to_15m.py`

```python
#!/usr/bin/env python3
"""Resample 1s Binance ticks to 15m OHLCV candles for TimesFM training."""

import argparse
import pandas as pd
from datetime import datetime
import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

def fetch_1s_ticks(start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch 1s Binance ticks from DB."""
    conn = psycopg2.connect(
        host=os.environ['DATABASE_HOST'],
        user=os.environ['DATABASE_USER'],
        password=os.environ['DATABASE_PASSWORD'],
        dbname=os.environ['DATABASE_NAME']
    )
    
    query = """
        SELECT timestamp, price, quantity, is_buyer_maker
        FROM ticks_binance
        WHERE timestamp >= %s AND timestamp <= %s
        ORDER BY timestamp
    """
    
    df = pd.read_sql_query(query, conn, params=(start_date, end_date))
    conn.close()
    
    return df

def resample_to_15m(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1s ticks to 15m OHLCV candles."""
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp')
    
    # Resample to 15m OHLCV
    ohlcv = df['price'].resample('15min').agg([
        ('open', 'first'),
        ('high', 'max'),
        ('low', 'min'),
        ('close', 'last')
    ])
    
    # Add volume
    volume = df['quantity'].resample('15min').sum()
    ohlcv['volume'] = volume
    
    # Reset index for TimesFM input
    ohlcv = ohlcv.reset_index()
    ohlcv['timestamp_ts'] = ohlcv['timestamp'].astype('int64') // 10**9
    
    return ohlcv

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--output', default='data/15m_candles.csv', help='Output file')
    args = parser.parse_args()
    
    print(f"Fetching 1s ticks from {args.start} to {args.end}...")
    ticks = fetch_1s_ticks(args.start, args.end)
    print(f"Fetched {len(ticks)} ticks")
    
    print("Resampling to 15m candles...")
    candles = resample_to_15m(ticks)
    print(f"Created {len(candles)} 15m candles")
    
    print(f"Saving to {args.output}...")
    candles.to_csv(args.output, index=False)
    print(f"Saved {len(candles)} candles to {args.output}")
    
    # Print stats
    print(f"\nDate range: {candles['timestamp'].min()} to {candles['timestamp'].max()}")
    print(f"Total 15m candles: {len(candles)}")
    print(f"Days covered: {len(candles) / 96:.1f}")

if __name__ == '__main__':
    main()
```

**Run:**
```bash
python scripts/resample_to_15m.py --start 2026-02-01 --end 2026-04-10 --output timesfm-calibration/data/15m_candles.csv
```

**Expected output:** ~5,760 candles (60 days × 96 per day)

#### Task 3.2: Validate Data Quality

**File:** `scripts/validate_15m_data.py`

```python
#!/usr/bin/env python3
"""Validate 15m candle data for TimesFM training."""

import pandas as pd
from datetime import datetime

def validate_candles(filepath: str):
    df = pd.read_csv(filepath)
    
    print("=== 15m Candle Data Validation ===\n")
    
    # Check for gaps
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp')
    
    # Expected 15m intervals
    expected_delta = pd.Timedelta(minutes=15)
    actual_deltas = df['timestamp'].diff()[1:]
    
    gaps = actual_deltas[actual_deltas != expected_delta]
    
    print(f"Total candles: {len(df)}")
    print(f"Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
    print(f"Expected candles: {len(df['timestamp'].dt.floor('15min').unique())}")
    print(f"Gaps detected: {len(gaps)}")
    
    if len(gaps) > 0:
        print("\nGaps:")
        for i, (idx, delta) in enumerate(gaps.items()):
            if i < 5:  # Show first 5
                print(f"  {df['timestamp'][idx]}: {delta}")
    
    # Price statistics
    print(f"\nPrice range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    print(f"Mean close: ${df['close'].mean():.2f}")
    print(f"Std dev: ${df['close'].std():.2f}")
    
    # Volume statistics
    print(f"\nTotal volume: {df['volume'].sum():.2f BTC}")
    print(f"Mean volume per 15m: {df['volume'].mean():.4f BTC}")
    
    # Check for nulls
    print(f"\nNull values:")
    print(df.isnull().sum())

if __name__ == '__main__':
    validate_candles('timesfm-calibration/data/15m_candles.csv')
```

**Run:**
```bash
python scripts/validate_15m_data.py
```

---

### Day 4: Train 15m TimesFM Model

#### Task 4.1: Add 15m Forecast Service

**File:** `timesfm-service/app/main_15m.py`

```python
"""15m TimesFM forecast service (parallel to 5m service)."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import logging
from forecaster import TimesFMForecaster

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TimesFM 15m Forecast Service")

# Global forecaster instance
forecaster = None

class ForecastRequest(BaseModel):
    prices: list[float]
    horizon: int = 60

class ForecastResponse(BaseModel):
    direction: str
    confidence: float
    predicted_close: float
    spread: float
    p10: list[float]
    p50: list[float]
    p90: list[float]

@app.on_event("startup")
async def startup():
    global forecaster
    from forecaster import TimesFMForecaster
    
    logger.info("Loading 15m TimesFM model...")
    forecaster = TimesFMForecaster(
        max_context=2048,  # 2048 15m candles = 21 days
        max_horizon=300,   # 300 steps × 15m = 75 hours forecast
    )
    forecaster.load()
    logger.info("15m TimesFM model loaded")

@app.get("/health")
async def health():
    if forecaster is None or not forecaster.is_loaded:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {"status": "healthy", "model": "15m"}

@app.post("/forecast", response_model=ForecastResponse)
async def forecast(request: ForecastRequest):
    if forecaster is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    try:
        result = forecaster.forecast(
            prices=request.prices,
            horizon=request.horizon
        )
        
        return ForecastResponse(
            direction=result['direction'],
            confidence=result['confidence'],
            predicted_close=result['predicted_close'],
            spread=result['spread'],
            p10=result['quantiles']['p10'],
            p50=result['quantiles']['p50'],
            p90=result['quantiles']['p90']
        )
    except Exception as e:
        logger.error(f"Forecast failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8081)
```

#### Task 4.2: Deploy 15m Service

**File:** `timesfm-service/Dockerfile.15m`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN pip install torch numpy pandas fastapi uvicorn psycopg2-binary python-dotenv

COPY app/ ./app/

EXPOSE 8081

CMD ["python", "app/main_15m.py"]
```

**Build and run:**
```bash
cd timesfm-service
docker build -f Dockerfile.15m -t timesfm-15m .
docker run -d -p 8081:8081 --name timesfm-15m timesfm-15m
```

**Test:**
```bash
curl -X POST http://localhost:8081/forecast \
  -H "Content-Type: application/json" \
  -d '{"prices": [66931.96, 66930.0, 66928.5], "horizon": 60}'
```

---

### Day 5: Calibration Layer

#### Task 5.1: Build Calibration Dataset

**File:** `timesfm-service/calibrate_15m.py`

```python
#!/usr/bin/env python3
"""Calibrate 15m TimesFM predictions using historical data."""

import pandas as pd
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.calibration import CalibratedClassifierCV

def load_predictions():
    """Load TimesFM predictions with outcomes."""
    df = pd.read_csv('timesfm-calibration/data/predictions.csv')
    return df

def build_calibration_dataset(df: pd.DataFrame):
    """Build dataset for calibration.
    
    Features:
    - TimesFM raw confidence
    - TimesFM direction (encoded)
    - VPIN at prediction time
    - Regime
    
    Target:
    - Correct (0 or 1)
    """
    # Filter to BTC 5m (proxy for 15m until we have 15m predictions)
    btc_df = df[(df['asset'] == 'BTC') & (df['timeframe'] == '5m')]
    
    X = btc_df[['timesfm_confidence', 'vpin']].values
    y = (btc_df['correct'] == 't').astype(int).values
    
    # Encode direction
    direction_map = {'UP': 1, 'DOWN': 0}
    X_dir = btc_df['timesfm_direction'].map(direction_map).values
    
    X = np.column_stack([X, X_dir])
    
    return X, y

def calibrate(X: np.ndarray, y: np.ndarray):
    """Calibrate TimesFM confidence using Platt scaling."""
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # Logistic regression for calibration
    clf = LogisticRegression()
    clf.fit(X_scaled, y)
    
    return clf, scaler

def evaluate_calibration(clf, X: np.ndarray, y: np.ndarray):
    """Evaluate calibration quality."""
    probs = clf.predict_proba(X)[:, 1]
    predictions = (probs > 0.5).astype(int)
    
    accuracy = (predictions == y).mean()
    
    # Brier score (lower is better)
    brier = ((probs - y) ** 2).mean()
    
    # Calibration curve
    bins = np.linspace(0, 1, 10)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    
    bin_accuracies = []
    for i in range(len(bins) - 1):
        mask = (probs >= bins[i]) & (probs < bins[i+1])
        if mask.sum() > 0:
            bin_accuracies.append(y[mask].mean())
        else:
            bin_accuracies.append(np.nan)
    
    return {
        'accuracy': accuracy,
        'brier_score': brier,
        'bin_accuracies': bin_accuracies,
        'bin_centers': bin_centers
    }

def main():
    print("Loading predictions...")
    df = load_predictions()
    
    print("Building calibration dataset...")
    X, y = build_calibration_dataset(df)
    
    print(f"Dataset: {len(y)} samples")
    print(f"Positive class (correct): {y.sum()} ({y.mean()*100:.1f}%)")
    
    print("Calibrating...")
    clf, scaler = calibrate(X, y)
    
    print("Evaluating calibration...")
    metrics = evaluate_calibration(clf, X, y)
    
    print(f"\nCalibration Results:")
    print(f"  Accuracy: {metrics['accuracy']*100:.1f}%")
    print(f"  Brier Score: {metrics['brier_score']:.4f}")
    
    print(f"\nCalibration Curve:")
    for center, acc in zip(metrics['bin_centers'], metrics['bin_accuracies']):
        if not np.isnan(acc):
            print(f"  {center:.2f}: {acc*100:.1f}%")
    
    # Save model
    import joblib
    joblib.dump(clf, 'models/15m_calibrator.pkl')
    joblib.dump(scaler, 'models/15m_scaler.pkl')
    
    print(f"\nSaved calibrator to models/15m_calibrator.pkl")

if __name__ == '__main__':
    main()
```

**Run:**
```bash
python timesfm-service/calibrate_15m.py
```

---

## Phase 3: Shadow Mode (Day 6-12)

### Day 6: Deploy Shadow Mode

#### Task 6.1: Enable Shadow Mode on Montreal

```bash
# SSH to Montreal
ssh -i /root/.ssh/novakash-montreal.pem ubuntu@15.223.247.178

# Update env vars
cd /home/novakash/novakash/engine
cat > .env << EOF
# Existing env vars...

# v10.0 15m System
V10_15M_ENABLED=true
V10_5M_CONFIRMATION_FILTER=true
V10_TIMESFM_ASYMMETRIC=true
V10_SHADOW_MODE=true

# 15m-specific
FIFTEEN_MIN_VPIN_GATE=0.40
FIFTEEN_MIN_CASCADE_THRESHOLD=0.65
FIFTEEN_MIN_MAX_ENTRY_PRICE=0.80

# TimesFM 15m
TIMESFM_15M_ENABLED=true
TIMESFM_15M_URL=http://16.52.148.255:8081
EOF

# Restart engine
sudo systemctl restart novakash-engine

# Check logs
tail -f /home/novakash/engine.log | grep -E "v10|15m|shadow"
```

#### Task 6.2: Monitor Shadow Decisions

**File:** `scripts/monitor_shadow_mode.py`

```python
#!/usr/bin/env python3
"""Monitor shadow mode decisions in real-time."""

import psycopg2
import time
from datetime import datetime, timedelta

def fetch_shadow_decisions(minutes: int = 60):
    """Fetch shadow mode decisions from last N minutes."""
    conn = psycopg2.connect(
        host=os.environ['DATABASE_HOST'],
        user=os.environ['DATABASE_USER'],
        password=os.environ['DATABASE_PASSWORD'],
        dbname=os.environ['DATABASE_NAME']
    )
    
    query = """
        SELECT 
            window_ts,
            timeframe,
            direction,
            vpin,
            regime,
            timesfm_direction,
            timesfm_confidence,
            five_min_confirmation,
            skip_reason,
            created_at
        FROM signal_evaluations
        WHERE created_at >= NOW() - INTERVAL '%s minutes'
        AND v10_shadow_mode = true
        ORDER BY created_at DESC
    """
    
    df = pd.read_sql_query(query, conn, params=(minutes,))
    conn.close()
    
    return df

def print_shadow_summary(df: pd.DataFrame):
    print(f"\n=== Shadow Mode Summary (last hour) ===")
    print(f"Total evaluations: {len(df)}")
    
    if len(df) == 0:
        print("No shadow decisions yet...")
        return
    
    # By 15m/5m
    for tf in ['15m', '5m']:
        tf_df = df[df['timeframe'] == tf]
        trades = len(tf_df[tf_df['skip_reason'].isna()])
        skips = len(tf_df[tf_df['skip_reason'].notna()])
        print(f"\n{tf} windows: {trades} trades, {skips} skips")
    
    # By regime
    print("\nBy regime:")
    for regime in df['regime'].unique():
        regime_df = df[df['regime'] == regime]
        trades = len(regime_df[regime_df['skip_reason'].isna()])
        print(f"  {regime}: {trades} trades")

def main():
    while True:
        df = fetch_shadow_decisions(60)
        print_shadow_summary(df)
        time.sleep(300)  # Update every 5 min

if __name__ == '__main__':
    main()
```

**Run:**
```bash
python scripts/monitor_shadow_mode.py
```

---

### Day 7-12: Daily Shadow Monitoring

**Daily Checklist:**

```bash
# 1. Check shadow decisions
python scripts/monitor_shadow_mode.py

# 2. Query DB for summary
psql -h <railway-host> -U <user> -d <database> << EOF
-- 15m shadow decisions today
SELECT 
    DATE(created_at) as date,
    COUNT(*) as total_evals,
    SUM(CASE WHEN skip_reason IS NULL THEN 1 ELSE 0 END) as trades,
    AVG(CASE WHEN skip_reason IS NULL THEN 1 ELSE 0 END) * 100 as trade_rate
FROM signal_evaluations
WHERE DATE(created_at) = CURRENT_DATE
AND v10_shadow_mode = true
AND timeframe = '15m'
GROUP BY DATE(created_at);

-- TimesFM 15m accuracy (when we have resolved windows)
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN timesfm_direction = actual_direction THEN 1 ELSE 0 END) as correct,
    ROUND(AVG(CASE WHEN timesfm_direction = actual_direction THEN 1 ELSE 0 END)::numeric * 100, 1) as accuracy
FROM signal_evaluations se
JOIN window_snapshots ws ON se.window_ts = ws.window_ts
WHERE se.timeframe = '15m'
AND se.v10_shadow_mode = true
AND ws.resolved = true;
EOF
```

**Daily Metrics to Track:**

| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| 15m Evaluations/Day | 8-15 | <5 or >20 |
| 15m Trade Rate | 15-25% | <10% or >35% |
| TimesFM 15m Accuracy | >=80% | <70% |
| 5m Confirmation Pass Rate | >=85% | <70% |

---

## Phase 4: Live Deployment (Day 13+)

### Day 13: Go/No-Go Decision

**Go Criteria:**
- Shadow mode WR >= 93% over >= 30 trades
- OR shadow mode >= 7 days with WR >= 93%
- TimesFM 15m accuracy >= 80% (when resolved)
- No critical bugs in 5m confirmation filter

**No-Go Triggers:**
- Shadow mode WR < 90% over 30 trades
- Critical bug in 5m filter causing missed trades
- TimesFM 15m accuracy < 70%

**If GO:**
```bash
# SSH to Montreal
ssh -i /root/.ssh/novakash-montreal.pem ubuntu@15.223.247.178

# Disable shadow mode
cd /home/novakash/novakash/engine
sed -i 's/V10_SHADOW_MODE=true/V10_SHADOW_MODE=false/' .env

# Restart engine
sudo systemctl restart novakash-engine

# Monitor first trades
tail -f /home/novakash/engine.log | grep -E "v10|15m|TRADE"
```

**If NO-GO:**
- Investigate failures
- Adjust thresholds
- Extend shadow mode

---

## Rollback Plan

**If Live WR < 90% over 30 trades:**

```bash
# 1. Pause 15m trading
sed -i 's/V10_15M_ENABLED=true/V10_15M_ENABLED=false/' .env
sudo systemctl restart novakash-engine

# 2. Revert to v9.0 5m system
sed -i 's/V10_SHADOW_MODE=false/V10_SHADOW_MODE=true/' .env  # Back to shadow
sudo systemctl restart novakash-engine

# 3. Investigate
psql -h <railway-host> -U <user> -d <database> << EOF
-- Failed trades
SELECT * FROM trades
WHERE engine_version = 'v10.0'
AND resolved = true
AND win = false
ORDER BY created_at DESC
LIMIT 20;
EOF
```

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Win Rate (30 trades) | >= 93% | DB: `trades` table |
| Daily PnL | +$15-25 | DB: `trades` + `window_snapshots` |
| Trade Frequency | 8-12/day | DB: `signal_evaluations` |
| TimesFM 15m Accuracy | >= 85% | DB: `signal_evaluations` + `window_snapshots` |
| Max Drawdown | < $20 | Rolling PnL tracking |

---

## Appendix: Risk Mitigation

### 1. Sample Size Risk (N=93 historical)
**Mitigation:** 7-day shadow mode → expect 56-84 trades → N=150+ total

### 2. Regime Shift Risk (April 3-5 downtrend)
**Mitigation:** Asymmetric TimesFM calibration (DOWN vs UP separate)

### 3. Model Drift Risk
**Mitigation:** Weekly recalibration, monitor accuracy daily

### 4. Execution Risk (FAK fills at $0.80)
**Mitigation:** Monitor fill rate in shadow mode, adjust cap if < 80%

---

**Plan Approved By:** Billy Richards  
**Start Date:** 2026-04-10  
**Target Live Date:** 2026-04-22  
**Review Date:** 2026-04-17 (mid-phase shadow review)
