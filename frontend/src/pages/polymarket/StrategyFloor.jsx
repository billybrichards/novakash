import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, utcHHMM } from './components/theme.js';

/**
 * StrategyFloor — Dedicated live dashboard for a single strategy.
 *
 * Factory-floor vibe: live price ticker, current window decision,
 * recent windows with "would have won" analysis, gate status.
 *
 * Parameterized by STRATEGY_CONFIG prop — same component renders
 * both v4_down_only and v4_up_asian tabs.
 *
 * Polls every 5s for real-time feel.
 */

const POLL_MS = 5000;

// ── Strategy configs ─────────────────────────────────────────────────────────

export const STRATEGY_CONFIGS = {
  v4_down_only: {
    id: 'v4_down_only',
    label: 'V4 DOWN-ONLY',
    color: '#10b981',
    colorDim: 'rgba(16,185,129,0.12)',
    direction: 'DOWN',
    gateLabel: 'DOWN filter · CLOB sizing · T-90-150',
    description: 'Trades DOWN signals at dist≥0.10 during T-90-150 window. CLOB-based sizing: 2.0x at ≥0.55, 1.2x at 0.35-0.55, skip <0.25.',
    thresholds: {
      minDist: 0.10,
      minOffset: 90,
      maxOffset: 150,
      clobSkip: 0.25,
    },
  },
  v4_up_asian: {
    id: 'v4_up_asian',
    label: 'V4 UP ASIAN',
    color: '#f59e0b',
    colorDim: 'rgba(245,158,11,0.12)',
    direction: 'UP',
    gateLabel: 'UP filter · Asian session · dist 0.15-0.20 · T-90-150',
    description: 'Trades UP signals at dist 0.15-0.20 during Asian session (23:00-02:59 UTC) in T-90-150 window.',
    thresholds: {
      minDist: 0.15,
      maxDist: 0.20,
      minOffset: 90,
      maxOffset: 150,
      asianHours: [23, 0, 1, 2],
    },
  },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function dirColor(d) {
  if (d === 'UP') return T.green;
  if (d === 'DOWN') return T.red;
  return T.textDim;
}

function parseTs(ts) {
  if (!ts) return 0;
  if (typeof ts === 'number') return ts;
  return Math.floor(new Date(ts).getTime() / 1000);
}

function fmtPrice(p) {
  if (p == null) return '\u2014';
  return '$' + Number(p).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function fmtMove(open, close) {
  if (open == null || close == null) return '\u2014';
  const move = close - open;
  const sign = move >= 0 ? '+' : '';
  return `${sign}${move.toFixed(0)}`;
}

// ── Window Lifecycle Bar ─────────────────────────────────────────────────────

function WindowLifecycleBar({ evalOffset, minOffset, maxOffset, color }) {
  // evalOffset = seconds remaining to window close
  // Bar: 300s total. EARLY (300-maxOffset) | SWEET SPOT (maxOffset-minOffset) | LATE (minOffset-0)
  const total = 300;
  const earlyPct = ((total - maxOffset) / total) * 100;
  const sweetPct = ((maxOffset - minOffset) / total) * 100;
  const latePct = (minOffset / total) * 100;

  // Current position
  const positionPct = evalOffset != null ? ((total - evalOffset) / total) * 100 : 0;
  const inSweet = evalOffset != null && evalOffset >= minOffset && evalOffset <= maxOffset;

  return (
    <div style={{ position: 'relative', height: 24, borderRadius: 4, overflow: 'hidden', background: 'rgba(15,23,42,0.6)', border: `1px solid ${T.border}` }}>
      {/* Segments */}
      <div style={{ display: 'flex', height: '100%' }}>
        <div style={{ width: `${earlyPct}%`, background: 'rgba(71,85,105,0.2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ fontSize: 7, color: T.textDim, letterSpacing: '0.08em' }}>EARLY</span>
        </div>
        <div style={{ width: `${sweetPct}%`, background: inSweet ? `${color}25` : `${color}10`, display: 'flex', alignItems: 'center', justifyContent: 'center', borderLeft: `1px solid ${color}40`, borderRight: `1px solid ${color}40` }}>
          <span style={{ fontSize: 7, color: inSweet ? color : T.textMuted, fontWeight: 700, letterSpacing: '0.1em' }}>T-{maxOffset}\u2013T-{minOffset}</span>
        </div>
        <div style={{ width: `${latePct}%`, background: 'rgba(239,68,68,0.06)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ fontSize: 7, color: T.textDim, letterSpacing: '0.08em' }}>LATE</span>
        </div>
      </div>
      {/* Position marker */}
      {evalOffset != null && (
        <div style={{
          position: 'absolute', top: 0, bottom: 0, left: `${positionPct}%`,
          width: 2, background: inSweet ? color : T.textMuted, transition: 'left 0.5s ease',
          boxShadow: inSweet ? `0 0 8px ${color}` : 'none',
        }} />
      )}
    </div>
  );
}

// ── Confidence Bar ───────────────────────────────────────────────────────────

function ConfidenceBar({ dist, config }) {
  const { minDist, maxDist } = config.thresholds;
  const maxBar = 0.30;
  const pct = dist != null ? Math.min(dist / maxBar, 1) * 100 : 0;
  const minPct = (minDist / maxBar) * 100;
  const maxPct = maxDist ? (maxDist / maxBar) * 100 : 100;
  const inBand = dist != null && dist >= minDist && (!maxDist || dist <= maxDist);

  return (
    <div style={{ position: 'relative', height: 18, borderRadius: 3, overflow: 'hidden', background: 'rgba(15,23,42,0.6)', border: `1px solid ${T.border}` }}>
      {/* Band highlight */}
      <div style={{
        position: 'absolute', left: `${minPct}%`, width: `${maxPct - minPct}%`, top: 0, bottom: 0,
        background: `${config.color}12`, borderLeft: `1px dashed ${config.color}60`, borderRight: maxDist ? `1px dashed ${config.color}60` : 'none',
      }} />
      {/* Fill */}
      <div style={{
        height: '100%', width: `${pct}%`, borderRadius: 3,
        background: inBand ? config.color : 'rgba(100,116,139,0.4)',
        transition: 'width 0.5s ease, background 0.3s',
      }} />
      {/* Label */}
      <div style={{ position: 'absolute', top: 1, right: 6, fontSize: 9, fontFamily: T.mono, color: inBand ? config.color : T.textMuted, fontWeight: 600 }}>
        {dist != null ? dist.toFixed(3) : '\u2014'}
      </div>
    </div>
  );
}

// ── Main Component ───────────────────────────────────────────────────────────

export default function StrategyFloor({ strategyId }) {
  const config = STRATEGY_CONFIGS[strategyId];
  if (!config) return <div style={{ color: T.red, padding: 40 }}>Unknown strategy: {strategyId}</div>;

  const api = useApi();
  const [decisions, setDecisions] = useState([]);
  const [hqData, setHqData] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [lastPrice, setLastPrice] = useState(null);
  const [prevPrice, setPrevPrice] = useState(null);
  const [loading, setLoading] = useState(true);
  const tickRef = useRef(null);

  useEffect(() => {
    document.title = `${config.label} \u2014 Polymarket \u2014 Novakash`;
    return () => { document.title = 'BTC Trader Hub'; };
  }, [config.label]);

  const fetchData = useCallback(async () => {
    try {
      const [dRes, hRes, oRes] = await Promise.allSettled([
        api('GET', `/v58/strategy-decisions?strategy_id=${config.id}&limit=200`),
        api('GET', '/v58/execution-hq?asset=btc&timeframe=5m&limit=50'),
        api('GET', '/v58/outcomes?limit=50'),
      ]);
      if (dRes.status === 'fulfilled') {
        const d = dRes.value?.data || dRes.value;
        setDecisions(Array.isArray(d) ? d : (d?.decisions ?? []));
      }
      if (hRes.status === 'fulfilled') {
        const h = hRes.value?.data || hRes.value;
        setHqData(h);
        const w = h?.windows?.[0];
        if (w?.btc_price) {
          setPrevPrice(lastPrice);
          setLastPrice(w.btc_price);
        }
      }
      if (oRes.status === 'fulfilled') {
        const o = oRes.value?.data || oRes.value;
        setOutcomes(o?.outcomes ?? (Array.isArray(o) ? o : []));
      }
    } catch (_) {}
    setLoading(false);
  }, [api, config.id, lastPrice]);

  useEffect(() => { fetchData(); }, []);  // eslint-disable-line
  useEffect(() => { const t = setInterval(fetchData, POLL_MS); return () => clearInterval(t); }, [fetchData]);

  // Current window info
  const cw = hqData?.windows?.[0] || {};
  const system = hqData?.system || {};
  const btcPrice = lastPrice || cw.btc_price || 0;
  const priceUp = prevPrice != null && btcPrice > prevPrice;
  const priceDown = prevPrice != null && btcPrice < prevPrice;

  // Window timing
  const windowTs = cw.window_ts_epoch || parseTs(cw.window_ts);
  const now = Math.floor(Date.now() / 1000);
  const remaining = windowTs ? Math.max(0, (windowTs + 300) - now) : null;
  const evalOffset = remaining;

  // Latest decision for this strategy
  const latestDec = decisions[0] || null;
  const latestMeta = (() => {
    try { return JSON.parse(latestDec?.metadata_json || '{}'); } catch { return {}; }
  })();
  const ctx = latestMeta._ctx || latestMeta;

  // Group decisions by window_ts (best per window)
  const windowDecisions = useMemo(() => {
    const byWindow = {};
    for (const d of decisions) {
      const wts = d.window_ts;
      if (!byWindow[wts] || (d.eval_offset > 60 && d.eval_offset < 180)) {
        byWindow[wts] = d;
      }
    }
    return Object.values(byWindow).sort((a, b) => b.window_ts - a.window_ts).slice(0, 50);
  }, [decisions]);

  // Merge with outcomes for actual direction
  const outcomeMap = useMemo(() => {
    const m = {};
    for (const o of outcomes) {
      const ts = parseTs(o.window_ts);
      m[ts] = o;
    }
    return m;
  }, [outcomes]);

  // Derive model direction from metadata (direction is null on SKIPs)
  function getModelDir(d) {
    if (d.direction) return d.direction;
    try {
      const m = JSON.parse(d.metadata_json || '{}');
      const pUp = m._ctx?.v4_p_up ?? m.v4_p_up;
      if (pUp != null) return pUp < 0.5 ? 'DOWN' : 'UP';
    } catch {}
    return null;
  }

  // Gate tightness stats — uses model direction, not decision direction
  const gateTightness = useMemo(() => {
    let skipped = 0, wouldHaveWon = 0, wouldHaveLost = 0;
    for (const d of windowDecisions) {
      if (d.action !== 'TRADE') {
        skipped++;
        const o = outcomeMap[d.window_ts];
        const actual = o?.actual_direction || (o?.close_price > o?.open_price ? 'UP' : o?.close_price < o?.open_price ? 'DOWN' : null);
        const modelDir = getModelDir(d);
        // Would this strategy's INTENDED direction have won?
        // For v4_down_only: only count if model said DOWN (strategy would trade DOWN)
        // For v4_up_asian: only count if model said UP (strategy would trade UP)
        const stratDir = config.direction; // 'DOWN' or 'UP'
        if (actual && modelDir === stratDir && modelDir === actual) {
          wouldHaveWon++;
        } else if (actual && modelDir === stratDir && modelDir !== actual) {
          wouldHaveLost++;
        }
      }
    }
    const relevant = wouldHaveWon + wouldHaveLost;
    return {
      skipped, wouldHaveWon, wouldHaveLost, relevant,
      pct: relevant > 0 ? Math.round(100 * wouldHaveWon / relevant) : 0,
    };
  }, [windowDecisions, outcomeMap, config.direction]);

  // Current UTC hour for Asian session check
  const nowHour = new Date().getUTCHours();
  const isAsian = config.thresholds.asianHours?.includes(nowHour);

  const dist = ctx.v4_p_up != null ? Math.abs(ctx.v4_p_up - 0.5) : (latestDec?.confidence_score != null ? latestDec.confidence_score / 2 : null);
  const clobAsk = ctx.clob_down_ask;

  if (loading && !hqData) {
    return <div style={{ minHeight: '100vh', background: T.bg, color: T.textMuted, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: T.mono }}>Loading {config.label}...</div>;
  }

  return (
    <div style={{ minHeight: '100vh', background: T.bg, color: T.text, padding: 10, fontFamily: T.mono, overflowY: 'auto' }}>

      {/* ── Header ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
        <div style={{ width: 4, height: 28, borderRadius: 2, background: config.color }} />
        <div>
          <div style={{ fontSize: 14, fontWeight: 800, color: config.color, letterSpacing: '0.05em' }}>{config.label}</div>
          <div style={{ fontSize: 9, color: T.textMuted }}>{config.gateLabel}</div>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{
            padding: '2px 8px', borderRadius: 3, fontSize: 9, fontWeight: 700,
            background: system.paper_mode === false ? 'rgba(16,185,129,0.15)' : 'rgba(245,158,11,0.12)',
            color: system.paper_mode === false ? T.green : T.amber,
          }}>
            {system.paper_mode === false ? 'LIVE' : 'PAPER'}
          </span>
          {config.thresholds.asianHours && (
            <span style={{
              padding: '2px 8px', borderRadius: 3, fontSize: 9, fontWeight: 700,
              background: isAsian ? `${config.color}20` : 'rgba(71,85,105,0.2)',
              color: isAsian ? config.color : T.textDim,
            }}>
              {isAsian ? `ASIAN SESSION (H${nowHour})` : `H${nowHour} UTC \u2014 next Asian: ${23 - nowHour > 0 ? 23 - nowHour : 24 + 23 - nowHour}h`}
            </span>
          )}
        </div>
      </div>

      {/* ── Band 1: Price Ticker + Window Lifecycle ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
        {/* Price */}
        <div style={{
          background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 4, padding: '10px 14px',
        }}>
          <div style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.1em', marginBottom: 4 }}>BTC PRICE</div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span ref={tickRef} style={{
              fontSize: 28, fontWeight: 800, fontFamily: T.mono, lineHeight: 1,
              color: priceUp ? T.green : priceDown ? T.red : T.text,
              transition: 'color 0.3s',
              textShadow: priceUp ? '0 0 12px rgba(16,185,129,0.3)' : priceDown ? '0 0 12px rgba(239,68,68,0.3)' : 'none',
            }}>
              {btcPrice ? fmtPrice(btcPrice) : '\u2014'}
            </span>
            {cw.open_price && btcPrice ? (
              <span style={{ fontSize: 11, color: btcPrice >= cw.open_price ? T.green : T.red, fontWeight: 600 }}>
                {((btcPrice - cw.open_price) / cw.open_price * 100).toFixed(3)}%
              </span>
            ) : null}
          </div>
          {cw.open_price && <div style={{ fontSize: 9, color: T.textDim, marginTop: 4 }}>Open: {fmtPrice(cw.open_price)}</div>}
        </div>

        {/* Window timing */}
        <div style={{
          background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 4, padding: '10px 14px',
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
            <span style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.1em' }}>WINDOW LIFECYCLE</span>
            <span style={{ fontSize: 12, fontWeight: 700, color: remaining != null && remaining <= 150 && remaining >= 90 ? config.color : T.textMuted }}>
              {remaining != null ? `T-${remaining}s` : '\u2014'}
            </span>
          </div>
          <WindowLifecycleBar evalOffset={evalOffset} minOffset={config.thresholds.minOffset} maxOffset={config.thresholds.maxOffset} color={config.color} />
        </div>
      </div>

      {/* ── Band 2: Current Decision + Gate Status ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
        {/* Current decision */}
        <div style={{
          background: T.card, borderRadius: 4, padding: '10px 14px',
          border: latestDec?.action === 'TRADE' ? `2px solid ${config.color}` : `1px solid ${T.cardBorder}`,
          boxShadow: latestDec?.action === 'TRADE' ? `0 0 20px ${config.color}30` : 'none',
        }}>
          <div style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.1em', marginBottom: 6 }}>CURRENT DECISION</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{
              fontSize: 18, fontWeight: 800,
              color: latestDec?.action === 'TRADE' ? config.color : T.textMuted,
            }}>
              {latestDec?.action || 'WAITING'}
            </span>
            {latestDec?.direction && (
              <span style={{ fontSize: 14, fontWeight: 700, color: dirColor(latestDec.direction) }}>
                {latestDec.direction === 'UP' ? '\u2191' : '\u2193'} {latestDec.direction}
              </span>
            )}
            {latestDec?.eval_offset != null && (
              <span style={{ fontSize: 9, color: T.textDim }}>T-{latestDec.eval_offset}</span>
            )}
          </div>
          {latestDec?.skip_reason && (
            <div style={{ fontSize: 9, color: T.textMuted, padding: '4px 8px', borderRadius: 3, background: 'rgba(71,85,105,0.15)', lineHeight: 1.4 }}>
              {latestDec.skip_reason}
            </div>
          )}
          {latestDec?.action === 'TRADE' && latestDec?.entry_reason && (
            <div style={{ fontSize: 9, color: config.color, marginTop: 4, fontWeight: 600 }}>
              {latestDec.entry_reason}
            </div>
          )}
        </div>

        {/* Gate status */}
        <div style={{ background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 4, padding: '10px 14px' }}>
          <div style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.1em', marginBottom: 8 }}>GATE STATUS</div>
          {/* Confidence */}
          <div style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 8, color: T.textDim, marginBottom: 3 }}>
              Conviction dist {config.thresholds.maxDist ? `${config.thresholds.minDist}\u2013${config.thresholds.maxDist}` : `\u2265${config.thresholds.minDist}`}
            </div>
            <ConfidenceBar dist={dist} config={config} />
          </div>
          {/* Strategy-specific */}
          {config.id === 'v4_down_only' && (
            <div>
              <div style={{ fontSize: 8, color: T.textDim, marginBottom: 2 }}>CLOB down_ask</div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: clobAsk != null ? (clobAsk >= 0.55 ? T.green : clobAsk >= 0.25 ? T.amber : T.red) : T.textDim }}>
                  {clobAsk != null ? clobAsk.toFixed(3) : 'NULL'}
                </span>
                <span style={{ fontSize: 9, color: T.textDim }}>
                  {clobAsk == null ? '1.5x (no data)' : clobAsk >= 0.55 ? '2.0x sizing' : clobAsk >= 0.35 ? '1.2x' : clobAsk >= 0.25 ? '1.0x' : 'SKIP (<0.25)'}
                </span>
              </div>
            </div>
          )}
          {config.id === 'v4_up_asian' && (
            <div>
              <div style={{ fontSize: 8, color: T.textDim, marginBottom: 2 }}>Asian Session (23/0/1/2 UTC)</div>
              <div style={{ display: 'flex', gap: 4 }}>
                {[21, 22, 23, 0, 1, 2, 3, 4].map(h => (
                  <span key={h} style={{
                    padding: '2px 6px', borderRadius: 2, fontSize: 9, fontWeight: h === nowHour ? 800 : 400,
                    background: [23, 0, 1, 2].includes(h) ? (h === nowHour ? `${config.color}30` : `${config.color}10`) : 'rgba(71,85,105,0.15)',
                    color: h === nowHour ? config.color : [23, 0, 1, 2].includes(h) ? T.textMuted : T.textDim,
                    border: h === nowHour ? `1px solid ${config.color}` : '1px solid transparent',
                  }}>
                    {h}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ── Band 3: Recent Windows Table ── */}
      <div style={{
        background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 4, padding: 0,
        marginBottom: 8,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 14px 0' }}>
          <span style={{ fontSize: 9, color: config.color, letterSpacing: '0.1em', fontWeight: 700 }}>RECENT WINDOWS ({windowDecisions.length})</span>
          {gateTightness.relevant > 0 ? (
            <span style={{
              fontSize: 9, padding: '2px 8px', borderRadius: 3, fontWeight: 600,
              background: gateTightness.pct > 80 ? 'rgba(239,68,68,0.12)' : gateTightness.pct > 50 ? 'rgba(245,158,11,0.12)' : 'rgba(16,185,129,0.08)',
              color: gateTightness.pct > 80 ? T.red : gateTightness.pct > 50 ? T.amber : T.green,
            }}>
              {gateTightness.wouldHaveWon} missed wins / {gateTightness.relevant} {config.direction} signals ({gateTightness.pct}% missed)
              {gateTightness.pct > 80 && ' \u2014 GATES TOO TIGHT'}
              {gateTightness.pct === 0 && gateTightness.relevant > 0 && ' \u2014 gates working perfectly'}
            </span>
          ) : gateTightness.skipped > 0 ? (
            <span style={{ fontSize: 9, color: T.textDim }}>
              {gateTightness.skipped} skips (no {config.direction} signals — model predicted opposite)
            </span>
          ) : null}
        </div>

        <div style={{ maxHeight: 500, overflowY: 'auto', padding: '0 0 4px' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: T.headerBg, position: 'sticky', top: 0, zIndex: 1 }}>
                {['Time', 'BTC Move', 'Signal', 'Decision', 'Skip Reason', 'Actual', 'Result', 'Missed?'].map(h => (
                  <th key={h} style={{
                    padding: '6px 8px', textAlign: h === 'Skip Reason' ? 'left' : 'center',
                    fontSize: 8, fontWeight: 600, letterSpacing: '0.06em', color: T.textMuted,
                    borderBottom: `1px solid ${T.border}`, fontFamily: T.mono, textTransform: 'uppercase',
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {windowDecisions.map((d, i) => {
                const o = outcomeMap[d.window_ts] || {};
                const actual = o.actual_direction || (o.close_price > o.open_price ? 'UP' : o.close_price < o.open_price ? 'DOWN' : null);
                const modelDir = getModelDir(d);
                const isTrade = d.action === 'TRADE';
                const isWin = isTrade && (d.direction || modelDir) && actual && (d.direction || modelDir) === actual;
                const isLoss = isTrade && (d.direction || modelDir) && actual && (d.direction || modelDir) !== actual;
                // "Missed?" = strategy's target direction matched actual, but we skipped
                const stratDir = config.direction; // 'DOWN' or 'UP'
                const wouldHaveTraded = !isTrade && modelDir === stratDir;
                const wouldHaveWon = wouldHaveTraded && actual === stratDir;
                const wouldHaveLost = wouldHaveTraded && actual && actual !== stratDir;

                const rowBg = isWin ? 'rgba(16,185,129,0.04)' : isLoss ? 'rgba(239,68,68,0.04)' : 'transparent';
                const rowOpacity = isTrade ? 1 : 0.6;

                const td = { padding: '5px 8px', fontSize: 10, fontFamily: T.mono, borderBottom: `1px solid rgba(51,65,85,0.3)`, textAlign: 'center' };

                return (
                  <tr key={d.window_ts || i} style={{ background: i % 2 === 0 ? rowBg : `rgba(15,23,42,${isTrade ? '0.3' : '0.15'})`, opacity: rowOpacity }}>
                    <td style={td}>{utcHHMM(d.window_ts)}</td>
                    <td style={td}>
                      {o.open_price && o.close_price ? (
                        <span style={{ color: o.close_price > o.open_price ? T.green : T.red, fontWeight: 600 }}>
                          {fmtMove(o.open_price, o.close_price)}
                        </span>
                      ) : '\u2014'}
                    </td>
                    <td style={{ ...td, color: dirColor(modelDir), fontWeight: 600 }}>{modelDir || '\u2014'}</td>
                    <td style={td}>
                      <span style={{
                        padding: '1px 6px', borderRadius: 2, fontSize: 9, fontWeight: 700,
                        background: isTrade ? `${config.color}20` : 'rgba(71,85,105,0.15)',
                        color: isTrade ? config.color : T.textDim,
                      }}>
                        {d.action || 'SKIP'}
                      </span>
                    </td>
                    <td style={{ ...td, textAlign: 'left', fontSize: 8, color: T.textDim, maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={d.skip_reason || ''}>
                      {d.skip_reason ? d.skip_reason.slice(0, 35) : '\u2014'}
                    </td>
                    <td style={{ ...td, color: dirColor(actual), fontWeight: 600 }}>{actual || '\u2014'}</td>
                    <td style={td}>
                      {isWin ? <span style={{ color: T.green, fontWeight: 700 }}>WIN</span>
                        : isLoss ? <span style={{ color: T.red, fontWeight: 700 }}>LOSS</span>
                        : <span style={{ color: T.textDim }}>SKIP</span>}
                    </td>
                    <td style={td}>
                      {wouldHaveWon ? (
                        <span style={{ color: T.amber, fontWeight: 700, fontSize: 9 }}>{'\u26A0'} MISSED WIN</span>
                      ) : wouldHaveLost ? (
                        <span style={{ color: T.green, fontSize: 9 }}>{'\u2713'} correct skip</span>
                      ) : wouldHaveTraded ? (
                        <span style={{ color: T.textDim, fontSize: 9 }}>unresolved</span>
                      ) : !isTrade && modelDir !== config.direction ? (
                        <span style={{ color: T.textDim, fontSize: 9 }}>wrong dir ({modelDir})</span>
                      ) : !isTrade ? (
                        <span style={{ color: T.textDim, fontSize: 9 }}>{'\u2014'}</span>
                      ) : '\u2014'}
                    </td>
                  </tr>
                );
              })}
              {windowDecisions.length === 0 && (
                <tr><td colSpan={8} style={{ padding: 20, textAlign: 'center', color: T.textDim, fontSize: 10 }}>No decisions yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Description ── */}
      <div style={{ fontSize: 9, color: T.textDim, lineHeight: 1.5, padding: '4px 8px' }}>
        {config.description}
      </div>
    </div>
  );
}
