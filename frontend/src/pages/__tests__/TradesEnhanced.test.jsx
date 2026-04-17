import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';

const mockUseApiLoader = vi.fn();
vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: (...args) => mockUseApiLoader(...args),
}));

vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
  }),
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import TradesEnhanced from '../TradesEnhanced.jsx';

const makeLoader = (overrides = {}) => ({
  data: null, error: null, loading: false, reload: vi.fn(), ...overrides,
});

const REAL_TRADES = [
  { id: 1, strategy: 'v4_5m', outcome: 'WIN', pnl_usd: 5, stake_usd: 10, is_phantom: false, created_at: '2026-04-17T12:00:00' },
  { id: 2, strategy: 'v4_5m', outcome: 'LOSS', pnl_usd: -3, stake_usd: 10, is_phantom: false, created_at: '2026-04-17T12:01:00' },
];

const PHANTOM_TRADES = [
  { id: 3, strategy: 'v4_5m', outcome: 'WIN', pnl_usd: 7, stake_usd: 10, is_phantom: true, created_at: '2026-04-17T12:02:00' },
  { id: 4, strategy: 'v4_5m', outcome: 'LOSS', pnl_usd: -2, stake_usd: 10, is_phantom: true, created_at: '2026-04-17T12:03:00' },
];

describe('TradesEnhanced page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders page header', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<TradesEnhanced />);
    expect(screen.getByRole('heading', { name: 'Trades' })).toBeInTheDocument();
  });

  it('does not show phantom banner when there are no phantom trades', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: REAL_TRADES }));
    render(<TradesEnhanced />);
    expect(screen.queryByTestId('phantom-banner')).not.toBeInTheDocument();
  });

  it('shows phantom banner with correct count when phantom trades exist', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    const banner = screen.getByTestId('phantom-banner');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent('2 phantom trades excluded from stats');
  });

  it('uses singular "trade" when there is exactly 1 phantom', () => {
    const singlePhantom = [{ id: 5, strategy: 'v4_5m', outcome: 'WIN', pnl_usd: 7, stake_usd: 10, is_phantom: true }];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...singlePhantom] }));
    render(<TradesEnhanced />);
    expect(screen.getByTestId('phantom-banner')).toHaveTextContent('1 phantom trade excluded from stats');
  });

  it('shows "hide" toggle button by default when phantoms present', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    expect(screen.getByTestId('phantom-toggle')).toHaveTextContent('hide');
  });

  it('toggle switches to "show" after clicking hide', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    fireEvent.click(screen.getByTestId('phantom-toggle'));
    expect(screen.getByTestId('phantom-toggle')).toHaveTextContent('show');
  });

  it('hides phantom rows from table after toggling off', () => {
    const allTrades = [...REAL_TRADES, ...PHANTOM_TRADES];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: allTrades }));
    render(<TradesEnhanced />);

    // Before toggle: 4 rows in the table (DataTable renders tbody tr per row)
    const rowsBefore = screen.getAllByRole('row').filter(r => r.closest('tbody'));
    expect(rowsBefore).toHaveLength(4);

    fireEvent.click(screen.getByTestId('phantom-toggle'));

    // After toggle: only 2 real rows
    const rowsAfter = screen.getAllByRole('row').filter(r => r.closest('tbody'));
    expect(rowsAfter).toHaveLength(2);
  });

  it('KPI Trades card excludes phantom count from main value', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    // kpi.n = 2 (real only), the Trades stat card shows '2'
    // The sub-text says '+ 2 phantom'
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('+ 2 phantom')).toBeInTheDocument();
  });

  it('shows phantom win-rate as sub-text in Win rate card', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    // phantom win-rate = 1/2 = 50.0%
    expect(screen.getByText('phantom: 50.0%')).toBeInTheDocument();
  });

  it('shows phantom PnL as sub-text in Net PnL card', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [...REAL_TRADES, ...PHANTOM_TRADES] }));
    render(<TradesEnhanced />);
    // phantom net = 7 - 2 = 5, fmtUSD(5) = '+$5.00'
    expect(screen.getByText('phantom: +$5.00')).toBeInTheDocument();
  });

  it('shows error message when load fails', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ error: 'Server error' }));
    render(<TradesEnhanced />);
    expect(screen.getByText('Load error: Server error')).toBeInTheDocument();
  });
});
