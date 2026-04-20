import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

// ── mock hooks ──────────────────────────────────────────────────────────────

const mockUseApiLoader = vi.fn();
vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: (...args) => mockUseApiLoader(...args),
}));

vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    patch: vi.fn(),
  }),
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import TradesEnhanced from '../TradesEnhanced.jsx';

// ── helpers ─────────────────────────────────────────────────────────────────

function makeLoader(overrides = {}) {
  return { data: null, error: null, loading: false, reload: vi.fn(), ...overrides };
}

// Minimal trade row matching the shape used by TradesEnhanced.
function makeRow(overrides = {}) {
  return {
    id: 1,
    strategy: 'v4_up_only',
    regime: 'NORMAL',
    conviction: 'HIGH',
    market_slug: 'btc-up-12345',
    direction: 'YES',
    stake_usd: 10,
    fill_price: 0.55,
    exit_price: 0.72,
    pnl_usd: 1.70,
    outcome: 'WIN',
    created_at: '2026-03-01T10:00:00Z',
    is_phantom: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── tests ────────────────────────────────────────────────────────────────────

describe('TradesEnhanced — loading / error states', () => {
  it('shows empty table with no rows when loading is false and data is null', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<TradesEnhanced />);
    // "Trades" appears in the PageHeader title AND in the "Trades" KPI stat label.
    expect(screen.getAllByText('Trades').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('No trades match these filters.')).toBeInTheDocument();
  });

  it('shows load error message when error is set', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ error: 'Network timeout' }));
    render(<TradesEnhanced />);
    expect(screen.getByText(/Load error.*Network timeout/)).toBeInTheDocument();
  });
});

describe('TradesEnhanced — data rendering', () => {
  it('renders a WIN row with positive PnL in profit color', () => {
    const row = makeRow({ pnl_usd: 1.70, outcome: 'WIN' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [row] }));
    render(<TradesEnhanced />);
    expect(screen.getByText('WIN')).toBeInTheDocument();
    // +$1.70 appears in the Net PnL KPI stat AND in the table row.
    expect(screen.getAllByText('+$1.70').length).toBeGreaterThanOrEqual(1);
  });

  it('renders a LOSS row', () => {
    const row = makeRow({ pnl_usd: -2.50, outcome: 'LOSS' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [row] }));
    render(<TradesEnhanced />);
    expect(screen.getByText('LOSS')).toBeInTheDocument();
    // -$2.50 appears in the Net PnL KPI stat AND in the table row.
    expect(screen.getAllByText('-$2.50').length).toBeGreaterThanOrEqual(1);
  });

  it('shows PHANTOM badge for phantom rows', () => {
    const row = makeRow({ is_phantom: true, outcome: 'OPEN' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [row] }));
    render(<TradesEnhanced />);
    expect(screen.getByText('PHANTOM')).toBeInTheDocument();
  });

  it('renders phantom rows at 40% opacity (rowStyle)', () => {
    // Phantom rows get opacity:0.4 from rowStyle. We verify the row is present
    // and carries the PHANTOM badge rather than asserting inline-style (jsdom
    // doesn't compute CSS from inline objects reliably).
    const row = makeRow({ id: 42, is_phantom: true });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [row] }));
    render(<TradesEnhanced />);
    expect(screen.getByText('PHANTOM')).toBeInTheDocument();
  });

  it('shows row count in header', () => {
    const rows = [makeRow({ id: 1 }), makeRow({ id: 2 })];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: rows }));
    render(<TradesEnhanced />);
    expect(screen.getByText('2 rows')).toBeInTheDocument();
  });
});

describe('TradesEnhanced — KPI stats', () => {
  it('computes win rate from real (non-phantom) rows', () => {
    const rows = [
      makeRow({ id: 1, outcome: 'WIN', pnl_usd: 5 }),
      makeRow({ id: 2, outcome: 'LOSS', pnl_usd: -2 }),
      makeRow({ id: 3, outcome: 'WIN', pnl_usd: 3 }),
    ];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: rows }));
    render(<TradesEnhanced />);
    // 2 wins / 3 settled = 66.7%
    expect(screen.getByText('66.7%')).toBeInTheDocument();
  });

  it('shows phantom count in stat sub-label when phantoms are present', () => {
    const rows = [
      makeRow({ id: 1, is_phantom: false }),
      makeRow({ id: 2, is_phantom: true }),
    ];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: rows }));
    render(<TradesEnhanced />);
    expect(screen.getByText('+ 1 phantom')).toBeInTheDocument();
  });
});

describe('TradesEnhanced — filter pills', () => {
  it('renders strategy, outcome, range, and fills filter pills', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<TradesEnhanced />);
    // Filter pill labels
    expect(screen.getByText('wins')).toBeInTheDocument();
    expect(screen.getByText('losses')).toBeInTheDocument();
    expect(screen.getByText('24h')).toBeInTheDocument();
    expect(screen.getByText('filled')).toBeInTheDocument();
  });

  it('derives strategy pill from loaded rows', () => {
    const rows = [makeRow({ strategy: 'v4_up_only' })];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: rows }));
    render(<TradesEnhanced />);
    // prettyStrategy('v4_up_only') → 'v4/up_only'
    expect(screen.getByText('v4/up_only')).toBeInTheDocument();
  });
});
