/**
 * sot.jsx -- POLY-SOT chip helpers.
 *
 * The SOT reconciler in engine/reconciliation/reconciler.py stamps every
 * manual_trades row with one of:
 *   agrees | unreconciled | engine_optimistic | polymarket_only | diverged
 *
 * This module exposes a small `sotChipFor(state, notes?)` helper that
 * returns a colour-coded JSX chip + tooltip explaining the state. Used by
 * TradeTicker, ManualTradePanel, and any future SOT dashboard view so the
 * styling stays consistent.
 *
 * Spec source: docs/POLY_SOT.md (this PR — feat/poly-sot-reconciler).
 */

import React from 'react';

const SOT_COLORS = {
  agrees: { fg: '#10b981', bg: 'rgba(16,185,129,0.12)', border: 'rgba(16,185,129,0.35)', label: 'AGREES' },
  unreconciled: { fg: '#f59e0b', bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.35)', label: 'PENDING' },
  engine_optimistic: { fg: '#ef4444', bg: 'rgba(239,68,68,0.12)', border: 'rgba(239,68,68,0.5)', label: 'ENGINE OPTIMISTIC' },
  polymarket_only: { fg: '#ef4444', bg: 'rgba(239,68,68,0.12)', border: 'rgba(239,68,68,0.5)', label: 'POLY ONLY' },
  diverged: { fg: '#ef4444', bg: 'rgba(239,68,68,0.12)', border: 'rgba(239,68,68,0.5)', label: 'DIVERGED' },
  null: { fg: '#f59e0b', bg: 'rgba(245,158,11,0.08)', border: 'rgba(245,158,11,0.25)', label: 'UNCHECKED' },
};

/**
 * Map an SOT state string to its colour record.
 */
export function sotColors(state) {
  return SOT_COLORS[state] || SOT_COLORS.null;
}

/**
 * Render a small inline chip for a single SOT state.
 *
 * @param {string|null} state                       sot_reconciliation_state
 * @param {{notes?: string|null, compact?: boolean, onHoverDetails?: any}} opts
 */
export function sotChipFor(state, opts = {}) {
  const colors = sotColors(state);
  const compact = opts.compact ?? false;
  const tooltipParts = [`SOT: ${colors.label}`];
  if (opts.notes) tooltipParts.push(opts.notes);
  if (opts.engineFillPrice != null) {
    tooltipParts.push(`engine entry: $${Number(opts.engineFillPrice).toFixed(4)}`);
  }
  if (opts.polyFillPrice != null) {
    tooltipParts.push(`poly fill: $${Number(opts.polyFillPrice).toFixed(4)}`);
  }
  if (opts.polyConfirmedStatus) {
    tooltipParts.push(`poly status: ${opts.polyConfirmedStatus}`);
  }
  if (opts.lastVerifiedAt) {
    tooltipParts.push(`verified: ${opts.lastVerifiedAt}`);
  }
  const tooltip = tooltipParts.join(' | ');

  return (
    <span
      title={tooltip}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: compact ? '1px 6px' : '2px 8px',
        borderRadius: 999,
        fontSize: compact ? 9 : 10,
        fontFamily: "'JetBrains Mono', monospace",
        fontWeight: 700,
        letterSpacing: '0.04em',
        color: colors.fg,
        background: colors.bg,
        border: `1px solid ${colors.border}`,
        whiteSpace: 'nowrap',
        cursor: 'help',
      }}
    >
      <span
        style={{
          display: 'inline-block',
          width: compact ? 5 : 6,
          height: compact ? 5 : 6,
          borderRadius: '50%',
          background: colors.fg,
        }}
      />
      {colors.label}
    </span>
  );
}

/**
 * Build a lookup map of trade_id -> sot row from the
 * /api/v58/manual-trades-sot response.
 */
export function buildSotMap(rows) {
  const out = new Map();
  if (!Array.isArray(rows)) return out;
  for (const r of rows) {
    if (r && r.trade_id) out.set(r.trade_id, r);
  }
  return out;
}
