// /desk — ETH/SOL/XRP sparklines.
//
// Tiny SVG spark per asset — last ~5 minutes of Chainlink price. Renders
// the same /api/ticks/chainlink endpoint with a different asset query.
// We limit the fetch to 60 rows (≈5m at 1Hz-ish) so we aren't paying for
// a second 300-row pull per asset per poll.

import React, { useEffect, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';

const ASSETS = ['ETH', 'SOL', 'XRP'];

export default function CrossAssetSparks({ pollMs = 10_000 }) {
  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 8 }}>
        CROSS-ASSET · ETH / SOL / XRP
      </div>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {ASSETS.map(a => (
          <Spark key={a} asset={a} pollMs={pollMs} />
        ))}
      </div>
    </div>
  );
}

function Spark({ asset, pollMs }) {
  const api = useApi();
  const [ticks, setTicks] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get(`/api/ticks/chainlink?asset=${encodeURIComponent(asset)}&limit=60`);
        const body = r?.data ?? r;
        const rows = Array.isArray(body?.rows) ? body.rows : Array.isArray(body) ? body : [];
        if (!cancelled) {
          setTicks(rows);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e.message || 'error');
      }
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, asset, pollMs]);

  const W = 160, H = 40;
  const prices = ticks.map(r => Number(r.price)).filter(n => Number.isFinite(n));
  let body;
  if (error && !prices.length) {
    body = <span style={{ fontSize: 10, color: T.loss }}>error</span>;
  } else if (prices.length < 2) {
    body = <span style={{ fontSize: 10, color: T.label }}>—</span>;
  } else {
    const min = Math.min(...prices);
    const max = Math.max(...prices);
    const span = Math.max(1e-9, max - min);
    const last = prices[prices.length - 1];
    const first = prices[0];
    const d = (last - first) / first;
    const pts = prices.map((p, i) => {
      const x = (i / (prices.length - 1)) * W;
      const y = H - ((p - min) / span) * H;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const colour = d >= 0 ? T.profit : T.loss;
    body = (
      <>
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }}>
          <path d={pts} fill="none" stroke={colour} strokeWidth={1.2} />
        </svg>
        <span style={{ fontSize: 10, color: colour }}>
          {d >= 0 ? '+' : ''}{(d * 100).toFixed(2)}%
        </span>
      </>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 10, color: T.label2, letterSpacing: '0.1em' }}>{asset}</div>
      {body}
    </div>
  );
}
