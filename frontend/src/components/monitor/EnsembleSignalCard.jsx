import React from 'react';
import { T } from '../../theme/tokens.js';

// Hero card — p_up / p_lgb / p_cls bars, disagreement + mode + conviction badges.
// Renders from the *current* snapshot prop; polling is done upstream in useSnapshotStream.

// Colour interpolation: red → grey → green across [0, 0.5, 1]
function probColor(p) {
  if (p == null) return T.label;
  // HSL from 0=red (0°) → 60=yellow → 120=green
  const hue = Math.max(0, Math.min(1, p)) * 120;
  return `hsl(${hue.toFixed(0)}, 65%, 48%)`;
}

function ProbBar({ label, value, saturationAlert }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(1, value)) * 100;
  const showNull = value == null;
  const isSaturated = saturationAlert && value != null && (value <= 0.01 || value >= 0.99);
  const color = showNull ? T.label : probColor(value);
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: T.label, marginBottom: 4 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {label}
          {isSaturated ? (
            <span title="classifier saturated (edge bin)" style={{
              display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: '#f59e0b',
            }} />
          ) : null}
        </span>
        <span style={{ color: T.text, fontFamily: T.font, fontWeight: 600 }}>
          {showNull ? '— null' : value.toFixed(4)}
        </span>
      </div>
      <div style={{
        position: 'relative',
        height: 8,
        background: 'rgba(255,255,255,0.04)',
        borderRadius: 2,
        overflow: 'hidden',
      }}>
        <div style={{
          position: 'absolute', left: 0, top: 0, bottom: 0,
          width: `${pct}%`,
          background: color,
          transition: 'width 0.4s ease, background 0.4s',
        }} />
        {/* 0.5 midline */}
        <div style={{
          position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1,
          background: 'rgba(255,255,255,0.15)',
        }} />
      </div>
    </div>
  );
}

function Badge({ children, color, title }) {
  return (
    <span
      title={title}
      style={{
        fontSize: 9,
        fontWeight: 700,
        letterSpacing: '0.1em',
        padding: '2px 6px',
        borderRadius: 2,
        border: `1px solid ${color}`,
        color,
        fontFamily: T.font,
        textTransform: 'uppercase',
      }}
    >{children}</span>
  );
}

function modeColor(mode) {
  if (mode === 'blend') return '#10b981';
  if (mode === 'fallback_lgb_only') return '#f59e0b';
  return '#ef4444'; // null / "disabled"
}

function convictionColor(c) {
  if (c === 'STRONG') return '#10b981';
  if (c === 'MODERATE') return '#06b6d4';
  if (c === 'WEAK') return '#f59e0b';
  return T.label;
}

function disagreementColor(mag, detected) {
  if (mag == null) return T.label;
  if (mag > 0.6 && detected) return '#ef4444';
  if (mag > 0.3) return '#f59e0b';
  return '#10b981';
}

export default function EnsembleSignalCard({ snapshot, error, lastFetchTs }) {
  const tf = snapshot?.timescales?.['5m'];
  const ensemble = tf?.ensemble_config;
  const stale = lastFetchTs > 0 && Date.now() - lastFetchTs > 5000;

  const disabled = tf && ensemble === null;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 4,
      padding: 14,
      minHeight: 260,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div style={{ fontSize: 11, color: T.label2, fontWeight: 600, letterSpacing: '0.08em' }}>
          ENSEMBLE SIGNAL · BTC 5m
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>2s</span>
          {stale ? <span title={`stale ${Math.floor((Date.now() - lastFetchTs) / 1000)}s`} style={{
            fontSize: 9, color: '#f59e0b', padding: '1px 6px', border: '1px solid #f59e0b', borderRadius: 2,
          }}>STALE</span> : null}
          {error ? <span title={error} style={{
            fontSize: 9, color: '#ef4444', padding: '1px 6px', border: '1px solid #ef4444', borderRadius: 2,
          }}>ERR</span> : null}
        </div>
      </div>

      {!tf ? (
        <div style={{ color: T.label, fontSize: 11 }}>waiting for first snapshot…</div>
      ) : disabled ? (
        <div style={{
          padding: 12, borderRadius: 2,
          border: '1px solid #ef4444',
          color: '#ef4444',
          fontSize: 12, fontWeight: 700, letterSpacing: '0.06em',
        }}>
          ENSEMBLE DISABLED — ensemble_config is null.
          <div style={{ fontSize: 10, marginTop: 4, color: T.label2, fontWeight: 400 }}>
            status={tf.status ?? 'unknown'} · classifier likely fell back
          </div>
        </div>
      ) : (
        <>
          <ProbBar label="p_up" value={tf.probability_up} />
          <ProbBar label="p_lgb" value={tf.probability_lgb} />
          <ProbBar label="p_cls" value={tf.probability_classifier} saturationAlert />

          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 14 }}>
            <Badge
              color={disagreementColor(ensemble?.disagreement_magnitude, ensemble?.disagreement_detected)}
              title={`magnitude=${ensemble?.disagreement_magnitude?.toFixed(3) ?? '—'} detected=${ensemble?.disagreement_detected ?? '—'}`}
            >
              disagree {ensemble?.disagreement_magnitude?.toFixed(2) ?? '—'}
            </Badge>
            <Badge color={modeColor(ensemble?.mode)} title={`weights lgb=${ensemble?.weights?.lgb ?? '—'} path1=${ensemble?.weights?.path1 ?? '—'}`}>
              mode {ensemble?.mode ?? 'null'}
            </Badge>
            <Badge color={convictionColor(tf.conviction)} title={`score=${tf.conviction_score?.toFixed(3) ?? '—'}`}>
              {tf.conviction ?? 'NONE'} · {tf.conviction_score?.toFixed(2) ?? '0.00'}
            </Badge>
          </div>
        </>
      )}
    </div>
  );
}
