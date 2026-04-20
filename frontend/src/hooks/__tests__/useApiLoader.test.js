import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// Mock useApi — useApiLoader calls useApi() internally. IMPORTANT: the mock
// must return the SAME object reference on every call. If it returns a fresh
// object each render, `api` changes → useCallback recreates `load` → useEffect
// re-fires → the previous request is aborted before data settles, causing an
// infinite abort+refetch loop that swallows mocked resolve values.
const mockGet = vi.fn();
const mockApiObj = { get: mockGet, post: vi.fn(), patch: vi.fn() };
vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => mockApiObj,
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import { useApiLoader } from '../useApiLoader.js';

beforeEach(() => {
  vi.clearAllMocks();
});

// ── helpers ────────────────────────────────────────────────────────────────

/** Wrap a value in a minimal axios-style response envelope. */
const axiosResp = (data) => ({ data });

describe('useApiLoader — envelope unwrapping', () => {
  it('unwraps raw array response', async () => {
    const items = [{ id: 1 }, { id: 2 }];
    mockGet.mockResolvedValueOnce(axiosResp(items));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(items);
    expect(result.current.error).toBeNull();
  });

  it('unwraps {rows:[...]} envelope', async () => {
    const rows = [{ a: 1 }, { a: 2 }];
    mockGet.mockResolvedValueOnce(axiosResp({ rows }));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(rows);
  });

  it('unwraps {trades:[...]} envelope', async () => {
    const trades = [{ id: 'T1' }];
    mockGet.mockResolvedValueOnce(axiosResp({ trades, total: 1 }));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/trades', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(trades);
  });

  it('unwraps {decisions:[...]} envelope', async () => {
    const decisions = [{ decision: 'BUY' }];
    mockGet.mockResolvedValueOnce(axiosResp({ decisions }));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/decisions', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(decisions);
  });

  it('unwraps {items:[...]} envelope', async () => {
    const items = [{ pnl: 100 }];
    mockGet.mockResolvedValueOnce(axiosResp({ items }));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/pnl', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(items);
  });

  it('returns raw object when no recognized array key', async () => {
    const obj = { total_pnl: 500, sharpe: 1.2 };
    mockGet.mockResolvedValueOnce(axiosResp(obj));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/stats', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(obj);
  });
});

describe('useApiLoader — abort behavior', () => {
  it('suppresses AbortError and does not set error state', async () => {
    const abortErr = new Error('Aborted');
    abortErr.name = 'AbortError';
    mockGet.mockRejectedValueOnce(abortErr);

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    // After abort, error should stay null and loading should resolve.
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
  });

  it('suppresses ERR_CANCELED and does not set error state', async () => {
    const cancelErr = new Error('canceled');
    cancelErr.code = 'ERR_CANCELED';
    mockGet.mockRejectedValueOnce(cancelErr);

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
  });

  it('sets error state for non-abort failures', async () => {
    mockGet.mockRejectedValueOnce(new Error('Network timeout'));

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    await waitFor(() => expect(result.current.error).toBe('Network timeout'));
    expect(result.current.loading).toBe(false);
  });
});

describe('useApiLoader — loading state', () => {
  it('starts in loading=true and transitions to false after resolve', async () => {
    let resolveP;
    const p = new Promise((res) => { resolveP = res; });
    mockGet.mockReturnValueOnce(p);

    const { result } = renderHook(() =>
      useApiLoader((signal, api) => api.get('/api/test', { signal }), [])
    );

    expect(result.current.loading).toBe(true);

    resolveP(axiosResp([{ id: 1 }]));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual([{ id: 1 }]);
  });
});
