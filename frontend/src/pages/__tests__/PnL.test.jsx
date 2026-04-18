/**
 * PnL page tests — for the useApi+useEffect version of the component.
 *
 * This PnL component does not use useApiLoader; it makes four direct api.get()
 * calls inside a useEffect. As a result there are no managed loading/error
 * states — failures are swallowed via console.warn. Tests focus on:
 *   - Static structure always present on mount
 *   - Data-driven renders once promises resolve
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

// ── mock recharts to avoid SVG rendering errors in jsdom ────────────────────
vi.mock('recharts', () => ({
  BarChart: ({ children }) => <div data-testid="bar-chart">{children}</div>,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  Cell: () => null,
}));

// ── mock sub-components that reference canvas / ResizeObserver ───────────────
vi.mock('../../components/EquityCurve', () => ({
  default: ({ data }) => <div data-testid="equity-curve">{data?.length ?? 0} points</div>,
}));

vi.mock('../../components/StatCard', () => ({
  default: ({ label, value }) => (
    <div data-testid={`stat-${label.replace(/[\s&]+/g, '-').toLowerCase()}`}>
      {label}: {value}
    </div>
  ),
}));

// ── mock auth + api ──────────────────────────────────────────────────────────
const mockGet = vi.fn();
const mockApiObj = { get: mockGet, post: vi.fn(), patch: vi.fn() };
vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => mockApiObj,
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import PnL from '../PnL.jsx';

// ── helpers ─────────────────────────────────────────────────────────────────

/** Stub all four api.get() calls with empty data. */
function stubAllEmpty() {
  mockGet.mockResolvedValue({ data: { items: [] } });
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── tests ────────────────────────────────────────────────────────────────────

describe('PnL page — static structure', () => {
  it('renders page heading', () => {
    stubAllEmpty();
    render(<PnL />);
    expect(screen.getByText('Profit & Loss')).toBeInTheDocument();
  });

  it('renders section labels for equity curve, daily chart, and monthly summary', () => {
    stubAllEmpty();
    render(<PnL />);
    expect(screen.getByText('Cumulative Equity')).toBeInTheDocument();
    expect(screen.getByText('Daily P&L')).toBeInTheDocument();
    expect(screen.getByText('Monthly Summary')).toBeInTheDocument();
  });

  it('renders the EquityCurve component', () => {
    stubAllEmpty();
    render(<PnL />);
    expect(screen.getByTestId('equity-curve')).toBeInTheDocument();
  });

  it('renders the BarChart component for daily P&L', () => {
    stubAllEmpty();
    render(<PnL />);
    expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
  });
});

describe('PnL page — data rendering', () => {
  it('shows stat cards when by-strategy data resolves', async () => {
    const byStrategy = {
      total_pnl: 1234.56,
      sharpe_ratio: 1.85,
      max_drawdown: -0.12,
      win_rate: 0.65,
      arb_pnl: 800,
      vpin_pnl: 434.56,
    };

    mockGet.mockImplementation((url) => {
      if (url.includes('by-strategy')) return Promise.resolve({ data: byStrategy });
      return Promise.resolve({ data: { items: [] } });
    });

    render(<PnL />);

    await waitFor(() => {
      expect(screen.getByTestId('stat-total-p-l')).toBeInTheDocument();
    });
    expect(screen.getByTestId('stat-sharpe')).toBeInTheDocument();
    expect(screen.getByTestId('stat-win-rate')).toBeInTheDocument();
  });

  it('shows monthly row data when monthly data resolves', async () => {
    const monthly = [
      { month: '2026-03', trade_count: 42, win_rate: 0.62, gross_pnl: 500, fees_paid: 30, net_pnl: 470 },
    ];

    mockGet.mockImplementation((url) => {
      if (url.includes('monthly')) return Promise.resolve({ data: { items: monthly } });
      return Promise.resolve({ data: { items: [] } });
    });

    render(<PnL />);

    await waitFor(() => {
      expect(screen.getByText('2026-03')).toBeInTheDocument();
    });
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  it('passes cumulative data to EquityCurve', async () => {
    const cumulative = [
      { date: '2026-03-01', cumulative_pnl: 100 },
      { date: '2026-03-02', cumulative_pnl: 150 },
    ];

    mockGet.mockImplementation((url) => {
      if (url.includes('cumulative')) return Promise.resolve({ data: { items: cumulative } });
      return Promise.resolve({ data: { items: [] } });
    });

    render(<PnL />);

    await waitFor(() => {
      expect(screen.getByText('2 points')).toBeInTheDocument();
    });
  });
});
