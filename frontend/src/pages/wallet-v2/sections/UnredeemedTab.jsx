import React, { useState, useCallback, useMemo } from 'react';
import { T } from '../../../theme/tokens.js';
import DataTable from '../../../components/shared/DataTable.jsx';
import SotBadge from '../sotBadge.jsx';
import { fmtUsd, fmtDuration, summarizeUnredeemed } from '../reconcile.js';

const SAFE_ID = /^[\w-]{1,80}$/;

/**
 * "Unredeemed wins" tab (spec #189 §5.3).
 *
 * - Default filter: only `redeemable=true`.
 * - Toggle: "show in-progress" lifts the filter (adds NegRisk-lag rows).
 * - No Redeem button (§7.4, SIWE gated). Per-row copy-CLI command string
 *   + polygonscan link for operator to run manually on Montreal.
 */
export default function UnredeemedTab({ rows, rank, legend, missing }) {
  const [showInProgress, setShowInProgress] = useState(false);
  const [copied, setCopied] = useState(null);

  const filtered = useMemo(() => {
    if (!Array.isArray(rows)) return [];
    return showInProgress ? rows : rows.filter(r => r.redeemable === true);
  }, [rows, showInProgress]);

  const stats = useMemo(() => summarizeUnredeemed(filtered), [filtered]);

  const copyCli = useCallback((row) => {
    const id = row.condition_id;
    if (!id || !SAFE_ID.test(String(id))) {
      setCopied({ key: row.key, ok: false });
      setTimeout(() => setCopied(null), 1500);
      return;
    }
    const cmd = `python scripts/ops/manual_redeem.py --condition-id=${id}`;
    if (!navigator.clipboard?.writeText) {
      setCopied({ key: row.key, ok: false });
      setTimeout(() => setCopied(null), 1500);
      return;
    }
    navigator.clipboard.writeText(cmd)
      .then(() => { setCopied({ key: row.key, ok: true }); setTimeout(() => setCopied(null), 1500); })
      .catch(() => { setCopied({ key: row.key, ok: false }); setTimeout(() => setCopied(null), 1500); });
  }, []);

  return (
    <div>
      <div style={panelStyle}>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          marginBottom: 10, flexWrap: 'wrap', gap: 8,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ fontSize: 13 }}>Unredeemed wins</div>
            <SotBadge rank={rank} compact />
          </div>
          <div style={{ fontSize: 11, color: T.label2 }}>
            {stats.redeemable} redeemable · {fmtUsd(stats.value)} total
            {stats.overdue5m > 0 ? (
              <span style={{ color: T.warn, marginLeft: 8 }}>
                · {stats.overdue5m} overdue &gt;5m
              </span>
            ) : null}
            {stats.overdue1h > 0 ? (
              <span style={{ color: T.loss, marginLeft: 8 }}>
                · {stats.overdue1h} overdue &gt;1h
              </span>
            ) : null}
          </div>
        </div>

        <div style={{
          display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12,
          fontSize: 11, color: T.label2,
        }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showInProgress}
              onChange={(e) => setShowInProgress(e.target.checked)}
            />
            Show in-progress (NegRisk lag)
          </label>
          {missing?.length ? (
            <span style={{ color: T.label }}>
              Fallback in use: {missing.join(', ')} missing.
            </span>
          ) : null}
        </div>

        <DataTable
          emptyText={rank === 'db-only'
            ? 'No pending wins in local DB. Run wallet_truth.py on Montreal to cross-check.'
            : 'Wallet clean. Nothing to redeem.'}
          columns={[
            {
              key: 'title', label: 'market', render: r => (
                <div>
                  <div style={{ color: T.text, fontSize: 11 }}>{r.title}</div>
                  {r.condition_id ? (
                    <a
                      href={`https://polygonscan.com/address/${r.condition_id}`}
                      target="_blank"
                      rel="noreferrer"
                      title="open condition on polygonscan"
                      style={{ color: T.label, fontSize: 9.5, textDecoration: 'none' }}
                    >{short(r.condition_id)}</a>
                  ) : null}
                </div>
              ),
            },
            { key: 'side', label: 'side', render: r => <span style={{
              color: r.side === 'Up' || r.side === 'YES' ? T.cyan
                : r.side === 'Down' || r.side === 'NO' ? T.purple
                : T.label2,
              fontSize: 10,
            }}>{r.side}</span> },
            { key: 'size', label: 'size', num: true, render: r => fmtSize(r.size) },
            { key: 'cost_usd', label: 'cost', num: true, render: r => <span style={{ color: T.label2 }}>{fmtUsd(r.cost_usd)}</span> },
            { key: 'current_value_usd', label: 'payout', num: true, render: r => <span style={{ color: T.profit, fontWeight: 500 }}>{fmtUsd(r.current_value_usd)}</span> },
            { key: 'overdue_seconds', label: 'age', render: r => <OverdueCell secs={r.overdue_seconds} /> },
            { key: 'stuck_reason', label: 'status', render: r => <StuckCell row={r} legend={legend} /> },
            { key: 'strategy', label: 'strategy', render: r => <span style={{ fontSize: 10, color: T.label2 }}>{r.strategy || '—'}</span> },
            { key: '_copy', label: '', render: r => {
              const disabled = !r.condition_id;
              const isCopied = copied && copied.key === r.key;
              const label = isCopied ? (copied.ok ? 'copied ✓' : 'copy failed') : 'copy cli';
              return (
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => copyCli(r)}
                  aria-label={`copy redeem command for ${r.condition_id || r.title}`}
                  title={disabled
                    ? 'condition_id missing — cannot build CLI command'
                    : 'Copy: python scripts/ops/manual_redeem.py --condition-id=<id>'}
                  style={{
                    ...btnStyle,
                    color: isCopied ? (copied.ok ? T.profit : T.loss) : (disabled ? T.label : T.text),
                    cursor: disabled ? 'not-allowed' : 'pointer',
                    opacity: disabled ? 0.5 : 1,
                  }}
                >{label}</button>
              );
            }},
          ]}
          rows={filtered.map(r => ({ ...r, _key: r.key }))}
        />
      </div>

      <div style={{ fontSize: 11, color: T.label, fontFamily: T.font }}>
        Per spec #189 §7.4: no in-browser redeem. Run the CLI on Montreal:
        {' '}<code style={codeStyle}>python scripts/ops/manual_redeem.py --condition-id=&lt;id&gt;</code>.
        SIWE gating tracked by audit-task #214.
      </div>
    </div>
  );
}

function OverdueCell({ secs }) {
  if (secs == null) return <span style={{ color: T.label }}>—</span>;
  const color = secs >= 3600 ? T.loss : secs >= 300 ? T.warn : T.label2;
  return <span style={{ color, fontSize: 11, fontVariantNumeric: 'tabular-nums' }}>{fmtDuration(secs)}</span>;
}

function StuckCell({ row, legend }) {
  if (row.stuck_reason) {
    const tip = legend && legend[row.stuck_reason];
    return (
      <span
        style={{ fontSize: 10, color: T.warn, letterSpacing: '0.04em' }}
        title={tip || row.stuck_reason}
      >{row.stuck_reason.replace(/_/g, ' ')}</span>
    );
  }
  if (row.redeemable) {
    return <span style={{ fontSize: 10, color: T.profit }}>redeemable</span>;
  }
  return <span style={{ fontSize: 10, color: T.label }}>in progress</span>;
}

function short(id) {
  if (!id) return '';
  const s = String(id);
  if (s.length <= 16) return s;
  return `${s.slice(0, 8)}…${s.slice(-6)}`;
}

function fmtSize(n) {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(2);
}

const panelStyle = {
  background: T.card,
  border: `1px solid ${T.border}`,
  padding: 14,
  borderRadius: 2,
  marginBottom: 14,
};

const btnStyle = {
  fontSize: 10,
  padding: '2px 8px',
  background: 'transparent',
  border: `1px solid ${T.borderStrong}`,
  color: T.text,
  fontFamily: T.font,
  borderRadius: 2,
};

const codeStyle = {
  background: T.bg,
  border: `1px solid ${T.border}`,
  padding: '1px 5px',
  borderRadius: 2,
  fontSize: 10.5,
  color: T.label2,
};
