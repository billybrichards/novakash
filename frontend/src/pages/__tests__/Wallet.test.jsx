import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock useApiLoader — controls snap (call 1), pending (call 2), recent (call 3)
const mockUseApiLoader = vi.fn();
vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: (...args) => mockUseApiLoader(...args),
}));

vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ data: {} }),
    post: vi.fn(),
  }),
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import Wallet from '../Wallet.jsx';

function makeLoader(overrides = {}) {
  return { data: null, error: null, loading: false, reload: vi.fn(), ...overrides };
}

// Factory for a realistic trade object used by the pending panel and ledger.
function makeTrade(overrides = {}) {
  return {
    id: 'trade-001',
    market_slug: 'btc-updown-5m-1776414300',
    direction: 'YES',
    stake_usd: 10.0,
    pnl_usd: 5.0,
    outcome: 'WIN',
    status: 'RESOLVED_WIN',
    redeemed: false,
    created_at: '2026-04-17T03:30:00Z',
    resolved_at: '2026-04-17T03:35:00Z',
    strategy_id: 'v4_5min',
    ...overrides,
  };
}

describe('Wallet page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders page header', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<Wallet />);
    expect(screen.getByText('Wallet')).toBeInTheDocument();
  });

  it('shows loading spinner when balance snapshot is loading', () => {
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 1) return makeLoader({ loading: true });
      return makeLoader();
    });
    render(<Wallet />);
    // Loading component renders a spinner — DataTable is empty when pending rows = 0
    expect(screen.queryByText('Balance snapshot unavailable.')).not.toBeInTheDocument();
  });

  it('shows fallback message when balance snapshot errors', () => {
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 1) return makeLoader({ error: 'Not found', data: null });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText('Balance snapshot unavailable.')).toBeInTheDocument();
    expect(screen.getByText(/audit-task #217/)).toBeInTheDocument();
  });

  it('renders balance stat cards when snapshot data is present', () => {
    const snapshotData = {
      usdc_proxy: 482.5,
      usdc_eoa: 12.0,
      matic_eoa: 0.9312,
      sources_agreed: true,
      block_number: 71234567,
    };
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 1) return makeLoader({ data: snapshotData });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText('USDC (proxy)')).toBeInTheDocument();
    expect(screen.getByText('$482.50')).toBeInTheDocument();
    expect(screen.getByText('USDC (EOA)')).toBeInTheDocument();
    expect(screen.getByText('$12.00')).toBeInTheDocument();
    expect(screen.getByText(/0\.9312 MATIC/)).toBeInTheDocument();
    expect(screen.getByText(/sources agree/)).toBeInTheDocument();
    expect(screen.getByText(/block 71234567/)).toBeInTheDocument();
  });

  it('renders consensus cell with count breakdown when agreed_count/total_count present', () => {
    const snapshotData = {
      usdc_proxy: 100,
      agreed_count: 2,
      total_count: 3,
    };
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 1) return makeLoader({ data: snapshotData });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText(/2 of 3 agree/)).toBeInTheDocument();
  });

  it('shows empty state when no pending wins', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<Wallet />);
    expect(screen.getByText('No pending wins. All resolved positions are redeemed.')).toBeInTheDocument();
  });

  it('renders pending wins panel with trade rows', () => {
    const trade = makeTrade({ id: 'trade-abc', payout: 15.0 });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 2) return makeLoader({ data: [trade] });
      return makeLoader();
    });
    render(<Wallet />);
    // Header label should be visible
    expect(screen.getByText('Pending wins · unredeemed')).toBeInTheDocument();
    // Trade market slug should appear in the table
    expect(screen.getByText('btc-updown-5m-1776414300')).toBeInTheDocument();
  });

  it('filters out already-redeemed trades from pending panel', () => {
    const redeemedTrade = makeTrade({ id: 'redeemed-1', redeemed: true });
    const pendingTrade = makeTrade({ id: 'pending-1', redeemed: false, market_slug: 'btc-updown-5m-9999999999' });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 2) return makeLoader({ data: [redeemedTrade, pendingTrade] });
      return makeLoader();
    });
    render(<Wallet />);
    // Only the unredeemed trade should appear
    expect(screen.getByText('btc-updown-5m-9999999999')).toBeInTheDocument();
    expect(screen.queryByText('btc-updown-5m-1776414300')).not.toBeInTheDocument();
    // Filtered count note should appear
    expect(screen.getByText(/filtered 1 already-redeemed/)).toBeInTheDocument();
  });

  it('renders copy CLI button for pending wins', () => {
    const trade = makeTrade({ id: 'trade-copytest' });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 2) return makeLoader({ data: [trade] });
      return makeLoader();
    });
    render(<Wallet />);
    const copyBtn = screen.getByRole('button', { name: /copy redeem command for position trade-copytest/ });
    expect(copyBtn).toBeInTheDocument();
    expect(copyBtn).toHaveTextContent('copy CLI cmd');
  });

  it('shows activity ledger panel heading', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<Wallet />);
    expect(screen.getByText(/Activity ledger/)).toBeInTheDocument();
  });

  it('builds ledger rows from recent trades and shows net P&L summary', () => {
    const winTrade = makeTrade({
      id: 'win-1',
      outcome: 'WIN',
      status: 'RESOLVED_WIN',
      pnl_usd: 5.0,
      created_at: '2026-04-17T03:30:00Z',
      resolved_at: '2026-04-17T03:35:00Z',
    });
    const lossTrade = makeTrade({
      id: 'loss-1',
      outcome: 'LOSS',
      status: 'RESOLVED_LOSS',
      pnl_usd: -3.0,
      market_slug: 'btc-updown-5m-1776414600',
      created_at: '2026-04-17T03:35:00Z',
      resolved_at: '2026-04-17T03:40:00Z',
    });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 3) return makeLoader({ data: [winTrade, lossTrade] });
      return makeLoader();
    });
    render(<Wallet />);
    // Should show 1W / 1L
    expect(screen.getByText('1W')).toBeInTheDocument();
    expect(screen.getByText('1L')).toBeInTheDocument();
    expect(screen.getByText(/2 settled/)).toBeInTheDocument();
  });

  it('shows REDEEM event in ledger for WIN trades', () => {
    const winTrade = makeTrade({
      id: 'win-event',
      outcome: 'WIN',
      pnl_usd: 5.0,
      resolved_at: '2026-04-17T03:35:00Z',
    });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 3) return makeLoader({ data: [winTrade] });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText('REDEEM')).toBeInTheDocument();
  });

  it('shows direction YES/NO labels in ledger', () => {
    const yesTrade = makeTrade({
      id: 'yes-trade',
      direction: 'YES',
      outcome: 'WIN',
      pnl_usd: 2.0,
      resolved_at: '2026-04-17T03:35:00Z',
    });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 3) return makeLoader({ data: [yesTrade] });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText('YES')).toBeInTheDocument();
  });

  it('flags 2-leg windows in ledger notes column', () => {
    // Two trades with the same market_slug timestamp → 2-leg detection
    const legA = makeTrade({
      id: 'leg-a',
      market_slug: 'btc-updown-5m-1776414300',
      outcome: 'WIN',
      pnl_usd: 5.0,
      resolved_at: '2026-04-17T03:35:00Z',
    });
    const legB = makeTrade({
      id: 'leg-b',
      market_slug: 'btc-updown-5m-1776414300',
      direction: 'NO',
      outcome: 'LOSS',
      pnl_usd: -2.0,
      resolved_at: '2026-04-17T03:35:05Z',
    });
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 3) return makeLoader({ data: [legA, legB] });
      return makeLoader();
    });
    render(<Wallet />);
    // Both trades share the same window key → "2-leg" should appear
    const twoLegLabels = screen.getAllByText(/2-leg/);
    expect(twoLegLabels.length).toBeGreaterThanOrEqual(1);
  });

  it('shows load error in ledger panel when recent fetch fails', () => {
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 3) return makeLoader({ error: 'Gateway timeout' });
      return makeLoader();
    });
    render(<Wallet />);
    expect(screen.getByText(/Gateway timeout/)).toBeInTheDocument();
  });

  it('shows read-only notice in page subtitle', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<Wallet />);
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
  });
});
