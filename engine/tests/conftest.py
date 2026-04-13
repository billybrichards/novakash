"""Engine test conftest — make `engine/` importable for tests.

Engine modules use absolute imports (`from domain.ports import ...`,
`from use_cases.reconcile_positions import ...`) so the test process
needs `engine/` on sys.path.

Tests are intended to run via:
    cd engine && python -m pytest tests/
or
    pytest engine/tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))
