"""TickFormerProbReader — extracts TickFormer probability from a surface snapshot.

Maps strategy_id → model version (v18 or v20) and reads the appropriate
probability field from FullDataSurface. For each open trade, converts the
raw P(UP) into the side-correct p_for_held and p_against_held context.

Side convention (matches EXIT_MONITOR_DESIGN.md §1):
  - side='DN' trade: p_against = prob_up = probability_tickformer_v18 directly
                     p_for     = 1 - prob_up
  - side='UP' trade: p_against = 1 - prob_up
                     p_for     = prob_up

The detector uses raw P(UP) so it can do both computations. This reader
just returns the raw P(UP) per asset, consistent with the surface field.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

# Strategies that use the v20 model head.
# Extend this set as new v20 strategy ids are deployed.
_V20_STRATEGY_IDS: frozenset[str] = frozenset()

# Strategies explicitly using v18.
# Default fallback is v18 when strategy_id is not in _V20_STRATEGY_IDS.
_V18_STRATEGY_IDS: frozenset[str] = frozenset(
    {
        "tickformer_v18_t180",
        "tickformer_v17_sniper",
    }
)


def detect_tickformer_model(strategy_id: str) -> str:
    """Return 'v18' or 'v20' for the given strategy.

    Heuristic: if strategy_id contains 'v20', it's v20. Otherwise v18.
    """
    s = (strategy_id or "").lower()
    if "v20" in s or strategy_id in _V20_STRATEGY_IDS:
        return "v20"
    return "v18"


def read_tickformer_prob(surface: Any, model: str) -> Optional[float]:
    """Read raw P(UP) from a surface for the given model version.

    Returns None when the field is absent or not numeric.
    """
    if surface is None:
        return None

    field = (
        "probability_tickformer_v20"
        if model == "v20"
        else "probability_tickformer_v18"
    )
    v = getattr(surface, field, None)
    if v is None:
        # Fallback: v20 not yet wired on surface → try v18
        if model == "v20":
            v = getattr(surface, "probability_tickformer_v18", None)
            if v is not None:
                log.debug(
                    "tickformer_prob_reader.v20_fallback_to_v18",
                    field=field,
                )
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_prob_by_asset(
    surface_by_asset: dict[str, Any],
    open_trades: list,
) -> dict[str, float]:
    """Build {asset: P(UP)} dict for assets present in open_trades.

    When a TickFormer prob is None for an asset, that asset is omitted from
    the result dict — the use case skips trades without a signal.
    """
    # Determine which model to query per asset (use first trade's strategy_id)
    model_by_asset: dict[str, str] = {}
    for trade in open_trades:
        if trade.asset not in model_by_asset:
            model_by_asset[trade.asset] = detect_tickformer_model(trade.strategy_id)

    result: dict[str, float] = {}
    for asset, model in model_by_asset.items():
        surface = surface_by_asset.get(asset)
        prob = read_tickformer_prob(surface, model)
        if prob is not None:
            result[asset] = prob
        else:
            log.debug(
                "tickformer_prob_reader.no_prob",
                asset=asset,
                model=model,
            )
    return result
