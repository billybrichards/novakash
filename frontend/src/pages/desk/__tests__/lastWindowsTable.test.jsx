// Regression test for the stale-strategy-id bug class (#218 / PR #427).
//
// LastWindowsTable used to hardcode pre-v8 strategy ids. The table is now
// driven by DESK_TRACKED_STRATEGY_IDS so a future LIVE flip touches the
// constant in constants/strategies.js, not this component. These tests
// pin that contract so a regression to literal ids fails CI before it
// hits the operator HUD.

import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import LastWindowsTable from '../components/LastWindowsTable.jsx';
import {
  DESK_TRACKED_STRATEGY_IDS,
  getStrategyMeta,
} from '../../../constants/strategies.js';

describe('LastWindowsTable — strategy column source of truth', () => {
  it('renders one column header per DESK_TRACKED_STRATEGY_IDS entry', () => {
    render(<LastWindowsTable picks={[]} resolvedByStrategy={{}} />);

    for (let i = 0; i < DESK_TRACKED_STRATEGY_IDS.length; i++) {
      const sid = DESK_TRACKED_STRATEGY_IDS[i];
      const expected = getStrategyMeta(sid, i).shortLabel || sid;
      // shortLabel is rendered as the column header text; the underlying
      // strategy_id appears as the title attribute on the <th>.
      const headers = screen.getAllByText(expected);
      expect(headers.length).toBeGreaterThan(0);
    }
  });

  it('does not render any pre-v8 strategy id as a column header', () => {
    render(<LastWindowsTable picks={[]} resolvedByStrategy={{}} />);

    const stale = ['v6_sniper', 'v4_fusion', 'v5_ensemble', 'v5_fresh'];
    for (const sid of stale) {
      // Stale ids may still appear inside title attributes if a hub
      // returns historical rows, but they should never be rendered as
      // header text. The test regresses if a literal slips back in.
      const headerCells = screen.queryAllByRole('columnheader');
      const headerTexts = headerCells.map(c => c.textContent);
      expect(headerTexts).not.toContain(sid);
    }
  });

  it('renders the WR footer with one cell per tracked strategy + You + Consensus', () => {
    render(<LastWindowsTable picks={[]} resolvedByStrategy={{}} />);

    // Footer cells (WR row) — count: WR label + You + N strategies + Consensus + 2 unused (Actual / Δ)
    // The unused trailing cell is colSpan=2, so we count siblings of the WR label cell.
    const wrLabel = screen.getByText('WR');
    const wrRow = wrLabel.closest('tr');
    const cells = wrRow.querySelectorAll('td');

    // WR + You + tracked strategies + Consensus + 1 colspan cell (Actual + Δ merged) = 4 + N
    expect(cells.length).toBe(4 + DESK_TRACKED_STRATEGY_IDS.length);
  });
});

describe('DESK_TRACKED_STRATEGY_IDS — registry contract', () => {
  it('contains v9_ensemble (current LIVE) as the first entry', () => {
    expect(DESK_TRACKED_STRATEGY_IDS[0]).toBe('v9_ensemble');
  });

  it('every tracked id has a registry entry with a shortLabel', () => {
    for (let i = 0; i < DESK_TRACKED_STRATEGY_IDS.length; i++) {
      const meta = getStrategyMeta(DESK_TRACKED_STRATEGY_IDS[i], i);
      expect(meta).toBeTruthy();
      expect(typeof meta.shortLabel).toBe('string');
      expect(meta.shortLabel.length).toBeGreaterThan(0);
    }
  });
});
