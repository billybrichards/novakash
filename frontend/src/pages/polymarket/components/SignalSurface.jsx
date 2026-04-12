import React, { useState } from 'react';
import { T, fmt, pct, SIGNAL_NAMES } from './theme.js';

/**
 * Band 3 — Signal Surface Panel (3 columns).
 *
 * Left:   Direction & Confidence
 * Center: Market Context
 * Right:  V4 Recommended Action
 *
 * Multi-timescale pills: 5m | 15m | 1h | 4h
 */

const TIMESCALE_TABS = ['5m', '15m', '1h', '4h'];

// --- Shared UI primitives ---

function Card({ children, style: extra = {} }) {
  return (
    <div style={{
      background: T.card, border: `1px solid ${T.cardBorder}`,
      borderRadius: 6, padding: '10px 12px', fontFamily: T.mono,
      ...extra,
    }}>{children}</div>
  );
}

function Label({ children }) {
  return (
    <div style={{
      fontSize: 8, color: T.purple, letterSpacing: '0.12em',
      fontWeight: 700, textTransform: 'uppercase', marginBottom: 4,
    }}>{children}</div>
  );
}

function SubLabel({ children }) {
  return (
    <span style={{ fontSize: 8, color: T.textMuted, letterSpacing: '0.04em' }}>{children}</span>
  );
}

// --- Sub-signal bar ---

function SignalBar({ name, value, maxVal = 1 }) {
  const displayName = SIGNAL_NAMES[name] || name;
  const barPct = value != null ? Math.min(Math.abs(value) / maxVal, 1) * 100 : 0;
  const isPositive = value != null && value >= 0;
  const color = isPositive ? T.green : T.red;

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
      <span style={{ fontSize: 9, color: T.textMuted, width: 80, flexShrink: 0, textAlign: 'right' }}>
        {displayName}
      </span>
      <div style={{
        flex: 1, height: 10, background: 'rgba(255,255,255,0.04)',
        borderRadius: 2, overflow: 'hidden', position: 'relative',
      }}>
        <div style={{
          position: 'absolute', top: 0, left: 0, height: '100%',
          width: `${barPct}%`, background: `${color}66`, borderRadius: 2,
          transition: 'width 0.3s ease',
        }} />
      </div>
      <span style={{ fontSize: 9, color, fontWeight: 600, width: 40, textAlign: 'right' }}>
        {value != null ? fmt(value, 3) : '\u2014'}
      </span>
    </div>
  );
}

// --- Left Column: Direction & Confidence ---

function DirectionColumn({ hqData, v3Snapshot }) {
  const w = hqData?.windows?.[0] || {};
  const direction = w.direction || '\u2014';
  const isUp = direction === 'UP';
  const dirColor = isUp ? T.green : direction === 'DOWN' ? T.red : T.textDim;

  // Source agreement from gate heartbeat
  const hb = hqData?.gate_heartbeat?.[0] || {};
  const gateResults = hb.gate_results || {};
  const srcAgreePass = gateResults.gate_agreement === true || gateResults.gate_agreement === 'PASS';
  const v10Stats = hqData?.v10_stats || {};
  const wrPct = v10Stats.wr_pct ?? null;

  // Sequoia p_up
  const pUp = w.v2_probability_up ?? null;

  // V3 composite
  const v3 = v3Snapshot || {};
  const compositeScore = v3.composite_score ?? v3.score ?? null;
  const timescales = v3.timescales || v3.components || {};

  return (
    <Card style={{ flex: '1 1 0', minWidth: 200 }}>
      <Label>Direction & Confidence</Label>

      {/* Large direction indicator */}
      <div style={{
        textAlign: 'center', padding: '8px 0', marginBottom: 8,
      }}>
        <div style={{
          fontSize: 36, fontWeight: 800, color: dirColor,
          textShadow: `0 0 20px ${dirColor}44`,
          letterSpacing: '0.05em',
        }}>
          {direction === 'UP' ? '\u25B2 UP' : direction === 'DOWN' ? '\u25BC DOWN' : '\u2014'}
        </div>
      </div>

      {/* Source agreement chip */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8,
        padding: '4px 8px', borderRadius: 3,
        background: srcAgreePass ? 'rgba(16,185,129,0.08)' : 'rgba(245,158,11,0.08)',
        border: `1px solid ${srcAgreePass ? 'rgba(16,185,129,0.3)' : 'rgba(245,158,11,0.3)'}`,
      }}>
        <span style={{
          fontSize: 10, fontWeight: 700,
          color: srcAgreePass ? T.green : T.amber,
        }}>
          {srcAgreePass ? 'SRC AGREE' : 'SRC DISAGREE'}
        </span>
        {wrPct != null && (
          <span style={{ fontSize: 9, color: T.textMuted }}>WR: {wrPct}%</span>
        )}
      </div>

      {/* Sequoia p_up gauge */}
      <div style={{ marginBottom: 8 }}>
        <SubLabel>Sequoia v5.2 p_up</SubLabel>
        <div style={{
          marginTop: 3, height: 14, background: 'rgba(255,255,255,0.04)',
          borderRadius: 3, overflow: 'hidden', position: 'relative',
        }}>
          {pUp != null && (
            <div style={{
              position: 'absolute', top: 0, left: 0, height: '100%',
              width: `${(pUp * 100).toFixed(0)}%`,
              background: pUp > 0.5 ? `${T.green}55` : `${T.red}55`,
              borderRadius: 3, transition: 'width 0.3s',
            }} />
          )}
          <span style={{
            position: 'absolute', top: 0, left: '50%', transform: 'translateX(-50%)',
            fontSize: 10, fontWeight: 600, color: T.text, lineHeight: '14px',
          }}>
            {pUp != null ? fmt(pUp, 3) : '\u2014'}
          </span>
        </div>
      </div>

      {/* V3 Composite with sparkline */}
      <div>
        <SubLabel>V3 Composite</SubLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 3 }}>
          <span style={{
            fontSize: 14, fontWeight: 700,
            color: compositeScore != null ? (compositeScore > 0 ? T.green : T.red) : T.textDim,
          }}>
            {compositeScore != null ? fmt(compositeScore, 3) : '\u2014'}
          </span>
          {/* Mini sparkline — 9 timescale dots */}
          <div style={{ display: 'flex', gap: 2, alignItems: 'flex-end' }}>
            {Object.entries(timescales).slice(0, 9).map(([k, v]) => {
              const val = typeof v === 'object' ? (v.score ?? v.value ?? 0) : (v ?? 0);
              const h = Math.max(3, Math.abs(val) * 20);
              const c = val > 0 ? T.green : val < 0 ? T.red : T.textDim;
              return (
                <div key={k} title={`${k}: ${fmt(val, 3)}`} style={{
                  width: 4, height: h, background: c, borderRadius: 1, opacity: 0.7,
                }} />
              );
            })}
          </div>
        </div>
      </div>
    </Card>
  );
}

// --- Center Column: Market Context ---

function MarketContextColumn({ hqData, v4Snapshot }) {
  const w = hqData?.windows?.[0] || {};

  // Consensus
  const consensus = v4Snapshot?.consensus || {};
  const conSources = consensus.sources || [];

  // Macro
  const macro = v4Snapshot?.macro || {};
  const macroFallback = macro.fallback === true || macro.status === 'fallback' || macro.unreachable === true;

  // Regime
  const regime = v4Snapshot?.regime || {};
  const regimeState = regime.state || regime.classification || '\u2014';
  const regimeConf = regime.regime_confidence ?? regime.confidence ?? null;
  const regimePersistence = regime.regime_persistence ?? null;

  // VPIN
  const vpinVal = w.vpin ?? null;

  // Sub-signals from the window or hq data
  const subSignals = w.sub_signals || hqData?.sub_signals || {};

  return (
    <Card style={{ flex: '1.3 1 0', minWidth: 260 }}>
      <Label>Market Context</Label>

      {/* Consensus */}
      <div style={{ marginBottom: 8 }}>
        <SubLabel>Consensus ({conSources.length} sources)</SubLabel>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 3 }}>
          {conSources.length > 0 ? conSources.map((s, i) => {
            const alive = s.price != null && s.price > 0;
            return (
              <div key={i} style={{
                padding: '2px 6px', borderRadius: 3, fontSize: 9,
                background: alive ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)',
                border: `1px solid ${alive ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.2)'}`,
                color: alive ? T.green : T.red,
              }}>
                {s.name || s.source || `src${i}`}: {alive ? `$${fmt(s.price, 0)}` : 'DEAD'}
                {s.divergence_bps != null && <span style={{ marginLeft: 3, color: T.textMuted }}>{fmt(s.divergence_bps, 0)}bp</span>}
              </div>
            );
          }) : (
            <span style={{ fontSize: 9, color: T.textMuted }}>No consensus data</span>
          )}
        </div>
      </div>

      {/* Macro */}
      <div style={{ marginBottom: 8 }}>
        <SubLabel>Macro</SubLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
          {macroFallback && (
            <span style={{
              padding: '2px 6px', borderRadius: 3, fontSize: 9, fontWeight: 700,
              background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.4)',
              color: T.red, animation: 'pulse 2s infinite',
            }}>FALLBACK</span>
          )}
          <span style={{ fontSize: 11, fontWeight: 600, color: macroFallback ? T.red : T.text }}>
            {macro.bias || macro.direction || (macroFallback ? 'UNREACHABLE' : '\u2014')}
          </span>
          {macro.gate && (
            <span style={{ fontSize: 9, color: T.textMuted }}>gate: {macro.gate}</span>
          )}
          {macro.modifier && (
            <span style={{ fontSize: 9, color: T.amber }}>mod: {macro.modifier}</span>
          )}
        </div>
      </div>

      {/* Regime */}
      <div style={{ marginBottom: 8 }}>
        <SubLabel>Regime</SubLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
          <span style={{
            fontSize: 11, fontWeight: 600,
            color: regimeState.includes('TREND') ? T.green
              : regimeState.includes('CHOP') ? T.amber
              : regimeState.includes('CASCADE') ? T.red
              : T.text,
          }}>
            {regimeState}
          </span>
          {regimeConf != null && (
            <span style={{ fontSize: 9, color: T.textMuted }}>conf: {pct(regimeConf)}</span>
          )}
          {regimePersistence != null && (
            <span style={{ fontSize: 9, color: T.textMuted }}>pers: {regimePersistence}</span>
          )}
        </div>
      </div>

      {/* VPIN */}
      <div style={{ marginBottom: 8 }}>
        <SubLabel>VPIN</SubLabel>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
          <span style={{
            fontSize: 14, fontWeight: 700,
            color: vpinVal != null ? (vpinVal >= 0.45 ? T.green : T.amber) : T.textDim,
          }}>
            {vpinVal != null ? fmt(vpinVal, 3) : '\u2014'}
          </span>
          {/* Threshold marker */}
          <div style={{
            flex: 1, height: 8, background: 'rgba(255,255,255,0.04)',
            borderRadius: 2, position: 'relative', overflow: 'hidden',
          }}>
            {vpinVal != null && (
              <div style={{
                position: 'absolute', top: 0, left: 0, height: '100%',
                width: `${Math.min(vpinVal * 100, 100)}%`,
                background: vpinVal >= 0.45 ? `${T.green}55` : `${T.amber}55`,
                borderRadius: 2,
              }} />
            )}
            {/* Threshold line at 45% */}
            <div style={{
              position: 'absolute', top: 0, left: '45%', width: 1, height: '100%',
              background: T.red, opacity: 0.6,
            }} />
          </div>
        </div>
      </div>

      {/* Sub-signals */}
      <div>
        <SubLabel>Sub-Signals</SubLabel>
        <div style={{ marginTop: 3 }}>
          {Object.keys(SIGNAL_NAMES).map(key => (
            <SignalBar key={key} name={key} value={subSignals[key] ?? null} />
          ))}
        </div>
      </div>
    </Card>
  );
}

// --- Right Column: V4 Recommended Action ---

function PolymarketOutcomeBlock({ outcome }) {
  const [extrasOpen, setExtrasOpen] = useState(false);

  const direction = outcome.direction || '\u2014';
  const tradeAdvised = outcome.trade_advised;
  const confidence = outcome.confidence ?? null;
  const confDist = outcome.confidence_distance ?? null;
  const regime = outcome.regime || '\u2014';
  const timing = outcome.timing || '\u2014';
  const reason = outcome.reason || '\u2014';
  const extras = outcome.extras || {};
  const hasExtras = Object.keys(extras).length > 0;

  const dirColor = direction === 'UP' ? T.green : direction === 'DOWN' ? T.red : T.textDim;
  const advisedColor = tradeAdvised === true || tradeAdvised === 'YES' ? T.green : T.red;
  const advisedLabel = tradeAdvised === true || tradeAdvised === 'YES' ? 'YES' : 'NO';

  return (
    <>
      {/* Direction */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 20, fontWeight: 800, color: dirColor }}>
          {direction === 'UP' ? '\u25B2 UP' : direction === 'DOWN' ? '\u25BC DOWN' : direction}
        </span>
        <span style={{
          fontSize: 10, fontWeight: 700, color: advisedColor,
          padding: '2px 8px', borderRadius: 3,
          background: `${advisedColor}15`, border: `1px solid ${advisedColor}30`,
        }}>
          TRADE: {advisedLabel}
        </span>
      </div>

      {/* Confidence + Distance */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 6 }}>
        {confidence != null && (
          <div>
            <SubLabel>Confidence</SubLabel>
            <span style={{ fontSize: 13, fontWeight: 700, color: T.text, marginLeft: 4 }}>
              {fmt(confidence, 3)}
            </span>
          </div>
        )}
        {confDist != null && (
          <div>
            <SubLabel>Conf Distance</SubLabel>
            <span style={{
              fontSize: 13, fontWeight: 700, marginLeft: 4,
              color: confDist > 0 ? T.green : T.amber,
            }}>
              {confDist > 0 ? '+' : ''}{fmt(confDist, 3)}
            </span>
          </div>
        )}
      </div>

      {/* Regime + Timing */}
      <div style={{
        display: 'flex', gap: 8, marginBottom: 6, flexWrap: 'wrap',
      }}>
        <div style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 9, fontWeight: 700,
          background: 'rgba(139,92,246,0.1)', border: '1px solid rgba(139,92,246,0.3)',
          color: T.purple,
        }}>
          {regime}
        </div>
        <div style={{
          padding: '2px 6px', borderRadius: 3, fontSize: 9, fontWeight: 700,
          background: 'rgba(6,182,212,0.1)', border: '1px solid rgba(6,182,212,0.3)',
          color: T.cyan,
        }}>
          {timing}
        </div>
      </div>

      {/* Reason */}
      <div style={{
        fontSize: 9, color: T.textMuted, marginBottom: 6,
        padding: '4px 6px', background: 'rgba(255,255,255,0.02)', borderRadius: 3,
        wordBreak: 'break-word', lineHeight: 1.4,
      }}>
        {reason}
      </div>

      {/* Collapsible extras */}
      {hasExtras && (
        <div>
          <button onClick={() => setExtrasOpen(!extrasOpen)} style={{
            background: 'none', border: 'none', cursor: 'pointer',
            fontSize: 9, color: T.purple, fontWeight: 600, fontFamily: T.mono,
            padding: '2px 0', letterSpacing: '0.04em',
          }}>
            {extrasOpen ? '\u25BC' : '\u25B6'} EXTRAS ({Object.keys(extras).length})
          </button>
          {extrasOpen && (
            <div style={{
              marginTop: 4, padding: '4px 6px', borderRadius: 3,
              background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)',
              fontSize: 9, color: T.textMuted, lineHeight: 1.5,
            }}>
              {Object.entries(extras).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', gap: 6 }}>
                  <span style={{ color: T.purple, minWidth: 80, flexShrink: 0 }}>{k}:</span>
                  <span style={{ wordBreak: 'break-word' }}>
                    {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}

function MarginActionFallback({ tsData, hqData }) {
  const side = tsData.side || tsData.direction || '\u2014';
  const conviction = tsData.conviction || 'NONE';
  const reason = tsData.reason || tsData.skip_reason || '\u2014';
  const score = tsData.conviction_score ?? tsData.score ?? null;

  const quantiles = tsData.quantiles || {};
  const p10 = quantiles.p10 ?? null;
  const p50 = quantiles.p50 ?? null;
  const p90 = quantiles.p90 ?? null;

  const engineDir = hqData?.windows?.[0]?.direction || null;
  const v4Dir = tsData.side || tsData.direction || null;
  const disagree = engineDir && v4Dir && engineDir.toUpperCase() !== v4Dir.toUpperCase() &&
    v4Dir !== 'SKIP' && v4Dir !== 'NONE';

  const sideColor = side === 'LONG' || side === 'UP' ? T.green
    : side === 'SHORT' || side === 'DOWN' ? T.red
    : T.textDim;

  const convColor = conviction === 'HIGH' ? T.green
    : conviction === 'MEDIUM' ? T.cyan
    : conviction === 'LOW' ? T.amber
    : T.textDim;

  return (
    <>
      <div style={{
        fontSize: 8, color: T.amber, fontWeight: 600, marginBottom: 6,
        padding: '2px 6px', borderRadius: 3,
        background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.2)',
      }}>
        MARGIN FALLBACK
      </div>

      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 6 }}>
        <span style={{ fontSize: 20, fontWeight: 800, color: sideColor }}>{side}</span>
        <span style={{
          fontSize: 11, fontWeight: 700, color: convColor,
          padding: '2px 6px', borderRadius: 3,
          background: `${convColor}15`, border: `1px solid ${convColor}30`,
        }}>
          {conviction}
          {score != null && <span style={{ marginLeft: 4, fontSize: 9, color: T.textMuted }}>{fmt(score, 2)}</span>}
        </span>
      </div>

      <div style={{
        fontSize: 9, color: T.textMuted, marginBottom: 8,
        padding: '4px 6px', background: 'rgba(255,255,255,0.02)', borderRadius: 3,
        wordBreak: 'break-word',
      }}>
        {reason}
      </div>

      {(p10 != null || p50 != null || p90 != null) && (
        <div style={{ marginBottom: 8 }}>
          <SubLabel>Quantile Fan</SubLabel>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, marginTop: 3,
            padding: '4px 6px', background: 'rgba(255,255,255,0.02)', borderRadius: 3,
          }}>
            <span style={{ fontSize: 9, color: T.red }}>P10: {p10 != null ? fmt(p10, 2) : '\u2014'}</span>
            <span style={{ fontSize: 10, fontWeight: 700, color: T.text }}>P50: {p50 != null ? fmt(p50, 2) : '\u2014'}</span>
            <span style={{ fontSize: 9, color: T.green }}>P90: {p90 != null ? fmt(p90, 2) : '\u2014'}</span>
          </div>
        </div>
      )}

      {disagree && (
        <div style={{
          padding: '4px 8px', borderRadius: 3, marginTop: 4,
          background: 'rgba(245,158,11,0.1)', border: '1px solid rgba(245,158,11,0.3)',
          fontSize: 9, color: T.amber, fontWeight: 600,
        }}>
          V4 says {v4Dir}, engine decided {engineDir}
        </div>
      )}
    </>
  );
}

function V4ActionColumn({ v4Snapshot, hqData }) {
  const timescales = v4Snapshot?.timescales || {};

  const [activeTs, setActiveTs] = useState('5m');
  const tsData = timescales[activeTs] || {};

  // Prefer polymarket_live_recommended_outcome, fall back to margin_recommended_action / raw tsData
  const polyOutcome = tsData.polymarket_live_recommended_outcome || null;
  const marginAction = tsData.margin_recommended_action || null;
  const hasPolyOutcome = polyOutcome && Object.keys(polyOutcome).length > 0;

  return (
    <Card style={{ flex: '1 1 0', minWidth: 200 }}>
      <Label>V4 Recommended Action</Label>

      {/* Timescale pills */}
      <div style={{ display: 'flex', gap: 3, marginBottom: 8 }}>
        {TIMESCALE_TABS.map(ts => (
          <button key={ts} onClick={() => setActiveTs(ts)} style={{
            padding: '3px 8px', fontSize: 9, fontWeight: 700, fontFamily: T.mono,
            borderRadius: 3, cursor: 'pointer', border: 'none',
            background: activeTs === ts ? `${T.purple}22` : 'transparent',
            color: activeTs === ts ? T.purple : T.textMuted,
            letterSpacing: '0.06em',
          }}>{ts}</button>
        ))}
      </div>

      {hasPolyOutcome ? (
        <PolymarketOutcomeBlock outcome={polyOutcome} />
      ) : marginAction ? (
        <MarginActionFallback tsData={marginAction} hqData={hqData} />
      ) : (
        <MarginActionFallback tsData={tsData} hqData={hqData} />
      )}
    </Card>
  );
}

// --- Main export ---

export default function SignalSurface({ hqData, v4Snapshot, v3Snapshot }) {
  return (
    <div style={{
      display: 'flex', gap: 6, marginBottom: 6,
      flexShrink: 0, minHeight: 0,
    }}>
      <DirectionColumn hqData={hqData} v3Snapshot={v3Snapshot} />
      <MarketContextColumn hqData={hqData} v4Snapshot={v4Snapshot} />
      <V4ActionColumn v4Snapshot={v4Snapshot} hqData={hqData} />
    </div>
  );
}
