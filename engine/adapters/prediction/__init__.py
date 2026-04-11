"""Prediction adapters -- thin wrappers around TimesFM forecast clients.

These adapters wrap the existing ``TimesFMClient`` (v1) and
``TimesFMV2Client`` (v2.2) behind a uniform interface.  No formal
``PredictionPort`` exists in ``engine/domain/ports.py`` yet -- these
adapters are structural shims that will implement the port once it is
defined in a future phase when the EvaluateWindowUseCase needs to call
the forecaster through a dependency-inverted boundary.

For now, the adapters provide:
- Constructor injection (accept the concrete client)
- structlog-based observability
- A consistent interface that can be extended to a port later
"""

from engine.adapters.prediction.timesfm_v1 import TimesFMV1Adapter
from engine.adapters.prediction.timesfm_v2 import TimesFMV2Adapter

__all__ = ["TimesFMV1Adapter", "TimesFMV2Adapter"]
