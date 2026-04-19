import React, { useSyncExternalStore, useMemo } from 'react';
import { T } from '../../theme/tokens.js';

const W = 340;
const H = 120;
const PAD = 6;

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

export default function DisagreementPlot({ buffer }) {
  const sub = useMemo(() => subscribe(buffer), [buffer]);
  const snap = useMemo(() => getSnapshot(buffer), [buffer]);
  useSyncExternalStore(sub, snap, snap);

  const samples = buffer.snapshot();
  const series = useMemo(() => samples
    .map((s) => s.disagreement_magnitude)
    .filter((v) => v != null), [samples, buffer.getVersion()]);

  const { mean, median, p95 } = useMemo(() => {
    if (series.length === 0) return { mean: null, median: null, p95: null };
    const sorted = [...series].sort((a, b) => a - b);
    const sum = sorted.reduce((a, b) => a + b, 0);
    const pickFrac = (f) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * f))];
    return { mean: sum / sorted.length, median: pickFrac(0.5), p95: pickFrac(0.95) };
  }, [series]);

  const line = useMemo(() => {
    if (series.length < 2) return '';
    const n = series.length;
    const max = 1.0;  // Disagreement magnitude is on [0, 1]
    return series.map((v, i) => {
      const x = PAD + (i / (n - 1)) * (W - 2 * PAD);
      const y = H - PAD - Math.max(0, Math.min(1, v / max)) * (H - 2 * PAD);
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }, [series]);

  const y06 = H - PAD - 0.6 * (H - 2 * PAD);
  const y03 = H - PAD - 0.3 * (H - 2 * PAD);

  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 4,
      padding: 14,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: T.label2, fontWeight: 600, letterSpacing: '0.08em' }}>
          |p_lgb − p_cls| · time series
        </div>
        <div style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>
          n={series.length} · rolling 1h
        </div>
      </div>

      {series.length < 2 ? (
        <div style={{ color: T.label, fontSize: 11, padding: 16, textAlign: 'center' }}>collecting…</div>
      ) : (
        <>
          <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ display: 'block' }}>
            {/* threshold bands */}
            <rect x={0} y={y06} width={W} height={H - y06 - PAD} fill="rgba(239,68,68,0.08)" />
            <rect x={0} y={y03} width={W} height={y06 - y03} fill="rgba(245,158,11,0.06)" />
            {/* threshold lines */}
            <line x1={0} y1={y06} x2={W} y2={y06} stroke="#ef4444" strokeDasharray="2 3" strokeWidth={0.5} />
            <line x1={0} y1={y03} x2={W} y2={y03} stroke="#f59e0b" strokeDasharray="2 3" strokeWidth={0.5} />
            <path d={line} fill="none" stroke={T.cyan} strokeWidth={1.2} />
          </svg>
          <div style={{ display: 'flex', gap: 14, marginTop: 8, fontSize: 10, fontFamily: T.font, color: T.label2 }}>
            <span>mean <b style={{ color: T.text }}>{mean?.toFixed(3)}</b></span>
            <span>median <b style={{ color: T.text }}>{median?.toFixed(3)}</b></span>
            <span>p95 <b style={{ color: T.text }}>{p95?.toFixed(3)}</b></span>
          </div>
        </>
      )}
    </div>
  );
}
