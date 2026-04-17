"""
Strategies API — registry listing.

Closes audit task #216: `GET /api/strategies`.

Returns a whitelist of strategy IDs with their parsed YAML config + an empty
`runtime` dict reserved for future DB config overrides. Consumed by
`frontend/src/pages/Strategies.jsx` to render the per-timeframe param
comparison table. Without this endpoint the FE falls back to deriving
strategy IDs from `/api/v58/strategy-decisions` but has no YAML params
to display, leaving the "YAML params" table empty.

NOTE: This endpoint deliberately omits any secret-bearing fields. YAML
configs in `engine/strategies/configs/*.yaml` contain only strategy
parameters (gates, sizing, versions) — no API keys or wallet material.
Keep it that way. If secrets ever leak into YAML, add a redaction pass
here before shipping.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
import yaml
from fastapi import APIRouter

log = structlog.get_logger(__name__)
router = APIRouter()

# hub/api/strategies.py → hub/api → hub → project root → engine/strategies/configs
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "engine",
    "strategies",
    "configs",
)


def _normalise_timeframe(parsed: dict) -> str:
    """Map YAML `timescale` field to the FE `timeframe` grouping key.

    FE Strategies.jsx groups by TIMEFRAMES = ['5m', '15m', '1h']. The YAML
    field is `timescale` (existing convention; see v4_fusion.yaml). Accept
    either for forward-compat; fall back to '5m' if neither set (current
    v4_fusion surface only emits 5m anyway).
    """
    return parsed.get("timescale") or parsed.get("timeframe") or "5m"


def _load_strategy_yaml(path: str, strategy_id: str) -> dict | None:
    try:
        with open(path, "r") as f:
            parsed = yaml.safe_load(f) or {}
        if not isinstance(parsed, dict):
            log.warning(
                "strategies.yaml_not_mapping",
                strategy_id=strategy_id,
                type=type(parsed).__name__,
            )
            return None
        return parsed
    except Exception as exc:
        log.warning(
            "strategies.yaml_parse_error",
            strategy_id=strategy_id,
            path=path,
            error=str(exc),
        )
        return None


@router.get("/strategies")
async def list_strategies() -> dict[str, dict[str, Any]]:
    """
    Return a registry of all strategies keyed by strategy_id.

    Response shape (consumed by Strategies.jsx):
    ```
    {
      "v4_fusion": {
        "timeframe": "5m",
        "yaml": {...parsed YAML...},
        "runtime": {}
      },
      "v15m_fusion": { "timeframe": "15m", ... },
      ...
    }
    ```

    The bare-map shape (no `{strategies: {...}}` envelope) is intentional:
    `useApiLoader` only unwraps `rows/trades/decisions/items` arrays, so a
    bare map reaches the page without interference.

    `runtime` is always empty in this revision — reserved for DB-backed
    trading-config overrides (follow-up audit task; see
    `reference_config_layering.md` for the 3-layer resolution rules).
    """
    registry: dict[str, dict[str, Any]] = {}

    if not os.path.isdir(_CONFIG_DIR):
        log.warning("strategies.config_dir_missing", path=_CONFIG_DIR)
        return registry

    for fname in sorted(os.listdir(_CONFIG_DIR)):
        if not fname.endswith(".yaml"):
            continue
        strategy_id = fname[: -len(".yaml")]
        parsed = _load_strategy_yaml(os.path.join(_CONFIG_DIR, fname), strategy_id)
        if parsed is None:
            continue

        registry[strategy_id] = {
            "timeframe": _normalise_timeframe(parsed),
            "yaml": parsed,
            "runtime": {},
        }

    return registry
