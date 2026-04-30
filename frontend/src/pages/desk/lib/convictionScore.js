// /desk — conviction-score tier mapping.
//
// `conviction_score` is a numeric 0-1 from /v4/snapshot. Distinct from
// the `convictionTier` lib in this folder, which maps the ensemble
// probability_up to a tier — that one is about *direction edge*, this
// one is about *engine sizing conviction* and is what tonight's audit
// #291 finding asked us to surface.
//
// Tier breaks are inclusive on the lower bound:
//   [0.00, 0.25) → NONE
//   [0.25, 0.50) → LOW
//   [0.50, 0.70) → MEDIUM
//   [0.70, 0.85) → HIGH
//   [0.85, 1.00] → VERY_HIGH

export const TIER_BREAKS = [
  { min: 0.0,  label: 'NONE'      },
  { min: 0.25, label: 'LOW'       },
  { min: 0.50, label: 'MEDIUM'    },
  { min: 0.70, label: 'HIGH'      },
  { min: 0.85, label: 'VERY HIGH' },
];

export function tierForScore(score) {
  if (score == null || !Number.isFinite(score)) return TIER_BREAKS[0];
  let pick = TIER_BREAKS[0];
  for (const t of TIER_BREAKS) {
    if (score >= t.min) pick = t;
  }
  return pick;
}
