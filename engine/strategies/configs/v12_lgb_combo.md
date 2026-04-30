# v12_lgb_combo Strategy

## Purpose

Agreement-required combo of v9 LGB (prod v5, `probability_lgb`) and v12 LGB
(`probability_lgb_v12`). Trades only when both models agree on direction with
sufficient conviction.

## Design

1. **Both signals required**: If either `probability_lgb` or
   `probability_lgb_v12` is None, the strategy SKIPs with an explicit reason.
2. **Direction agreement**: Both models must predict the same direction
   (UP or DOWN). Disagreement -> SKIP.
3. **Conviction floor**: `min(dist_v9, dist_v12)` must exceed 0.10 (conservative
   — uses the *weaker* model's distance, not the average).
4. **Combo probability**: Simple average `(p_v9 + p_v12) / 2` is swapped onto
   the surface and delegated to the v9_ensemble gate stack.
5. **Confidence score**: `min(combo_dist * 2, 1.0)` — same mapping as v10.

## Sizing

Identical to v10_lgb_only: fraction=0.025, max_collateral=0.058, schedule
with max Kelly multiplier 2.5x at VHC tier (combo_dist >= 0.25).

## Tradeoffs

- **Conservative by design**: min(dist) floor means the weaker model gates
  entry. This filters out cases where one model is confident but the other
  is near 0.5 (random).
- **Latency**: v12 probability must be served alongside v9 on /v4/snapshot.
  Until the timesfm-repo PR ships, v12 will always be None and the strategy
  will SKIP on every tick.
- **No aggressive Kelly**: Max 2.5x matches v10. Future v12.1.0 may increase
  once shadow period validates combo calibration.

## Metadata fields

- `probability_lgb`: v9 LGB probability (prod v5)
- `probability_lgb_v12`: v12 LGB probability
- `p_combo`: averaged probability used for delegation
- `dist_v9`, `dist_v12`, `combo_dist`: distance metrics
- `direction_agree`: always True when TRADE (False -> SKIP)

## Activation

1. Deploy with `mode: GHOST` (default)
2. Verify `probability_lgb_v12` populated in decision metadata
3. Run 48h shadow comparison
4. Flip to LIVE via YAML or runtime override
