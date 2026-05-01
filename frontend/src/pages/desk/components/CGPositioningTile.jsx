// /desk Phase 3 — CoinGlass positioning tile.
//
// Liquidation flow shows realised forced-exit volume; this tile shows
// LIVE crowd positioning + funding pressure, which is what tells you
// whether the next cascade is more likely long or short.
//
// Fields read from the v4 snapshot top-level `cg` block (mapped from
// engine/data/feeds/coinglass_enhanced.py CoinGlassSnapshot):
//   long_short_ratio     >1 = more longs, <1 = more shorts (crowd, Binance)
//   funding_rate         positive = longs paying shorts (8h)
//   oi_usd               total open interest (USD)
//   oi_delta_pct_1m      OI change since last poll
//   taker_buy_volume_1m / taker_sell_volume_1m
//
// We also opportunistically read top_position_long_pct / top_position_short_pct
// (smart-money L/S) — these aren't currently included in the FullDataSurface
// passthrough, but if a future hub release exposes them they'll render.

import React from 'react';
import { T } from '../../../theme/tokens.js';

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export default function CGPositioningTile({ snapshot, fiveMin }) {
  const cg = snapshot?.cg ?? snapshot?.coinglass ?? null;
  // Some hub builds flatten cg fields into the 5m block; fall back to that.
  const lsr = num(cg?.long_short_ratio ?? fiveMin?.cg_long_short_ratio);
  const longPct = num(cg?.long_pct);
  const shortPct = num(cg?.short_pct);
  const topLongPct = num(cg?.top_position_long_pct);
  const topShortPct = num(cg?.top_position_short_pct);
  const topRatio = num(cg?.top_position_ratio);
  const funding = num(cg?.funding_rate ?? fiveMin?.funding_rate);
  const fundingAnnual = num(cg?.funding_rate_annual);
  const oiDelta = num(cg?.oi_delta_pct_1m);
  const takerB = num(cg?.taker_buy_volume_1m);
  const takerS = num(cg?.taker_sell_volume_1m);
  const takerRatio = (takerB != null && takerS != null && (takerB + takerS) > 0)
    ? takerB / (takerB + takerS)
    : null;

  const anyData = [lsr, longPct, topLongPct, funding, oiDelta, takerRatio]
    .some(v => v != null);

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 8 }}>
        CG POSITIONING
      </div>

      {!anyData ? (
        <div style={{ color: T.label, fontSize: 11, fontFamily: T.font }}>
          No CoinGlass data on snapshot.
        </div>
      ) : (
        <div style={{ fontFamily: T.font, fontSize: 11 }}>
          {/* Crowd L/S */}
          {(longPct != null && shortPct != null) ? (
            <BipolarRow
              label="Crowd L/S"
              leftPct={longPct}
              rightPct={shortPct}
              leftColor={T.profit}
              rightColor={T.loss}
              meta={lsr != null ? `ratio ${lsr.toFixed(2)}` : null}
            />
          ) : lsr != null ? (
            <Row label="Crowd L/S ratio" value={lsr.toFixed(2)}
                 color={lsr > 1 ? T.profit : lsr < 1 ? T.loss : T.label} />
          ) : null}

          {/* Smart money */}
          {(topLongPct != null && topShortPct != null) ? (
            <BipolarRow
              label="Top L/S"
              leftPct={topLongPct}
              rightPct={topShortPct}
              leftColor={T.profit}
              rightColor={T.loss}
              meta={topRatio != null ? `ratio ${topRatio.toFixed(2)}` : null}
            />
          ) : null}

          {/* Funding rate */}
          {funding != null ? (
            <Row
              label="Funding"
              value={`${(funding * 100).toFixed(4)}%${fundingAnnual != null ? ` (${(fundingAnnual * 100).toFixed(1)}% APR)` : ''}`}
              color={funding > 0 ? T.warn : T.profit}
              hint="Positive = longs paying shorts (long-pressure)."
            />
          ) : null}

          {/* OI delta */}
          {oiDelta != null ? (
            <Row
              label="ΔOI · 1m"
              value={`${oiDelta >= 0 ? '+' : ''}${(oiDelta * 100).toFixed(2)}%`}
              color={Math.abs(oiDelta) > 0.005 ? T.warn : T.label2}
              hint="Open-interest change since last 1m poll."
            />
          ) : null}

          {/* Taker buy / sell flow */}
          {takerRatio != null ? (
            <BipolarRow
              label="Taker flow"
              leftPct={takerRatio * 100}
              rightPct={(1 - takerRatio) * 100}
              leftColor={T.profit}
              rightColor={T.loss}
              meta={`buy ${(takerRatio * 100).toFixed(0)}% / sell ${((1 - takerRatio) * 100).toFixed(0)}%`}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}

function Row({ label, value, color, hint }) {
  return (
    <div title={hint} style={{
      display: 'grid',
      gridTemplateColumns: '90px 1fr',
      gap: 8,
      padding: '3px 0',
    }}>
      <span style={{ color: T.label, fontSize: 10 }}>{label}</span>
      <span style={{ color: color || T.text }}>{value}</span>
    </div>
  );
}

function BipolarRow({ label, leftPct, rightPct, leftColor, rightColor, meta }) {
  const total = (leftPct ?? 0) + (rightPct ?? 0);
  const leftWidth = total > 0 ? (leftPct / total) * 100 : 50;
  return (
    <div title={meta} style={{ padding: '4px 0' }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 10,
        color: T.label,
        marginBottom: 2,
      }}>
        <span>{label}</span>
        <span style={{ color: T.label2 }}>{meta}</span>
      </div>
      <div style={{
        display: 'flex',
        height: 6,
        border: `1px solid ${T.border}`,
        borderRadius: 1,
        overflow: 'hidden',
      }}>
        <div style={{ width: `${leftWidth}%`, background: leftColor, opacity: 0.7 }} />
        <div style={{ width: `${100 - leftWidth}%`, background: rightColor, opacity: 0.7 }} />
      </div>
    </div>
  );
}
