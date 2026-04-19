import React, { useEffect, useRef, useState } from 'react';
import { T } from '../../theme/tokens.js';
import { parseModelVersion, formatModelDate } from './modelVersion.js';

// Circuit-breaker / model-version card.
// V5_ENSEMBLE_PATH1 is NOT exposed directly — derive:
//   mode == "blend" && weights.path1 > 0
// "Last switch" is a client-side observation log (session-only).

function Row({ label, value, tone }) {
  const color = tone === 'good' ? '#10b981'
    : tone === 'warn' ? '#f59e0b'
    : tone === 'bad' ? '#ef4444'
    : T.text;
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
      padding: '5px 0', borderBottom: `1px solid ${T.border}`,
      fontSize: 11, fontFamily: T.font,
    }}>
      <span style={{ color: T.label, letterSpacing: '0.04em' }}>{label}</span>
      <span style={{ color, fontWeight: 600, textAlign: 'right' }}>{value}</span>
    </div>
  );
}

function humanDuration(ms) {
  if (ms == null || ms < 0) return '—';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  return `${Math.floor(h / 24)}d ${h % 24}h ago`;
}

export default function CircuitBreakerCard({ snapshot }) {
  const tf5 = snapshot?.timescales?.['5m'];
  const ensemble = tf5?.ensemble_config;

  // Derive path1 enabled
  let path1Tone = 'bad';
  let path1Value = 'UNKNOWN';
  if (ensemble === null) {
    path1Value = 'OFF (ensemble_config null)';
    path1Tone = 'bad';
  } else if (ensemble) {
    const on = ensemble.mode === 'blend' && (ensemble.weights?.path1 ?? 0) > 0;
    path1Value = on ? 'ON' : 'OFF';
    path1Tone = on ? 'good' : 'warn';
  }

  // Mode-switch log — session-only ref. Each mode transition stamped.
  const logRef = useRef([]); // [{ mode, ts }]
  const [lastSwitch, setLastSwitch] = useState(null);
  useEffect(() => {
    if (!tf5) return;
    const mode = ensemble === null ? 'null' : (ensemble?.mode ?? 'null');
    const log = logRef.current;
    const prev = log[log.length - 1];
    if (!prev || prev.mode !== mode) {
      const entry = { mode, ts: Date.now() };
      log.push(entry);
      if (log.length > 200) log.splice(0, log.length - 200);
      setLastSwitch(entry);
    }
  }, [tf5, ensemble]);

  const mv = parseModelVersion(tf5?.model_version);

  // Timescales active: check which tf keys are present + have non-null p_up
  const timescales = snapshot?.timescales
    ? Object.keys(snapshot.timescales).map((k) => {
        const t = snapshot.timescales[k];
        const healthy = t.probability_up != null && t.ensemble_config !== null;
        return { key: k, healthy };
      })
    : [];

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
          CIRCUIT BREAKER / MODEL VERSION
        </div>
        <span style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>10s</span>
      </div>

      <Row label="V5_ENSEMBLE_PATH1" value={path1Value} tone={path1Tone} />
      <Row
        label="ensemble mode"
        value={ensemble == null
          ? 'null'
          : `${ensemble.mode}  (lgb=${ensemble.weights?.lgb ?? '—'}/path1=${ensemble.weights?.path1 ?? '—'})`}
        tone={ensemble?.mode === 'blend' ? 'good' : ensemble?.mode === 'fallback_lgb_only' ? 'warn' : 'bad'}
      />
      {mv ? (
        <>
          <Row label="LoRA adapter" value={`${mv.hash}  ·  ${formatModelDate(mv.date)}`} />
          <Row label="Classifier head" value={`v${mv.reg}/${mv.asset}/${mv.tf_id}`} />
        </>
      ) : (
        <Row label="model_version" value={
          <code style={{ fontSize: 10, color: T.label2 }}>{tf5?.model_version ?? '—'}</code>
        } tone="warn" />
      )}

      <Row
        label="Timescales active"
        value={timescales.length === 0 ? '—' : timescales.map(({ key, healthy }) => (
          <span key={key} style={{
            display: 'inline-flex', alignItems: 'center', gap: 3, marginLeft: 6,
            color: healthy ? '#10b981' : '#f59e0b',
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: healthy ? '#10b981' : '#f59e0b',
              display: 'inline-block',
            }} />
            {key}
          </span>
        ))}
      />
      <Row
        label="Last mode switch"
        value={lastSwitch
          ? `${lastSwitch.mode}  ·  ${humanDuration(Date.now() - lastSwitch.ts)}`
          : 'no change observed'}
      />
    </div>
  );
}
