import React, { useEffect, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from '../polymarket/components/theme.js';

/**
 * PositionSnapshotBar
 * --------------------
 * Sticky top bar for the /telegram page. Polls /api/positions/snapshot every
 * 5 seconds and shows wallet, pending redemption value, effective balance,
 * relayer cooldown state, and remaining daily redemption quota.
 *
 * Tolerates missing/null fields (loading + error states render gracefully).
 */

function fmtAge(s) {
  if (s == null) return '—';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

function fmtUsd(v) {
  if (v == null || isNaN(Number(v))) return '$—';
  return `$${Number(v).toFixed(2)}`;
}

export default function PositionSnapshotBar() {
  const api = useApi();
  const [snap, setSnap] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      try {
        const res = await api.get('/positions/snapshot');
        if (!live) return;
        // useApi returns an axios instance — payload lives at res.data
        setSnap(res?.data ?? res);
        setError(null);
      } catch (e) {
        if (live) setError(e?.message || String(e));
      }
    };
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [api]);

  if (error && !snap) {
    return (
      <div style={{ padding: 12, color: T.red, fontFamily: T.mono, fontSize: 12 }}>
        snapshot error: {error}
      </div>
    );
  }
  if (!snap) {
    return (
      <div style={{ padding: 12, color: T.textMuted, fontFamily: T.mono, fontSize: 12 }}>
        loading snapshot…
      </div>
    );
  }

  const cd = snap.cooldown || {};
  const overdueCount = Number(snap.overdue_count || 0);
  const pendingCount = Number(snap.pending_count || 0);
  const pendingTone = overdueCount > 0 ? T.amber : T.cyan;
  const cooldownTone = cd.active ? T.red : T.green;

  const quotaRemaining = snap.quota_remaining ?? '—';
  const quotaLimit = snap.daily_quota_limit ?? '—';

  return (
    <div
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 10,
        background: T.headerBg,
        borderBottom: `1px solid ${T.border}`,
        padding: '10px 14px',
        fontFamily: T.mono,
        color: T.text,
        fontSize: 12,
        display: 'flex',
        flexWrap: 'wrap',
        gap: 16,
        alignItems: 'center',
      }}
    >
      <div>
        <span style={{ color: T.textMuted }}>Wallet </span>
        <span style={{ color: T.text }}>{fmtUsd(snap.wallet_usdc)}</span>
      </div>
      <div>
        <span style={{ color: T.textMuted }}>Pending </span>
        <span style={{ color: pendingTone }}>
          {fmtUsd(snap.pending_total_usd)} ({pendingCount})
          {overdueCount > 0 && (
            <span style={{ color: T.amber, marginLeft: 6 }}>
              · {overdueCount} OVERDUE
            </span>
          )}
        </span>
      </div>
      <div>
        <span style={{ color: T.textMuted }}>Effective </span>
        <span style={{ color: T.green, fontWeight: 600 }}>
          {fmtUsd(snap.effective_balance)}
        </span>
      </div>
      <div style={{ marginLeft: 'auto' }}>
        <span style={{ color: cooldownTone }}>
          {cd.active
            ? `🚫 cooldown ${fmtAge(cd.remaining_seconds)}`
            : '🟢 relayer ok'}
        </span>
        <span style={{ color: T.textMuted, marginLeft: 8 }}>
          {quotaRemaining}/{quotaLimit} quota
        </span>
      </div>
    </div>
  );
}
