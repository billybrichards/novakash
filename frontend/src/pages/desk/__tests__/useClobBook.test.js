// Tests for the useClobBook fallback logic.
//
// Tests the key paths:
//   1. Primary path (HUB_ALLOW_CLOB_FETCH set): condition + /api/clob/book → book.source
//      is set from callers (not from server body in primary path).
//   2. Fallback path: /api/windows/condition returns 503 feature_flag_off
//      → hook calls /api/desk/clob-book and surfaces the snapshot book.
//   3. Fallback path: /api/clob/book returns 503 feature_flag_off
//      → hook falls back to /api/desk/clob-book.
//   4. Fallback book shape: yes_ask / no_ask flat aliases are added to the
//      normalised book so ManualTradeBar's read pattern works.
//   5. Stale snapshot: book.stale=true propagates correctly.
//
// Strategy: renderHook with a fake useApi that resolves/rejects based on URL.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// Mock useApi before importing the hook.
vi.mock('../../../hooks/useApi.js', () => ({
  useApi: vi.fn(),
}));

import { useApi } from '../../../hooks/useApi.js';
import { useClobBook } from '../hooks/useClobBook.js';

// Clear the module-level conditionMemo between tests to avoid cross-test pollution.
// We do this by resetting the vi.mock and clearing the memo via the module import.
// Since conditionMemo is not exported, we rely on using distinct windowEpoch values
// per test group so cached hits never interfere.

// Each describe block uses a unique windowEpoch to avoid conditionMemo cross-contamination.
const EPOCH_PRIMARY   = 1_700_001_000;
const EPOCH_COND_503  = 1_700_002_000;
const EPOCH_BOOK_503  = 1_700_003_000;
const EPOCH_STALE     = 1_700_004_000;
const EPOCH_FALSY_CHK = 0;

// ── Helpers ──────────────────────────────────────────────────────────────────

function make503Error(reason = 'feature_flag_off') {
  const err = new Error('503 Service Unavailable');
  err.response = {
    status: 503,
    data: { detail: { error: 'clob_unavailable', reason } },
  };
  return err;
}

function make404Error() {
  const err = new Error('404 Not Found');
  err.response = { status: 404, data: { detail: { error: 'market_not_found' } } };
  return err;
}

const FAKE_CONDITION = {
  condition_id: '0xdeadbeef',
  yes_token_id: 'tok_yes',
  no_token_id: 'tok_no',
};

const FAKE_CLOB_BOOK = {
  condition_id: '0xdeadbeef',
  yes: {
    bids: [{ price: 0.54, size: 60 }],
    asks: [{ price: 0.56, size: 40 }],
  },
  no: {
    bids: [{ price: 0.43, size: 50 }],
    asks: [{ price: 0.45, size: 70 }],
  },
  implied_p_up: 0.55,
  spread: 0.02,
  imbalance: 0.2,
  cached_at: Date.now() / 1000,
};

const FAKE_SNAPSHOT_BOOK = {
  yes: { bids: [{ price: 0.52, size: 0 }], asks: [{ price: 0.54, size: 0 }] },
  no: { bids: [{ price: 0.44, size: 0 }], asks: [{ price: 0.46, size: 0 }] },
  implied_p_up: 0.53,
  spread: 0.02,
  imbalance: null,
  source: 'window_snapshots',
  age_s: 5,
  stale: false,
};

const FAKE_STALE_SNAPSHOT_BOOK = {
  ...FAKE_SNAPSHOT_BOOK,
  age_s: 120,
  stale: true,
};

// ── Tests ────────────────────────────────────────────────────────────────────

describe('useClobBook — primary path (condition + clob/book succeed)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('sets book from /api/clob/book on success', async () => {
    const mockGet = vi.fn(async (url) => {
      if (url.includes('/api/windows/condition')) return { data: FAKE_CONDITION };
      if (url.includes('/api/clob/book')) return { data: FAKE_CLOB_BOOK };
      throw new Error(`Unexpected URL: ${url}`);
    });
    useApi.mockReturnValue({ get: mockGet });

    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: EPOCH_PRIMARY })
    );

    await waitFor(() => result.current.book !== null);

    expect(result.current.book.yes.asks[0].price).toBe(0.56);
    expect(result.current.unavailable).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.bookSource).toBe('clob');
  });
});

describe('useClobBook — fallback path (condition 503 → snapshots)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('falls back to /api/desk/clob-book when condition returns 503 feature_flag_off', async () => {
    const mockGet = vi.fn(async (url) => {
      if (url.includes('/api/windows/condition')) throw make503Error('feature_flag_off');
      if (url.includes('/api/desk/clob-book')) return { data: FAKE_SNAPSHOT_BOOK };
      throw new Error(`Unexpected URL: ${url}`);
    });
    useApi.mockReturnValue({ get: mockGet });

    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: EPOCH_COND_503 })
    );

    await waitFor(() => result.current.book !== null);

    expect(result.current.book.yes.asks[0].price).toBe(0.54);
    expect(result.current.book.source).toBe('window_snapshots');
    expect(result.current.book.yes_ask).toBe(0.54);   // flat alias
    expect(result.current.book.no_ask).toBe(0.46);    // flat alias
    expect(result.current.unavailable).toBe(false);
    expect(result.current.bookSource).toBe('snapshots');
  });
});

describe('useClobBook — fallback path (clob/book 503 → snapshots)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('falls back to /api/desk/clob-book when /api/clob/book returns 503 feature_flag_off', async () => {
    const mockGet = vi.fn(async (url) => {
      if (url.includes('/api/windows/condition')) return { data: FAKE_CONDITION };
      if (url.includes('/api/clob/book')) throw make503Error('feature_flag_off');
      if (url.includes('/api/desk/clob-book')) return { data: FAKE_SNAPSHOT_BOOK };
      throw new Error(`Unexpected URL: ${url}`);
    });
    useApi.mockReturnValue({ get: mockGet });

    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: EPOCH_BOOK_503 })
    );

    await waitFor(() => result.current.book !== null);

    expect(result.current.bookSource).toBe('snapshots');
    expect(result.current.book.source).toBe('window_snapshots');
    // Flat alias must be present
    expect(result.current.book.yes_ask).toBe(0.54);
    expect(result.current.book.no_ask).toBe(0.46);
  });

  it('also falls back on other 503 reasons (upstream_timeout etc.)', async () => {
    const mockGet = vi.fn(async (url) => {
      if (url.includes('/api/windows/condition')) return { data: FAKE_CONDITION };
      if (url.includes('/api/clob/book')) throw make503Error('upstream_timeout');
      if (url.includes('/api/desk/clob-book')) return { data: FAKE_SNAPSHOT_BOOK };
      throw new Error(`Unexpected URL: ${url}`);
    });
    useApi.mockReturnValue({ get: mockGet });

    // Use a unique epoch so conditionMemo from prior tests doesn't interfere.
    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: EPOCH_BOOK_503 + 1 })
    );

    await waitFor(() => result.current.book !== null);
    expect(result.current.bookSource).toBe('snapshots');
  });
});

describe('useClobBook — stale snapshot', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('propagates stale=true from snapshot book', async () => {
    const mockGet = vi.fn(async (url) => {
      if (url.includes('/api/windows/condition')) throw make503Error('feature_flag_off');
      if (url.includes('/api/desk/clob-book')) return { data: FAKE_STALE_SNAPSHOT_BOOK };
      throw new Error(`Unexpected URL: ${url}`);
    });
    useApi.mockReturnValue({ get: mockGet });

    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: EPOCH_STALE })
    );

    await waitFor(() => result.current.book !== null);

    expect(result.current.book.stale).toBe(true);
    expect(result.current.book.age_s).toBe(120);
  });
});

describe('useClobBook — no book when windowEpoch is falsy', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('does not fetch when windowEpoch is 0 / null', async () => {
    const mockGet = vi.fn();
    useApi.mockReturnValue({ get: mockGet });

    const { result } = renderHook(() =>
      useClobBook({ windowEpoch: 0 })
    );

    // Give time for any async fetch to fire
    await new Promise(r => setTimeout(r, 30));
    expect(result.current.book).toBeNull();
    expect(mockGet).not.toHaveBeenCalled();
  });
});
