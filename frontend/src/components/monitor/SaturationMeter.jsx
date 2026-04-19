import React, { useSyncExternalStore, useMemo } from 'react';
import { T } from '../../theme/tokens.js';

// Prominent KPI for path1 classifier saturation. Surfaces three numbers the
// user asked for: current p_cls, bimodal fraction over the rolling buffer,
// and the "stuck at edges" streak length (count of consecutive recent ticks
// with p_cls <= 0.01 OR >= 0.99).
//
// Thresholds per spec §8 / §5.1:
//   bimodal_frac > 0.70 → amber
//   bimodal_frac > 0.85 → red

const EDGE_LO = 0.01;
const EDGE_HI = 0.99;
const AMBER_THRESHOLD = 0.70;
const RED_THRESHOLD = 0.85;

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

function computeMetrics(samples) {
  let n = 0;
  let atEdge = 0;
  let streak = 0;
  // Walk from newest sample backward to find current streak
  for (let i = samples.length - 1; i >= 0; i -= 1) {
    const v = samples[i].probability_classifier;
    if (v == null) break;
    const edge = v <= EDGE_LO || v >= EDGE_HI;
    if (edge) streak += 1;
    else break;
  }
  // Rolling totals
  for (const s of samples) {
    if (s.probability_classifier == null) continue;
    n += 1;
    const v = s.probability_classifier;
    if (v <= EDGE_LO || v >= EDGE_HI) atEdge += 1;
  }
  const current = samples.length
    ? samples[samples.length - 1].probability_classifier
    : null;
  const frac = n > 0 ? atEdge / n : 0;
  return { current, atEdge, n, frac, streak };
}

function tierColor(frac) {
  if (frac >= RED_THRESHOLD) return T.loss || '#ef4444';
  if (frac >= AMBER_THRESHOLD) return T.warn || '#f59e0b';
  return T.profit || '#10b981';
}

function tierLabel(frac) {
  if (frac >= RED_THRESHOLD) return 'RED';
  if (frac >= AMBER_THRESHOLD) return 'AMBER';
  return 'OK';
}

export default function SaturationMeter({ buffer }) {
  useSyncExternalStore(subscribe(buffer), getSnapshot(buffer));
  const samples = buffer.snapshot();
  const { current, atEdge, n, frac, streak } = useMemo(
    () => computeMetrics(samples),
    // version-change already drives rerender; samples derived from it
    [buffer.getVersion()], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const color = tierColor(frac);
  const currentPegged =
    current != null && (current <= EDGE_LO || current >= EDGE_HI);

  return (
    <div
      data-testid="saturation-meter"
      style={{
        background: T.card,
        border: `1px solid ${T.border}`,
        padding: 14,
        borderRadius: 2,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          marginBottom: 10,
        }}
      >
        <div style={{ fontSize: 12 }}>Classifier saturation</div>
        <div
          style={{
            fontSize: 10,
            letterSpacing: '0.12em',
            color,
            fontWeight: 600,
          }}
        >
          {tierLabel(frac)}
        </div>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr 1fr',
          gap: 12,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        <div>
          <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>
            P_CLS NOW
          </div>
          <div
            style={{
              fontSize: 22,
              color: currentPegged ? color : T.text,
              fontWeight: 500,
            }}
          >
            {current == null ? '—' : current.toFixed(3)}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            {currentPegged ? 'pegged at edge' : 'interior'}
          </div>
        </div>

        <div>
          <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>
            BIMODAL · 1h
          </div>
          <div style={{ fontSize: 22, color, fontWeight: 500 }}>
            {n === 0 ? '—' : `${(frac * 100).toFixed(1)}%`}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            {atEdge}/{n} at edges
          </div>
        </div>

        <div>
          <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>
            CURRENT STREAK
          </div>
          <div
            style={{
              fontSize: 22,
              color: streak > 10 ? color : T.text,
              fontWeight: 500,
            }}
          >
            {streak}
          </div>
          <div style={{ fontSize: 10, color: T.label }}>
            {streak > 0 ? 'consecutive pegged ticks' : 'not stuck'}
          </div>
        </div>
      </div>

      {frac >= AMBER_THRESHOLD && (
        <div
          style={{
            marginTop: 10,
            padding: '6px 10px',
            background: `${color}22`,
            borderLeft: `2px solid ${color}`,
            fontSize: 11,
            color,
          }}
        >
          {frac >= RED_THRESHOLD
            ? `Classifier saturated — ${(frac * 100).toFixed(0)}% of last hour pegged at 0 or 1. Bimodal sigmoid — treat p_up as LGB-only until resolved.`
            : `Warning — ${(frac * 100).toFixed(0)}% pegged in last hour. Classifier signal degraded.`}
        </div>
      )}
    </div>
  );
}

export { computeMetrics, EDGE_LO, EDGE_HI, AMBER_THRESHOLD, RED_THRESHOLD };
