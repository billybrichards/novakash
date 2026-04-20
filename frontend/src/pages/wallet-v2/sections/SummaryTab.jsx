import React from 'react';
import { T } from '../../../theme/tokens.js';
import SotBadge from '../sotBadge.jsx';
import EmptyState from '../../../components/shared/EmptyState.jsx';
import { fmtUsd, fmtDuration } from '../reconcile.js';

/**
 * Summary tab content (spec #189 §5.2). Shows the four-column balance card
 * (rendered by the parent) plus a health grid for redeemer + open
 * positions.
 */
export default function SummaryTab({ balance, rank, missing }) {
  if (!balance) {
    return (
      <EmptyState
        message="Balance data unavailable."
        hint={missing
          ? `Hub endpoints missing: ${missing.join(', ')}. Fallback also empty.`
          : 'Retry the page — no snapshot returned from hub.'}
      />
    );
  }
  const health = balance.redeemer_health || {};
  const sweepAge = health.last_sweep_utc
    ? Math.floor((Date.now() - new Date(health.last_sweep_utc).getTime()) / 1000)
    : null;

  return (
    <div>
      <div style={panelStyle}>
        <SectionHeader title="Redeemer health" badge={<SotBadge rank={rank} compact />} />
        {rank === 'db-only' ? (
          <div style={{ fontSize: 11, color: T.label2, marginBottom: 10 }}>
            All values from DB tables populated by the engine — if the scanner
            is behind (audit-task #252), these numbers lie by definition. See
            banner above.
          </div>
        ) : null}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
          gap: 12,
        }}>
          <MiniStat
            lbl="LAST SWEEP"
            val={sweepAge != null ? `${fmtDuration(sweepAge)} ago` : '—'}
            tone={sweepAge != null && sweepAge > 3600 ? 'warn' : null}
          />
          <MiniStat
            lbl="SWEEPS / HR"
            val={health.sweeps_last_hour ?? '—'}
          />
          <MiniStat
            lbl="WINS MISSED 24H"
            val={health.wins_missed_24h ?? (rank === 'db-only' ? '?' : '0')}
            tone={Number(health.wins_missed_24h) > 0 ? 'bad' : 'good'}
          />
          <MiniStat
            lbl="QUOTA USED"
            val={
              health.quota_used_today != null && health.quota_limit != null
                ? `${health.quota_used_today} / ${health.quota_limit}`
                : '—'
            }
          />
        </div>
        {health.cooldown ? (
          <div style={{ marginTop: 10, fontSize: 11, color: T.warn }}>
            Cooldown active: {String(health.cooldown.reason || 'unknown reason')}
          </div>
        ) : null}
      </div>

      <div style={panelStyle}>
        <SectionHeader title="Open bets (mark)" badge={<SotBadge rank={rank} compact />} />
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
          gap: 12,
        }}>
          <MiniStat lbl="COUNT" val={balance.open_count ?? '—'} />
          <MiniStat lbl="MARK VALUE" val={fmtUsd(balance.open_mark_value)} />
          <MiniStat
            lbl="NEXT RESOLVE"
            val={balance.next_resolve_utc
              ? new Date(balance.next_resolve_utc).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
              : '—'}
          />
        </div>
      </div>

      <div style={{ fontSize: 11, color: T.label, fontFamily: T.font }}>
        Spec: hub note #189. Memory: <code style={codeStyle}>reference_wallet_truth.md</code>,
        {' '}<code style={codeStyle}>reference_clob_audit.md</code>. Source-of-truth
        for "is this wallet right?" is still <code style={codeStyle}>scripts/ops/wallet_truth.py</code>
        until audit-task #217 ships <code style={codeStyle}>/api/wallet/snapshot</code>.
      </div>
    </div>
  );
}

function SectionHeader({ title, badge }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      marginBottom: 10,
    }}>
      <div style={{ fontSize: 13, color: T.text }}>{title}</div>
      {badge}
    </div>
  );
}

function MiniStat({ lbl, val, tone }) {
  const color = tone === 'good' ? T.profit : tone === 'warn' ? T.warn : tone === 'bad' ? T.loss : T.text;
  return (
    <div>
      <div style={{
        fontSize: 10, letterSpacing: '0.12em',
        color: T.label, textTransform: 'uppercase',
      }}>{lbl}</div>
      <div style={{ fontSize: 16, marginTop: 4, color, fontVariantNumeric: 'tabular-nums' }}>{val}</div>
    </div>
  );
}

const panelStyle = {
  background: T.card,
  border: `1px solid ${T.border}`,
  padding: 14,
  borderRadius: 2,
  marginBottom: 14,
};

const codeStyle = {
  background: T.bg,
  border: `1px solid ${T.border}`,
  padding: '1px 5px',
  borderRadius: 2,
  fontSize: 10.5,
  color: T.label2,
};
