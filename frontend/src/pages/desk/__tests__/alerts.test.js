import { describe, it, expect } from 'vitest';
import { detectVhcActive, detectM2MStopLoss } from '../lib/alerts.js';

describe('detectVhcActive', () => {
  it('returns inactive when fiveMin is null', () => {
    expect(detectVhcActive(null)).toEqual({ active: false, edge: null });
  });

  it('returns inactive when ensemble_config absent', () => {
    expect(detectVhcActive({})).toEqual({ active: false, edge: null });
  });

  it('returns inactive when vhc_bypass_active is false', () => {
    const fm = { ensemble_config: { vhc_bypass_active: false } };
    expect(detectVhcActive(fm).active).toBe(false);
  });

  it('does NOT fire on string "true" — strict bool only', () => {
    const fm = { ensemble_config: { vhc_bypass_active: 'true' } };
    expect(detectVhcActive(fm).active).toBe(false);
  });

  it('does NOT fire on string "false" — strict bool only', () => {
    const fm = { ensemble_config: { vhc_bypass_active: 'false' } };
    expect(detectVhcActive(fm).active).toBe(false);
  });

  it('fires when vhc_bypass_active is strict true', () => {
    const fm = {
      ensemble_config: { vhc_bypass_active: true },
      probability_classifier: 0.82,
    };
    const result = detectVhcActive(fm);
    expect(result.active).toBe(true);
    expect(result.edge).toBeCloseTo(0.32, 6);
  });

  it('returns edge null when probability_classifier absent', () => {
    const fm = { ensemble_config: { vhc_bypass_active: true } };
    expect(detectVhcActive(fm).edge).toBeNull();
  });

  it('fires on vhc_active alias', () => {
    const fm = { ensemble_config: { vhc_active: true } };
    expect(detectVhcActive(fm).active).toBe(true);
  });
});

describe('detectM2MStopLoss', () => {
  it('returns inactive when fiveMin is null', () => {
    expect(detectM2MStopLoss(null)).toEqual({ active: false, unrealizedPnl: null });
  });

  it('returns inactive when position_monitor absent', () => {
    expect(detectM2MStopLoss({})).toEqual({ active: false, unrealizedPnl: null });
  });

  it('does NOT fire on string "true" — strict bool only', () => {
    const fm = { position_monitor: { stop_loss_triggered: 'true' } };
    expect(detectM2MStopLoss(fm).active).toBe(false);
  });

  it('fires when stop_loss_triggered is strict true', () => {
    const fm = { position_monitor: { stop_loss_triggered: true, unrealized_pnl: -1.23 } };
    const result = detectM2MStopLoss(fm);
    expect(result.active).toBe(true);
    expect(result.unrealizedPnl).toBeCloseTo(-1.23, 6);
  });

  it('fires on detector_type mark_to_market', () => {
    const fm = { position_monitor: { detector_type: 'mark_to_market' } };
    expect(detectM2MStopLoss(fm).active).toBe(true);
  });

  it('reads from position fallback', () => {
    const fm = { position: { m2m_stop_active: true, upnl: -0.50 } };
    const result = detectM2MStopLoss(fm);
    expect(result.active).toBe(true);
    expect(result.unrealizedPnl).toBeCloseTo(-0.50, 6);
  });
});
