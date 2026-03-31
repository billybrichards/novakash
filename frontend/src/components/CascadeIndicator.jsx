import React from 'react';

/**
 * CascadeIndicator — FSM state visualization: IDLE → CASCADE_DETECTED → EXHAUSTING → BET_SIGNAL → COOLDOWN
 */
export default function CascadeIndicator({ cascade }) {
  const states = ['IDLE', 'CASCADE_DETECTED', 'EXHAUSTING', 'BET_SIGNAL', 'COOLDOWN'];

  if (!cascade) {
    cascade = { state: 'IDLE', direction: null, vpin: 0, oi_delta_pct: 0, liq_volume_usd: 0 };
  }

  const currentIndex = states.indexOf(cascade.state);

  return (
    <div className="space-y-4">
      {/* State Machine */}
      <div className="flex items-center justify-between text-xs">
        {states.map((state, i) => (
          <div key={state} className="flex flex-col items-center flex-1">
            <div
              style={{
                background: i <= currentIndex ? 'var(--accent-cyan)' : 'rgba(255,255,255,0.05)',
                color: i <= currentIndex ? '#000' : 'var(--text-secondary)',
              }}
              className="w-10 h-10 rounded-full flex items-center justify-center font-semibold mb-2"
            >
              {i + 1}
            </div>
            <div className="text-center">{state}</div>
            {i < states.length - 1 && (
              <div
                style={{
                  background: i < currentIndex ? 'var(--accent-cyan)' : 'rgba(255,255,255,0.05)',
                  height: '2px',
                  width: '100%',
                  margin: '8px 0',
                }}
              />
            )}
          </div>
        ))}
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-3 gap-3 pt-4" style={{ borderTop: '1px solid var(--border)' }}>
        <div>
          <div style={{ color: 'var(--text-secondary)' }} className="text-xs mb-1">
            VPIN
          </div>
          <div className="font-semibold">{cascade.vpin?.toFixed(4)}</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-secondary)' }} className="text-xs mb-1">
            OI Δ%
          </div>
          <div className="font-semibold">{(cascade.oi_delta_pct * 100).toFixed(2)}%</div>
        </div>
        <div>
          <div style={{ color: 'var(--text-secondary)' }} className="text-xs mb-1">
            Liq Vol
          </div>
          <div className="font-semibold">${(cascade.liq_volume_usd / 1e6).toFixed(1)}M</div>
        </div>
      </div>

      {/* Direction */}
      {cascade.direction && (
        <div className="text-center py-2 rounded" style={{ background: 'rgba(255,255,255,0.05)' }}>
          <span style={{ color: cascade.direction === 'UP' ? 'var(--loss)' : 'var(--profit)' }} className="font-semibold">
            {cascade.direction === 'UP' ? '↑ Price Up Cascade' : '↓ Price Down Cascade'}
          </span>
        </div>
      )}
    </div>
  );
}
