"""VPINGate -- VPIN floor + optional CASCADE regime block.

Reads ``surface.vpin`` and ``surface.regime`` (VPIN regime, one of
CALM | NORMAL | TRANSITION | CASCADE). Passes when:
  * vpin >= min, AND
  * if block_cascade=True, regime != "CASCADE".

Introduced for v4_down_only v2.3.0 — prevents firing during the
CASCADE-inversion pattern seen 2026-04-17 (project_overnight_collapse_apr17).

Runtime overrides (via YAML ``gate_params:`` block or DB strategy_runtime_overrides):
  * ``vpin_gate_block_cascade`` (bool) — overrides constructor ``block_cascade``
  * ``vpin_gate_min`` (float) — overrides constructor ``min``

When no override is set, constructor values are used (backward compatible).
Useful for strategies where CASCADE×DOWN is the alpha cell (e.g.
v_v12_extreme_dn_btc_5m Hub note #347) — set ``vpin_gate_block_cascade: false``
in gate_params without redeploying code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strategies import gate_params as _gp
from strategies.gates.base import Gate, GateResult

if TYPE_CHECKING:
    from strategies.data_surface import FullDataSurface


class VPINGate(Gate):
    """VPIN floor gate with optional CASCADE block.

    Constructor params set the YAML-level defaults. At evaluate() time, the
    gate also checks the active ``gate_params`` context (set by the registry
    around each strategy evaluation) for runtime overrides:

      * ``vpin_gate_min`` (float)        — per-strategy VPIN floor override
      * ``vpin_gate_block_cascade`` (bool) — per-strategy CASCADE block override

    This enables operators to lift the CASCADE block on strategies where
    CASCADE×DOWN is profitable (e.g. v_v12_extreme_dn_btc_5m) without
    restarting the engine — just update strategy_runtime_overrides in the DB.
    """

    def __init__(self, min: float = 0.40, block_cascade: bool = True):
        self._min = float(min)
        self._block_cascade = bool(block_cascade)

    @property
    def name(self) -> str:
        return "vpin_gate"

    def evaluate(self, surface: "FullDataSurface") -> GateResult:
        # Resolve effective params: runtime override > constructor value.
        effective_min = _gp.get_float("vpin_gate_min", None, self._min)
        effective_block_cascade = _gp.get_bool(
            "vpin_gate_block_cascade", None, self._block_cascade
        )

        vpin = surface.vpin
        if vpin is None:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="vpin is None",
            )
        if vpin < effective_min:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"vpin={vpin:.3f} < {effective_min:.3f}",
                data={"vpin": vpin, "min": effective_min},
            )
        if effective_block_cascade:
            regime = surface.regime
            if regime is not None and str(regime).upper() == "CASCADE":
                return GateResult(
                    passed=False,
                    gate_name=self.name,
                    reason=f"vpin_regime=CASCADE blocked (vpin={vpin:.3f})",
                    data={"vpin": vpin, "vpin_regime": regime},
                )
        return GateResult(
            passed=True,
            gate_name=self.name,
            reason=f"vpin={vpin:.3f} >= {effective_min:.3f} (regime={surface.regime})",
            data={"vpin": vpin, "vpin_regime": surface.regime},
        )
