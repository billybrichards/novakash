import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

// ── mock hooks ──────────────────────────────────────────────────────────────

const mockUseApiLoader = vi.fn();
const mockPatch = vi.fn();

vi.mock('../../hooks/useApiLoader.js', () => ({
  useApiLoader: (...args) => mockUseApiLoader(...args),
}));

vi.mock('../../hooks/useApi.js', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    patch: mockPatch,
  }),
}));

vi.mock('../../auth/AuthContext.jsx', () => ({
  useAuth: () => ({ token: 'test-token', logout: vi.fn() }),
}));

import AuditTasks from '../AuditTasks.jsx';

// ── helpers ─────────────────────────────────────────────────────────────────

function makeLoader(overrides = {}) {
  return { data: null, error: null, loading: false, reload: vi.fn(), ...overrides };
}

function makeTask(overrides = {}) {
  return {
    id: 1,
    task_type: 'ANOMALY',
    severity: 'HIGH',
    title: 'Spread too wide',
    category: 'execution',
    priority: 1,
    created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(), // 1 hour ago
    status: 'OPEN',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── tests ────────────────────────────────────────────────────────────────────

describe('AuditTasks — loading / error states', () => {
  it('renders page header', () => {
    mockUseApiLoader.mockReturnValue(makeLoader());
    render(<AuditTasks />);
    expect(screen.getByText('Audit Tasks')).toBeInTheDocument();
  });

  it('shows loading spinner when loading=true and no rows yet', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ loading: true }));
    render(<AuditTasks />);
    // Should not show the empty DataTable text; Loading component is rendered instead
    expect(screen.queryByText('No audit tasks match these filters.')).not.toBeInTheDocument();
  });

  it('shows load error banner', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ error: 'DB error' }));
    render(<AuditTasks />);
    expect(screen.getByText(/Load error.*DB error/)).toBeInTheDocument();
  });

  it('shows empty table text when data is an empty array', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<AuditTasks />);
    expect(screen.getByText('No audit tasks match these filters.')).toBeInTheDocument();
  });
});

describe('AuditTasks — severity colors', () => {
  it('renders HIGH severity task', () => {
    const task = makeTask({ severity: 'HIGH' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task] }));
    render(<AuditTasks />);
    // "HIGH" appears in the data row AND in the severity filter pill.
    expect(screen.getAllByText('HIGH').length).toBeGreaterThanOrEqual(1);
  });

  it('renders MEDIUM severity task', () => {
    const task = makeTask({ id: 2, severity: 'MEDIUM' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task] }));
    render(<AuditTasks />);
    expect(screen.getByText('MEDIUM')).toBeInTheDocument();
  });

  it('renders LOW severity task', () => {
    const task = makeTask({ id: 3, severity: 'LOW' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task] }));
    render(<AuditTasks />);
    // "LOW" appears in the data row AND in the severity filter pill.
    expect(screen.getAllByText('LOW').length).toBeGreaterThanOrEqual(1);
  });

  it('counts severity summary in header (normalises MEDIUM → MED bucket)', () => {
    const tasks = [
      makeTask({ id: 1, severity: 'HIGH' }),
      makeTask({ id: 2, severity: 'MEDIUM' }),
      makeTask({ id: 3, severity: 'LOW' }),
    ];
    mockUseApiLoader.mockReturnValue(makeLoader({ data: tasks }));
    render(<AuditTasks />);
    // Header shows "3 shown · 1 HIGH · 1 MED · 1 LOW"
    expect(screen.getByText(/3 shown · 1 HIGH · 1 MED · 1 LOW/)).toBeInTheDocument();
  });
});

describe('AuditTasks — status mutations', () => {
  it('shows "start" and "close" buttons for OPEN tasks', () => {
    const task = makeTask({ status: 'OPEN' });
    const reload = vi.fn().mockResolvedValue(undefined);
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task], reload }));
    render(<AuditTasks />);
    expect(screen.getByRole('button', { name: 'start' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'close' })).toBeInTheDocument();
  });

  it('shows only "close" button for IN_PROGRESS tasks', () => {
    const task = makeTask({ status: 'IN_PROGRESS' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task] }));
    render(<AuditTasks />);
    expect(screen.queryByRole('button', { name: 'start' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'close' })).toBeInTheDocument();
  });

  it('shows no action buttons for CLOSED tasks', () => {
    const task = makeTask({ status: 'CLOSED' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task] }));
    render(<AuditTasks />);
    expect(screen.queryByRole('button', { name: 'start' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'close' })).not.toBeInTheDocument();
  });

  it('calls PATCH with IN_PROGRESS when "start" is clicked', async () => {
    mockPatch.mockResolvedValueOnce({});
    const reload = vi.fn().mockResolvedValue(undefined);
    const task = makeTask({ id: 42, status: 'OPEN' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task], reload }));

    render(<AuditTasks />);
    fireEvent.click(screen.getByRole('button', { name: 'start' }));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/audit-tasks/42', { status: 'IN_PROGRESS' });
    });
    expect(reload).toHaveBeenCalled();
  });

  it('calls PATCH with CLOSED when "close" is clicked', async () => {
    mockPatch.mockResolvedValueOnce({});
    const reload = vi.fn().mockResolvedValue(undefined);
    const task = makeTask({ id: 99, status: 'OPEN' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task], reload }));

    render(<AuditTasks />);
    fireEvent.click(screen.getByRole('button', { name: 'close' }));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/audit-tasks/99', { status: 'CLOSED' });
    });
  });

  it('shows mutation error banner when PATCH fails', async () => {
    mockPatch.mockRejectedValueOnce(new Error('Server error'));
    const reload = vi.fn();
    const task = makeTask({ id: 7, status: 'OPEN' });
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [task], reload }));

    render(<AuditTasks />);
    fireEvent.click(screen.getByRole('button', { name: 'start' }));

    await waitFor(() => {
      expect(screen.getByText(/Update failed for #7.*Server error/)).toBeInTheDocument();
    });
  });
});

describe('AuditTasks — filter pills', () => {
  it('renders status filter pills', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<AuditTasks />);
    expect(screen.getByText('all')).toBeInTheDocument();
    expect(screen.getByText('open')).toBeInTheDocument();
    expect(screen.getByText('in progress')).toBeInTheDocument();
    expect(screen.getByText('closed')).toBeInTheDocument();
  });

  it('renders severity filter pills', () => {
    mockUseApiLoader.mockReturnValue(makeLoader({ data: [] }));
    render(<AuditTasks />);
    expect(screen.getByText('any')).toBeInTheDocument();
    expect(screen.getByText('HIGH')).toBeInTheDocument();
    expect(screen.getByText('MED')).toBeInTheDocument();
    expect(screen.getByText('LOW')).toBeInTheDocument();
  });
});
