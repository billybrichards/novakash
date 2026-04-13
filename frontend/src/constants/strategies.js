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
};

export const STRATEGY_LIST = Object.values(STRATEGIES);
export const STRATEGY_IDS = Object.keys(STRATEGIES);

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
