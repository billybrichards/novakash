// /desk Phase 3 — v9-specific operator alert banners.
//
// VHC bypass (#358 PL VHC bypass with classifier cross-veto):
//   When the classifier conviction crosses the VHC threshold AND no
//   cross-veto fires, v9_ensemble bypasses TRANSITION block + UP-distance
//   gate and trades the classifier signal at 2x Kelly. The operator
//   wants to know that's happening — the model isn't following its
//   normal rules in this window.
//
// Mark-to-market stop-loss (#364, #424, #425):
//   The position-monitor evaluates open positions every tick; if mark
//   crosses below entry by more than the M2M threshold the engine
//   files an aggressive sell. We surface a banner if the snapshot
//   carries a `position_monitor` block flagging an active stop.
//
// Exit window (#365):
//   The engine sells via CLOB confirmation between T-48 and T-30. When
//   the countdown is in that range AND a v9 position is currently open,
//   show a tight banner so the operator knows the engine is in its
//   exit decision window. Falls back gracefully when position state
//   isn't on the snapshot.
//
// All three are best-effort reads: if the snapshot doesn't carry the
// fields, the banner stays invisible.

import React from 'react';
import { T } from '../../../theme/tokens.js';

const VHC_THRESHOLD = 0.25; // |pc - 0.5| >= 0.25 — see strategies.js v9_ensemble.thresholds

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export default function AlertBanners({ fiveMin, pc, pl }) {
  // VHC bypass detection. Prefer engine-flagged, fall back to client math.
  const ensemble = fiveMin?.ensemble_config || {};
  const vhcServer = ensemble?.vhc_bypass_active ?? ensemble?.vhc_active ?? null;
  const pcNum = num(pc);
  const plNum = num(pl);
  const classifierEdge = pcNum != null ? Math.abs(pcNum - 0.5) : null;
  const lgbDir = plNum == null ? null : plNum >= 0.5 ? 'UP' : 'DOWN';
  const pcDir = pcNum == null ? null : pcNum >= 0.5 ? 'UP' : 'DOWN';
  const crossVeto = pcDir && lgbDir && pcDir !== lgbDir;
  const vhcClient = (
    classifierEdge != null
    && classifierEdge >= VHC_THRESHOLD
    && !crossVeto
  );
  const vhcActive = vhcServer == null ? vhcClient : Boolean(vhcServer);

  // M2M stop-loss state (best effort — depends on position_monitor block).
  const monitor = fiveMin?.position_monitor
    ?? fiveMin?.position
    ?? null;
  const m2mTriggered = !!(
    monitor?.stop_loss_triggered
    || monitor?.m2m_stop_active
    || monitor?.detector_type === 'mark_to_market'
  );
  const m2mPnl = num(monitor?.unrealized_pnl ?? monitor?.upnl);

  // Exit-window indicator covered separately on Header (Phase 3) — we
  // don't repeat it here unless a position is open AND we're inside the
  // window, in which case the M2M banner has the relevant info.

  const banners = [];

  if (vhcActive) {
    banners.push({
      kind: 'vhc',
      color: T.profit,
      title: 'VHC BYPASS ACTIVE',
      body: `Classifier edge ${classifierEdge?.toFixed(3) ?? '—'} ≥ ${VHC_THRESHOLD} · v9 trading at 2× Kelly, gate bypass on TRANSITION + UP-distance`,
    });
  }

  if (m2mTriggered) {
    banners.push({
      kind: 'm2m',
      color: T.loss,
      title: 'M2M STOP-LOSS FIRED',
      body: m2mPnl != null
        ? `Mark-to-market exit triggered · uPnL ${m2mPnl >= 0 ? '+' : ''}${m2mPnl.toFixed(2)} · engine selling at $0.01 limit`
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
