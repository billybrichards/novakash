import React, { useSyncExternalStore, useMemo } from 'react';
import { T } from '../../theme/tokens.js';

const MODE_COLORS = {
  blend: '#10b981',
  fallback_lgb_only: '#f59e0b',
  disabled: '#ef4444',
  null: '#6b7280',
};

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

export default function ModeShareCard({ buffer }) {
  const sub = useMemo(() => subscribe(buffer), [buffer]);
  const snap = useMemo(() => getSnapshot(buffer), [buffer]);
  useSyncExternalStore(sub, snap, snap);

  const samples = buffer.snapshot();
  const counts = useMemo(() => {
    const c = {};
    for (const s of samples) {
      const key = s.mode == null ? 'null' : s.mode;
      c[key] = (c[key] || 0) + 1;
    }
    return c;
  }, [samples, buffer.getVersion()]);

  const total = samples.length;
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const fallbackFrac = (counts.fallback_lgb_only || 0) / (total || 1);
  const fallbackTone = fallbackFrac > 0.50 ? '#ef4444' : fallbackFrac > 0.20 ? '#f59e0b' : '#10b981';

  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 4,
      padding: 14, minHeight: 200,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <div style={{ fontSize: 11, color: T.label2, fontWeight: 600, letterSpacing: '0.08em' }}>
          ensemble_config.mode share
        </div>
        <div style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>
          n={total} · rolling 1h
        </div>
      </div>

      {total === 0 ? (
        <div style={{ color: T.label, fontSize: 11, padding: 16, textAlign: 'center' }}>collecting…</div>
      ) : (
        <>
          {/* Stacked horizontal bar */}
          <div style={{ display: 'flex', height: 14, borderRadius: 2, overflow: 'hidden', marginBottom: 14 }}>
            {entries.map(([mode, n]) => (
              <div key={mode} title={`${mode}: ${n}/${total}`} style={{
                background: MODE_COLORS[mode] ?? '#6b7280',
                width: `${(n / total) * 100}%`,
              }} />
            ))}
          </div>
          {entries.map(([mode, n]) => (
            <div key={mode} style={{
              display: 'flex', justifyContent: 'space-between',
              fontSize: 10, fontFamily: T.font, color: T.text,
              padding: '3px 0',
            }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{
                  width: 8, height: 8, borderRadius: '50%',
                  background: MODE_COLORS[mode] ?? '#6b7280',
                }} />
                {mode}
              </span>
              <span style={{ color: T.label2 }}>
                {((n / total) * 100).toFixed(1)}% · {n}
              </span>
            </div>
          ))}
          <div style={{ marginTop: 10, fontSize: 10, fontFamily: T.font, color: fallbackTone }}>
            fallback_lgb_only: <b>{(fallbackFrac * 100).toFixed(1)}%</b>
            {fallbackFrac > 0.50 ? ' — ensemble mostly degraded'
              : fallbackFrac > 0.20 ? ' — fallback rising'
              : ''}
          </div>
        </>
      )}
    </div>
  );
}
