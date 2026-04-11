/**
 * CompositeSignals.jsx — Real-time v3 composite signal dashboard.
 *
 * Shows the 9-timescale composite scores, individual signal breakdown,
 * cascade state, and signal history. Polls /v3/snapshot every 2s.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

const T = {
  bg: '#0a0a0f', card: '#12121a', border: 'rgba(255,255,255,0.06)',
  label: '#666', label2: '#888', mono: "'JetBrains Mono', 'Fira Code', monospace",
  profit: '#22c55e', loss: '#ef4444', purple: '#a855f7', cyan: '#06b6d4',
  warning: '#eab308', blue: '#3b82f6',
};

const SIGNAL_COLORS = {
  elm: '#a855f7', cascade: '#ef4444', taker: '#06b6d4',
  oi: '#3b82f6', funding: '#eab308', vpin: '#22c55e', momentum: '#f97316',
};

function SignalBar({ name, value, color }) {
  const pct = Math.abs(value || 0) * 100;
  const direction = (value || 0) >= 0 ? 'right' : 'left';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
      <div style={{ width: 70, fontSize: 9, color: T.label, fontWeight: 600, textTransform: 'uppercase' }}>{name}</div>
      <div style={{ flex: 1, height: 14, background: 'rgba(255,255,255,0.03)', borderRadius: 3, position: 'relative', overflow: 'hidden' }}>
        <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'rgba(255,255,255,0.1)' }} />
        <div style={{
          position: 'absolute',
          [direction === 'right' ? 'left' : 'right']: '50%',
          top: 1, bottom: 1,
          width: `${Math.min(pct, 100) / 2}%`,
          background: color,
          borderRadius: 2,
          opacity: 0.7,
        }} />
      </div>
      <div style={{ width: 45, fontSize: 10, fontFamily: T.mono, color: value >= 0 ? T.profit : T.loss, textAlign: 'right' }}>
        {value != null ? (value >= 0 ? '+' : '') + value.toFixed(3) : '—'}
      </div>
    </div>
  );
}

function TimescaleCard({ timescale, data }) {
  if (!data) {
    return (
      <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 10, padding: 14 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: T.label, marginBottom: 8 }}>{timescale}</div>
        <div style={{ fontSize: 10, color: T.label }}>Waiting for data...</div>
      </div>
    );
  }

  const composite = data.composite;
  const compositeColor = composite >= 0.3 ? T.profit : composite <= -0.3 ? T.loss : T.warning;
  const direction = composite >= 0.1 ? 'LONG' : composite <= -0.1 ? 'SHORT' : 'NEUTRAL';
  const signals = data.signals || {};

  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 10, padding: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <div style={{ fontSize: 13, fontWeight: 800, color: '#fff' }}>{timescale}</div>
        <div style={{
          fontSize: 9, fontWeight: 800, padding: '2px 8px', borderRadius: 4,
          background: direction === 'LONG' ? 'rgba(34,197,94,0.15)' : direction === 'SHORT' ? 'rgba(239,68,68,0.15)' : 'rgba(234,179,8,0.15)',
          color: direction === 'LONG' ? T.profit : direction === 'SHORT' ? T.loss : T.warning,
          border: `1px solid ${direction === 'LONG' ? 'rgba(34,197,94,0.3)' : direction === 'SHORT' ? 'rgba(239,68,68,0.3)' : 'rgba(234,179,8,0.3)'}`,
        }}>{direction}</div>
      </div>

      <div style={{ fontSize: 28, fontWeight: 900, fontFamily: T.mono, color: compositeColor, marginBottom: 12 }}>
        {composite >= 0 ? '+' : ''}{composite.toFixed(3)}
      </div>

      {Object.entries(SIGNAL_COLORS).map(([key, color]) => (
        <SignalBar key={key} name={key} value={signals[key]} color={color} />
      ))}

      {data.cascade && data.cascade.strength > 0.05 && (
        <div style={{
          marginTop: 8, padding: '6px 8px', borderRadius: 6,
          background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.2)',
          fontSize: 9, color: T.loss,
        }}>
          CASCADE S={data.cascade.strength.toFixed(2)} | tau1={data.cascade.tau1.toFixed(1)}s | exhaust={data.cascade.exhaustion_t.toFixed(0)}s
        </div>
      )}
    </div>
  );
}

export default function CompositeSignals() {
  const api = useApi();
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastGoodSnapshot, setLastGoodSnapshot] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await api('GET', '/api/v3/snapshot?asset=BTC');
      const data = res?.data ?? null;
      setSnapshot(data);
      if (data?.timescales) setLastGoodSnapshot(data);
      setError(null);
    } catch (e) {
      setError(e.message);
      // Keep showing stale data if we had it
    }
    finally { setLoading(false); }
  }, [api]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 2000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <div style={{ color: T.label, padding: 40, fontFamily: T.mono }}>Loading composite signals...</div>;

  const displaySnapshot = snapshot || lastGoodSnapshot;
  const timescales = displaySnapshot?.timescales || {};
  const isStale = !snapshot && !!lastGoodSnapshot;
  const shortTerm = ['5m', '15m', '1h', '4h'];
  const longTerm = ['24h', '48h', '72h', '1w', '2w'];

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1400, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h1 style={{ fontSize: 18, fontWeight: 800, color: '#fff', margin: 0 }}>V3 Composite Signals</h1>
            {displaySnapshot?.model?.model_family && (
              <div
                title={displaySnapshot.model.model_version || ''}
                style={{
                  fontSize: 9, fontWeight: 800, padding: '3px 8px', borderRadius: 4,
                  background: 'rgba(168,85,247,0.12)', color: T.purple,
                  border: '1px solid rgba(168,85,247,0.3)',
                  fontFamily: T.mono, letterSpacing: '0.04em',
                }}
              >
                {displaySnapshot.model.model_family}
              </div>
            )}
          </div>
          <p style={{ fontSize: 10, color: T.label, margin: '4px 0 0' }}>7-signal fusion across 9 timescales</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {isStale && (
            <div style={{ fontSize: 9, color: T.warning, padding: '4px 8px', background: 'rgba(234,179,8,0.1)', borderRadius: 4, border: '1px solid rgba(234,179,8,0.2)' }}>
              STALE — showing last known data
            </div>
          )}
          {error && !displaySnapshot && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 6,
              fontSize: 10, color: T.cyan, padding: '8px 12px',
              background: 'rgba(6,182,212,0.08)', borderRadius: 6, border: '1px solid rgba(6,182,212,0.2)',
            }}>
              <div style={{
                width: 6, height: 6, borderRadius: '50%', background: T.cyan,
                animation: 'pulse 1.5s infinite',
              }} />
              Connecting to signal service...
            </div>
          )}
        </div>
      </div>

      {/* Connection error banner — only if no data at all */}
      {error && !displaySnapshot && (
        <div style={{
          padding: '20px', marginBottom: 20, borderRadius: 10, textAlign: 'center',
          background: 'rgba(6,182,212,0.05)', border: '1px solid rgba(6,182,212,0.15)',
        }}>
          <div style={{ fontSize: 24, marginBottom: 8, opacity: 0.6 }}>📡</div>
          <div style={{ fontSize: 12, color: '#fff', fontWeight: 700, marginBottom: 4 }}>Signal Service Connecting</div>
          <div style={{ fontSize: 10, color: T.label }}>
            The v3 composite signal feed is starting up. Data will appear automatically once the connection is established.
          </div>
        </div>
      )}

      {/* Short-term timescales */}
      <div style={{ fontSize: 10, fontWeight: 700, color: T.label, marginBottom: 8, letterSpacing: '0.08em' }}>SHORT-TERM (IN-MEMORY)</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 12, marginBottom: 24 }}>
        {shortTerm.map(ts => (
          <TimescaleCard key={ts} timescale={ts} data={timescales[ts]} />
        ))}
      </div>

      {/* Long-term timescales */}
      <div style={{ fontSize: 10, fontWeight: 700, color: T.label, marginBottom: 8, letterSpacing: '0.08em' }}>LONG-TERM (DB-BACKED)</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 12 }}>
        {longTerm.map(ts => (
          <TimescaleCard key={ts} timescale={ts} data={timescales[ts]} />
        ))}
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
      `}</style>
    </div>
  );
}
