// /desk — conviction tier helpers.
//
// Conviction tier is a coarse bucketing of |p - 0.5| used to colour-code
// the signal-stack row and gate the operator's eye. Thresholds mirror
// engine/signals conventions (see hub note #218 §3 and #226).
//
// Buckets (exclusive on lower, inclusive on upper):
//   NONE       [0,     0.05)
//   LOW        [0.05,  0.12)
//   MEDIUM     [0.12,  0.20)
//   HIGH       [0.20,  0.25)
//   VERY_HIGH  [0.25,  0.50]
//
// `probUp` may be null (classifier warming up). Returned tier is 'NONE'
// for null input so the UI renders the cold-start state without gating
// the whole column behind a conditional.

export const CONVICTION_TIERS = ['NONE', 'LOW', 'MEDIUM', 'HIGH', 'VERY_HIGH'];

export function convictionTier(probUp) {
  if (probUp == null || Number.isNaN(probUp)) return 'NONE';
  const edge = Math.abs(probUp - 0.5);
  if (edge < 0.05) return 'NONE';
  if (edge < 0.12) return 'LOW';
  if (edge < 0.20) return 'MEDIUM';
  if (edge < 0.25) return 'HIGH';
  return 'VERY_HIGH';
}

// VHC = Very High Classifier — fires when the classifier's own prob is
// >= 0.25 away from 0.5. Per note #226 this is the 89–94% dir_acc band.
export function isClassifierHighConviction(probClassifier) {
  if (probClassifier == null || Number.isNaN(probClassifier)) return false;
  return Math.abs(probClassifier - 0.5) >= 0.25;
}

// Disagreement badge: classifier head vs LGB head. > 0.25 absolute is
// the threshold the note cites for "SOURCE CONFLICT".
export function probDisagreement(pc, pl) {
  if (pc == null || pl == null) return null;
  return Math.abs(pc - pl);
}

export function isSourceConflict(pc, pl) {
  const d = probDisagreement(pc, pl);
  if (d == null) return false;
  return d > 0.25;
}

// Direction from prob_up (for display only — engine makes the real call).
export function directionFromProbUp(probUp) {
  if (probUp == null || Number.isNaN(probUp)) return null;
  if (probUp > 0.5) return 'UP';
  if (probUp < 0.5) return 'DOWN';
  return null;
}
