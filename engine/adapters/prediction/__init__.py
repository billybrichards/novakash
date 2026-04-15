"""Prediction adapters — HTTP clients for TimesFM forecast services.

Re-exports the concrete clients used throughout the engine:
- TimesFMClient (v1, raw forecast)
- TimesFMV2Client (v2.2, calibrated probability)
"""

from adapters.prediction.timesfm_v1 import TimesFMClient, TimesFMForecast
from adapters.prediction.timesfm_v2 import TimesFMV2Client

__all__ = ["TimesFMClient", "TimesFMForecast", "TimesFMV2Client"]
