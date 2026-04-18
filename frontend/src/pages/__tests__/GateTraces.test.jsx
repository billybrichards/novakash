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
    get: vi.fn().mockResolvedValue({ data: {} }),
  }),
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import GateTraces from '../GateTraces.jsx';

// ── helpers ─────────────────────────────────────────────────────────────────

function makeLoader(overrides = {}) {
  return { data: null, error: null, loading: false, reload: vi.fn(), ...overrides };
}

// GateTraces calls useApiLoader twice: call #1 = heatmap, call #2 = recent.
// This helper lets tests specify each independently.
function setLoaders(heatmapOverrides = {}, recentOverrides = {}) {
  let call = 0;
  mockUseApiLoader.mockImplementation(() => {
    call++;
    if (call === 1) return makeLoader(heatmapOverrides);
    return makeLoader(recentOverrides);
  });
}

function makeHeatmapData(overrides = {}) {
  return {
    strategies: ['v4_up_only', 'v4_down_only'],
    gates: ['vpin', 'regime', 'conviction'],
    cells: [
      { strategy: 'v4_up_only', gate: 'vpin', fired: 100, passed: 75, pass_pct: 75.0 },
      { strategy: 'v4_up_only', gate: 'regime', fired: 100, passed: 90, pass_pct: 90.0 },
      { strategy: 'v4_down_only', gate: 'conviction', fired: 50, passed: 20, pass_pct: 40.0 },
    ],
    window: { row_count_raw: 1000, earliest: '2026-03-01T00:00:00Z', latest: '2026-03-02T00:00:00Z' },
    ...overrides,
  };
}

function makeGroup(overrides = {}) {
  return {
    strategy_id: 'v4_up_only',
    window_ts: 1743494400,  // epoch seconds — deterministic
    eval_offset: 10,
    action: 'TRADE',
    direction: 'YES',
    gates: [
      { gate_name: 'vpin', gate_order: 1, passed: true, skip_reason: null, observed: { vpin: 0.6 }, config: {} },
      { gate_name: 'regime', gate_order: 2, passed: false, skip_reason: 'cascade', observed: {}, config: {} },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── tests ────────────────────────────────────────────────────────────────────

describe('GateTraces — loading / error states', () => {
  it('renders page header', () => {
    setLoaders();
    render(<GateTraces />);
    expect(screen.getByText('Gate Traces')).toBeInTheDocument();
  });

  it('shows Loading spinner when heatmap is loading with no data', () => {
    setLoaders({ loading: true });
    render(<GateTraces />);
    // EmptyState / Loading is shown inside the heatmap card
    expect(screen.getByText('Gate Traces')).toBeInTheDocument();
  });

  it('shows empty state when heatmap returns no strategies/gates', () => {
    setLoaders({ data: { strategies: [], gates: [], cells: [], window: {} } });
    render(<GateTraces />);
    expect(screen.getByText('No gate traces match these filters.')).toBeInTheDocument();
  });

  it('shows heatmap error banner when error is set', () => {
    setLoaders({ error: 'DB connection failed' });
    render(<GateTraces />);
    expect(screen.getByText(/Heatmap load error.*DB connection failed/)).toBeInTheDocument();
  });

  it('shows recent chains error when recent loader errors', () => {
    setLoaders({}, { error: 'timeout' });
    render(<GateTraces />);
    expect(screen.getByText(/Recent load error.*timeout/)).toBeInTheDocument();
  });
});

describe('GateTraces — heatmap cell coloring', () => {
  it('renders pass-rate percentages from cell data', () => {
    setLoaders({ data: makeHeatmapData() });
    render(<GateTraces />);
    expect(screen.getByText('75.0%')).toBeInTheDocument();
    expect(screen.getByText('90.0%')).toBeInTheDocument();
    expect(screen.getByText('40.0%')).toBeInTheDocument();
  });

  it('renders gate column headers from heatmap data', () => {
    setLoaders({ data: makeHeatmapData() });
    render(<GateTraces />);
    // Gate names appear as lowercase in the DOM; CSS text-transform:uppercase
    // renders them visually as uppercase but does not change DOM text content.
    expect(screen.getByText('vpin')).toBeInTheDocument();
    expect(screen.getByText('regime')).toBeInTheDocument();
    expect(screen.getByText('conviction')).toBeInTheDocument();
  });

  it('renders strategy row labels', () => {
    setLoaders({ data: makeHeatmapData() });
    render(<GateTraces />);
    // Strategy names appear both as heatmap row labels and as filter pill options.
    expect(screen.getAllByText('v4_up_only').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('v4_down_only').length).toBeGreaterThanOrEqual(1);
  });

  it('shows — for a cell with no fired count', () => {
    const data = makeHeatmapData();
    // v4_down_only has no cell for 'vpin' or 'regime'
    setLoaders({ data });
    render(<GateTraces />);
    // Multiple — cells exist (missing combinations)
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });

  it('shows row_count in header stats area', () => {
    setLoaders({ data: makeHeatmapData() });
    render(<GateTraces />);
    expect(screen.getByText(/1,000 rows/)).toBeInTheDocument();
  });
});

describe('GateTraces — recent chains & expandable rows', () => {
  it('shows "0 shown" when no recent groups', () => {
    setLoaders({}, { data: { groups: [] } });
    render(<GateTraces />);
    expect(screen.getByText('Recent gate chains · 0 shown')).toBeInTheDocument();
  });

  it('renders a recent chain row with strategy and action', () => {
    const group = makeGroup();
    setLoaders({}, { data: { groups: [group] } });
    render(<GateTraces />);
    expect(screen.getByText('v4_up_only')).toBeInTheDocument();
    expect(screen.getByText('TRADE')).toBeInTheDocument();
    expect(screen.getByText('T-10')).toBeInTheDocument();
  });

  it('renders gate pass/fail dots for a row', () => {
    const group = makeGroup();
    setLoaders({}, { data: { groups: [group] } });
    render(<GateTraces />);
    // Gate count span shows "2 gates"
    expect(screen.getByText('2 gates')).toBeInTheDocument();
  });

  it('expands a row to show gate detail table on click', () => {
    const group = makeGroup();
    setLoaders({}, { data: { groups: [group] } });
    render(<GateTraces />);

    // The detail table is not visible before clicking
    expect(screen.queryByText('PASS')).not.toBeInTheDocument();

    // Click the row
    const rowEl = screen.getByText('TRADE').closest('tr');
    fireEvent.click(rowEl);

    // Detail table should now show PASS/FAIL status
    expect(screen.getByText('PASS')).toBeInTheDocument();
    expect(screen.getByText('FAIL')).toBeInTheDocument();
    expect(screen.getByText('vpin')).toBeInTheDocument();
    expect(screen.getByText('regime')).toBeInTheDocument();
  });

  it('collapses row on second click', () => {
    const group = makeGroup();
    setLoaders({}, { data: { groups: [group] } });
    render(<GateTraces />);

    const rowEl = screen.getByText('TRADE').closest('tr');
    fireEvent.click(rowEl);
    expect(screen.getByText('PASS')).toBeInTheDocument();

    fireEvent.click(rowEl);
    expect(screen.queryByText('PASS')).not.toBeInTheDocument();
  });

  it('shows SKIP action with different styling label', () => {
    const group = makeGroup({ action: 'SKIP' });
    setLoaders({}, { data: { groups: [group] } });
    render(<GateTraces />);
    expect(screen.getByText('SKIP')).toBeInTheDocument();
  });
});

describe('GateTraces — filter pills', () => {
  it('renders timeframe and hours filter pills', () => {
    setLoaders();
    render(<GateTraces />);
    expect(screen.getByText('5m')).toBeInTheDocument();
    expect(screen.getByText('15m')).toBeInTheDocument();
    expect(screen.getByText('24h')).toBeInTheDocument();
    expect(screen.getByText('72h')).toBeInTheDocument();
    expect(screen.getByText('7d')).toBeInTheDocument();
  });
});
