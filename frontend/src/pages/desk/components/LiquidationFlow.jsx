// /desk Phase 2 — liquidation flow gauge + funding ticker + taker-buy ratio chip.
//
// Spec §5 asks for:
//   (a) stacked micro-bar of last-5m $ longs vs shorts liquidated
//   (b) funding-rate chip
//   (c) taker-buy-ratio (derived — same CoinGlass bundle)
//
// Reality check against live /v4/snapshot (curl-verified):
//   - NO liquidation field surfaces anywhere in the payload — neither
//     at root, nor under timescales.5m.sub_signals, nor in
//     polymarket_live_recommended_outcome.extras.
//   - funding lives at snap.macro.inputs.funding_rate (per-asset).
//   - taker_buy_ratio lives at snap.macro.inputs.taker_buy_ratio.
//
// Rather than silently render a blank micro-bar or fabricate values, we:
//   - Render a Liquidations chip that says "awaiting engine feed" when
//     the path is empty — signals an engine-side task for Phase 3.
//   - Render funding + taker-buy chips that work today, so this
//     component earns its place on the page immediately.

import React from 'react';
import { T } from '../../../theme/tokens.js';

export default function LiquidationFlow({ snapshot }) {
  const macroInputs = snapshot?.macro?.inputs || {};
  const fundingRate = num(macroInputs.funding_rate);
  const takerBuyRatio = num(macroInputs.taker_buy_ratio);

  // Liquidations path — check known candidates (may arrive later).
  const liq5m = extractLiquidations(snapshot);

  return (
    <div style={{
      border: `1px solid ${T.border}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em', marginBottom: 6 }}>
        FLOW
      </div>

      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 8,
        marginBottom: 8,
      }}>
        <FundingChip rate={fundingRate} />
        <TakerBuyChip ratio={takerBuyRatio} />
      </div>

      <LiquidationBar liq={liq5m} />
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function FundingChip({ rate }) {
  // funding_rate is typically a per-8h decimal (e.g. 0.0001 = 1 bp / 8h).
  // Positive = longs paying shorts (crowded longs); negative = shorts paying longs.
  if (rate == null) return <Empty label="funding" />;
  const color = rate >= 0.0002 ? T.loss : rate <= -0.0002 ? T.profit : T.label2;
  const pctText = `${(rate * 100).toFixed(4)}%`;
  return (
    <div>
      <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>funding (8h)</div>
      <div style={{ color, fontSize: 13, fontFamily: T.font, fontWeight: 500 }}>
        {pctText}
      </div>
      <div style={{ color: T.label2, fontSize: 9 }}>
        {rate >= 0 ? 'longs paying shorts' : 'shorts paying longs'}
      </div>
    </div>
  );
}

function TakerBuyChip({ ratio }) {
  // taker_buy_ratio in [0, 1]. >0.6 = aggressive buying; <0.4 = aggressive selling.
  if (ratio == null) return <Empty label="taker buy%" />;
  const color = ratio >= 0.6 ? T.profit : ratio <= 0.4 ? T.loss : T.label2;
  return (
    <div>
      <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>taker buy %</div>
      <div style={{ color, fontSize: 13, fontFamily: T.font, fontWeight: 500 }}>
        {(ratio * 100).toFixed(1)}%
      </div>
      <div style={{ color: T.label2, fontSize: 9 }}>
        {ratio >= 0.5 ? 'aggressive bids' : 'aggressive asks'}
      </div>
    </div>
  );
}

function LiquidationBar({ liq }) {
  if (!liq || (liq.longs_usd == null && liq.shorts_usd == null)) {
    return (
      <div style={{
        border: `1px dashed ${T.border}`,
        padding: 8,
        fontSize: 9,
        color: T.label,
        textAlign: 'center',
      }}>
        liquidation feed: awaiting engine-side exposure in /v4/snapshot
      </div>
    );
  }
  const longs = Math.max(0, liq.longs_usd || 0);
  const shorts = Math.max(0, liq.shorts_usd || 0);
  const total = longs + shorts;
  const longsFrac = total > 0 ? longs / total : 0.5;
  const shortsFrac = total > 0 ? shorts / total : 0.5;
  return (
    <div>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 9,
        color: T.label,
        letterSpacing: '0.05em',
        marginBottom: 3,
      }}>
        <span>longs liquidated</span>
        <span>shorts liquidated</span>
      </div>
      <div style={{ display: 'flex', height: 14, background: T.grid }}>
        <div style={{
          width: `${longsFrac * 100}%`,
          background: T.loss,
          opacity: 0.7,
        }} />
        <div style={{
          width: `${shortsFrac * 100}%`,
          background: T.profit,
          opacity: 0.7,
        }} />
      </div>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 10,
        fontFamily: T.font,
        marginTop: 2,
      }}>
        <span style={{ color: T.loss }}>{fmtUsd(longs)}</span>
        <span style={{ color: T.profit }}>{fmtUsd(shorts)}</span>
      </div>
    </div>
  );
}

function Empty({ label }) {
  return (
    <div>
      <div style={{ color: T.label, fontSize: 9, letterSpacing: '0.05em' }}>{label}</div>
      <div style={{ color: T.label2, fontSize: 13, fontFamily: T.font }}>—</div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmtUsd(v) {
  if (!Number.isFinite(v) || v <= 0) return '$0';
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

export function extractLiquidations(snap) {
  if (!snap) return null;
  // Known / hoped-for paths — first one with numeric data wins.
  const candidates = [
    snap.timescales?.['5m']?.liquidations,
    snap.timescales?.['5m']?.sub_signals?.liquidations,
    snap.liquidations,
    snap.coinglass?.liquidations_5m,
  ];
  for (const c of candidates) {
    if (!c) continue;
    const longs = Number(c.longs_usd ?? c.longs ?? c.long);
    const shorts = Number(c.shorts_usd ?? c.shorts ?? c.short);
    if (Number.isFinite(longs) || Number.isFinite(shorts)) {
      return { longs_usd: longs, shorts_usd: shorts };
    }
  }
  return null;
}
