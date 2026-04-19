import React, { useSyncExternalStore, useMemo } from 'react';
import { T } from '../../theme/tokens.js';

const BIN_COUNT = 10;
const MIN_SAMPLES = 50;

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

/**
 * Bin probability_classifier values from the ring buffer into BIN_COUNT bins
 * on [0, 1]. Values at exactly 1.0 land in the last bin (inclusive upper
 * edge), which is the common bug-case we care about.
 */
function binClassifier(samples) {
  const bins = new Array(BIN_COUNT).fill(0);
  let total = 0;
  let satEdge = 0;
  for (const s of samples) {
    const v = s.probability_classifier;
    if (v == null) continue;
    total += 1;
    if (v <= 0.01 || v >= 0.99) satEdge += 1;
    let idx = Math.floor(v * BIN_COUNT);
    if (idx >= BIN_COUNT) idx = BIN_COUNT - 1;
    if (idx < 0) idx = 0;
    bins[idx] += 1;
  }
  return { bins, total, satEdge };
}

export default function ClassifierHistogram({ buffer }) {
  const sub = useMemo(() => subscribe(buffer), [buffer]);
  const snap = useMemo(() => getSnapshot(buffer), [buffer]);
  useSyncExternalStore(sub, snap, snap);

  const samples = buffer.snapshot();
  const { bins, total, satEdge } = useMemo(() => binClassifier(samples), [samples, buffer.getVersion()]);

  const max = Math.max(1, ...bins);
  const bimodalFrac = total > 0 ? satEdge / total : 0;
  const bimodalTone = bimodalFrac > 0.70 ? '#ef4444' : bimodalFrac > 0.30 ? '#f59e0b' : '#10b981';

  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 4,
      padding: 14,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ fontSize: 11, color: T.label2, fontWeight: 600, letterSpacing: '0.08em' }}>
          p_classifier · 10-bin histogram
        </div>
        <div style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>
          n={total} · rolling 1h
        </div>
      </div>

      {total < MIN_SAMPLES ? (
        <div style={{ color: T.label, fontSize: 11, fontFamily: T.font, padding: 16, textAlign: 'center' }}>
          collecting ({total}/{MIN_SAMPLES})…
        </div>
      ) : (
        <>
          <div style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${BIN_COUNT}, 1fr)`,
            gap: 2, alignItems: 'end', height: 120,
            fontFamily: T.font,
          }}>
            {bins.map((count, i) => {
              const pct = (count / max) * 100;
              const edge = (i === 0 || i === BIN_COUNT - 1);
              const color = edge && count / total > 0.15 ? '#f59e0b' : T.cyan;
              return (
                <div key={i} title={`${(i / BIN_COUNT).toFixed(1)}–${((i + 1) / BIN_COUNT).toFixed(1)} : ${count}`} style={{
                  background: color, height: `${Math.max(2, pct)}%`, opacity: count === 0 ? 0.1 : 1,
                  borderRadius: 1,
                }} />
              );
            })}
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 9, color: T.label, fontFamily: T.font }}>
            <span>0.0</span><span>0.5</span><span>1.0</span>
          </div>
          <div style={{ marginTop: 10, fontSize: 10, fontFamily: T.font, color: bimodalTone }}>
            edge-bin fraction: <b>{(bimodalFrac * 100).toFixed(1)}%</b>
            {bimodalFrac > 0.70 ? ' — bimodal saturation'
              : bimodalFrac > 0.30 ? ' — warming'
              : ''}
          </div>
        </>
      )}
    </div>
  );
}
