import React, { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';
import TradeTable from '../components/TradeTable.jsx';

/**
 * Trades — Paginated trade list with filters.
 *
 * Filters: strategy, outcome, market_slug
 */
export default function Trades() {
  const api = useApi();
  const [trades, setTrades] = useState([]);
  const [stats, setStats] = useState(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const [filters, setFilters] = useState({
    strategy: '',
    outcome: '',
    market_slug: '',
  });

  useEffect(() => {
    const fetchTrades = async () => {
      try {
        setLoading(true);
        const params = {
          page,
          page_size: pageSize,
          ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)),
        };

        const res = await api.get('/trades', { params });
        setTrades(res.data.trades);
        setTotal(res.data.total);

        const statsRes = await api.get('/trades/stats');
        setStats(statsRes.data);
      } catch (err) {
        console.error('Failed to fetch trades:', err);
      } finally {
        setLoading(false);
      }
    };

    fetchTrades();
  }, [api, page, pageSize, filters]);

  const pages = Math.ceil(total / pageSize);

  return (
    <div className="space-y-6 p-6">
      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <div className="card p-4">
            <div className="text-xs text-muted mb-1">Total Trades</div>
            <div className="text-lg font-semibold">{stats.total_trades}</div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-muted mb-1">Win Rate</div>
            <div className="text-lg font-semibold text-profit">{(stats.win_rate * 100).toFixed(1)}%</div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-muted mb-1">Total P&L</div>
            <div className={`text-lg font-semibold ${stats.total_pnl > 0 ? 'text-profit' : 'text-loss'}`}>
              ${stats.total_pnl.toFixed(2)}
            </div>
          </div>
          <div className="card p-4">
            <div className="text-xs text-muted mb-1">Avg P&L</div>
            <div className={`text-lg font-semibold ${stats.avg_pnl > 0 ? 'text-profit' : 'text-loss'}`}>
              ${stats.avg_pnl.toFixed(2)}
            </div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="card p-4 flex flex-col sm:flex-row gap-3">
        <input
          type="text"
          placeholder="Filter by strategy"
          value={filters.strategy}
          onChange={e => { setFilters(prev => ({ ...prev, strategy: e.target.value })); setPage(1); }}
          className="flex-1 rounded px-3 py-2 text-sm"
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid var(--border)',
            color: 'var(--text-primary)',
          }}
        />
        <select
          value={filters.outcome}
          onChange={e => { setFilters(prev => ({ ...prev, outcome: e.target.value })); setPage(1); }}
          className="flex-1 rounded px-3 py-2 text-sm"
          style={{
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid var(--border)',
            color: 'var(--text-primary)',
          }}
        >
          <option value="">All Outcomes</option>
          <option value="WIN">WIN</option>
          <option value="LOSS">LOSS</option>
          <option value="PUSH">PUSH</option>
        </select>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-center py-8 text-muted">Loading trades…</div>
      ) : (
        <>
          <TradeTable trades={trades} />

          {/* Pagination */}
          <div className="flex justify-between items-center">
            <span className="text-sm text-muted">{total} total trades</span>
            <div className="flex gap-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="px-3 py-2 rounded text-sm"
                style={{
                  background: page === 1 ? 'rgba(255,255,255,0.05)' : 'var(--accent-purple)',
                  opacity: page === 1 ? 0.5 : 1,
                  cursor: page === 1 ? 'not-allowed' : 'pointer',
                }}
              >
                Prev
              </button>
              <span className="px-3 py-2 text-sm text-muted">
                Page {page} of {pages}
              </span>
              <button
                onClick={() => setPage(p => Math.min(pages, p + 1))}
                disabled={page === pages}
                className="px-3 py-2 rounded text-sm"
                style={{
                  background: page === pages ? 'rgba(255,255,255,0.05)' : 'var(--accent-purple)',
                  opacity: page === pages ? 0.5 : 1,
                  cursor: page === pages ? 'not-allowed' : 'pointer',
                }}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
