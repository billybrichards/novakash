# v10_gate Strategy

**Version:** 10.6.0 (Strategy Engine v2 port)
**Mode:** GHOST
**Direction:** Determined by source agreement + v2 probability

## Overview

The V10 gate strategy uses an 8-gate pipeline derived from the
original V10GateStrategy. Each gate is now a reusable component
from the gate library.

## Gate Pipeline

1. **TimingGate** (5-300) -- wide window, V10 evaluates across full range
2. **SourceAgreementGate** (min=2) -- at least 2 price sources agree on direction
3. **DeltaMagnitudeGate** (0.0005) -- delta must be significant
4. **TakerFlowGate** -- CoinGlass taker buy/sell aligns with direction
5. **CGConfirmationGate** -- OI + liquidation data doesn't contradict
6. **ConfidenceGate** (min=0.12) -- V10 uses higher threshold than V4 strategies
7. **SpreadGate** (100bps) -- CLOB spread must be reasonable
8. **DynamicCapGate** (0.65) -- entry cap based on confidence

## Post-Gate Hook

`classify_confidence`: Classifies V2 probability as HIGH (max(p, 1-p) > 0.75)
or MODERATE, adjusting the sizing label.

## References

- `engine/signals/gates.py` (original 8-gate pipeline)
- `engine/adapters/strategies/v10_gate_strategy.py` (original adapter)
- Audit: SP-02
