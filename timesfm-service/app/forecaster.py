"""TimesFM 2.5 200M PyTorch model wrapper for BTC price forecasting."""

import logging
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Quantile levels we care about
QUANTILE_LEVELS = [0.1, 0.25, 0.5, 0.75, 0.9]


class TimesFMForecaster:
    """
    Wraps TimesFM 2.5 200M PyTorch model for zero-shot BTC price forecasting.

    The model is loaded once at startup and kept in memory for fast inference.
    """

    def __init__(
        self,
        model_id: str = "google/timesfm-2.5-200m-pytorch",
        max_context: int = 1024,
        max_horizon: int = 60,
        normalize_inputs: bool = True,
        use_continuous_quantile_head: bool = True,
    ):
        self.model_id = model_id
        self.max_context = max_context
        self.max_horizon = max_horizon
        self.normalize_inputs = normalize_inputs
        self.use_continuous_quantile_head = use_continuous_quantile_head
        self._model = None
        self._loaded = False
        self._load_time: Optional[float] = None

    def load(self) -> None:
        """Load the TimesFM model. Call once at startup."""
        if self._loaded:
            logger.warning("Model already loaded, skipping.")
            return

        logger.info(f"Loading TimesFM model: {self.model_id}")
        start = time.time()

        try:
            import timesfm

            self._model = timesfm.TimesFm(
                hparams=timesfm.TimesFmHparams(
                    backend="pytorch",
                    per_core_batch_size=32,
                    horizon_len=self.max_horizon,
                    num_layers=20,
                    use_positional_embedding=False,
                    context_len=self.max_context,
                ),
                checkpoint=timesfm.TimesFmCheckpoint(
                    huggingface_repo_id=self.model_id,
                ),
            )

            elapsed = time.time() - start
            self._loaded = True
            self._load_time = time.time()
            logger.info(f"TimesFM model loaded successfully in {elapsed:.1f}s")

        except Exception as e:
            logger.error(f"Failed to load TimesFM model: {e}", exc_info=True)
            raise RuntimeError(f"Model load failed: {e}") from e

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def forecast(
        self,
        prices: list[float],
        horizon: int = 60,
    ) -> dict:
        """
        Run TimesFM forecast on a price series.

        Args:
            prices: List of BTC prices (1-second ticks or similar)
            horizon: Number of steps to forecast ahead (default: 60)

        Returns:
            dict with point_forecast, quantiles, predicted_close,
            direction, confidence, spread, horizon, input_length, timestamp
        """
        if not self._loaded or self._model is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        if len(prices) < 2:
            raise ValueError("Need at least 2 price points to forecast.")

        # Clip to max context window
        context = prices[-self.max_context :]
        prices_np = np.array(context, dtype=np.float32)

        # TimesFM expects list of arrays (batch dimension)
        # frequency_input: 0 = high-frequency (sub-daily)
        try:
            point_forecast, quantile_forecasts = self._model.forecast(
                inputs=[prices_np],
                freq=[0],  # 0 = high frequency (e.g. per-second ticks)
            )
        except Exception as e:
            logger.error(f"Forecast failed: {e}", exc_info=True)
            raise RuntimeError(f"Forecast inference failed: {e}") from e

        # Extract first batch item, clip to requested horizon
        pf = point_forecast[0][:horizon].tolist()

        # quantile_forecasts shape: (batch, horizon, n_quantiles)
        qf = quantile_forecasts[0][:horizon]  # (horizon, n_quantiles)

        # TimesFM default quantile levels (check model output order)
        # Typically [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9] for CQH
        # We map the ones we need
        try:
            default_quantiles = self._model.quantile_levels  # type: ignore[attr-defined]
        except AttributeError:
            # Fallback: assume standard 9-quantile CQH output
            default_quantiles = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

        def _get_quantile(level: float) -> list[float]:
            """Extract a specific quantile level from quantile_forecasts."""
            if level in default_quantiles:
                idx = default_quantiles.index(level)
                return qf[:, idx].tolist()
            # Interpolate between nearest available quantiles
            arr = np.array(sorted(default_quantiles))
            below = arr[arr <= level]
            above = arr[arr >= level]
            if len(below) == 0:
                idx = 0
            elif len(above) == 0:
                idx = len(default_quantiles) - 1
            else:
                lo = below[-1]
                hi = above[0]
                if lo == hi:
                    idx = default_quantiles.index(lo)
                    return qf[:, idx].tolist()
                lo_idx = default_quantiles.index(lo)
                hi_idx = default_quantiles.index(hi)
                alpha = (level - lo) / (hi - lo)
                return (qf[:, lo_idx] * (1 - alpha) + qf[:, hi_idx] * alpha).tolist()
            return qf[:, idx].tolist()

        p10 = _get_quantile(0.1)
        p25 = _get_quantile(0.25)
        p50 = _get_quantile(0.5)
        p75 = _get_quantile(0.75)
        p90 = _get_quantile(0.9)

        # Derived metrics
        open_price = float(prices[0])  # window open (first price in full series)
        predicted_close = float(pf[-1]) if pf else float(prices[-1])
        spread = float(p90[-1]) - float(p10[-1]) if p10 and p90 else 0.0

        direction = "UP" if predicted_close >= open_price else "DOWN"

        # Confidence: inverse of relative spread (tighter = more confident)
        # spread / open_price gives a % uncertainty; map to 0-1 inverted
        if open_price > 0 and spread >= 0:
            relative_spread = spread / open_price
            # e.g. 0.5% spread → ~0.9 confidence, 2% → ~0.5
            confidence = float(max(0.0, min(1.0, 1.0 / (1.0 + relative_spread * 50))))
        else:
            confidence = 0.5

        return {
            "point_forecast": pf,
            "quantiles": {
                "p10": p10,
                "p25": p25,
                "p50": p50,
                "p75": p75,
                "p90": p90,
            },
            "predicted_close": predicted_close,
            "direction": direction,
            "confidence": confidence,
            "spread": spread,
            "horizon": horizon,
            "input_length": len(context),
            "timestamp": time.time(),
        }
