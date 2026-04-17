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

Deploy-path hardening (2026-04-17):
PR #251 shipped with a single `engine/strategies/configs` path anchored to
the repo root — which exists locally and in Docker-compose layouts but NOT
on the AWS-rsync hub deploy (see .github/workflows/deploy-hub.yml — only
the hub/ subdirectory is shipped). Post-deploy the endpoint returned an
empty registry. Fixed by:
  1. The deploy workflow now also rsyncs `engine/strategies/configs/` to
     `/home/ubuntu/hub/strategy_configs/` on the hub host.
  2. `_pick_config_dir` below checks multiple candidate paths in order,
     matching either the local repo layout (engine/strategies/configs) or
     the AWS-deploy layout (hub/strategy_configs). An optional env var
     `STRATEGY_CONFIGS_DIR` overrides both.
The twin bug in `hub/api/strategy_decisions.py::get_strategy_config`
(same hard-coded path) is patched via the same resolver.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
import yaml
from fastapi import APIRouter

log = structlog.get_logger(__name__)
router = APIRouter()


def _pick_config_dir() -> str:
    """Resolve strategy-configs directory with fallback order.

    Resolution order:
      1. ``STRATEGY_CONFIGS_DIR`` env var if set (explicit operator override).
      2. ``<hub_root>/strategy_configs/`` — the AWS-rsync target. This is
         where ``.github/workflows/deploy-hub.yml`` lands the engine YAML
         on the hub host so the hub can read it without needing
         ``engine/`` in its deploy tree.
      3. ``<repo_root>/engine/strategies/configs/`` — the local dev and
         docker-compose layout (hub/ and engine/ are siblings under the
         repo root).

    If none of the candidates exist on disk, returns the canonical AWS
    path (#2) so startup diagnostics point at the correct fix location
    rather than a stale repo-relative guess.
    """
    override = os.environ.get("STRATEGY_CONFIGS_DIR")
    if override:
        return override

    hub_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (
        os.path.join(hub_root, "strategy_configs"),
        os.path.join(os.path.dirname(hub_root), "engine", "strategies", "configs"),
    ):
        if os.path.isdir(candidate):
            return candidate

    return os.path.join(hub_root, "strategy_configs")


_CONFIG_DIR = _pick_config_dir()


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
