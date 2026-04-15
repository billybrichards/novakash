"""conftest for adapter unit tests.

Adapter tests use `from engine.adapters...` imports (engine as a package),
so the worktree root (parent of engine/) must also be on sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Parent of engine/ — makes `import engine.adapters...` resolvable
WORKTREE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))
