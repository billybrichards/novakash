// /desk — pu / pc / pl / pl_v9_1 classifier scorecard.
//
// Mini-tiles for each probability plotted as a 0..1 bar with reference
// ticks at 0.50 (direction split) and the v8/v9 fill-band edges (0.00,
// 0.82). Below them: |pc − pl| disagreement vs the v9 veto threshold,
// and (when /v4/snapshot publishes it) the v9.1 retrained LGB head
// alongside the v9 PROD LGB so the operator can eyeball the head-to-head
// agreement before promoting v9.1 from GHOST.
//
// Each tile carries a tiny direction-tier pill (DOWN / NEUTRAL / UP)
// derived from the bar's distance from 0.5.

import React from 'react';
import { T } from '../../../theme/tokens.js';

const FILL_BAND_LOW = 0.00;
const FILL_BAND_HIGH = 0.82;
const VETO_THRESHOLD = 0.25;
const NEUTRAL_BAND = 0.05; // ±0.05 around 0.5

export default function ClassifierScorecard({ pu, pc, pl, plV91, conflict }) {
  const gap = (pc != null && pl != null) ? Math.abs(pc - pl) : null;
  const v91Gap = (pl != null && plV91 != null) ? Math.abs(plV91 - pl) : null;
  const showV91 = plV91 != null && Number.isFinite(plV91);

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        CLASSIFIER · pu / pc / pl{showV91 ? ' / pl(v9.1)' : ''} · |pc−pl|
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(2, 1fr)',
        gap: 8,
      }}>
        <ProbTile
          label="Consensus (pu)"
          hint="ensemble probability — what the engine trades on"
          value={pu}
        />
        <ProbTile
          label="Classifier (pc)"
          hint={pc == null ? 'classifier warming up' : 'v2 classifier head'}
          value={pc}
        />
        <ProbTile
          label="LGB v9 (pl)"
          hint="v9 PROD LightGBM head"
          value={pl}
        />
        <GapTile gap={gap} conflict={conflict} />
        {showV91 ? (
          <>
            <ProbTile
              label="LGB v9.1 (pl_v9_1)"
              hint="v9.1 retrain · priceToBeat-aligned · GHOST"
              value={plV91}
              accent="#fb7185"
            />
            <V91DiffTile diff={v91Gap} v9={pl} v91={plV91} />
          </>
        ) : null}
      </div>
    </div>
  );
}

function ProbTile({ label, hint, value, accent }) {
  const v = Number.isFinite(value) ? value : null;
  const tier = tierFor(v);
  return (
    <div style={accent ? { ...tileStyle, borderColor: accent } : tileStyle}>
      <div style={tileHeaderRow}>
        <span style={{ color: T.label2, fontSize: 10 }}>{label}</span>
        <DirectionPill tier={tier} />
      </div>
      <div style={tileValueRow}>
        <span style={{
          fontSize: 18,
          fontWeight: 600,
          color: tierColor(tier),
          fontVariantNumeric: 'tabular-nums',
        }}>
          {v == null ? '—' : v.toFixed(3)}
        </span>
        <span style={{ color: T.label2, fontSize: 9 }}>{hint}</span>
      </div>
      <ProbBar value={v} />
    </div>
  );
}

// Compares v9 PROD LGB vs v9.1 retrain — operator's confidence-in-promotion
// signal. Disagree direction = pause; agree direction = green for go.
function V91DiffTile({ diff, v9, v91 }) {
  const sameDir = (v9 != null && v91 != null)
    ? ((v9 - 0.5) * (v91 - 0.5) > 0)
    : null;
  const dirColor = sameDir === true ? T.profit : sameDir === false ? T.loss : T.label2;
  return (
    <div style={{ ...tileStyle, borderColor: '#fb7185' }}>
      <div style={tileHeaderRow}>
        <span style={{ color: T.label2, fontSize: 10 }}>v9 vs v9.1 agreement</span>
        <span style={pillStyle(dirColor)}>
          {sameDir === true ? 'AGREE' : sameDir === false ? 'DISAGREE' : '—'}
        </span>
      </div>
      <div style={tileValueRow}>
        <span style={{
          fontSize: 18,
          fontWeight: 600,
          color: dirColor,
          fontVariantNumeric: 'tabular-nums',
        }}>
          {diff == null ? '—' : `Δ ${diff.toFixed(3)}`}
        </span>
        <span style={{ color: T.label2, fontSize: 9 }}>
          {v9 != null && v91 != null
            ? `v9 ${v9.toFixed(2)} → v9.1 ${v91.toFixed(2)}`
            : 'v9.1 model not loaded'}
        </span>
      </div>
    </div>
  );
}

function GapTile({ gap, conflict }) {
  const veto = gap != null && gap > VETO_THRESHOLD;
  const colour = veto ? T.loss : (gap != null && gap > 0.10 ? T.warn : T.text);
  return (
    <div style={{ ...tileStyle, borderColor: veto ? T.loss : T.border }}>
      <div style={tileHeaderRow}>
        <span style={{ color: T.label2, fontSize: 10 }}>|pc − pl| disagreement</span>
        {(veto || conflict) ? (
          <span style={pillStyle(T.loss)}>CONFLICT</span>
        ) : (
          <span style={pillStyle(T.label2)}>OK</span>
        )}
      </div>
      <div style={tileValueRow}>
        <span style={{
          fontSize: 18,
          fontWeight: 600,
          color: colour,
          fontVariantNumeric: 'tabular-nums',
        }}>
          {gap == null ? '—' : gap.toFixed(3)}
        </span>
        <span style={{ color: T.label2, fontSize: 9 }}>
          v9 veto at &gt; {VETO_THRESHOLD.toFixed(2)}
        </span>
      </div>
      <GapBar gap={gap} />
    </div>
  );
}

function ProbBar({ value }) {
  const W = 200, H = 14;
  const xFor = v => Math.max(0, Math.min(1, v)) * W;
  const v = Number.isFinite(value) ? value : null;
  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`}
         preserveAspectRatio="none"
         style={{ display: 'block', marginTop: 6 }}>
      {/* track */}
      <rect x={0} y={H / 2 - 1} width={W} height={2} fill={T.border} />
      {/* fill-band shading */}
      <rect
        x={xFor(FILL_BAND_LOW)}
        y={2}
        width={xFor(FILL_BAND_HIGH) - xFor(FILL_BAND_LOW)}
        height={H - 4}
        fill={T.purple}
        fillOpacity={0.07}
      />
      {/* 0.50 reference */}
      <line x1={xFor(0.5)} x2={xFor(0.5)} y1={1} y2={H - 1}
            stroke={T.label2} strokeDasharray="2 2" />
      {/* fill-band edges */}
      <line x1={xFor(FILL_BAND_LOW)} x2={xFor(FILL_BAND_LOW)}
            y1={2} y2={H - 2} stroke={T.purple} strokeOpacity={0.5} />
      <line x1={xFor(FILL_BAND_HIGH)} x2={xFor(FILL_BAND_HIGH)}
            y1={2} y2={H - 2} stroke={T.purple} strokeOpacity={0.5} />
      {/* value marker */}
      {v != null ? (
        <circle cx={xFor(v)} cy={H / 2} r={4}
                fill={tierColor(tierFor(v))} stroke="#0b0b0e" strokeWidth={1} />
      ) : null}
    </svg>
  );
}

function GapBar({ gap }) {
  const W = 200, H = 14;
  const MAX = 0.5; // visual cap; 0.50 is already double the veto
  const xFor = v => Math.max(0, Math.min(1, v / MAX)) * W;
  const v = Number.isFinite(gap) ? gap : null;
  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`}
         preserveAspectRatio="none"
         style={{ display: 'block', marginTop: 6 }}>
      <rect x={0} y={H / 2 - 1} width={W} height={2} fill={T.border} />
      {/* veto threshold */}
      <line x1={xFor(VETO_THRESHOLD)} x2={xFor(VETO_THRESHOLD)}
            y1={1} y2={H - 1} stroke={T.loss} strokeDasharray="2 2" />
      {/* "soft warn" line at 0.10 */}
      <line x1={xFor(0.10)} x2={xFor(0.10)}
            y1={1} y2={H - 1} stroke={T.warn} strokeDasharray="1 3" />
      {v != null ? (
        <circle cx={xFor(v)} cy={H / 2} r={4}
                fill={v > VETO_THRESHOLD ? T.loss : (v > 0.10 ? T.warn : T.text)}
                stroke="#0b0b0e" strokeWidth={1} />
      ) : null}
    </svg>
  );
}

function tierFor(p) {
  if (p == null) return 'NONE';
  if (Math.abs(p - 0.5) < NEUTRAL_BAND) return 'NEUTRAL';
  return p > 0.5 ? 'UP' : 'DOWN';
}

function tierColor(tier) {
  if (tier === 'UP') return T.profit;
  if (tier === 'DOWN') return T.loss;
  if (tier === 'NEUTRAL') return T.label;
  return T.label2;
}

function DirectionPill({ tier }) {
  const colour = tierColor(tier);
  return <span style={pillStyle(colour)}>{tier}</span>;
}

const panelStyle = {
  border: `1px solid ${T.border}`,
  padding: 10,
  background: T.card,
  marginBottom: 12,
};

const headerStyle = {
  fontSize: 10,
  color: T.label,
  letterSpacing: '0.1em',
  marginBottom: 8,
};

const tileStyle = {
  border: `1px solid ${T.border}`,
  padding: 8,
  background: 'rgba(255,255,255,0.02)',
};

const tileHeaderRow = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
};

const tileValueRow = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'baseline',
  marginTop: 2,
};

const pillStyle = (colour) => ({
  border: `1px solid ${colour}`,
  color: colour,
  fontSize: 9,
  padding: '0 4px',
  borderRadius: 2,
  letterSpacing: '0.1em',
});
