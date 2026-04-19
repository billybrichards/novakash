import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';

const mockPost = vi.fn();
const mockApi = { post: mockPost, get: vi.fn(), put: vi.fn(), delete: vi.fn() };

vi.mock('../../../hooks/useApi.js', () => ({
  useApi: () => mockApi,
}));

import KillConfirmModal from '../KillConfirmModal.jsx';

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('KillConfirmModal', () => {
  it('submit button disabled until KILL is typed and stays disabled during 10s countdown', async () => {
    render(
      <KillConfirmModal
        isOpen={true}
        onClose={() => {}}
        onKilled={() => {}}
        systemStatus={{ engine_status: 'active' }}
      />
    );

    const submit = screen.getByTestId('kill-submit');
    expect(submit).toBeDisabled();
    expect(submit.textContent).toMatch(/TYPE KILL/);

    // Type "KILL" — countdown starts
    fireEvent.change(screen.getByTestId('kill-input'), { target: { value: 'KILL' } });
    // Submit still disabled during countdown
    expect(submit).toBeDisabled();
    expect(submit.textContent).toMatch(/CONFIRMING IN/);

    // Advance 5s — still disabled
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(submit).toBeDisabled();
    expect(submit.textContent).toMatch(/CONFIRMING IN/);

    // Advance past the 10s mark — now enabled
    await act(async () => {
      vi.advanceTimersByTime(5200);
    });
    expect(submit).not.toBeDisabled();
    expect(submit.textContent).toMatch(/CONFIRM KILL/);
  });

  it('partial or wrong text does NOT start the countdown', async () => {
    render(
      <KillConfirmModal
        isOpen={true}
        onClose={() => {}}
        onKilled={() => {}}
        systemStatus={null}
      />
    );

    fireEvent.change(screen.getByTestId('kill-input'), { target: { value: 'KIL' } });
    // Advance 12s — still should say TYPE KILL
    await act(async () => {
      vi.advanceTimersByTime(12000);
    });
    const submit = screen.getByTestId('kill-submit');
    expect(submit).toBeDisabled();
    expect(submit.textContent).toMatch(/TYPE KILL/);
  });

  it('fires POST /api/system/kill once on success, no auto-retry on failure', async () => {
    mockPost.mockRejectedValueOnce(new Error('hub down'));

    const onClose = vi.fn();
    const onKilled = vi.fn();

    render(
      <KillConfirmModal
        isOpen={true}
        onClose={onClose}
        onKilled={onKilled}
        systemStatus={{ status: 'active' }}
      />
    );

    fireEvent.change(screen.getByTestId('kill-input'), { target: { value: 'KILL' } });
    await act(async () => {
      vi.advanceTimersByTime(11000);
    });

    // Switch to real timers for the async POST to flush
    vi.useRealTimers();
    fireEvent.click(screen.getByTestId('kill-submit'));

    await waitFor(() => {
      expect(screen.getByTestId('kill-error').textContent).toMatch(/hub down/);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      '/api/system/kill',
      {},
      { headers: { 'X-Confirm': 'KILL' } }
    );
    expect(onKilled).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    // Re-instate fake timers to match afterEach reset expectation (useRealTimers noop)
    vi.useFakeTimers();
  });
});
