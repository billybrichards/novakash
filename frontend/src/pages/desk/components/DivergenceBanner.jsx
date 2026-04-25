// /desk Phase 2 — divergence alert banner.
//
// Fires on either:
//   (1) server-computed ensemble_config.disagreement_detected === true, or
//   (2) |implied_p_up - pu| > 0.10 when CLOB ladder data is present.
//
// Auto-dismisses when the condition clears (no manual dismiss — stale
// banners lying around during a trading session would be worse than
// flicker).

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { pickDivergenceBanner } from '../lib/divergence.js';

export default function DivergenceBanner({ fiveMin, pu, pc, pl, impliedPUp }) {
  const banner = pickDivergenceBanner({ fiveMin, pu, pc, pl, impliedPUp });
  if (!banner) return null;

  const background = banner.kind === 'implied' ? `${T.warn}18` : `${T.purple}18`;
  const border = banner.kind === 'implied' ? T.warn : T.purple;

  return (
    <div style={{
      border: `1px solid ${border}`,
      background,
      padding: '6px 10px',
      marginBottom: 10,
      fontFamily: T.font,
      fontSize: 11,
      color: T.text,
      display: 'flex',
      alignItems: 'center',
      gap: 8,
    }}>
      <span aria-hidden="true" style={{ color: border }}>⚠</span>
      {banner.kind === 'implied' ? (
        <ImpliedText banner={banner} />
      ) : (
        <EnsembleText banner={banner} />
      )}
    </div>
  );
}

function ImpliedText({ banner }) {
  const { impliedPUp, modelPUp, diff, direction } = banner;
  const arrow = diff > 0 ? '↑' : '↓';
  const pctDelta = `${diff > 0 ? '+' : ''}${(diff * 100).toFixed(1)}pt`;
  const directionLabel = direction === 'model_bullish'
    ? 'market cheap vs model'
    : 'market rich vs model';
  return (
    <span>
      <b>Divergence</b> · implied {(impliedPUp * 100).toFixed(1)}% vs model {(modelPUp * 100).toFixed(1)}% ({pctDelta} {arrow}) — {directionLabel}
    </span>
  );
}

function EnsembleText({ banner }) {
  const { pc, pl, magnitude } = banner;
  const pcTxt = pc == null ? '—' : pc.toFixed(2);
  const plTxt = pl == null ? '—' : pl.toFixed(2);
  const magTxt = magnitude == null ? '' : ` · mag ${magnitude.toFixed(2)}`;
  const pcDir = pc == null ? '?' : pc >= 0.5 ? 'UP' : 'DOWN';
  const plDir = pl == null ? '?' : pl >= 0.5 ? 'UP' : 'DOWN';
  return (
    <span>
      <b>Ensemble divergence</b> · classifier says {pcDir} {pcTxt} / LGB says {plDir} {plTxt}{magTxt}
    </span>
  );
}
