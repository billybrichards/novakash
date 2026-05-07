"""ChainlinkFreshnessGate -- skip when the on-chain Chainlink oracle is stale.

Audit #374 (2026-05-06).

The Polygon Chainlink Aggregator V3 contracts publish a new round every
~10-30s under normal conditions. When a round stops landing (RPC outage,
oracle node failure, network congestion), `delta_chainlink` keeps reading
the last cached price — strategies that gate on direction agreement will
silently trade against a frozen signal.

This gate reads `surface.delta_chainlink_age_seconds` (populated by
DataSurfaceManager from ChainlinkFeed.latest_updated_at[asset]) and SKIPs
when the staleness exceeds the configured ceiling.

Defaults: 60 seconds (heartbeat + ~30s grace). Configurable per-strategy via
YAML ``params: { max_age_seconds: 30 }`` for stricter enforcement.

Backward compatibility: when the surface field is None (older surface
revision or feed not yet populated), the gate PASSES with reason
``chainlink_age_unknown`` rather than failing closed — matches existing
behaviour where missing data short-circuits to feed-availability gates
(SourceAgreementGate, OracleDirectionGate) rather than to this freshness
check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class ChainlinkFreshnessGate(Gate):
    """Pass when the Chainlink delta source is fresher than `max_age_seconds`.

    Args:
        max_age_seconds: Skip when `delta_chainlink_age_seconds` exceeds this
            ceiling.

            Default 60 seconds: the Polygon BTC/USD Chainlink aggregator
            publishes a new round every ~27s under normal conditions, plus a
            0.5% deviation trigger. In quiet markets the on-chain ``updatedAt``
            can legitimately sit at 25-28s, and any RPC jitter pushes past the
            old 30s default on a *healthy* feed. 60s = 1 heartbeat + ~30s grace
            for RPC jitter and quiet-market scenarios.

            Strategies that want stricter freshness enforcement can override via
            ``params: { max_age_seconds: 30 }`` in their YAML gate definition,
            or via the ``chainlink_max_age_seconds`` runtime config key.

        skip_when_unknown: When True, also fail when the surface field is
            None (paranoid mode). Default False — pass-through so a missing
            field doesn't break upstream gates that already null-block.
    """

    def __init__(
        self,
        max_age_seconds: int = 60,
        skip_when_unknown: bool = False,
    ):
        self._max_age_seconds = int(max_age_seconds)
        self._skip_when_unknown = bool(skip_when_unknown)

    @property
    def name(self) -> str:
        return "chainlink_freshness"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        age = getattr(surface, "delta_chainlink_age_seconds", None)
        if age is None:
            if self._skip_when_unknown:
                return GateResult(
                    passed=False,
                    gate_name=self.name,
                    reason="chainlink_age_unknown (skip_when_unknown=True)",
                )
            return GateResult(
                passed=True,
                gate_name=self.name,
                reason="chainlink_age_unknown (no surface data, pass-through)",
            )
        age_int = int(age)
        if age_int > self._max_age_seconds:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=(
                    f"chainlink stale: {age_int}s > {self._max_age_seconds}s"
                ),
                data={
                    "age_seconds": age_int,
                    "max_age_seconds": self._max_age_seconds,
                },
            )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"chainlink fresh: {age_int}s <= {self._max_age_seconds}s",
            data={"age_seconds": age_int},
        )
