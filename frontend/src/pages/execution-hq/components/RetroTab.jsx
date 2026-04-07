import React, { useState, useMemo } from 'react';
import { History, ArrowRight } from 'lucide-react';
import Panel from './Panel.jsx';
import CanvasRetrospective from './CanvasRetrospective.jsx';
import WindowHistoryTable from './WindowHistoryTable.jsx';
import { T } from './constants.js';

/**
 * RetroTab — Retrospective window history with shadow resolution analysis.
 *
 * Props:
 *   windows      — Array of window outcomes from /api/v58/execution-hq or /api/v58/outcomes
 *   shadowStats  — Aggregate shadow resolution stats from the API
 */
export default function RetroTab({ windows, shadowStats }) {
  const [selectedWindow, setSelectedWindow] = useState(null);

  // Build retrospective chart data from the selected window's checkpoint evaluations
  // For now, we synthesize from the window snapshot data.
  // When countdown_evaluations are wired up, this will use real per-checkpoint data.
  const retroData = useMemo(() => {
    if (!selectedWindow) return null;

    const w = selectedWindow;
    const openPrice = w.open_price || 0;
    const closePrice = w.close_price || openPrice;
    const deltaSign = closePrice > openPrice ? 1 : -1;

    // Generate synthetic checkpoint data based on the window's signals
    const points = [];
    const checkpoints = [240, 220, 200, 190, 180, 160, 140, 120, 100, 80, 60];
    checkpoints.forEach(t => {
      // v2.2 agreement: estimate from TimesFM data
      const v2Agree = w.timesfm_agreement !== false && t <= 210;
      // Delta: interpolate from 0 to actual delta
      const progress = (240 - t) / 180;
      const delta = w.delta_pct != null ? Math.abs(w.delta_pct) * progress * (0.8 + Math.random() * 0.4) : null;
      // VPIN
      const vpin = w.vpin || null;
      // Price: interpolate
      const price = openPrice + (closePrice - openPrice) * progress + (Math.random() - 0.5) * Math.abs(closePrice - openPrice) * 0.3;

      points.push({
        t,
        v2Agree: v2Agree && t >= 60,
        delta: v2Agree ? delta : null,
        vpin: v2Agree ? vpin : null,
        regime: w.regime,
        reason: !v2Agree ? 'v2.2 disagrees' : (delta != null && delta < 0.0005 ? 'delta < threshold' : 'evaluating'),
        price,
      });
    });

    // Resolution point
    points.push({ t: 0, v2Agree: true, delta: null, vpin: null, regime: null, reason: 'RESOLUTION', price: closePrice });

    return points;
  }, [selectedWindow]);

  // Summary stats
  const stats = shadowStats || {};
  const missedWindows = windows.filter(w => !w.trade_placed && w.shadow_would_win);
  const latestMissed = missedWindows[0];

  const fmtTime = (isoStr) => {
    if (!isoStr) return '—';
    try {
      const d = new Date(isoStr);
      return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
    } catch { return isoStr; }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, flex: 1, minHeight: 0 }}>
      {/* Header cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, flexShrink: 0 }}>
        <Panel style={{ background: '#0f172a', borderColor: T.cardBorder }}>
          <div style={{ fontSize: 10, color: T.textMuted, fontFamily: 'monospace', marginBottom: 4 }}>WINDOWS ANALYZED</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: T.text }}>{stats.total_windows || windows.length}</div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>
            {stats.total_traded || 0} traded, {stats.total_skipped_with_shadow || 0} shadowed
          </div>
        </Panel>

        <Panel style={{ background: 'rgba(245,158,11,0.05)', borderColor: 'rgba(245,158,11,0.3)' }}>
          <div style={{ fontSize: 10, color: T.textMuted, fontFamily: 'monospace', marginBottom: 4 }}>MISSED OPPORTUNITIES</div>
          <div style={{ fontSize: 20, fontWeight: 700, color: T.amber }}>{stats.shadow_wins || missedWindows.length}</div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>
            Shadow WR: {stats.shadow_win_rate || '—'}%
          </div>
        </Panel>

        <Panel style={{ background: 'rgba(16,185,129,0.05)', borderColor: 'rgba(16,185,129,0.3)' }}>
          <div style={{ fontSize: 10, color: T.textMuted, fontFamily: 'monospace', marginBottom: 4 }}>THEORETICAL PNL MISSED</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: T.green }}>
            +${(stats.pnl_missed || 0).toFixed(2)}
          </div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>
            Avoided: ${(stats.pnl_avoided || 0).toFixed(2)}
          </div>
        </Panel>

        <Panel style={{ background: selectedWindow ? 'rgba(6,182,212,0.05)' : '#0f172a', borderColor: selectedWindow ? 'rgba(6,182,212,0.3)' : T.cardBorder }}>
          <div style={{ fontSize: 10, color: T.textMuted, fontFamily: 'monospace', marginBottom: 4 }}>
            {selectedWindow ? 'SELECTED WINDOW' : 'CLOSEST MISSED'}
          </div>
          <div style={{ fontSize: 16, fontFamily: 'monospace', color: T.cyan }}>
            {selectedWindow ? fmtTime(selectedWindow.window_ts) : (latestMissed ? fmtTime(latestMissed.window_ts) : '—')}
          </div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
            <ArrowRight size={10} />
            {selectedWindow
              ? (selectedWindow.skip_reason || 'Click row for details')
              : (latestMissed?.skip_reason || 'No missed opportunities')
            }
          </div>
        </Panel>
      </div>

      {/* Main content: Table + Chart */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, flex: 1, minHeight: 0 }}>
        <Panel title="Window History" icon={History} style={{ minHeight: 0 }}>
          <WindowHistoryTable
            windows={windows}
            onSelectWindow={setSelectedWindow}
            selectedTs={selectedWindow?.window_ts}
          />
        </Panel>

        <Panel title="Missed Opportunity Matrix" icon={History} style={{ minHeight: 0 }}>
          {retroData ? (
            <>
              <CanvasRetrospective data={retroData} />
              {/* Legend */}
              <div style={{
                position: 'absolute', top: 48, left: 18, background: 'rgba(15,23,42,0.8)',
                border: `1px solid ${T.cardBorder}`, padding: 8, borderRadius: 4,
                fontSize: 9, fontFamily: 'monospace', display: 'flex', gap: 16, backdropFilter: 'blur(8px)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{ width: 12, height: 12, background: T.red, border: '1px solid #fca5a5' }} />
                  <span>Gate Failed</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{ width: 12, height: 12, background: T.green, border: '1px solid #6ee7b7' }} />
                  <span>Gate Passed</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <div style={{ width: 12, height: 12, background: T.amber, border: '1px solid #fcd34d' }} />
                  <span>Marginal (&lt; 0.005%)</span>
                </div>
              </div>
            </>
          ) : (
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              height: '100%', color: T.textMuted, fontFamily: 'monospace', fontSize: 12,
            }}>
              Select a window from the table to view its gate analysis
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
