# Backward-compat shim — real implementation in adapters/prediction/timesfm_v2.py
from adapters.prediction.timesfm_v2 import *  # noqa: F401, F403
from adapters.prediction.timesfm_v2 import TimesFMV2Client  # explicit for IDEs
