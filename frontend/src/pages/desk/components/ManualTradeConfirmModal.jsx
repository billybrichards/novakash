// ManualTradeConfirmModal.jsx
//
// Confirmation modal for manual BTC 5m trades.
// Shows trade summary, posts to /api/desk/manual-trade on confirm.
// Errors displayed inline — modal stays open on 4xx so operator can adjust.
// 200 → closes modal, fires toast via onSuccess callback.

import React, { useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { useAuth } from '../../../auth/AuthContext.jsx';
import { T } from '../../../theme/tokens.js';

export default function ManualTradeConfirmModal({
  direction,       // 'UP' | 'DOWN'
  stakeUsd,        // number
  askPrice,        // number (entry price for this side)
  windowEpoch,     // int
  mode,            // 'live' | 'paper'
  onSuccess,       // (result) => void — called after 200
  onCancel,        // () => void
}) {
  const api = useApi();
  const { user } = useAuth();
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);

  const shares = askPrice > 0 ? (stakeUsd / askPrice).toFixed(4) : '—';
  const payout = askPrice > 0 ? (stakeUsd / askPrice).toFixed(2) : '—';
  const dirUp = direction === 'UP';

  const handleConfirm = async () => {
    setSubmitting(true);
    setErrorMsg(null);
    try {
      const body = {
        direction,
        stake_usd: stakeUsd,
        price: askPrice,
        mode,
        window_epoch: windowEpoch,
        asset: 'BTC',
        order_type: 'FAK',
      };
      const r = await api.post('/api/desk/manual-trade', body);
      const result = r?.data ?? r;
      onSuccess(result);
    } catch (e) {
      const status = e?.response?.status;
      const msg = e?.response?.data?.message || e?.response?.data?.detail || e?.message || 'Request failed';
      if (status === 409) {
        setErrorMsg('Conflict: a pending manual trade already exists for this window.');
      } else if (status === 429) {
        setErrorMsg('Rate limit: max 12 manual trades per hour reached.');
      } else if (status === 400) {
        setErrorMsg(`Invalid: ${msg}`);
      } else if (status === 401) {
        setErrorMsg('Unauthorized — please reload and log in again.');
      } else {
        setErrorMsg(msg);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    // Overlay
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Confirm manual trade"
      onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}
      style={{
        position: 'fixed', inset: 0,
        background: 'rgba(0,0,0,0.75)',
        zIndex: 200,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: T.font,
      }}
    >
      <div style={{
        background: '#0f0f18',
        border: `1px solid ${T.borderStrong}`,
        borderRadius: 4,
        padding: 28,
        minWidth: 340,
        maxWidth: 420,
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}>
        {/* Title */}
        <div style={{
          fontSize: 13,
          fontWeight: 700,
          letterSpacing: '0.12em',
          color: dirUp ? '#4ade80' : '#f87171',
        }}>
          MANUAL TRADE · BTC 5m · {direction}
        </div>

        {/* Summary */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'auto 1fr',
          gap: '6px 16px',
          fontSize: 12,
          color: T.label2,
        }}>
          <span style={{ color: T.label }}>Direction</span>
          <span style={{ color: dirUp ? '#4ade80' : '#f87171', fontWeight: 700 }}>{direction}</span>

          <span style={{ color: T.label }}>Window</span>
          <span style={{ color: T.text }}>{windowEpoch != null ? new Date(windowEpoch * 1000).toUTCString() : '—'}</span>

          <span style={{ color: T.label }}>Stake</span>
          <span style={{ color: T.text }}>${stakeUsd.toFixed(2)}</span>

          <span style={{ color: T.label }}>Ask price</span>
          <span style={{ color: T.text }}>${askPrice.toFixed(4)}</span>

          <span style={{ color: T.label }}>Shares</span>
          <span style={{ color: T.text }}>{shares}</span>

          <span style={{ color: T.label }}>Payout if win</span>
          <span style={{ color: '#4ade80' }}>${payout}</span>

          <span style={{ color: T.label }}>Mode</span>
          <span style={{
            color: mode === 'paper' ? '#f59e0b' : '#4ade80',
            fontWeight: 700,
            letterSpacing: '0.1em',
          }}>{mode.toUpperCase()}</span>

          <span style={{ color: T.label }}>Operator</span>
          <span style={{ color: T.text }}>{user?.username ?? '—'}</span>
        </div>

        {/* Error */}
        {errorMsg && (
          <div style={{
            fontSize: 11,
            color: '#f87171',
            border: '1px solid rgba(248,113,113,0.3)',
            borderRadius: 2,
            padding: '8px 12px',
            lineHeight: 1.6,
          }}>
            {errorMsg}
          </div>
        )}

        {/* Buttons */}
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
          <button
            onClick={onCancel}
            disabled={submitting}
            style={{
              fontFamily: T.font,
              fontSize: 11,
              letterSpacing: '0.12em',
              padding: '7px 16px',
              background: 'none',
              border: `1px solid ${T.border}`,
              color: T.label2,
              borderRadius: 2,
              cursor: submitting ? 'not-allowed' : 'pointer',
              opacity: submitting ? 0.5 : 1,
            }}
          >
            CANCEL
          </button>
          <button
            onClick={handleConfirm}
            disabled={submitting}
            style={{
              fontFamily: T.font,
              fontSize: 11,
              letterSpacing: '0.12em',
              fontWeight: 700,
              padding: '7px 20px',
              background: dirUp ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)',
              border: `1px solid ${dirUp ? '#4ade80' : '#f87171'}`,
              color: dirUp ? '#4ade80' : '#f87171',
              borderRadius: 2,
              cursor: submitting ? 'not-allowed' : 'pointer',
              opacity: submitting ? 0.7 : 1,
            }}
          >
            {submitting ? 'SUBMITTING…' : `CONFIRM ${direction}`}
          </button>
        </div>
      </div>
    </div>
  );
}
