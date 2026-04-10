"""
CI smoke test — asserts MarginSettings rejects an unknown exchange_venue.

Paired with the happy-path check in check_settings_ok.py. Together they
validate both branches of the field_validator.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Same repo-root injection as check_settings_ok.py — see that file for rationale.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from margin_engine.infrastructure.config.settings import MarginSettings

    try:
        MarginSettings()
    except Exception as e:
        print(f"ok: rejected unknown exchange_venue with {type(e).__name__}: {e}")
        return 0
    print("FAIL: MarginSettings accepted a garbage exchange_venue", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
