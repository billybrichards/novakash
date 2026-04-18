# v5_ensemble Strategy

**Version:** 5.1.0 (Strategy Engine v2)
**Mode:** LIVE (real-money execution alongside v4_fusion)
**Direction:** ALL (direction determined by V4 surface)

## Overview

Fork of `v4_fusion` that consumes the audit #121 Path 1 ensemble:
TimesFM-classifier-head + LGB blended on the timesfm-repo side
(commits `d1f1965`..`f62e9d8` on `claude/audit-121-timesfm-finetune`).
Audit showed +4.38 to +7.54pp WR@gate lift across all 6 decision-Δ
points vs prod LGB (hub notes 155 + 158).

All 13 v4_fusion gates are inherited verbatim. Only two surgical
additions:

1. **Signal-source selection** — `V5_ENSEMBLE_SIGNAL_SOURCE` selects
   which p_up the strategy reads (`ensemble` / `lgb_only` /
   `path1_only`). Default `ensemble`.
2. **Two new gates** inserted after the existing `confidence` gate:
   - `ensemble_fallback_sanity` — skip when timesfm reports
     `ensemble_config.mode == "fallback_lgb_only"` (classifier head
     unavailable). Default ON.
   - `ensemble_disagreement` — skip when
     `|p_lgb - p_classifier| > V5_ENSEMBLE_DISAGREEMENT_THRESHOLD`.
     Default OFF (threshold = 0).

## Mode note (SHADOW vs GHOST)

The handoff spec calls for `mode: SHADOW`, but the engine registry
(`engine/strategies/registry.py`) only recognises `LIVE | GHOST |
DISABLED`. `GHOST` is the matching semantic: the strategy is
evaluated and persisted via `decision_repo`, but TRADE actions emit
`registry.ghost_decision` log lines instead of executing. Adding a
true `SHADOW` mode is out of scope here.

## Cross-repo contract

The `/v4/snapshot` per-timescale block now contains three new keys
(timesfm-repo `app/v4_snapshot_assembler.py` commit `f62e9d8`):

| key | type | meaning |
|---|---|---|
| `probability_lgb`        | float\|null | LGB-only calibrated p_up (null when ensemble disabled) |
| `probability_classifier` | float\|null | Path 1 classifier p_up (null when head unavailable) |
| `ensemble_config`        | dict\|null  | `{mode, weights, disagreement_magnitude, disagreement_detected}` |

These are read in `engine/strategies/data_surface.py` and exposed on
`FullDataSurface` as `probability_lgb`, `probability_classifier`,
`ensemble_config`. **Do NOT rename without a coordinated
timesfm-repo PR.**

## Engine-side env vars (Montreal `.env`)

| var | default | meaning |
|---|---|---|
| `V5_ENSEMBLE_SIGNAL_SOURCE`         | `ensemble` | Which p_up to trade on (`ensemble` / `lgb_only` / `path1_only`) |
| `V5_ENSEMBLE_DISAGREEMENT_THRESHOLD`| `0`        | If >0, skip when \|p_lgb − p_path1\| exceeds this |
| `V5_ENSEMBLE_SKIP_ON_FALLBACK`      | `true`     | Skip trades when ensemble fell back to LGB-only |

## Timesfm-side env vars (sister-repo Montreal `.env`)

| var | default | meaning |
|---|---|---|
| `TIMESFM_LORA_ADAPTER_URI`     | unset   | S3 URI of #121 fine-tune LoRA adapter |
| `TIMESFM_CLASSIFIER_HEAD_URI`  | unset   | S3 URI of Path 1 classifier head |
| `V5_ENSEMBLE_PATH1_ENABLED`    | `false` | Master switch for ensemble blending in v2_scorer |

## Two-sided kill switch behaviour

| timesfm `V5_ENSEMBLE_PATH1_ENABLED` | engine `V5_ENSEMBLE_SIGNAL_SOURCE` | Result |
|---|---|---|
| true | `ensemble` (default) | Strategy reads blended p_up — full Path 1 active |
| true | `lgb_only` | Strategy reads `probability_lgb` field — A-B against ensemble |
| true | `path1_only` | Strategy reads `probability_classifier` — research mode |
| false | `ensemble` | `probability_up` is LGB calibrated → strategy = v4_fusion behaviour |
| false | `lgb_only` | Same as above (fallback to `poly_confidence`) |
| false | `path1_only` | Strategy skips every window: `path1_only: classifier unavailable` |

## Staging plan (operator-driven, no auto-promote)

| phase | yaml mode | timesfm env | engine env | what trades |
|---|---|---|---|---|
| 0 — merged | GHOST | all off | all default | v4_fusion runs; v5_ensemble logs only |
| 1 — classifier live | GHOST | all set | all default | v2_scorer blends; v5 reads ensemble surface, still log-only |
| 2 — canary | LIVE (manual flip) | unchanged | size_fraction reduced (manual) | small portion of capital |
| 3 — full | LIVE | unchanged | default | 100% v5; v4_fusion held in GHOST for comparison |

Per `feedback_no_auto_promotion.md` — Billy flips modes manually.

## Rollback triggers

Manually flip yaml back to `GHOST` if:
- Live WR drops >2pp below v4_fusion over 100+ trades
- Classifier-head unavailability rate >1% (timesfm service issue)
- Disagreement rate >50% (distribution shift)
- Any runtime exception in the new hook path

## References

- Spec: `novakash-timesfm-repo:claude/audit-121-timesfm-finetune` →
  `docs/superpowers/specs/2026-04-18-v5-ensemble-strategy.md`
  (commit `c354bb8`)
- Hub note 159 — handoff
- Hub notes 155 + 158 — Path 1 audit results
- Sister-repo commits `d1f1965`..`f62e9d8`
