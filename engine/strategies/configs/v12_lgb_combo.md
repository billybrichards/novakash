# v12_lgb_combo Strategy

## Purpose

Combo of v9 LGB (prod v5, `probability_lgb`) and v12 LGB
(`probability_lgb_v12`) that supports two complementary trading modes:

* **Option A — agreement path**: trade the averaged probability when both
  models point the same way with sufficient conviction.
* **Option C — disagreement-contrarian path**: when the two models point
  opposite ways, trust v12's call at half kelly. Per hub note #299 + 3-day
  replay (Apr 28-30), v12 is right 51.5% of the time on directional
  disagreements with a +$398/3d net edge driven by asymmetry (v12-fix
  avg|pnl|=$12 vs v12-break $6.7). Half-stake bounds blast radius if the
  contrarian edge fades.

Hub note #299 has the full phased-rollout plan (PRs A → D); this strategy
ships PR #1 (combined A + C) so the v9-baseline regression risk is
identical to the original A-only proposal while preserving the
disagreement edge.

## Design

### Pre-checks (both modes)
1. **Both signals required**: if either `probability_lgb` or
   `probability_lgb_v12` is None, SKIP with explicit reason.
2. **Compute directions and distances**: `dir_v9`, `dir_v12`, `dist_v9`,
   `dist_v12`.

### Option A — agreement (both directions match)
3. **Conviction floor**: `min(dist_v9, dist_v12)` must exceed `combo_min_dist`
   (default 0.10). Uses the *weaker* model's distance, not the average.
4. **Combo probability**: `(p_v9 + p_v12) / 2` is swapped onto the surface
   and delegated to the v9_ensemble gate stack (LGB-only forced, classifier
   nulled — same delegation pattern as v10_lgb_only.py).
5. **Sizing**: full v9_ensemble / CLOBSizingGate kelly (max 2.5x at VHC).
6. **Confidence score**: `min(combo_dist * 2, 1.0)`.
7. `metadata.v12_contrarian_mode = False`.

### Option C — disagreement (v12-contrarian)
3. **Flag check**: `gate_params.v12_contrarian_enabled` (default `true`)
   must be on; otherwise SKIP with
   `v9_v12_direction_disagreement_contrarian_disabled` so operators can
   canary-disable Option C while keeping A.
4. **v12 conviction floor**: `dist_v12 >= v12_contrarian_min_dist`
   (default 0.10). Below floor → SKIP `disagreement_v12_weak`.
5. **Trade v12's call**: swap `probability_lgb = p_v12` onto the surface
   (so direction follows v12) and delegate to v9_ensemble.
6. **Half kelly**: after delegation, multiply the returned `collateral_pct`
   by `gate_params.v12_contrarian_kelly_modifier` (default 0.5).
7. **Confidence score**: `min(dist_v12 * 2, 1.0)`.
8. `metadata.v12_contrarian_mode = True`,
   `metadata.kelly_half_modifier = 0.5`,
   `metadata.collateral_pct_pre_halving = <pre-halve value>`.

## Sizing

Option A: identical to v10_lgb_only — `fraction=0.025`,
`max_collateral=0.058`, schedule with max kelly multiplier 2.5x at VHC tier
(`combo_dist >= 0.25`).

Option C: same schedule resolution path, but the final `collateral_pct` is
multiplied by 0.5. So an Option C trade with `dist_v12 = 0.20` (which
maps to schedule `modifier 2.0`) becomes effective 1.0x kelly post-halving.

## Tradeoffs

* **Option A is conservative**: `min(dist)` floor means the weaker model
  gates entry. This filters cases where one model is confident but the
  other is near 0.5 (random).
* **Option C is bounded**: half-stake limits damage if the contrarian edge
  fades. The `v12_contrarian_enabled` flag is an instant kill switch
  without code change.
* **Latency**: v12 probability must be served alongside v9 on
  `/v4/snapshot`. Until the timesfm-repo PR ships, v12 is None and the
  strategy SKIPs every tick (`probability_lgb_v12 unavailable`).
* **No aggressive Kelly in either mode**: PR #1 caps Option A at 2.5x
  (matches v10) and Option C at 0.5x of that. Future PR #2 (Option B)
  may raise the agreement-mode ceiling once shadow validates calibration.

## Metadata fields

* `probability_lgb` — v9 LGB probability (prod v5)
* `probability_lgb_v12` — v12 LGB probability
* `dir_v9`, `dir_v12` — derived directions (`UP` / `DOWN`)
* `dist_v9`, `dist_v12` — distance from 0.5
* `direction_agree` — True (Option A) / False (Option C path or
  pre-disagreement-skip)
* `v12_contrarian_mode` — True only when Option C trade fires
* Option A only: `p_combo`, `combo_dist`, `combo_min_dist`,
  `lgb_only_forced`, `v12_combo_model`
* Option C only: `kelly_half_modifier`, `collateral_pct_pre_halving`,
  optionally `v12_contrarian_min_dist` (on weak-v12 skips)

## Activation

1. Deploy with `mode: GHOST` (default).
2. Verify `probability_lgb_v12` populated in decision metadata.
3. Run 48-72h shadow comparison vs v9_lgb_only / v10_lgb_only.
4. Track WR on **agreement-mode** vs **disagreement-mode** trades
   separately. Option C must show WR >= 50% with positive avg PnL — if
   not, flip `v12_contrarian_enabled: false` to canary-disable.
5. Per-delta canary on first LIVE flip (start delta 240 — biggest replay
   edge + sample).
6. Flip to LIVE via YAML or runtime override.
