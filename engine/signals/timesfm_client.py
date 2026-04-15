# Backward-compat shim — real implementation in adapters/prediction/timesfm_v1.py
from adapters.prediction.timesfm_v1 import *  # noqa: F401, F403
from adapters.prediction.timesfm_v1 import TimesFMClient  # explicit for IDEs
