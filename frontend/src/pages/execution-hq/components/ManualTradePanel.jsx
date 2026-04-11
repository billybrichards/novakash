import React, { useState, useEffect, useRef, useCallback } from 'react';
import ReactDOM from 'react-dom';
import { Zap, X, ChevronUp, ChevronDown, Loader2 } from 'lucide-react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from './constants.js';

/**
 * ManualTradePanel -- Floating trade execution panel (portal-based).
 *
 * Props:
 *   hqData -- execution HQ data for market context
 */
export default function ManualTradePanel({ hqData }) {
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [direction, setDirection] = useState('UP');
  const [orderType, setOrderType] = useState('FAK');
  const [priceOverride, setPriceOverride] = useState('');
  const [stake, setStake] = useState('4.00');
  // LT-03 — optional free-text reason the operator clicked this trade,
  // persisted server-side in manual_trade_snapshots.operator_rationale.
  const [rationale, setRationale] = useState('');
  const [livePrices, setLivePrices] = useState(null);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState(null);
  const pollRef = useRef(null);

  // Derive market info from hqData
  const latestWindow = hqData?.windows?.[0] || {};
  const system = hqData?.system || {};
  const mode = system.paper_mode ? 'paper' : 'live';

  // Fetch live prices when panel is open
  const fetchPrices = useCallback(async () => {
    try {
      const res = await api('GET', '/v58/live-prices');
      const data = res?.data || res;
      setLivePrices(data);
      // Pre-fill price if user hasn't overridden
      if (!priceOverride) {
        const autoPrice = direction === 'UP' ? data.up_price : data.down_price;
        if (autoPrice) setPriceOverride(autoPrice.toFixed(4));
      }
    } catch {
      // Silently handle -- panel shows stale or no data
    }
  }, [api, direction, priceOverride]);

  useEffect(() => {
    if (!open) return;
    fetchPrices();
    pollRef.current = setInterval(fetchPrices, 4000);
    return () => clearInterval(pollRef.current);
  }, [open, fetchPrices]);

  // Reset price when direction changes
  useEffect(() => {
    if (livePrices) {
      const autoPrice = direction === 'UP' ? livePrices.up_price : livePrices.down_price;
      if (autoPrice) setPriceOverride(autoPrice.toFixed(4));
    }
  }, [direction]);

  const handleExecute = async () => {
    setExecuting(true);
    setResult(null);
    try {
      const trimmedRationale = rationale.trim();
      const payload = {
        direction,
        mode,
        order_type: orderType,
        stake_usd: parseFloat(stake) || 4.0,
        // LT-03 — send null instead of empty string so the DB stores NULL
        operator_rationale: trimmedRationale.length > 0 ? trimmedRationale : null,
      };
      const priceVal = parseFloat(priceOverride);
      if (priceVal > 0) {
        payload.price_override = priceVal;
      }
      const res = await api.post('/v58/manual-trade', payload);
      const data = res?.data || res;
      setResult({ ok: true, data });
      // Clear rationale after a successful trade so it isn't reused by accident
      setRationale('');
    } catch (err) {
      const msg = err?.response?.data?.detail || err.message || 'Trade failed';
      setResult({ ok: false, error: msg });
    } finally {
      setExecuting(false);
    }
  };

  // CLOB ask from live prices
  const clobAsk = direction === 'UP' ? livePrices?.up_price : livePrices?.down_price;
  const duneP = latestWindow.dune_probability_up;
  const dunePDir = duneP != null ? Math.max(duneP, 1 - duneP) : null;
  const clStatus = latestWindow.delta_chainlink != null;
  const tiStatus = latestWindow.delta_tiingo != null;

  const toggleButton = (
    <button
      onClick={() => { setOpen(o => !o); setResult(null); }}
      style={{
        position: 'fixed', bottom: 20, right: 20, zIndex: 9998,
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '10px 20px', borderRadius: 999,
        background: 'linear-gradient(135deg, #a855f7 0%, #7c3aed 100%)',
        color: '#fff', border: 'none', cursor: 'pointer',
        fontFamily: 'monospace', fontWeight: 700, fontSize: 13,
        boxShadow: '0 4px 20px rgba(168,85,247,0.4)',
        transition: 'all 150ms',
      }}
    >
      <Zap size={16} />
      {open ? 'CLOSE' : 'TRADE'}
    </button>
  );

  const panel = open ? (
    <div style={{
      position: 'fixed', bottom: 70, right: 20, zIndex: 9999,
      width: 320, maxHeight: 'calc(100vh - 100px)',
      background: 'rgba(7,7,12,0.92)', backdropFilter: 'blur(20px)',
      border: '1px solid rgba(168,85,247,0.3)',
      borderRadius: 8, padding: 16,
      boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
      fontFamily: 'monospace', color: T.text,
      display: 'flex', flexDirection: 'column', gap: 12,
      overflowY: 'auto',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: T.purple, letterSpacing: '0.05em' }}>
          MANUAL TRADE
        </span>
        <button onClick={() => setOpen(false)} style={{
          background: 'none', border: 'none', color: T.textMuted, cursor: 'pointer', padding: 2,
        }}>
          <X size={14} />
        </button>
      </div>

      {/* Mode indicator */}
      <div style={{
        fontSize: 10, padding: '4px 8px', borderRadius: 4,
        background: mode === 'paper' ? 'rgba(245,158,11,0.1)' : 'rgba(239,68,68,0.1)',
        border: `1px solid ${mode === 'paper' ? 'rgba(245,158,11,0.3)' : 'rgba(239,68,68,0.3)'}`,
        color: mode === 'paper' ? T.amber : T.red,
        textAlign: 'center', fontWeight: 700,
      }}>
        {mode.toUpperCase()} MODE
      </div>

      {/* Direction toggle */}
      <div style={{ display: 'flex', gap: 8 }}>
        {['UP', 'DOWN'].map(dir => (
          <button key={dir} onClick={() => setDirection(dir)} style={{
            flex: 1, padding: '8px 0', borderRadius: 4,
            border: `1px solid ${dir === direction ? (dir === 'UP' ? 'rgba(16,185,129,0.5)' : 'rgba(239,68,68,0.5)') : T.cardBorder}`,
            background: dir === direction ? (dir === 'UP' ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)') : 'transparent',
            color: dir === direction ? (dir === 'UP' ? T.green : T.red) : T.textMuted,
            cursor: 'pointer', fontFamily: 'monospace', fontWeight: 700, fontSize: 13,
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4,
            transition: 'all 150ms',
          }}>
            {dir === 'UP' ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            {dir}
          </button>
        ))}
      </div>

      {/* Order type */}
      <div>
        <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>Order Type</div>
        <div style={{ display: 'flex', gap: 6 }}>
          {['FAK', 'FOK', 'GTC'].map(ot => (
            <button key={ot} onClick={() => setOrderType(ot)} style={{
              flex: 1, padding: '5px 0', borderRadius: 3,
              border: `1px solid ${ot === orderType ? 'rgba(168,85,247,0.5)' : T.cardBorder}`,
              background: ot === orderType ? 'rgba(168,85,247,0.15)' : 'transparent',
              color: ot === orderType ? T.purple : T.textMuted,
              cursor: 'pointer', fontFamily: 'monospace', fontWeight: 600, fontSize: 11,
              transition: 'all 150ms',
            }}>
              {ot}
            </button>
          ))}
        </div>
      </div>

      {/* Price input */}
      <div>
        <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>
          Entry Price (override)
        </div>
        <input
          type="number"
          step="0.0001"
          value={priceOverride}
          onChange={e => setPriceOverride(e.target.value)}
          style={{
            width: '100%', padding: '6px 10px', borderRadius: 4,
            background: 'rgba(15,23,42,0.8)', border: `1px solid ${T.cardBorder}`,
            color: T.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 13,
            outline: 'none', boxSizing: 'border-box',
          }}
          placeholder="Auto from Gamma API"
        />
      </div>

      {/* Stake input */}
      <div>
        <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>
          Stake (USD)
        </div>
        <input
          type="number"
          step="0.50"
          min="1"
          max="50"
          value={stake}
          onChange={e => setStake(e.target.value)}
          style={{
            width: '100%', padding: '6px 10px', borderRadius: 4,
            background: 'rgba(15,23,42,0.8)', border: `1px solid ${T.cardBorder}`,
            color: T.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 13,
            outline: 'none', boxSizing: 'border-box',
          }}
        />
      </div>

      {/* LT-03 Rationale (optional) — captured in manual_trade_snapshots */}
      <div>
        <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>
          Rationale (optional)
        </div>
        <textarea
          value={rationale}
          onChange={e => setRationale(e.target.value)}
          rows={2}
          placeholder="Why this trade? e.g. '2 DOWNs in a row, feels due for UP'"
          style={{
            width: '100%', padding: '6px 10px', borderRadius: 4,
            background: 'rgba(15,23,42,0.8)', border: `1px solid ${T.cardBorder}`,
            color: T.text, fontFamily: "'JetBrains Mono', monospace", fontSize: 10,
            outline: 'none', boxSizing: 'border-box', resize: 'vertical',
          }}
        />
      </div>

      {/* Market info */}
      <div style={{
        padding: '8px 10px', borderRadius: 4,
        background: 'rgba(15,23,42,0.6)', border: `1px solid ${T.cardBorder}`,
        fontSize: 10, display: 'flex', flexDirection: 'column', gap: 4,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: T.textMuted }}>CLOB Ask</span>
          <span style={{ color: T.cyan, fontFamily: "'JetBrains Mono', monospace" }}>
            {clobAsk != null ? `$${clobAsk.toFixed(4)}` : '--'}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: T.textMuted }}>DUNE P</span>
          <span style={{
            color: dunePDir != null ? (dunePDir >= 0.75 ? T.green : dunePDir >= 0.60 ? T.amber : T.red) : T.textDim,
            fontFamily: "'JetBrains Mono', monospace",
          }}>
            {dunePDir != null ? dunePDir.toFixed(3) : '--'}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: T.textMuted }}>Sources</span>
          <span>
            <span style={{ color: clStatus ? T.green : T.red }}>CL</span>
            {' '}
            <span style={{ color: tiStatus ? T.green : T.red }}>TI</span>
          </span>
        </div>
      </div>

      {/* Execute button */}
      <button
        onClick={handleExecute}
        disabled={executing}
        style={{
          padding: '10px 0', borderRadius: 4, border: 'none',
          background: executing
            ? 'rgba(100,116,139,0.3)'
            : direction === 'UP'
              ? 'linear-gradient(135deg, #10b981 0%, #059669 100%)'
              : 'linear-gradient(135deg, #ef4444 0%, #dc2626 100%)',
          color: '#fff', cursor: executing ? 'not-allowed' : 'pointer',
          fontFamily: 'monospace', fontWeight: 700, fontSize: 14,
          letterSpacing: '0.1em',
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
          boxShadow: executing ? 'none' : `0 4px 16px ${direction === 'UP' ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}`,
          transition: 'all 150ms',
        }}
      >
        {executing ? <Loader2 size={16} style={{ animation: 'spin 1s linear infinite' }} /> : <Zap size={16} />}
        {executing ? 'EXECUTING...' : `EXECUTE ${direction}`}
      </button>

      {/* Result area */}
      {result && (
        <div style={{
          padding: '8px 10px', borderRadius: 4, fontSize: 11,
          background: result.ok ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)',
          border: `1px solid ${result.ok ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)'}`,
          color: result.ok ? T.green : T.red,
        }}>
          {result.ok ? (
            <>
              Trade placed: {result.data.direction} @ ${result.data.entry_price?.toFixed(4)}
              <br />
              Stake: ${result.data.stake?.toFixed(2)} | {result.data.order_type} | {result.data.mode}
            </>
          ) : (
            <>Error: {result.error}</>
          )}
        </div>
      )}
    </div>
  ) : null;

  return ReactDOM.createPortal(
    <>
      {panel}
      {toggleButton}
      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </>,
    document.body
  );
}
