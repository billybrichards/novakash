// /desk — exit window indicator math.
//
// Engine evaluates the v9 sell decision between T-48 and T-30 (#365).
// Inclusive on both ends — the engine's `exit_eval_start_offset: 48` /
// `exit_eval_end_offset: 30` in v9_ensemble.yaml.
//
// `useWindow` clamps `secondsRemaining` to >= 0 so we don't have to
// guard negatives here.

export const EXIT_WINDOW_OPEN_S = 48;
export const EXIT_WINDOW_CLOSE_S = 30;

export function inExitWindow(secondsRemaining) {
  if (secondsRemaining == null || !Number.isFinite(secondsRemaining)) return false;
  return secondsRemaining <= EXIT_WINDOW_OPEN_S
    && secondsRemaining >= EXIT_WINDOW_CLOSE_S;
}
