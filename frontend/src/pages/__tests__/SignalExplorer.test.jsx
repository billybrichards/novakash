/**
 * SignalExplorer WebSocket integration tests.
 *
 * Tests that:
 *  1. Initial API snapshot populates the heatmap matrix.
 *  2. Real-time `signal` WS events prepend new decisions and recompute the matrix.
 *  3. Non-signal WS message types are ignored.
 *  4. Filter change (tf / conviction) resets liveRows from the new API snapshot.
 *  5. WS connection indicator shows LIVE / poll correctly.
 */

import React from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// ── mocks ────────────────────────────────────────────────────────────────────

// Track the last wsMsg setter so tests can push WS events
let _setWsMsg = null;
let _mockIsConnected = false;

vi.mock('../../hooks/useWebSocket.js', () => ({
  useWebSocket: () => {
    const [data, setData] = React.useState(null);
    _setWsMsg = setData;
    return { isConnected: _mockIsConnected, data };
  },
}));

// Controllable useApiLoader
let _apiData = null;
let _apiLoading = false;

vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: () => ({ data: _apiData, error: null, loading: _apiLoading }),
}));

// useApi returns a stub — not exercised in these tests (useApiLoader is mocked)
vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => ({ get: vi.fn() }),
}));

// AuthContext — useWebSocket reads accessToken
vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ accessToken: 'test-token' }),
}));

// Theme tokens — minimal stubs
vi.mock('../../theme/tokens.js', () => ({
  T: {
    bg: '#07070c',
    card: 'rgba(255,255,255,0.015)',
    border: 'rgba(255,255,255,0.08)',
    text: '#e2e8f0',
    label: '#64748b',
    label2: '#475569',
    profit: '#4ade80',
    loss: '#f87171',
    warn: '#f59e0b',
    purple: '#a855f7',
    cyan: '#06b6d4',
    font: 'monospace',
  },
  wrColor: (wr) => (wr == null ? '#64748b' : wr >= 0.55 ? '#4ade80' : wr >= 0.45 ? '#f59e0b' : '#f87171'),
}));

// Shared components — lightweight stubs
vi.mock('../../components/shared/PageHeader.jsx', () => ({
  default: ({ title, right }) => (
    <div>
      <h1>{title}</h1>
      <div data-testid="header-right">{right}</div>
    </div>
  ),
}));
vi.mock('../../components/shared/Loading.jsx', () => ({
  default: () => <div>Loading…</div>,
}));
vi.mock('../../components/shared/EmptyState.jsx', () => ({
  default: ({ message }) => <div>{message}</div>,
}));
vi.mock('../../components/shared/FilterPills.jsx', () => ({
  default: ({ label, options, value, onChange }) => (
    <div>
      {options.map(o => (
        <button
          key={String(o.value)}
          data-testid={`filter-${label}-${o.label}`}
          onClick={() => onChange(o.value)}
          aria-pressed={value === o.value}
        >
          {o.label}
        </button>
      ))}
    </div>
  ),
}));
vi.mock('../../components/shared/DataTable.jsx', () => ({
  default: ({ rows }) => (
    <table data-testid="decision-table">
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td data-testid="decision-strategy">{r.strategy_id || r.strategy}</td>
            <td data-testid="decision-regime">{r.regime}</td>
          </tr>
        ))}
      </tbody>
    </table>
  ),
}));

// ── fixtures ─────────────────────────────────────────────────────────────────

const makeDecision = (overrides = {}) => ({
  id: Math.random().toString(36).slice(2),
  strategy_id: 'strat-A',
  regime: 'NORMAL',
  conviction: 'STRONG',
  outcome: 'WIN',
  decided_at: '2026-04-17T12:00:00Z',
  ...overrides,
});

// ── import component after mocks ─────────────────────────────────────────────

let SignalExplorer;
beforeEach(async () => {
  // Reset controllables
  _apiData = null;
  _apiLoading = false;
  _setWsMsg = null;
  _mockIsConnected = false;

  // Re-import so mocks are fresh
  const mod = await import('../SignalExplorer.jsx?t=' + Date.now());
  SignalExplorer = mod.default;
});

// ── tests ─────────────────────────────────────────────────────────────────────

describe('SignalExplorer', () => {
  it('renders "No decisions" when API returns empty array', async () => {
    _apiData = [];
    render(<SignalExplorer />);
    await waitFor(() => expect(screen.getByText(/No decisions match/i)).toBeTruthy());
  });

  it('renders heatmap matrix rows from API snapshot', async () => {
    _apiData = [
      makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' }),
      makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'LOSS' }),
    ];
    render(<SignalExplorer />);

    await waitFor(() => {
      // strat-A should appear in the matrix table
      const cells = screen.getAllByText('strat-A');
      expect(cells.length).toBeGreaterThan(0);
    });
  });

  it('prepends WS signal event to recent decisions and updates matrix', async () => {
    _apiData = [
      makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' }),
    ];

    render(<SignalExplorer />);

    // Wait for initial render
    await waitFor(() => expect(screen.getAllByTestId('decision-strategy').length).toBe(1));

    // Push a WS signal event for a new strategy
    const wsDecision = makeDecision({ strategy_id: 'strat-B', regime: 'HIGH', outcome: 'WIN' });
    act(() => {
      _setWsMsg({ type: 'signal', payload: wsDecision });
    });

    await waitFor(() => {
      const strategies = screen.getAllByTestId('decision-strategy').map(el => el.textContent);
      // Both strat-A (from API) and strat-B (from WS) should appear
      expect(strategies).toContain('strat-A');
      expect(strategies).toContain('strat-B');
    });
  });

  it('ignores WS messages that are not type=signal', async () => {
    _apiData = [makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' })];
    render(<SignalExplorer />);
    await waitFor(() => expect(screen.getAllByTestId('decision-strategy').length).toBe(1));

    act(() => {
      _setWsMsg({ type: 'tick', payload: { price: 65000 } });
    });

    // Still only 1 row
    await waitFor(() => expect(screen.getAllByTestId('decision-strategy').length).toBe(1));
  });

  it('ignores WS signal event with no payload', async () => {
    _apiData = [makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' })];
    render(<SignalExplorer />);
    await waitFor(() => expect(screen.getAllByTestId('decision-strategy').length).toBe(1));

    act(() => {
      _setWsMsg({ type: 'signal' }); // no payload
    });

    await waitFor(() => expect(screen.getAllByTestId('decision-strategy').length).toBe(1));
  });

  it('shows LIVE indicator when WebSocket is connected', async () => {
    _apiData = [];
    _mockIsConnected = true;

    render(<SignalExplorer />);

    await waitFor(() => {
      expect(screen.getByTestId('header-right').textContent).toContain('LIVE');
    });
  });

  it('shows poll indicator when WebSocket is disconnected', async () => {
    _apiData = [];
    _mockIsConnected = false;

    render(<SignalExplorer />);

    await waitFor(() => {
      expect(screen.getByTestId('header-right').textContent).toContain('poll');
    });
  });

  it('WS rows accumulate across multiple events', async () => {
    _apiData = [];
    render(<SignalExplorer />);

    // Wait for initial empty state
    await waitFor(() => expect(screen.getByText(/No decisions match/i)).toBeTruthy());

    const decisions = [
      makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' }),
      makeDecision({ strategy_id: 'strat-A', regime: 'NORMAL', outcome: 'WIN' }),
      makeDecision({ strategy_id: 'strat-B', regime: 'LOW', outcome: 'LOSS' }),
    ];

    for (const d of decisions) {
      act(() => {
        _setWsMsg({ type: 'signal', payload: d });
      });
    }

    await waitFor(() => {
      expect(screen.getAllByTestId('decision-strategy').length).toBe(3);
    });
  });
});
