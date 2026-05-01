// /desk Phase 3 — v9-specific operator alert banners.
//
// VHC bypass (#358 PL VHC bypass with classifier cross-veto):
//   When v9 fires the VHC bypass path, it overrides several gates
//   (R9 in v9_ensemble.yaml: TRANSITION block, UP-distance, disagreement
//   veto, lgb_safety_floor, oracle_direction) and trades classifier
//   conviction at 2× Kelly. This banner only fires when the engine
//   surfaces an explicit `vhc_bypass_active === true` flag — we don't
//   compute a client-side approximation because the engine's true gate
//   involves oracle delta agreement that the FE can't see, and a green
//   "trading at 2× Kelly" banner during a window the engine actually
//   skipped is the worst possible operator UX.
//
// Mark-to-market stop-loss (#364, #424, #425):
//   Same fail-closed posture — fires only on a strict-true server
//   `stop_loss_triggered` / `m2m_stop_active` flag, or when the engine
//   reports `detector_type === 'mark_to_market'` for the open position.
//
// Both detectors live in lib/alerts.js so the bool/coercion logic is
// unit-tested without rendering a component.

import React from 'react';
import { T } from '../../../theme/tokens.js';
import { detectVhcActive, detectM2MStopLoss } from '../lib/alerts.js';

export default function AlertBanners({ fiveMin, pc, pl }) {
  // pc/pl props remain in the API for future server-corroborated
  // diagnostics; the banner itself doesn't compute on them.
  void pc; void pl;

  const vhc = detectVhcActive(fiveMin);
  const m2m = detectM2MStopLoss(fiveMin);

  const banners = [];

  if (vhc.active) {
    banners.push({
      kind: 'vhc',
      color: T.profit,
      title: 'VHC BYPASS ACTIVE',
      body: `Classifier conviction ${vhc.edge != null ? `edge ${vhc.edge.toFixed(3)} ` : ''}— v9 trading at 2× Kelly · gate bypass on TRANSITION + UP-distance + disagreement + lgb_safety_floor + oracle_direction (R9, v9_ensemble.yaml)`,
    });
  }

  if (m2m.active) {
    banners.push({
      kind: 'm2m',
      color: T.loss,
      title: 'M2M STOP-LOSS FIRED',
      body: m2m.unrealizedPnl != null
        ? `Mark-to-market exit triggered · uPnL ${m2m.unrealizedPnl >= 0 ? '+' : ''}${m2m.unrealizedPnl.toFixed(2)} · engine selling at $0.01 limit`
        : 'Mark-to-market exit triggered · engine selling at aggressive price',
    });
  }

  if (banners.length === 0) return null;

  return (
    <>
      {banners.map(b => (
        <div key={b.kind} style={{
          border: `1px solid ${b.color}`,
          background: `${b.color}14`,
          padding: '6px 10px',
          marginBottom: 8,
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          fontFamily: T.font,
          fontSize: 11,
          color: T.text,
        }}>
          <span aria-hidden="true" style={{ color: b.color }}>⚡</span>
          <b style={{ color: b.color, letterSpacing: '0.08em' }}>{b.title}</b>
          <span style={{ color: T.label2 }}>{b.body}</span>
        </div>
      ))}
    </>
  );
}
