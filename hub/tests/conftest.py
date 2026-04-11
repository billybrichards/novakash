"""
Hub test conftest — make `hub/` importable for tests run from the repo root.

Hub modules use absolute imports (`from db.database import ...`,
`from api.config_v2 import ...`) so the test process needs `hub/` on
sys.path. This conftest is the simplest way to do that without adding
a setup.py / pyproject to the hub package.

Tests are intended to run via:
    cd hub && python -m pytest tests/
or
    pytest hub/tests/

The conftest also stubs out optional runtime imports the hub modules
do at import time (structlog) so the test doesn't have to install the
full hub requirements.
"""

from __future__ import annotations

import sys
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
if str(HUB_ROOT) not in sys.path:
    sys.path.insert(0, str(HUB_ROOT))
