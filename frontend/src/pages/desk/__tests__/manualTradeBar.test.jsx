// manualTradeBar.test.jsx
//
// Component tests for ManualTradeBar.
//
// Strategy: mock the three hooks the bar depends on so we can exercise
// rendering + disabled states + modal trigger without a live hub.

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';

import ManualTradeBar from '../components/ManualTradeBar.jsx';

// ─── Mock hooks ───────────────────────────────────────────────────────────────

// useClobBook
vi.mock('../hooks/useClobBook.js', () => ({
  useClobBook: vi.fn(),
}));

// useManualTrades
vi.mock('../hooks/useManualTrades.js', () => ({
  useManualTrades: vi.fn(),
}));

// useToast — minimal stub
vi.mock('../../../components/shared/Toast.jsx', () => ({
  useToast: () => ({ addToast: vi.fn(), removeToast: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

// ManualTradeConfirmModal — stub so we can detect it opens without a real API
vi.mock('../components/ManualTradeConfirmModal.jsx', () => ({
  default: ({ direction, stakeUsd, askPrice, onCancel }) => (
    <div data-testid="confirm-modal">
      <span data-testid="modal-direction">{direction}</span>
      <span data-testid="modal-stake">{stakeUsd}</span>
      <span data-testid="modal-ask">{askPrice}</span>
      <button onClick={onCancel}>Cancel</button>
    </div>
  ),
}));

// AuthContext — stub user
vi.mock('../../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ user: { username: 'billy' } }),
}));

// useApi — not needed for bar render tests (used by modal only)
vi.mock('../../../hooks/useApi.js', () => ({
  useApi: () => ({}),
}));

import { useClobBook } from '../hooks/useClobBook.js';
import { useManualTrades } from '../hooks/useManualTrades.js';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function defaultClobBook(overrides = {}) {
  return {
    book: {
      yes_ask: 0.55,
      no_ask: 0.45,
    },
    unavailable: false,
    error: null,
    ...overrides,
  };
}

function defaultManualTrades(overrides = {}) {
  return {
    rows: [],
    latestPending: null,
    latestForWindow: () => null,
    reload: vi.fn(),
    loading: false,
    error: null,
    ...overrides,
  };
}

function renderBar(props = {}) {
  useClobBook.mockReturnValue(defaultClobBook(props.clob));
  useManualTrades.mockReturnValue(defaultManualTrades(props.trades));
  return render(
    <ManualTradeBar
      windowEpoch={1714000000}
      systemStatus="LIVE"
      {...props}
    />,
  );
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe('ManualTradeBar — renders', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the bar label', () => {
    renderBar();
    expect(screen.getByText(/MANUAL TRADE · BTC 5m/i)).toBeInTheDocument();
  });

  it('renders UP and DOWN buttons', () => {
    renderBar();
    expect(screen.getByTestId('manual-trade-up-btn')).toBeInTheDocument();
    expect(screen.getByTestId('manual-trade-down-btn')).toBeInTheDocument();
  });

  it('shows ask prices on buttons when book is available', () => {
    renderBar();
    // UP ask = 0.55
    expect(screen.getByTestId('manual-trade-up-btn')).toHaveTextContent('UP $0.5500');
    // DOWN ask = 0.45
    expect(screen.getByTestId('manual-trade-down-btn')).toHaveTextContent('DOWN $0.4500');
  });
});

describe('ManualTradeBar — disabled when ask is undefined', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('disables UP button when yes_ask is null', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: null, no_ask: 0.45 }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    expect(screen.getByTestId('manual-trade-up-btn')).toBeDisabled();
  });

  it('disables DOWN button when no_ask is null', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: 0.55, no_ask: null }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    expect(screen.getByTestId('manual-trade-down-btn')).toBeDisabled();
  });

  it('disables both buttons when book is null', () => {
    useClobBook.mockReturnValue({ book: null, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    expect(screen.getByTestId('manual-trade-up-btn')).toBeDisabled();
    expect(screen.getByTestId('manual-trade-down-btn')).toBeDisabled();
  });
});

describe('ManualTradeBar — disabled when ask > 0.82', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('disables UP button when yes_ask = 0.83', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: 0.83, no_ask: 0.17 }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    const upBtn = screen.getByTestId('manual-trade-up-btn');
    expect(upBtn).toBeDisabled();
    expect(upBtn).toHaveAttribute('title', expect.stringMatching(/cap/i));
  });

  it('does NOT disable UP button at exactly 0.82', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: 0.82, no_ask: 0.18 }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    expect(screen.getByTestId('manual-trade-up-btn')).not.toBeDisabled();
  });
});

describe('ManualTradeBar — stake input clamps [1, 25]', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('clamps below 1 to 1 on blur', () => {
    renderBar();
    const input = screen.getByLabelText('Stake in USD');
    fireEvent.change(input, { target: { value: '0' } });
    fireEvent.blur(input);
    expect(input.value).toBe('1');
  });

  it('clamps above 25 to 25 on blur', () => {
    renderBar();
    const input = screen.getByLabelText('Stake in USD');
    fireEvent.change(input, { target: { value: '100' } });
    fireEvent.blur(input);
    expect(input.value).toBe('25');
  });

  it('accepts value within range', () => {
    renderBar();
    const input = screen.getByLabelText('Stake in USD');
    fireEvent.change(input, { target: { value: '10' } });
    fireEvent.blur(input);
    expect(input.value).toBe('10');
  });
});

describe('ManualTradeBar — click UP opens confirm modal', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('modal does not render initially', () => {
    renderBar();
    expect(screen.queryByTestId('confirm-modal')).not.toBeInTheDocument();
  });

  it('clicking UP shows confirm modal with UP direction', () => {
    renderBar();
    fireEvent.click(screen.getByTestId('manual-trade-up-btn'));
    const modal = screen.getByTestId('confirm-modal');
    expect(modal).toBeInTheDocument();
    expect(screen.getByTestId('modal-direction')).toHaveTextContent('UP');
  });

  it('modal preview shows correct ask price', () => {
    renderBar();
    fireEvent.click(screen.getByTestId('manual-trade-up-btn'));
    // yes_ask is 0.55
    expect(screen.getByTestId('modal-ask')).toHaveTextContent('0.55');
  });

  it('modal preview shows correct stake (default $5)', () => {
    renderBar();
    fireEvent.click(screen.getByTestId('manual-trade-up-btn'));
    expect(screen.getByTestId('modal-stake')).toHaveTextContent('5');
  });

  it('clicking DOWN shows confirm modal with DOWN direction', () => {
    renderBar();
    fireEvent.click(screen.getByTestId('manual-trade-down-btn'));
    const modal = screen.getByTestId('confirm-modal');
    expect(modal).toBeInTheDocument();
    expect(screen.getByTestId('modal-direction')).toHaveTextContent('DOWN');
  });

  it('cancel closes modal', () => {
    renderBar();
    fireEvent.click(screen.getByTestId('manual-trade-up-btn'));
    expect(screen.getByTestId('confirm-modal')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByTestId('confirm-modal')).not.toBeInTheDocument();
  });
});

describe('ManualTradeBar — disabled when engine KILLED', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('disables both buttons when systemStatus=KILLED', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: 0.55, no_ask: 0.45 }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades());
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="KILLED" />);
    expect(screen.getByTestId('manual-trade-up-btn')).toBeDisabled();
    expect(screen.getByTestId('manual-trade-down-btn')).toBeDisabled();
    expect(screen.getByTestId('manual-trade-up-btn')).toHaveAttribute('title', expect.stringMatching(/killed/i));
  });
});

describe('ManualTradeBar — disabled when pending trade in window', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('disables both buttons when there is a pending trade for this window', () => {
    useClobBook.mockReturnValue({ book: { yes_ask: 0.55, no_ask: 0.45 }, unavailable: false, error: null });
    useManualTrades.mockReturnValue(defaultManualTrades({
      latestPending: { trade_id: 't1', status: 'pending_live', window_epoch: 1714000000 },
      latestForWindow: () => ({ trade_id: 't1', status: 'pending_live', window_epoch: 1714000000 }),
    }));
    render(<ManualTradeBar windowEpoch={1714000000} systemStatus="LIVE" />);
    expect(screen.getByTestId('manual-trade-up-btn')).toBeDisabled();
    expect(screen.getByTestId('manual-trade-down-btn')).toBeDisabled();
  });
});
