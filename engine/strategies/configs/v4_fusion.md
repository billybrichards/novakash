# v4_fusion Strategy

**Version:** 4.1.0 (Strategy Engine v2 port)
**Mode:** GHOST
**Direction:** ALL (direction determined by V4 surface)

## Overview

V4 fusion is the original parent strategy. It has three evaluation paths:

1. **polymarket_v2** (preferred): Clean venue-specific recommendation from timesfm-repo.
   Uses poly_direction, trade_advised, confidence_distance, timing, max_entry_price.
   Complex late_window CLOB divergence logic (>= 4pp).

2. **polymarket legacy**: For old timesfm builds that emit venue="polymarket" in extras.
   Uses recommended_side + confidence_distance.

3. **legacy margin-engine**: For non-Polymarket templates.
   5-gate pipeline: regime, consensus, conviction threshold, direction, macro.

## Why Custom Hook

The polymarket_v2 path has timing logic (early/optimal/late_window/expired) and
CLOB divergence checks that don't reduce to simple gate configs. The late_window
path requires computing `abs(confidence - 0.5) - abs(clob_implied - 0.5)` and
checking >= 0.04, which is too specific for a reusable gate.

## References

- `engine/adapters/strategies/v4_fusion_strategy.py` (original)
- Audit: SP-03
