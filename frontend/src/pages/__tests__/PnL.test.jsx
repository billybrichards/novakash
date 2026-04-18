import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// Mock useApiLoader to control data states
const mockUseApiLoader = vi.fn();
vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: (...args) => mockUseApiLoader(...args),
}));

// Mock recharts to avoid rendering SVGs in jsdom
vi.mock('recharts', () => ({
  ComposedChart: ({ children }) => <div data-testid="composed-chart">{children}</div>,
  AreaChart: ({ children }) => <div data-testid="area-chart">{children}</div>,
  Area: () => null,
  // Render `name` fields from data so strategy name assertions work
  BarChart: ({ children, data }) => (
    <div data-testid="bar-chart">
      {data?.map((d, i) => d.name ? <span key={i}>{d.name}</span> : null)}
      {children}
    </div>
  ),
  Bar: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  CartesianGrid: () => null,
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  Cell: () => null,
  ReferenceArea: () => <div data-testid="reference-area" />,
  ReferenceLine: () => null,
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

import PnL from '../PnL.jsx';

function makeLoader(overrides = {}) {
  return { data: null, error: null, loading: false, reload: vi.fn(), ...overrides };
}

// Dates within 30 days of 2026-04-17 (the fixed "today" in this project)
const CUMULATIVE_DATA = [
  { date: '2026-03-20', cumulative_pnl: 0 },
  { date: '2026-03-28', cumulative_pnl: 200 },
  { date: '2026-04-05', cumulative_pnl: 100 },
  { date: '2026-04-15', cumulative_pnl: 150 },
];

const DAILY_DATA = [
  { date: '2026-03-20', net_pnl: 50 },
  { date: '2026-03-28', net_pnl: -20 },
  { date: '2026-04-05', net_pnl: 80 },
];

const MONTHLY_DATA = [
  { month: '2026-03', trade_count: 42, win_rate: 0.62, gross_pnl: 500, fees_paid: 30, net_pnl: 470 },
];

const BY_STRATEGY_DATA = {
  total_pnl: 1234.56,
  sharpe_ratio: 1.85,
  max_drawdown: -0.12,
  win_rate: 0.65,
  arb_pnl: 800,
  vpin_pnl: 434.56,
};

// Helper: return data for a specific call index (1-based)
// Call order: 1=cumulative, 2=daily, 3=monthly, 4=byStrategy
function callMap(map) {
  let call = 0;
  return () => {
    call++;
    return map[call] ?? makeLoader();
  };
}

describe('PnL page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ── Loading / Error / Empty ──────────────────────────────────────────────

  it('shows loading state when all fetches are loading', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ loading: true }));
    render(<PnL />);
    expect(screen.getByText('Loading P&L data...')).toBeInTheDocument();
  });

  it('shows error message when a fetch fails', () => {
    let call = 0;
    mockUseApiLoader.mockImplementation(() => {
      call++;
      if (call === 1) return makeLoader({ error: 'Network timeout' });
      return makeLoader();
    });
    render(<PnL />);
    expect(screen.getByText('Network timeout')).toBeInTheDocument();
  });

  it('shows empty states when no data is available', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<PnL />);
    expect(screen.getByText('No cumulative P&L data.')).toBeInTheDocument();
    expect(screen.getByText('No daily P&L data.')).toBeInTheDocument();
    expect(screen.getByText('No monthly summary data.')).toBeInTheDocument();
  });

  // ── Page Structure ────────────────────────────────────────────────────────

  it('renders page header with correct title', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<PnL />);
    expect(screen.getByText('Profit & Loss')).toBeInTheDocument();
  });

  it('renders filter pills for range selection', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<PnL />);
    expect(screen.getByText('30d')).toBeInTheDocument();
    expect(screen.getByText('90d')).toBeInTheDocument();
    expect(screen.getByText('all')).toBeInTheDocument();
  });

  // ── Cumulative Equity Chart ────────────────────────────────────────────────

  it('renders cumulative chart when data is present', () => {
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: CUMULATIVE_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Cumulative Equity')).toBeInTheDocument();
    expect(screen.getByTestId('cumulative-chart-wrapper')).toBeInTheDocument();
    expect(screen.getByTestId('composed-chart')).toBeInTheDocument();
  });

  it('renders Sharpe trend legend when cumulative data is present', () => {
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: CUMULATIVE_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText(/Rolling Sharpe/)).toBeInTheDocument();
  });

  it('shows max-drawdown period legend when there is a drawdown', () => {
    // Give a clear drawdown: 200 → 100 → 50 (dates within 30-day window)
    const ddData = [
      { date: '2026-03-20', cumulative_pnl: 0 },
      { date: '2026-03-28', cumulative_pnl: 200 },
      { date: '2026-04-05', cumulative_pnl: 100 },
      { date: '2026-04-15', cumulative_pnl: 50 },
    ];
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: ddData }),
    }));
    render(<PnL />);
    // Legend text appears when ddStart is found
    expect(screen.getByText(/Max Drawdown Period/)).toBeInTheDocument();
  });

  it('does not show drawdown legend when cumulative data is flat (no drawdown)', () => {
    const flatData = [
      { date: '2026-04-01', cumulative_pnl: 100 },
      { date: '2026-04-08', cumulative_pnl: 110 },
      { date: '2026-04-15', cumulative_pnl: 120 },
    ];
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: flatData }),
    }));
    render(<PnL />);
    expect(screen.queryByText(/Max Drawdown Period/)).not.toBeInTheDocument();
  });

  // ── Daily P&L Chart ───────────────────────────────────────────────────────

  it('renders daily P&L bar chart when daily data is present', () => {
    mockUseApiLoader.mockImplementation(callMap({
      2: makeLoader({ data: DAILY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Daily P&L')).toBeInTheDocument();
    expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
  });

  // ── Stat Cards ─────────────────────────────────────────────────────────────

  it('renders stat cards when by-strategy data is present', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: BY_STRATEGY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Total P&L')).toBeInTheDocument();
    expect(screen.getByText('Sharpe')).toBeInTheDocument();
    expect(screen.getByText('Max Drawdown')).toBeInTheDocument();
    expect(screen.getByText('Win Rate')).toBeInTheDocument();
    expect(screen.getByText('Arb P&L')).toBeInTheDocument();
    expect(screen.getByText('VPIN P&L')).toBeInTheDocument();
  });

  it('formats sharpe ratio to 2 decimal places in stat card', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: BY_STRATEGY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('1.85')).toBeInTheDocument();
  });

  it('shows em-dash when sharpe_ratio is null', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: { ...BY_STRATEGY_DATA, sharpe_ratio: null } }),
    }));
    render(<PnL />);
    // em-dash rendered as unicode
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('does not render stat cards when by-strategy data is an array', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: [BY_STRATEGY_DATA] }),
    }));
    render(<PnL />);
    expect(screen.queryByText('Total P&L')).not.toBeInTheDocument();
  });

  // ── Per-Strategy Breakdown ─────────────────────────────────────────────────

  it('renders per-strategy breakdown chart when stats are available', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: BY_STRATEGY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Per-Strategy P&L Breakdown')).toBeInTheDocument();
    expect(screen.getByTestId('strategy-breakdown-chart')).toBeInTheDocument();
  });

  it('shows correct strategy names in breakdown chart', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: BY_STRATEGY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Sub-$1 Arb')).toBeInTheDocument();
    expect(screen.getByText('VPIN Cascade')).toBeInTheDocument();
  });

  it('does not render breakdown chart when stats are absent', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<PnL />);
    expect(screen.queryByText('Per-Strategy P&L Breakdown')).not.toBeInTheDocument();
  });

  // ── Monthly Summary ────────────────────────────────────────────────────────

  it('renders monthly summary table when monthly data is present', () => {
    mockUseApiLoader.mockImplementation(callMap({
      3: makeLoader({ data: MONTHLY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Monthly Summary')).toBeInTheDocument();
    expect(screen.getByText('2026-03')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  // ── Edge Cases ────────────────────────────────────────────────────────────

  it('handles single cumulative data point without crashing', () => {
    const singlePoint = [{ date: '2026-04-15', cumulative_pnl: 100 }];
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: singlePoint }),
    }));
    expect(() => render(<PnL />)).not.toThrow();
    expect(screen.getByTestId('cumulative-chart-wrapper')).toBeInTheDocument();
  });

  it('handles empty daily data without crashing during Sharpe computation', () => {
    // Use a copy of CUMULATIVE_DATA (already has recent dates)
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: [...CUMULATIVE_DATA] }),
      2: makeLoader({ data: [] }),
    }));
    expect(() => render(<PnL />)).not.toThrow();
  });

  it('handles by-strategy data with zero pnl values', () => {
    mockUseApiLoader.mockImplementation(callMap({
      4: makeLoader({ data: { ...BY_STRATEGY_DATA, arb_pnl: 0, vpin_pnl: 0 } }),
    }));
    render(<PnL />);
    expect(screen.getByText('Per-Strategy P&L Breakdown')).toBeInTheDocument();
  });

  it('renders all section headers simultaneously when all data is loaded', () => {
    mockUseApiLoader.mockImplementation(callMap({
      1: makeLoader({ data: CUMULATIVE_DATA }),
      2: makeLoader({ data: DAILY_DATA }),
      3: makeLoader({ data: MONTHLY_DATA }),
      4: makeLoader({ data: BY_STRATEGY_DATA }),
    }));
    render(<PnL />);
    expect(screen.getByText('Cumulative Equity')).toBeInTheDocument();
    expect(screen.getByText('Per-Strategy P&L Breakdown')).toBeInTheDocument();
    expect(screen.getByText('Daily P&L')).toBeInTheDocument();
    expect(screen.getByText('Monthly Summary')).toBeInTheDocument();
  });
});
