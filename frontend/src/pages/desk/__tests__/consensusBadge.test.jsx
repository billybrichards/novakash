// Tests for ConsensusBadge — consensus block rendering + fail-closed behaviour.
//
// H2: component must render a "?" chip when consensus block is absent (not null).
// H3: string prices must coerce via Number() before toLocaleString().
// Defensive sources: array-shaped sources must not crash the component.

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import ConsensusBadge from '../components/ConsensusBadge.jsx';

function makeSnap(consensus) {
  return { consensus };
}

describe('ConsensusBadge — fail-closed (H2)', () => {
  it('renders "?" chip when snapshot is null', () => {
    render(<ConsensusBadge snapshot={null} />);
    expect(screen.getByText('?')).toBeTruthy();
  });

  it('renders "?" chip when consensus block is absent', () => {
    render(<ConsensusBadge snapshot={{}} />);
    expect(screen.getByText('?')).toBeTruthy();
  });

  it('renders "?" chip when safe_to_trade is undefined', () => {
    render(<ConsensusBadge snapshot={makeSnap({ max_divergence_bps: 5 })} />);
    // safe_to_trade is absent — should render ? not SAFE/UNSAFE
    expect(screen.getByText('?')).toBeTruthy();
    expect(screen.queryByText('SAFE')).toBeNull();
    expect(screen.queryByText('UNSAFE')).toBeNull();
  });
});

describe('ConsensusBadge — safe_to_trade chip', () => {
  it('renders "SAFE" chip when safe_to_trade is true', () => {
    render(<ConsensusBadge snapshot={makeSnap({ safe_to_trade: true })} />);
    expect(screen.getByText('SAFE')).toBeTruthy();
  });

  it('renders "UNSAFE" chip when safe_to_trade is false', () => {
    render(<ConsensusBadge snapshot={makeSnap({ safe_to_trade: false })} />);
    expect(screen.getByText('UNSAFE')).toBeTruthy();
  });

  it('uses red border on container when safe_to_trade is false', () => {
    const { container } = render(
      <ConsensusBadge snapshot={makeSnap({ safe_to_trade: false })} />
    );
    const wrapper = container.firstChild;
    // JSDOM normalises hex → rgb(), so compare with getComputedStyle or check
    // that the border-color is non-default (a loss/red colour).
    // #f87171 → rgb(248, 113, 113) after JSDOM normalisation.
    expect(wrapper.style.borderColor).toBe('rgb(248, 113, 113)');
  });

  it('uses default border on container when safe_to_trade is true', () => {
    const { container } = render(
      <ConsensusBadge snapshot={makeSnap({ safe_to_trade: true })} />
    );
    const wrapper = container.firstChild;
    // rgba(255,255,255,0.06) → JSDOM adds spaces: rgba(255, 255, 255, 0.06)
    expect(wrapper.style.borderColor).toBe('rgba(255, 255, 255, 0.06)');
  });
});

describe('ConsensusBadge — divergence colouring', () => {
  it('renders max divergence text for 45 bps with red (loss) color', () => {
    render(<ConsensusBadge snapshot={makeSnap({ safe_to_trade: true, max_divergence_bps: 45 })} />);
    const divEl = screen.getByText('45.0 bps');
    expect(divEl).toBeTruthy();
    // JSDOM normalises #f87171 → rgb(248, 113, 113)
    expect(divEl.style.color).toBe('rgb(248, 113, 113)');
  });

  it('renders max divergence text for 15 bps with warn (amber) color', () => {
    render(<ConsensusBadge snapshot={makeSnap({ safe_to_trade: true, max_divergence_bps: 15 })} />);
    const divEl = screen.getByText('15.0 bps');
    expect(divEl).toBeTruthy();
    // JSDOM normalises #f59e0b → rgb(245, 158, 11)
    expect(divEl.style.color).toBe('rgb(245, 158, 11)');
  });

  it('renders max divergence text for 5 bps with profit (green) color', () => {
    render(<ConsensusBadge snapshot={makeSnap({ safe_to_trade: true, max_divergence_bps: 5 })} />);
    const divEl = screen.getByText('5.0 bps');
    expect(divEl).toBeTruthy();
    // JSDOM normalises #4ade80 → rgb(74, 222, 128)
    expect(divEl.style.color).toBe('rgb(74, 222, 128)');
  });
});

describe('ConsensusBadge — sources shape (defensive)', () => {
  it('does not crash when sources is an array', () => {
    const consensus = {
      safe_to_trade: true,
      sources: ['chainlink', 'tiingo'],
    };
    // Should not throw — array is normalised to empty object
    expect(() => render(<ConsensusBadge snapshot={makeSnap(consensus)} />)).not.toThrow();
    // Source count chip ("N/M") should not appear for an array
    expect(screen.queryByText(/\d+\/\d+/)).toBeNull();
  });

  it('renders source count for a valid sources object', () => {
    const consensus = {
      safe_to_trade: true,
      sources: {
        chainlink: { available: true, price: '95000', age_ms: 100 },
        tiingo: { available: false, price: null, age_ms: 2000 },
      },
    };
    render(<ConsensusBadge snapshot={makeSnap(consensus)} />);
    expect(screen.getByText('1/2')).toBeTruthy();
  });

  it('does not crash when source price is a string (H3)', () => {
    const consensus = {
      safe_to_trade: true,
      sources: {
        chainlink: { available: true, price: '95123.45', age_ms: 50 },
      },
    };
    expect(() => render(<ConsensusBadge snapshot={makeSnap(consensus)} />)).not.toThrow();
  });
});
