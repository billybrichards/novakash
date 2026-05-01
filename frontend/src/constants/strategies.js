/**
 * Shared strategy constants — SINGLE SOURCE OF TRUTH for strategy metadata.
 *
 * All pages that display strategy names, colors, or config keys should import
 * from this file instead of maintaining their own local copies.
 *
 * When Strategy Engine v2 lands (CA-07), these will be populated from
 * GET /api/strategies at runtime. For now, they are static.
 */

export const STRATEGIES = {
  v6_sniper: {
    id: 'v6_sniper',
    label: 'V6 SNIPER',
    shortLabel: 'V6S',
    color: '#a855f7',
    colorDim: 'rgba(168,85,247,0.14)',
    direction: 'ANY',
    description:
      'Bidirectional ensemble sniper. Conviction buckets: agree_strong, pegged_path1, '
      + 'mid_conf_blocked, no_eval_blocked. Primary LIVE strategy (entry_cap 0.85).',
    configKey: 'V6_SNIPER_MODE',
    defaultMode: 'LIVE',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB + Path1 consensus · entry_cap 0.85',
    family: 'ensemble',
    thresholds: {},
    // UI hint: strategies exposed in per-window "current lineup" filter.
    inCurrentLineup: true,
  },
  v5_ensemble: {
    id: 'v5_ensemble',
    label: 'V5 ENSEMBLE',
    shortLabel: 'V5E',
    color: '#22d3ee',
    colorDim: 'rgba(34,211,238,0.14)',
    direction: 'ANY',
    description:
      'Ensemble baseline (5.1.0). poly_confidence blend. GHOST shadow for v6 benchmarking.',
    configKey: 'V5_ENSEMBLE_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Ensemble · entry_cap 0.72',
    thresholds: {},
    inCurrentLineup: true,
  },
  v5_fresh: {
    id: 'v5_fresh',
    label: 'V5 FRESH',
    shortLabel: 'V5F',
    color: '#f472b6',
    colorDim: 'rgba(244,114,182,0.14)',
    direction: 'ANY',
    description: 'v5_ensemble variant (5.3.0) — relaxed freshness. GHOST.',
    configKey: 'V5_FRESH_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Ensemble · fresh path1',
    thresholds: {},
    inCurrentLineup: true,
  },
  v4_down_only: {
    id: 'v4_down_only',
    label: 'V4 DOWN-ONLY',
    shortLabel: 'DN',
    color: '#10b981',
    colorDim: 'rgba(16,185,129,0.12)',
    direction: 'DOWN',
    description: 'DOWN-only filter on V4 fusion surface. 90.3% WR from 897K-sample analysis.',
    configKey: 'V4_DOWN_ONLY_MODE',
    defaultMode: 'LIVE',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'DOWN filter \u00b7 CLOB sizing \u00b7 T-90-150',
    thresholds: {
      minDist: 0.10,
      minOffset: 90,
      maxOffset: 150,
      clobSkip: 0.25,
    },
  },
  v4_up_basic: {
    id: 'v4_up_basic',
    label: 'V4 UP-BASIC',
    shortLabel: 'UP',
    color: '#3b82f6',
    colorDim: 'rgba(59,130,246,0.12)',
    direction: 'UP',
    description: 'Global UP strategy. dist>=0.10, T-60-180, all hours. Expected 70-80% WR.',
    configKey: 'V4_UP_BASIC_MODE',
    defaultMode: 'GHOST',
    deployed: false, // Not yet registered in engine runtime_config — ships with CA-07
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'UP filter \u00b7 dist\u22650.10 \u00b7 T-60-180',
    thresholds: {
      minDist: 0.10,
      minOffset: 60,
      maxOffset: 180,
    },
  },
  v4_up_asian: {
    id: 'v4_up_asian',
    label: 'V4 UP-ASIAN',
    shortLabel: 'ASIAN',
    color: '#f59e0b',
    colorDim: 'rgba(245,158,11,0.12)',
    direction: 'UP',
    description: 'Asian session UP strategy. dist 0.15-0.20, hours 23-02 UTC. SIG-06 proposes relaxing to 0.10.',
    configKey: 'V4_UP_ASIAN_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'UP filter \u00b7 Asian session \u00b7 dist 0.15-0.20 \u00b7 T-90-150',
    thresholds: {
      minDist: 0.15, // Live engine value. SIG-06 will relax to 0.10
      maxDist: 0.20,
      minOffset: 90,
      maxOffset: 150,
      asianHours: [23, 0, 1, 2],
    },
  },
  v4_fusion: {
    id: 'v4_fusion',
    label: 'V4 FUSION',
    shortLabel: 'V4F',
    color: '#06b6d4',
    colorDim: 'rgba(6,182,212,0.12)',
    direction: 'ANY',
    description: 'Multi-signal fusion with polymarket venue-aware evaluation.',
    configKey: 'V4_FUSION_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Full V4 surface (UP+DOWN)',
    thresholds: {},
    inCurrentLineup: true,
  },
  v10_gate: {
    id: 'v10_gate',
    label: 'V10 GATE',
    shortLabel: 'V10',
    color: '#a855f7',
    colorDim: 'rgba(168,85,247,0.12)',
    direction: 'ANY',
    description: 'V10.6 8-gate pipeline with DUNE confidence scoring.',
    configKey: 'V10_GATE_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: '8-gate pipeline + DUNE',
    thresholds: {},
  },
  v8_champion_lgb_only: {
    id: 'v8_champion_lgb_only',
    label: 'V8 CHAMPION LGB-ONLY',
    shortLabel: 'V8_LGB',
    color: '#fb923c',
    colorDim: 'rgba(251,146,60,0.14)',
    direction: 'ANY',
    description:
      'LGB-only fine-tune variant of v8_champion. Classifier intentionally '
      + 'disabled. Adds 20-min post-loss cooldown, blocks DOWN in TRANSITION '
      + 'vpin regime, relaxes fill_band LOW with explicit DOWN floor 0.15. '
      + 'GHOST until 7d shadow validates — see Hub note #222.',
    configKey: 'V8_CHAMPION_LGB_ONLY_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB-only · cooldown 20m · fill [0.00,0.82]',
    family: 'lgb_only',
    thresholds: {
      lgbDistMinDown: 0.10,
      lgbDistMinUp: 0.15,
      fillBandMin: 0.00,
      fillBandMax: 0.82,
      downMinFillPrice: 0.15,
      postLossCooldownMin: 20,
    },
    inCurrentLineup: true,
  },
  v9_ensemble: {
    id: 'v9_ensemble',
    label: 'V9 ENSEMBLE',
    shortLabel: 'V9E',
    color: '#ec4899',
    colorDim: 'rgba(236,72,153,0.14)',
    direction: 'ANY',
    description:
      'Reinforced-Agreement LGB + v2-classifier ensemble. Disagreement veto '
      + '(|pc-pl|>0.25), direction agreement, T-minus-aware blend weights, '
      + 'VHC reinforcement tier (2.5x Kelly, bypasses TRANSITION + UP dist). '
      + 'Falls back to v8_lgb_only when pc=None. See Hub notes #221, #222, #226.',
    configKey: 'V9_ENSEMBLE_MODE',
    defaultMode: 'LIVE',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB+pc ensemble · VHC reinforced · disagreement veto',
    family: 'ensemble',
    thresholds: {
      ensembleDisagreementThreshold: 0.25,
      vhcThreshold: 0.25,
      vhcKellyMultiplier: 2.5,
      lgbDistMinDown: 0.10,
      lgbDistMinUp: 0.15,
      fillBandMin: 0.00,
      fillBandMax: 0.82,
      downMinFillPrice: 0.15,
      postLossCooldownMin: 20,
    },
    inCurrentLineup: true,
  },
  v8_champion: {
    id: 'v8_champion',
    label: 'V8 CHAMPION',
    shortLabel: 'V8C',
    color: '#f97316',
    colorDim: 'rgba(249,115,22,0.14)',
    direction: 'ANY',
    description:
      'v8.0.0 ensemble champion — LGB + v2 classifier blend. Predecessor of v9 '
      + 'ensemble. GHOST shadow for v9 benchmarking.',
    configKey: 'V8_CHAMPION_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Ensemble · 4-tier conviction · entry_cap 0.85',
    family: 'ensemble',
    thresholds: {},
  },
  v9_lgb_only: {
    id: 'v9_lgb_only',
    label: 'V9 LGB-ONLY',
    shortLabel: 'V9_LGB',
    color: '#f43f5e',
    colorDim: 'rgba(244,63,94,0.14)',
    direction: 'ANY',
    description:
      'v9.1.0 LGB-only — classifier disabled, 3-tier T-minus-aware aggression '
      + '(early 50%, mid 55%, aggressive 70%). VHC bypasses TRANSITION + UP dist.',
    configKey: 'V9_LGB_ONLY_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB-only · 3-tier mark · VHC reinforced',
    family: 'lgb_only',
    thresholds: {
      lgbDistMinDown: 0.10,
      lgbDistMinUp: 0.15,
      vhcThreshold: 0.25,
    },
  },
  v10_lgb_only: {
    id: 'v10_lgb_only',
    label: 'V10 LGB-ONLY',
    shortLabel: 'V10_LGB',
    color: '#8b5cf6',
    colorDim: 'rgba(139,92,246,0.14)',
    direction: 'ANY',
    description:
      'v10.0.0 LGB — wider dist tiers (0.10–0.25) calibrated to v10 LGB '
      + 'distribution: 90.8% acc at dist≥0.25, 81.0% at dist≥0.10.',
    configKey: 'V10_LGB_ONLY_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB-only · v10 model · 4-tier dist',
    family: 'lgb_only',
    thresholds: {
      tier1Dist: 0.10,
      tier2Dist: 0.15,
      tier3Dist: 0.20,
      vhcDist: 0.25,
    },
  },
  v12_lgb_solo: {
    id: 'v12_lgb_solo',
    label: 'V12 LGB-SOLO',
    shortLabel: 'V12_SOLO',
    color: '#14b8a6',
    colorDim: 'rgba(20,184,166,0.14)',
    direction: 'ANY',
    description:
      'v12 LGB solo — single-model LGB on v12 feature set. Same dist tiers as '
      + 'v10_lgb_only (0.10/0.15/0.20/0.25) for like-for-like comparison.',
    configKey: 'V12_LGB_SOLO_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'LGB solo · v12 features · 4-tier dist',
    family: 'lgb_only',
    thresholds: {
      tier1Dist: 0.10,
      tier2Dist: 0.15,
      tier3Dist: 0.20,
      vhcDist: 0.25,
    },
  },
  v12_lgb_combo: {
    id: 'v12_lgb_combo',
    label: 'V12 LGB-COMBO',
    shortLabel: 'V12_CMB',
    color: '#0ea5e9',
    colorDim: 'rgba(14,165,233,0.14)',
    direction: 'ANY',
    description:
      'v12 LGB combo — combines v10 + v12 LGB outputs by averaged distance. '
      + 'Fires only on combo_dist tiers (≥0.10), reducing model-specific noise.',
    configKey: 'V12_LGB_COMBO_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Combo LGB · v10⊕v12 · combo_dist tiers',
    family: 'combo',
    thresholds: {
      tier1ComboDist: 0.10,
      tier2ComboDist: 0.15,
      tier3ComboDist: 0.20,
      vhcComboDist: 0.25,
    },
  },
  v8_v12_strong_agree: {
    id: 'v8_v12_strong_agree',
    label: 'V8⊕V12 STRONG-AGREE',
    shortLabel: 'V8V12',
    color: '#84cc16',
    colorDim: 'rgba(132,204,22,0.14)',
    direction: 'ANY',
    description:
      'Trades only when v8_champion and v12_lgb agree directionally with strong '
      + 'conviction. High-precision low-recall combo strategy.',
    configKey: 'V8_V12_STRONG_AGREE_MODE',
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: 'Combo · v8 ∧ v12 strong agreement',
    family: 'combo',
    thresholds: {},
  },
};

export const STRATEGY_LIST = Object.values(STRATEGIES);
export const STRATEGY_IDS = Object.keys(STRATEGIES);

// Current (post-2026-04-20) lineup — v6_sniper LIVE dominant,
// v5_ensemble / v4_fusion / v5_fresh GHOST shadow. Ordered so v6_sniper
// renders first in the per-window "current lineup" rows.
export const CURRENT_LINEUP_IDS = [
  'v6_sniper',
  'v5_ensemble',
  'v4_fusion',
  'v5_fresh',
  'v8_champion_lgb_only',
  'v9_ensemble',
];

// Legacy strategy ids — surfaced behind an "archive" toggle on Window
// History so users can still audit historical v4/v10 decisions without
// polluting the primary LIVE/GHOST comparison.
export const LEGACY_LINEUP_IDS = [
  'v4_down_only',
  'v4_up_basic',
  'v4_up_asian',
  'v10_gate',
];

// Currently LIVE strategy. Hot-flips happen via strategy_runtime_overrides
// (#350) but this is the static FE default — update on any FE-visible flip.
// Components that need the runtime-current id should still call
// /api/strategies; this is a reasonable fallback when that fetch hasn't
// landed yet.
export const LIVE_STRATEGY_ID = 'v9_ensemble';

// Strategies the /desk play-along HUD tracks side-by-side. LIVE first, then
// the most relevant GHOST shadow for comparison. Pre-v8 ids dropped — the
// hub returns nothing for them and they made the panel look broken.
export const DESK_TRACKED_STRATEGY_IDS = [
  'v9_ensemble',
  'v8_champion_lgb_only',
];

// Family-grouped lineup for /desk's StrategyDecisions panel — lets the page
// show the full BTC 5m field at a glance, grouped so the operator can compare
// like-for-like (ensemble vs LGB-only vs combo) without 12 unsorted rows.
// Order within each family puts the LIVE / most-trusted variant first.
export const DESK_TRACKED_FAMILIES = [
  {
    family: 'ensemble',
    label: 'Ensemble',
    description: 'LGB ⊕ classifier blend',
    ids: ['v9_ensemble', 'v8_champion', 'v6_sniper'],
  },
  {
    family: 'lgb_only',
    label: 'LGB-only',
    description: 'Classifier disabled — distance tiers',
    ids: ['v8_champion_lgb_only', 'v9_lgb_only', 'v10_lgb_only', 'v12_lgb_solo'],
  },
  {
    family: 'combo',
    label: 'Combo',
    description: 'Multi-model agreement',
    ids: ['v12_lgb_combo', 'v8_v12_strong_agree'],
  },
];

// Flat id list derived from the family layout — preferred over hand-edited
// arrays for places that just need "every strategy /desk shows".
export const DESK_ALL_STRATEGY_IDS = DESK_TRACKED_FAMILIES.flatMap(f => f.ids);

/** Look up strategy metadata by id. Falls back to a generated entry for unknown ids. */
export function getStrategyMeta(id, index) {
  if (STRATEGIES[id]) return STRATEGIES[id];
  const FALLBACK_COLORS = ['#a855f7', '#06b6d4', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6'];
  return {
    id,
    label: id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
    shortLabel: id.slice(0, 4).toUpperCase(),
    color: FALLBACK_COLORS[(index || 0) % FALLBACK_COLORS.length],
    colorDim: 'rgba(100,116,139,0.12)',
    direction: null,
    description: '',
    configKey: null,
    defaultMode: 'GHOST',
    timescale: '5m',
    asset: 'BTC',
    gateLabel: '',
    thresholds: {},
  };
}

// Gate library -- maps to engine/strategies/gates/
export const GATES = {
  timing: { label: 'Timing', icon: '\u23F1', description: 'Eval offset window check' },
  direction: { label: 'Direction', icon: '\u2195', description: 'UP/DOWN/ANY filter' },
  confidence: { label: 'Confidence', icon: '\uD83C\uDFAF', description: 'Probability distance from 0.5' },
  session_hours: { label: 'Session', icon: '\uD83C\uDF0F', description: 'Trading hours filter (UTC)' },
  clob_sizing: { label: 'CLOB Size', icon: '\uD83D\uDCCA', description: 'CLOB-based position sizing' },
  source_agreement: { label: 'Sources', icon: '\uD83E\uDD1D', description: 'Price source agreement' },
  delta_magnitude: { label: 'Delta', icon: '\uD83D\uDCC8', description: 'Minimum delta threshold' },
  taker_flow: { label: 'Taker', icon: '\uD83D\uDCB9', description: 'Taker buy/sell alignment' },
  cg_confirmation: { label: 'CoinGlass', icon: '\uD83D\uDD0D', description: 'OI + liquidation confirmation' },
  spread: { label: 'Spread', icon: '\u2194', description: 'CLOB spread check' },
  dynamic_cap: { label: 'Cap', icon: '\uD83C\uDF9A', description: 'Dynamic entry cap' },
  regime: { label: 'Regime', icon: '\uD83C\uDF21', description: 'HMM regime filter' },
  macro_direction: { label: 'Macro', icon: '\uD83E\uDDED', description: 'Macro bias alignment' },
  trade_advised: { label: 'Advised', icon: '\u2705', description: 'V4 trade_advised check' },
};

// Strategy -> gate pipeline mapping (from YAML configs in engine/strategies/configs/)
export const STRATEGY_GATES = {
  v4_down_only: ['timing', 'direction', 'confidence', 'trade_advised', 'clob_sizing'],
  v4_up_basic: ['timing', 'direction', 'confidence'],
  v4_up_asian: ['timing', 'direction', 'confidence', 'session_hours'],
  v4_fusion: [],  // Custom hook-based evaluation
  v10_gate: [
    'timing', 'source_agreement', 'delta_magnitude', 'taker_flow',
    'cg_confirmation', 'confidence', 'spread', 'dynamic_cap',
  ],
};

// Data surface source health categories
export const DATA_SOURCES = {
  binance_ws: { label: 'Binance WS', expectedHz: 3, staleAfterMs: 2000 },
  tiingo: { label: 'Tiingo', expectedHz: 0.5, staleAfterMs: 5000 },
  chainlink: { label: 'Chainlink', expectedHz: 0.2, staleAfterMs: 15000 },
  clob: { label: 'CLOB', expectedHz: 0.5, staleAfterMs: 5000 },
  coinglass: { label: 'CoinGlass', expectedHz: 0.1, staleAfterMs: 15000 },
  v4_snapshot: { label: 'V4 Snapshot', expectedHz: 0.5, staleAfterMs: 5000 },
  v3_composite: { label: 'V3 Multi-Horizon', expectedHz: 0.2, staleAfterMs: 10000 },
  vpin: { label: 'VPIN', expectedHz: 1, staleAfterMs: 3000 },
};

// Data source -> field mapping (for Data Health page)
export const DATA_SOURCE_FIELDS = {
  binance_ws: ['current_price', 'delta_binance', 'vpin'],
  tiingo: ['delta_tiingo'],
  chainlink: ['delta_chainlink'],
  clob: ['clob_up_bid', 'clob_up_ask', 'clob_down_bid', 'clob_down_ask', 'clob_implied_up'],
  coinglass: ['cg_oi_usd', 'cg_funding_rate', 'cg_taker_buy_vol', 'cg_taker_sell_vol', 'cg_liq_total'],
  v4_snapshot: ['v2_probability_up', 'poly_direction', 'poly_confidence', 'poly_trade_advised'],
  v3_composite: ['v3_5m_composite', 'v3_15m_composite', 'v3_1h_composite', 'v3_4h_composite'],
  vpin: ['vpin', 'regime'],
};
