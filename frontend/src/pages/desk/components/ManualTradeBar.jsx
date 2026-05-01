// ManualTradeBar.jsx
//
// Sticky manual-trade control bar for /desk.
// Rendered ABOVE the existing Header (z-index 10 vs Header's z-index 5).
//
// Layout (single horizontal row, compact):
//   [MANUAL TRADE · BTC 5m]  [UP $ask]  [DOWN $ask]  [$stake]  [→ preview]  [LIVE/PAPER]  [status dot]
//
// Disabled states:
//   - Ask > $0.82 (above hard cap)
//   - Pending manual trade already exists in this window
//   - ENGINE is KILLED
//   - Stake out of [1, 25]
//
// Calls useManualTrades internally for polling + transition toasts.

import React, { useCallback, useState } from 'react';
import { T } from '../../../theme/tokens.js';
import { useClobBook } from '../hooks/useClobBook.js';
import { useManualTrades } from '../hooks/useManualTrades.js';
import { useToast } from '../../../components/shared/Toast.jsx';
import ManualTradeConfirmModal from './ManualTradeConfirmModal.jsx';

const PRICE_CAP = 0.82;
const STAKE_MIN = 1;
const STAKE_MAX = 25;
const STAKE_DEFAULT = 5;

export default function ManualTradeBar({ windowEpoch, systemStatus }) {
  const { addToast } = useToast();
  const [stake, setStake] = useState(STAKE_DEFAULT);
  const [mode, setMode] = useState('live'); // 'live' | 'paper'
  const [modal, setModal] = useState(null); // null | { direction, askPrice }

  // Track trade_id → toast-id so we don't spam on re-render.
  const knownToastsRef = React.useRef(new Set());

  const handleTransition = useCallback((t) => {
    const key = `${t.trade_id}:${t.kind}`;
    if (knownToastsRef.current.has(key)) return;
    knownToastsRef.current.add(key);

    if (t.kind === 'executing') {
      addToast({ variant: 'pending', message: 'Manual trade EXECUTING…' });
    } else if (t.kind === 'filled') {
      const fp = t.fill_price != null ? `$${Number(t.fill_price).toFixed(4)}` : '—';
      const fs = t.fill_size != null ? Number(t.fill_size).toFixed(4) : '—';
      addToast({ variant: 'success', message: `Manual trade FILLED · ${fs} sh @ ${fp}` });
    } else if (t.kind === 'failed') {
      addToast({ variant: 'error', message: `Manual trade FAILED: ${t.reason}` });
    }
  }, [addToast]);

  const { latestPending, latestForWindow } = useManualTrades({
    onTransition: handleTransition,
    pollMs: 3_000,
  });

  // CLOB book for UP/DOWN ask prices.
  const clob = useClobBook({ windowEpoch, asset: 'BTC', pollMs: 10_000 });
  const upAsk = clob.book?.asks_yes?.[0]?.price ?? clob.book?.yes_ask ?? null;
  const downAsk = clob.book?.asks_no?.[0]?.price ?? clob.book?.no_ask ?? null;

  // Active window pending trade.
  const windowTrade = latestForWindow(windowEpoch);
  const hasPendingInWindow = windowTrade != null &&
    (windowTrade.status === 'pending_live' || windowTrade.status === 'executing');

  const engineKilled = systemStatus === 'KILLED';
  const stakeValid = stake >= STAKE_MIN && stake <= STAKE_MAX;

  function isDisabledFor(direction) {
    const ask = direction === 'UP' ? upAsk : downAsk;
    if (!stakeValid) return 'Stake must be between $1 and $25';
    if (engineKilled) return 'Engine is KILLED';
    if (hasPendingInWindow) return 'Pending trade already open for this window';
    if (ask == null) return 'Price unavailable';
    if (ask > PRICE_CAP) return `Ask $${ask.toFixed(4)} exceeds hard cap $${PRICE_CAP}`;
    return null; // not disabled
  }

  const upDisabledReason = isDisabledFor('UP');
  const downDisabledReason = isDisabledFor('DOWN');

  const handleClick = useCallback((direction) => {
    const ask = direction === 'UP' ? upAsk : downAsk;
    if (!ask) return;
    setModal({ direction, askPrice: ask });
  }, [upAsk, downAsk]);

  const handleModalSuccess = useCallback((result) => {
    setModal(null);
    const id = result?.trade_id ?? '?';
    addToast({ variant: 'info', message: `Manual trade queued · #${id}` });
    // Mark it as known so the next poll's "first-sight" doesn't double-fire.
    // (The polling hook ignores first-sight rows already.)
  }, [addToast]);

  // Status indicator.
  const statusDot = getStatusDot(latestPending, windowTrade);

  // Live preview — show UP side ask as the reference price.
  const previewAsk = upAsk;
  const previewShares = previewAsk != null && previewAsk > 0
    ? (stake / previewAsk).toFixed(2)
    : null;
  const previewPayout = previewShares != null
    ? (Number(previewShares)).toFixed(2)
    : null;

  return (
    <>
      <div
        data-testid="manual-trade-bar"
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 10,
          background: '#09090f',
          borderBottom: `1px solid ${T.borderStrong}`,
          padding: '0 12px',
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          height: 44,
          fontFamily: T.font,
          fontSize: 11,
          letterSpacing: '0.06em',
          flexWrap: 'nowrap',
          overflow: 'hidden',
        }}
      >
        {/* Label */}
        <span style={{ color: T.label, whiteSpace: 'nowrap', flexShrink: 0 }}>
          MANUAL TRADE · BTC 5m
        </span>

        {/* UP button */}
        <TradeButton
          direction="UP"
          ask={upAsk}
          disabledReason={upDisabledReason}
          onClick={() => handleClick('UP')}
        />

        {/* DOWN button */}
        <TradeButton
          direction="DOWN"
          ask={downAsk}
          disabledReason={downDisabledReason}
          onClick={() => handleClick('DOWN')}
        />

        {/* Stake input */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
          <span style={{ color: T.label }}>$</span>
          <input
            type="number"
            aria-label="Stake in USD"
            value={stake}
            min={STAKE_MIN}
            max={STAKE_MAX}
            step={1}
            onChange={e => {
              const v = Number(e.target.value);
              // Clamp silently only on blur; let user type freely.
              setStake(e.target.value === '' ? '' : v);
            }}
            onBlur={e => {
              const v = Number(e.target.value);
              if (isNaN(v) || e.target.value === '') {
                setStake(STAKE_DEFAULT);
              } else {
                setStake(Math.min(STAKE_MAX, Math.max(STAKE_MIN, v)));
              }
            }}
            style={{
              width: 48,
              background: 'transparent',
              border: `1px solid ${stakeValid ? T.border : '#f87171'}`,
              borderRadius: 2,
              color: T.text,
              fontFamily: T.font,
              fontSize: 11,
              padding: '3px 6px',
              textAlign: 'right',
              outline: 'none',
            }}
          />
        </div>

        {/* Live preview */}
        {previewAsk != null && previewShares != null && stakeValid ? (
          <span style={{ color: T.label, whiteSpace: 'nowrap', flexShrink: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            → buy {previewShares} sh @ ${previewAsk.toFixed(4)} = ${previewPayout} if win
          </span>
        ) : (
          <span style={{ color: T.label, opacity: 0.4, flexShrink: 1 }}>
            → awaiting book…
          </span>
        )}

        <div style={{ flex: 1 }} />

        {/* Mode toggle */}
        <ModeToggle mode={mode} onChange={setMode} />

        {/* Status dot */}
        <StatusDot info={statusDot} />
      </div>

      {/* Confirm modal — rendered outside the bar to avoid z-index conflicts */}
      {modal && (
        <ManualTradeConfirmModal
          direction={modal.direction}
          stakeUsd={Number(stake) || STAKE_DEFAULT}
          askPrice={modal.askPrice}
          windowEpoch={windowEpoch}
          mode={mode}
          onSuccess={handleModalSuccess}
          onCancel={() => setModal(null)}
        />
      )}
    </>
  );
}

// ─── TradeButton ──────────────────────────────────────────────────────────────

function TradeButton({ direction, ask, disabledReason, onClick }) {
  const isUp = direction === 'UP';
  const disabled = !!disabledReason;
  const label = ask != null
    ? `${direction} $${Number(ask).toFixed(4)}`
    : `${direction} —`;

  return (
    <button
      data-testid={`manual-trade-${direction.toLowerCase()}-btn`}
      title={disabledReason || label}
      disabled={disabled}
      onClick={disabled ? undefined : onClick}
      style={{
        fontFamily: "'IBM Plex Mono', monospace",
        fontSize: 11,
        letterSpacing: '0.1em',
        fontWeight: 700,
        padding: '5px 12px',
        borderRadius: 2,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.35 : 1,
        border: `1px solid ${isUp ? '#4ade80' : '#f87171'}`,
        background: isUp ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.08)',
        color: isUp ? '#4ade80' : '#f87171',
        whiteSpace: 'nowrap',
        flexShrink: 0,
        transition: 'opacity 0.15s',
      }}
    >
      {label}
    </button>
  );
}

// ─── ModeToggle ───────────────────────────────────────────────────────────────

function ModeToggle({ mode, onChange }) {
  return (
    <div style={{
      display: 'flex',
      border: `1px solid ${T.border}`,
      borderRadius: 2,
      overflow: 'hidden',
      flexShrink: 0,
    }}>
      {['live', 'paper'].map(m => (
        <button
          key={m}
          data-testid={`mode-toggle-${m}`}
          onClick={() => onChange(m)}
          style={{
            fontFamily: T.font,
            fontSize: 10,
            letterSpacing: '0.12em',
            padding: '4px 10px',
            border: 'none',
            cursor: 'pointer',
            background: mode === m
              ? (m === 'live' ? 'rgba(74,222,128,0.15)' : 'rgba(245,158,11,0.15)')
              : 'transparent',
            color: mode === m
              ? (m === 'live' ? '#4ade80' : '#f59e0b')
              : T.label,
            fontWeight: mode === m ? 700 : 400,
          }}
        >
          {m.toUpperCase()}
        </button>
      ))}
    </div>
  );
}

// ─── StatusDot ────────────────────────────────────────────────────────────────

function getStatusDot(latestPending, windowTrade) {
  if (latestPending && latestPending.status === 'executing') {
    return { kind: 'spinner', label: 'Executing…' };
  }
  if (latestPending) {
    return { kind: 'spinner', label: 'Pending…' };
  }
  if (windowTrade && windowTrade.status === 'open') {
    return { kind: 'check', label: `Filled @ $${Number(windowTrade.fill_price || 0).toFixed(4)}` };
  }
  if (windowTrade && (windowTrade.status === 'failed_no_token' || windowTrade.status === 'failed_risk_gate')) {
    return { kind: 'error', label: 'Failed' };
  }
  return { kind: 'idle', label: 'Ready' };
}

function StatusDot({ info }) {
  const { kind, label } = info;

  const dotStyle = {
    width: 8,
    height: 8,
    borderRadius: '50%',
    flexShrink: 0,
  };

  let indicator;
  if (kind === 'spinner') {
    indicator = (
      <svg width="12" height="12" viewBox="0 0 24 24" aria-label="pending"
           style={{ flexShrink: 0, animation: 'spin 1s linear infinite' }}>
        <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
        <circle cx="12" cy="12" r="10" stroke="#f59e0b" strokeWidth="3" fill="none" strokeDasharray="31" strokeDashoffset="10" />
      </svg>
    );
  } else if (kind === 'check') {
    indicator = <span style={{ color: '#4ade80', fontSize: 12 }}>✓</span>;
  } else if (kind === 'error') {
    indicator = <span style={{ color: '#f87171', fontSize: 12 }}>✕</span>;
  } else {
    // idle — green dot
    indicator = <div style={{ ...dotStyle, background: '#4ade80', boxShadow: '0 0 4px #4ade80' }} />;
  }

  return (
    <div
      title={label}
      style={{ display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0 }}
      aria-label={label}
    >
      {indicator}
    </div>
  );
}
