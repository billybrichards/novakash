import React, { useState, useEffect, useRef } from 'react';
import ReactDOM from 'react-dom';
import { T } from './constants.js';

/**
 * TradeToast -- Brief notification when a new trade is detected.
 * Compares recent_trades[0].id with a ref to detect new arrivals.
 * Auto-dismisses after 10 seconds.
 *
 * Props:
 *   recentTrades -- Array of trade objects from hqData.recent_trades
 */
export default function TradeToast({ recentTrades }) {
  const [toast, setToast] = useState(null);
  const lastTradeIdRef = useRef(null);

  useEffect(() => {
    if (!recentTrades || recentTrades.length === 0) return;
    const latest = recentTrades[0];
    if (!latest || !latest.id) return;

    // First render -- just record the ID, don't show toast
    if (lastTradeIdRef.current === null) {
      lastTradeIdRef.current = latest.id;
      return;
    }

    // Check if this is a new trade
    if (latest.id !== lastTradeIdRef.current) {
      lastTradeIdRef.current = latest.id;
      const dir = latest.direction || '?';
      const entry = latest.entry_price;
      const stake = latest.stake_usd;

      setToast({
        id: latest.id,
        text: `TRADE PLACED: ${dir} at $${entry != null ? entry.toFixed(2) : '?'}`,
        detail: `Stake $${stake != null ? stake.toFixed(2) : '4.00'}`,
      });

      // Auto-dismiss after 10s
      const timer = setTimeout(() => setToast(null), 10000);
      return () => clearTimeout(timer);
    }
  }, [recentTrades]);

  if (!toast) return null;

  return ReactDOM.createPortal(
    <div style={{
      position: 'fixed', top: 16, left: '50%', transform: 'translateX(-50%)',
      zIndex: 10000,
      background: 'rgba(7,7,12,0.92)', backdropFilter: 'blur(16px)',
      border: '1px solid rgba(168,85,247,0.4)',
      borderRadius: 8, padding: '10px 20px',
      boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
      display: 'flex', alignItems: 'center', gap: 12,
      animation: 'toastSlideIn 300ms ease-out',
    }}>
      <span style={{ fontSize: 18 }}>{'\uD83C\uDFAF'}</span>
      <div>
        <div style={{
          fontSize: 12, fontWeight: 700, color: T.purple,
          fontFamily: "'JetBrains Mono', monospace",
        }}>
          {toast.text}
        </div>
        <div style={{ fontSize: 10, color: T.textMuted, marginTop: 2, fontFamily: 'monospace' }}>
          {toast.detail}
        </div>
      </div>
      <button
        onClick={() => setToast(null)}
        style={{
          background: 'none', border: 'none', color: T.textMuted,
          cursor: 'pointer', fontSize: 16, padding: '0 4px', marginLeft: 8,
        }}
      >
        {'\u00d7'}
      </button>

      <style>{`
        @keyframes toastSlideIn {
          from { opacity: 0; transform: translateX(-50%) translateY(-20px); }
          to { opacity: 1; transform: translateX(-50%) translateY(0); }
        }
      `}</style>
    </div>,
    document.body
  );
}
