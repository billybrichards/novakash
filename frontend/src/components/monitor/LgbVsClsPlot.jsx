import React, { useSyncExternalStore, useMemo } from 'react';
import { T } from '../../theme/tokens.js';

// Two-line time-series — p_lgb vs p_classifier on same [0,1] axis, with
// p_up overlay (dim) for reference. Both lines stay visible when one is
// null (null samples skipped). Y-axis is fixed 0-1, 0.5 midline drawn.

const W = 340;
const H = 140;
const PAD = 6;

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

function buildPath(samples, key) {
  const n = samples.length;
  if (n < 2) return '';
  let d = '';
  let moved = false;
  for (let i = 0; i < n; i += 1) {
    const v = samples[i][key];
    if (v == null || Number.isNaN(v)) {
      moved = false;
      continue;
    }
    const x = PAD + (i / (n - 1)) * (W - 2 * PAD);
    const y = H - PAD - Math.max(0, Math.min(1, v)) * (H - 2 * PAD);
    d += `${moved ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
    moved = true;
  }
  return d.trim();
}

export default function LgbVsClsPlot({ buffer }) {
  useSyncExternalStore(subscribe(buffer), getSnapshot(buffer));
  const samples = buffer.snapshot();

  const { pLgb, pCls, pUp } = useMemo(
    () => ({
      pLgb: buildPath(samples, 'probability_lgb'),
      pCls: buildPath(samples, 'probability_classifier'),
      pUp: buildPath(samples, 'probability_up'),
    }),
    [buffer.getVersion()], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const latest = samples.length ? samples[samples.length - 1] : null;
  const midY = H - PAD - 0.5 * (H - 2 * PAD);

  return (
    <div
      data-testid="lgb-vs-cls-plot"
      style={{
        background: T.card,
        border: `1px solid ${T.border}`,
        borderRadius: 4,
        padding: 14,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: T.label2,
            fontWeight: 600,
            letterSpacing: '0.08em',
          }}
        >
          p_lgb vs p_classifier · time series
        </div>
        <div style={{ fontSize: 9, color: T.label }}>
          n={samples.length} · rolling 1h
        </div>
      </div>

      {samples.length < 2 ? (
        <div
          style={{
            color: T.label,
            fontSize: 11,
            padding: 16,
            textAlign: 'center',
          }}
        >
          collecting…
        </div>
      ) : (
        <>
          <svg
            width="100%"
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            style={{ display: 'block' }}
          >
            {/* 0.5 midline */}
            <line
              x1={0}
              y1={midY}
              x2={W}
              y2={midY}
              stroke={T.border}
              strokeDasharray="2 3"
              strokeWidth={0.5}
            />
            {/* p_up dim reference */}
            <path d={pUp} fill="none" stroke={T.label} strokeWidth={0.8} opacity={0.45} />
            {/* p_lgb cyan */}
            <path d={pLgb} fill="none" stroke="#06b6d4" strokeWidth={1.3} />
            {/* p_cls magenta */}
            <path d={pCls} fill="none" stroke="#ec4899" strokeWidth={1.3} />
          </svg>

          <div
            style={{
              display: 'flex',
              gap: 14,
              marginTop: 8,
              fontSize: 10,
              fontFamily: T.font,
              color: T.label2,
            }}
          >
            <span>
              <b style={{ color: '#06b6d4' }}>— p_lgb</b>{' '}
              <span style={{ color: T.text }}>
                {latest?.probability_lgb?.toFixed(3) ?? '—'}
              </span>
            </span>
            <span>
              <b style={{ color: '#ec4899' }}>— p_cls</b>{' '}
              <span style={{ color: T.text }}>
                {latest?.probability_classifier?.toFixed(3) ?? '—'}
              </span>
            </span>
            <span>
              <b style={{ color: T.label }}>— p_up</b>{' '}
              <span style={{ color: T.text }}>
                {latest?.probability_up?.toFixed(3) ?? '—'}
              </span>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
