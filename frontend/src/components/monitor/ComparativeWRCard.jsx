import React, { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from '../../theme/tokens.js';

// Comparative counterfactual WR between the three probability sources —
// p_lgb alone, p_classifier alone, and the final p_up ensemble. Joins the
// rolling snapshot buffer (which carries p_lgb / p_cls / p_up per window_ts)
// with resolved strategy_decisions (carries outcome via window_snapshots
// shadow — see migration 20260419_01).
//
// A source is treated as "would have won" on a window when its probability
// and the resolved direction agree: p > 0.5 + direction == UP, or p < 0.5 +
// direction == DOWN. p == 0.5 or null → abstain (not counted either way).

const POLL_MS = 15000; // decisions refresh — slower than buffer
const MIN_FOR_WR = 3;

function subscribe(buffer) {
  return (cb) => buffer.subscribe(cb);
}

function getSnapshot(buffer) {
  return () => buffer.getVersion();
}

// One row per window_ts with the most recent probabilities we observed for
// that window. (Same window can appear multiple times in the 2s buffer as
// eval-offset marches toward T-0; we keep the latest.)
function dedupeByWindow(samples) {
  const map = new Map();
  for (const s of samples) {
    if (s.window_ts == null) continue;
    map.set(s.window_ts, s);
  }
  return map;
}

function verdictFor(p, actual) {
  if (p == null || Number.isNaN(p)) return null;
  if (actual !== 'UP' && actual !== 'DOWN') return null;
  const predictUp = p > 0.5;
  if (p === 0.5) return null;
  const correct = (predictUp && actual === 'UP') || (!predictUp && actual === 'DOWN');
  return correct ? 'WIN' : 'LOSS';
}

function summarise(results) {
  const settled = results.filter((r) => r != null);
  const wins = settled.filter((r) => r === 'WIN').length;
  const losses = settled.filter((r) => r === 'LOSS').length;
  return {
    n: settled.length,
    wins,
    losses,
    wr: settled.length > 0 ? wins / settled.length : null,
  };
}

function wrColor(wr) {
  if (wr == null) return T.label;
  if (wr >= 0.6) return T.profit || '#10b981';
  if (wr >= 0.5) return T.text;
  return T.loss || '#ef4444';
}

export default function ComparativeWRCard({ buffer }) {
  const api = useApi();
  useSyncExternalStore(subscribe(buffer), getSnapshot(buffer));

  const [decisions, setDecisions] = useState([]);
  const [error, setError] = useState(null);
  const [loadingFirst, setLoadingFirst] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const fetchDecisions = async () => {
      try {
        const res = await api.get(
          '/api/v58/strategy-decisions?strategy_id=v5_ensemble&timeframe=5m&limit=500',
        );
        if (cancelled) return;
        const rows = (res?.data ?? res)?.decisions ?? [];
        setDecisions(rows);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(e.message || 'decisions fetch failed');
      } finally {
        if (!cancelled) setLoadingFirst(false);
      }
    };

    fetchDecisions();
    const t = setInterval(fetchDecisions, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [api]);

  const samples = buffer.snapshot();
  const rows = useMemo(() => {
    const byWindow = dedupeByWindow(samples);
    // A window is scoreable iff the resolved decision has `outcome != null`
    // AND the buffer has a sample for that same window_ts (so we have p_lgb /
    // p_cls captured at snapshot time).
    //
    // Derive actual direction from: if the decision's own `direction` ==
    // 'UP' and outcome == 'WIN' → actual UP; 'UP' + 'LOSS' → actual DOWN;
    // same mirrored for 'DOWN'. Works because the shadow view computes
    // outcome from direction vs actual_direction (migration 20260419_01).
    const out = [];
    for (const d of decisions) {
      if (d.outcome !== 'WIN' && d.outcome !== 'LOSS') continue;
      if (d.direction !== 'UP' && d.direction !== 'DOWN') continue;
      const w = byWindow.get(d.window_ts);
      if (!w) continue; // no buffer sample for this window — skip
      const actual =
        (d.outcome === 'WIN' && d.direction) ||
        (d.outcome === 'LOSS' && (d.direction === 'UP' ? 'DOWN' : 'UP'));
      out.push({
        window_ts: d.window_ts,
        actual,
        p_lgb: w.probability_lgb,
        p_cls: w.probability_classifier,
        p_up: w.probability_up,
        lgbVerdict: verdictFor(w.probability_lgb, actual),
        clsVerdict: verdictFor(w.probability_classifier, actual),
        upVerdict: verdictFor(w.probability_up, actual),
      });
    }
    return out;
  }, [decisions, buffer.getVersion()]); // eslint-disable-line react-hooks/exhaustive-deps

  const lgb = summarise(rows.map((r) => r.lgbVerdict));
  const cls = summarise(rows.map((r) => r.clsVerdict));
  const up = summarise(rows.map((r) => r.upVerdict));

  const bufferWindowCount = useMemo(
    () => dedupeByWindow(samples).size,
    [buffer.getVersion()], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const resolvedCount = decisions.filter(
    (d) => d.outcome === 'WIN' || d.outcome === 'LOSS',
  ).length;

  return (
    <div
      data-testid="comparative-wr"
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
        <div style={{ fontSize: 12 }}>Counterfactual WR · by source</div>
        <div style={{ fontSize: 10, color: T.label }}>
          {rows.length} scoreable · {resolvedCount} resolved · {bufferWindowCount} buffered
        </div>
      </div>

      {error && (
        <div style={{ color: T.loss, fontSize: 11, marginBottom: 8 }}>
          {error}
        </div>
      )}

      {loadingFirst ? (
        <div style={{ color: T.label, fontSize: 11, padding: 12 }}>
          loading decisions…
        </div>
      ) : rows.length === 0 ? (
        <div style={{ color: T.label, fontSize: 11, padding: 12 }}>
          No overlap yet between buffer samples and resolved decisions. The
          buffer is session-only (~1h); a resolved v5_ensemble window whose
          snapshot was captured since you opened this page will populate.
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr
              style={{
                color: T.label,
                fontSize: 10,
                letterSpacing: '0.1em',
              }}
            >
              <th style={{ textAlign: 'left', padding: '6px 10px' }}>SOURCE</th>
              <th style={{ textAlign: 'right', padding: '6px 10px' }}>WIN</th>
              <th style={{ textAlign: 'right', padding: '6px 10px' }}>LOSS</th>
              <th style={{ textAlign: 'right', padding: '6px 10px' }}>WR</th>
            </tr>
          </thead>
          <tbody>
            {[
              { label: 'LGB only', color: '#06b6d4', s: lgb },
              { label: 'Classifier only', color: '#ec4899', s: cls },
              { label: 'Ensemble (p_up)', color: T.profit || '#10b981', s: up },
            ].map(({ label, color, s }) => (
              <tr key={label} style={{ borderTop: `1px solid ${T.border}` }}>
                <td style={{ padding: '6px 10px', color }}>{label}</td>
                <td
                  style={{
                    textAlign: 'right',
                    padding: '6px 10px',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {s.wins}
                </td>
                <td
                  style={{
                    textAlign: 'right',
                    padding: '6px 10px',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {s.losses}
                </td>
                <td
                  style={{
                    textAlign: 'right',
                    padding: '6px 10px',
                    color: wrColor(s.wr),
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {s.n < MIN_FOR_WR
                    ? `— (n=${s.n})`
                    : `${(s.wr * 100).toFixed(1)}% (n=${s.n})`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div
        style={{
          marginTop: 10,
          fontSize: 10,
          color: T.label,
          lineHeight: 1.5,
        }}
      >
        Session-only — buffer holds ~1h of snapshots; rows grow as new
        v5_ensemble windows resolve. WR = (p&gt;0.5 &amp; actual=UP) or
        (p&lt;0.5 &amp; actual=DOWN) per source.
      </div>
    </div>
  );
}

export { dedupeByWindow, verdictFor, summarise };
