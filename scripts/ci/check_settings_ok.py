"""
CI smoke test — asserts MarginSettings loads with exchange_venue=hyperliquid.

Invoked by .github/workflows/ci.yml. Reads MARGIN_* env vars set by the
workflow step (all hardcoded literals, no untrusted input).

Runs as a first-class script so the workflow YAML stays free of multi-line
heredocs and the script is locally runnable for debugging.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable regardless of CWD. scripts/ci/ sits two
# levels below the root; walking up twice gives us /repo-root.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from margin_engine.infrastructure.config.settings import MarginSettings

    settings = MarginSettings()
    if settings.exchange_venue != "hyperliquid":
        print(
            f"FAIL: expected exchange_venue='hyperliquid', got {settings.exchange_venue!r}",
            file=sys.stderr,
        )
        return 1
    if settings.paper_mode is not True:
        print(
            f"FAIL: expected paper_mode=True, got {settings.paper_mode!r}",
            file=sys.stderr,
        )
        return 1
    print(
        f"ok: settings load with exchange_venue={settings.exchange_venue!r}, "
        f"paper_mode={settings.paper_mode!r}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
