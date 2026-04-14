import React from 'react';
import { T, SIGNAL_COLORS, SHORT_TERM } from './constants.js';

function SignalBar({ name, value, color }) {
  const pct = Math.abs(value || 0) * 100;
  const direction = (value || 0) >= 0 ? 'right' : 'left';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
      <div style={{ width: 55, fontSize: 8, color: T.textMuted, fontWeight: 600, textTransform: 'uppercase' }}>{name}</div>
      <div style={{ flex: 1, height: 10, background: 'rgba(255,255,255,0.03)', borderRadius: 2, position: 'relative', overflow: 'hidden' }}>
        <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'rgba(255,255,255,0.08)' }} />
        <div style={{
          position: 'absolute',
          [direction === 'right' ? 'left' : 'right']: '50%',
          top: 0, bottom: 0,
          width: `${Math.min(pct, 100) / 2}%`,
          background: color,
          borderRadius: 1,
          opacity: 0.6,
        }} />
      </div>
      <div style={{ width: 38, fontSize: 9, fontFamily: T.mono, color: value >= 0 ? T.green : T.red, textAlign: 'right' }}>
        {value != null ? value.toFixed(2) : '—'}
      </div>
    </div>
  );
}

function TimescaleCard({ timescale, data }) {
  if (!data) {
    return (
      <div style={{ background: 'rgba(15,23,42,0.5)', border: `1px solid ${T.cardBorder}`, borderRadius: 6, padding: 10 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.textMuted }}>{timescale}</div>
        <div style={{ fontSize: 9, color: T.textDim, marginTop: 4 }}>No data</div>
      </div>
    );
  }

  const c = data.composite;
  const color = c >= 0.3 ? T.green : c <= -0.3 ? T.red : T.amber;
  const dir = c >= 0.1 ? 'LONG' : c <= -0.1 ? 'SHORT' : 'FLAT';
  const signals = data.signals || {};

  return (
    <div style={{ background: 'rgba(15,23,42,0.5)', border: `1px solid ${T.cardBorder}`, borderRadius: 6, padding: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: T.white }}>{timescale}</span>
        <span style={{
          fontSize: 7, fontWeight: 800, padding: '1px 5px', borderRadius: 3, letterSpacing: '0.05em',
          background: dir === 'LONG' ? 'rgba(16,185,129,0.15)' : dir === 'SHORT' ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.15)',
          color: dir === 'LONG' ? T.green : dir === 'SHORT' ? T.red : T.amber,
        }}>{dir}</span>
      </div>
      <div style={{ fontSize: 22, fontWeight: 900, fontFamily: T.mono, color, marginBottom: 8 }}>
        {c >= 0 ? '+' : ''}{c.toFixed(3)}
      </div>
      {Object.entries(SIGNAL_COLORS).map(([key, col]) => (
        <SignalBar key={key} name={key} value={signals[key]} color={col} />
      ))}
    </div>
  );
}

export default function SignalPanel({ snapshot }) {
  const timescales = snapshot?.timescales || {};
  const hasData = Object.values(timescales).some(v => v !== null);

  return (
    <div style={{ background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 8, overflow: 'hidden' }}>
      <div style={{ padding: '10px 14px', borderBottom: `1px solid ${T.cardBorder}`, background: T.headerBg, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <span style={{ fontSize: 11, fontWeight: 700, color: T.text }}>COMPOSITE SIGNALS</span>
          <span style={{ fontSize: 9, color: T.textMuted, marginLeft: 8 }}>v3 | 7 signals × 4 timescales</span>
        </div>
        {!hasData && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <div style={{
              width: 5, height: 5, borderRadius: '50%', background: T.amber,
              animation: 'signalPulse 1.5s infinite',
            }} />
            <span style={{ fontSize: 8, color: T.amber, fontFamily: T.mono, fontWeight: 600 }}>CONNECTING</span>
          </div>
        )}
      </div>
      <div style={{ padding: 12 }}>
        {!hasData ? (
          <div style={{ textAlign: 'center', padding: '16px 0' }}>
            <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4 }}>Waiting for v3 signal feed...</div>
            <div style={{ fontSize: 8, color: T.textDim }}>Signals will appear once the TimesFM connection is established</div>
            <div style={{ marginTop: 10 }}>
              <a
                href="https://github.com/billybrichards/novakash/wiki/Signal-Pipeline"
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 9, color: T.cyan, textDecoration: 'none' }}
              >
                Documentation →
              </a>
            </div>
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
            {SHORT_TERM.map(ts => (
              <TimescaleCard key={ts} timescale={ts} data={timescales[ts]} />
            ))}
          </div>
        )}
      </div>
      <style>{`
        @keyframes signalPulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
      `}</style>
    </div>
  );
}
