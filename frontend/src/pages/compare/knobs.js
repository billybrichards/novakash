// Curated knob list for /compare page.
// Order = render order (top to bottom). Each row describes one differentiating
// knob pulled from a strategy's YAML config (as returned by /api/strategies).
//
// `path` is a dotted lookup path against the yaml object, e.g. "gate_params.min_offset_sec".
// `get` (optional) lets a knob synthesise a value (e.g. min-max timing pair).
// `desc` is the one-line "what it actually does" hint shown in the legend.
//
// Extend this list as new knobs become relevant. This file is the single source
// of truth for which knobs the operator cares about — the hub is the source of
// truth for the values themselves.

/** Dotted-path getter with `missing` sentinel so we can distinguish "not set" from "null". */
export const MISSING = Symbol('missing');

export function readPath(obj, path) {
  if (!obj || !path) return MISSING;
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object' || !(p in cur)) return MISSING;
    cur = cur[p];
  }
  return cur;
}

/** Human-readable rendering of any value. */
export function renderValue(v) {
  if (v === MISSING) return '—';
  if (v === null) return 'null';
  if (v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) {
    if (v.length === 0) return '[]';
    return `[${v.join(', ')}]`;
  }
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

/** Stable equality for diff highlighting. Uses JSON for objects/arrays. */
export function stableKey(v) {
  if (v === MISSING) return '__missing__';
  if (v === null) return '__null__';
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return typeof v + ':' + String(v);
}

/**
 * Compute the modal (most-common) value for a row across strategies.
 * Ties broken by first-seen order. Returns the stable-key of the mode, or null
 * if every strategy has a different value (no modal group).
 */
export function modalKey(values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const counts = new Map();
  const order = [];
  for (const v of values) {
    const k = stableKey(v);
    if (!counts.has(k)) order.push(k);
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  // Pick highest count; tie → first-seen.
  let best = null, bestN = 0;
  for (const k of order) {
    const n = counts.get(k);
    if (n > bestN) { best = k; bestN = n; }
  }
  // If every value distinct (bestN === 1 and more than 1 group), no modal.
  if (bestN <= 1 && counts.size > 1) return null;
  return best;
}

/**
 * Build a row's cell data: array of { value, key, isModal, isMissing } aligned
 * with the input strategyList order.
 */
export function buildRow(knob, strategies) {
  const cells = strategies.map(s => {
    const raw = knob.get ? knob.get(s.yaml || {}) : readPath(s.yaml || {}, knob.path);
    return { value: raw, key: stableKey(raw), isMissing: raw === MISSING };
  });
  const mode = modalKey(cells.map(c => c.value));
  return cells.map(c => ({
    ...c,
    isModal: mode != null && c.key === mode,
  }));
}

export const KNOBS = [
  // ── Identity / routing ─────────────────────────────────────────────────
  {
    id: 'mode',
    label: 'Mode',
    path: 'mode',
    desc: 'LIVE trades real money. GHOST is shadow-only.',
  },
  {
    id: 'version',
    label: 'Version',
    path: 'version',
    desc: 'YAML `version` field. Bumps on config changes.',
  },
  {
    id: 'timescale',
    label: 'Timescale',
    path: 'timescale',
    desc: 'Window length the strategy trades (5m / 15m / 1h).',
  },
  {
    id: 'hooks_file',
    label: 'Hook file',
    path: 'hooks_file',
    desc: 'Python module implementing the pre/post gate hooks.',
  },
  {
    id: 'pre_gate_hook',
    label: 'Pre-gate hook',
    path: 'pre_gate_hook',
    desc: 'Entry point — runs before declarative gates. Selects signal path.',
  },
  {
    id: 'post_gate_hook',
    label: 'Post-gate hook',
    path: 'post_gate_hook',
    desc: 'Runs after gates pass — typically conviction classification.',
  },

  // ── Timing ─────────────────────────────────────────────────────────────
  {
    id: 'min_offset_sec',
    label: 'min_offset_sec',
    path: 'gate_params.min_offset_sec',
    desc: 'Earliest T-seconds in window we will fire. Tiny = close to resolution.',
  },
  {
    id: 'max_offset_sec',
    label: 'max_offset_sec',
    path: 'gate_params.max_offset_sec',
    desc: 'Latest T-seconds. Often absent → uses global default.',
  },

  // ── Health / safety gates ──────────────────────────────────────────────
  {
    id: 'health_gate',
    label: 'health_gate',
    path: 'gate_params.health_gate',
    desc: 'Feed-health tier required to trade. degraded = strict, unsafe = lenient.',
  },
  {
    id: 'skip_calm',
    label: 'skip_calm',
    path: 'gate_params.skip_calm',
    desc: 'Refuse to trade when regime classifier says "calm".',
  },
  {
    id: 'skip_stale_sources',
    label: 'skip_stale_sources',
    path: 'gate_params.skip_stale_sources',
    desc: 'Refuse to trade if Chainlink/Tiingo ticks are stale.',
  },
  {
    id: 'require_tiingo_agree',
    label: 'require_tiingo_agree',
    path: 'gate_params.require_tiingo_agree',
    desc: 'Tiingo must confirm Chainlink direction before firing.',
  },

  // ── Regime / risk-off handling ─────────────────────────────────────────
  {
    id: 'tradeable_v4_regimes',
    label: 'tradeable_v4_regimes',
    path: 'gate_params.tradeable_v4_regimes',
    desc: 'Which v4 regime labels the strategy will act on.',
  },
  {
    id: 'risk_off_override_enabled',
    label: 'risk_off_override_enabled',
    path: 'gate_params.risk_off_override_enabled',
    desc: 'If true, allow entries in risk_off regime when dist-to-yes is extreme.',
  },
  {
    id: 'risk_off_override_dist_min',
    label: 'risk_off_override_dist_min',
    path: 'gate_params.risk_off_override_dist_min',
    desc: 'Min |dist| required to trigger the risk-off override path.',
  },
  {
    id: 'blocked_utc_hours',
    label: 'blocked_utc_hours / block_utc_hours',
    // v6 uses blocked_utc_hours, v5 uses block_utc_hours. Fall through.
    get: (y) => {
      const gp = y.gate_params || {};
      if ('blocked_utc_hours' in gp) return gp.blocked_utc_hours;
      if ('block_utc_hours' in gp) return gp.block_utc_hours;
      return MISSING;
    },
    desc: 'UTC hours where strategy refuses to trade (e.g. Asian illiquid window).',
  },

  // ── Ensemble / fallback ────────────────────────────────────────────────
  {
    id: 'ensemble_signal_source',
    label: 'ensemble_signal_source',
    path: 'gate_params.ensemble_signal_source',
    desc: 'Which signal surface (v4 / ensemble) drives the entry decision.',
  },
  {
    id: 'ensemble_skip_on_fallback',
    label: 'ensemble_skip_on_fallback',
    path: 'gate_params.ensemble_skip_on_fallback',
    desc: 'Skip when ensemble classifier unavailable (null) — no fallback to v4.',
  },
  {
    id: 'ensemble_disagreement_threshold',
    label: 'ensemble_disagreement_threshold',
    path: 'gate_params.ensemble_disagreement_threshold',
    desc: 'Max allowed |v4_prob - ensemble_prob|. 0.0 = disabled, 0.2 = strict.',
  },

  // ── v6 Sniper-specific bucketing ───────────────────────────────────────
  {
    id: 'bucket_abs_dist_strong',
    label: 'bucket_abs_dist_strong',
    path: 'gate_params.bucket_abs_dist_strong',
    desc: 'v6: |dist| above this classifies a strong bucket.',
  },
  {
    id: 'bucket_path1_extreme_high',
    label: 'bucket_path1_extreme_high',
    path: 'gate_params.bucket_path1_extreme_high',
    desc: 'v6: path1 prob at/above this → extreme-high bucket.',
  },
  {
    id: 'bucket_path1_extreme_low',
    label: 'bucket_path1_extreme_low',
    path: 'gate_params.bucket_path1_extreme_low',
    desc: 'v6: path1 prob at/below this → extreme-low bucket.',
  },
  {
    id: 'bucket_lgb_opposite_block',
    label: 'bucket_lgb_opposite_block',
    path: 'gate_params.bucket_lgb_opposite_block',
    desc: 'v6: LGB disagreement margin that forces a block.',
  },
  {
    id: 'bucket_block_mid_conf',
    label: 'bucket_block_mid_conf',
    path: 'gate_params.bucket_block_mid_conf',
    desc: 'v6: block mid-conviction buckets regardless of other gates.',
  },
  {
    id: 'path1_max_age_s',
    label: 'path1_max_age_s',
    path: 'gate_params.path1_max_age_s',
    desc: 'Max acceptable age of path1 signal. v6 enforces 30s freshness.',
  },
  {
    id: 'path1_skip_on_null',
    label: 'path1_skip_on_null',
    path: 'gate_params.path1_skip_on_null',
    desc: 'Skip trade if path1 signal is null (no forecast).',
  },
  {
    id: 'prefer_raw_probability',
    label: 'prefer_raw_probability',
    path: 'gate_params.prefer_raw_probability',
    desc: 'v6: use raw path1 prob instead of calibrated bucket mean.',
  },
  {
    id: 'entry_cap_override',
    label: 'entry_cap_override',
    path: 'gate_params.entry_cap_override',
    desc: 'Max Polymarket YES price we will pay. Overrides surface default.',
  },

  // ── VPIN / source agreement ────────────────────────────────────────────
  {
    id: 'vpin_min',
    label: 'vpin_min',
    path: 'gate_params.vpin_min',
    desc: 'Minimum VPIN informed-flow score to consider entry.',
  },
  {
    id: 'source_agreement_require_chainlink',
    label: 'src_agree.require_chainlink',
    path: 'gate_params.source_agreement_require_chainlink',
    desc: 'Require Chainlink tick direction to match trade.',
  },
  {
    id: 'source_agreement_require_tiingo',
    label: 'src_agree.require_tiingo',
    path: 'gate_params.source_agreement_require_tiingo',
    desc: 'Require Tiingo tick direction to match trade.',
  },

  // ── Sizing ─────────────────────────────────────────────────────────────
  {
    id: 'sizing_type',
    label: 'sizing.type',
    path: 'sizing.type',
    desc: 'fixed_kelly = standard. custom = hook-driven (v6 clob_sizing).',
  },
  {
    id: 'sizing_fraction',
    label: 'sizing.fraction',
    path: 'sizing.fraction',
    desc: 'Kelly fraction. 0.025 = 2.5% bankroll per trade.',
  },
  {
    id: 'sizing_max_collateral_pct',
    label: 'sizing.max_collateral_pct',
    path: 'sizing.max_collateral_pct',
    desc: 'Hard cap on open exposure per position as % of bankroll.',
  },
  {
    id: 'sizing_custom_hook',
    label: 'sizing.custom_hook',
    path: 'sizing.custom_hook',
    desc: 'When type=custom, Python hook computing position size.',
  },
  {
    id: 'sizing_schedule',
    label: 'sizing.schedule',
    path: 'sizing.schedule',
    desc: 'v6: conviction-tiered size modifiers (threshold → multiplier).',
  },
];
