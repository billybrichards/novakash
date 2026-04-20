import React from 'react';
import { T } from '../../theme/tokens.js';

/**
 * Source-of-truth badge.
 *
 * Per spec #189 §3 the wallet page ranks data sources by trust:
 *   1. On-chain Polygon RPC (2-of-3 consensus)         → rank="onchain"
 *   2. data-api.polymarket.com                         → rank="data-api"
 *   3. Local DB enriched by a trusted primary          → rank="db-enriched"
 *   4. Local DB only (phantoms / stale possible)       → rank="db-only"
 *   5. Endpoint missing → dropout, renders as          → rank="missing"
 *
 * Colour is reliability-coded: green=trustworthy, cyan=good, amber=ok-caveat,
 * red=low-trust, grey=unknown. Every cell on the wallet page should carry one
 * so the operator can see at a glance which numbers to believe.
 */

export const SOT_LEVELS = {
  onchain: {
    label: 'on-chain',
    color: T.profit,
    tooltip: '2-of-3 Polygon RPC consensus (Alchemy / drpc / 1rpc). Source-of-truth.',
  },
  'data-api': {
    label: 'data-api',
    color: T.cyan,
    tooltip: 'data-api.polymarket.com — Polymarket-canonical positions / activity.',
  },
  'db-enriched': {
    label: 'db+',
    color: T.warn,
    tooltip: 'Local DB, enriched by a trusted primary. OK for names, stake context.',
  },
  'db-only': {
    label: 'db-only',
    color: T.loss,
    tooltip: 'Local DB only — may include phantom rows or stale redeemer state. Do NOT trust blindly.',
  },
  missing: {
    label: 'endpoint missing',
    color: T.label,
    tooltip: 'Required hub endpoint has not shipped. Values below are a degraded fallback.',
  },
};

/**
 * Accepted `rank` values: onchain | data-api | db-enriched | db-only | missing.
 * Unknown ranks render as a neutral grey badge.
 */
export default function SotBadge({ rank, title, children, compact = false }) {
  const level = SOT_LEVELS[rank] ?? { label: String(rank ?? '?'), color: T.label, tooltip: null };
  const pad = compact ? '1px 6px' : '2px 8px';
  const fontSize = compact ? 9 : 10;
  return (
    <span
      title={title ?? level.tooltip ?? undefined}
      style={{
        display: 'inline-block',
        fontSize,
        letterSpacing: '0.08em',
        color: level.color,
        border: `1px solid ${level.color}`,
        padding: pad,
        borderRadius: 2,
        textTransform: 'lowercase',
        fontFamily: T.font,
      }}
    >
      {children ?? level.label}
    </span>
  );
}
