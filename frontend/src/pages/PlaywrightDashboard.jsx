import { useEffect, useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

export default function PlaywrightDashboard() {
  const api = useApi();
  const [status, setStatus] = useState(null);
  const [balance, setBalance] = useState(null);
  const [positions, setPositions] = useState([]);
  const [redeemable, setRedeemable] = useState([]);
  const [history, setHistory] = useState([]);
  const [screenshotUrl, setScreenshotUrl] = useState(null);
  const [redeeming, setRedeeming] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const [statusRes, balRes, posRes, redeemRes, histRes] = await Promise.all([
        api.get('/playwright/status'),
        api.get('/playwright/balance'),
        api.get('/playwright/positions'),
        api.get('/playwright/redeemable'),
        api.get('/playwright/history'),
      ]);
      setStatus(statusRes.data);
      setBalance(balRes.data);
      setPositions(Array.isArray(posRes.data) ? posRes.data : []);
      setRedeemable(Array.isArray(redeemRes.data) ? redeemRes.data : []);
      setHistory(Array.isArray(histRes.data) ? histRes.data : []);
      setLastRefresh(new Date());
    } catch (e) {
      console.error('Playwright fetch error:', e);
    }
  }, [api]);

  const refreshScreenshot = useCallback(async () => {
    try {
      const res = await api.get('/playwright/screenshot', { responseType: 'blob' });
      if (res.data && res.data.size > 0) {
        const url = URL.createObjectURL(res.data);
        setScreenshotUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
      }
    } catch (e) {
      console.error('Screenshot error:', e);
    }
  }, [api]);

  useEffect(() => {
    fetchAll();
    refreshScreenshot();
    const dataInterval = setInterval(fetchAll, 30000);
    const ssInterval = setInterval(refreshScreenshot, 30000);
    return () => {
      clearInterval(dataInterval);
      clearInterval(ssInterval);
    };
  }, [fetchAll, refreshScreenshot]);

  const handleRedeem = async () => {
    setRedeeming(true);
    try {
      await api.post('/playwright/redeem');
      setTimeout(fetchAll, 10000);
    } catch (e) {
      console.error('Redeem error:', e);
    }
    setRedeeming(false);
  };

  const statusColor = status?.logged_in ? 'var(--profit)' : 'var(--loss)';
  const statusText = status?.logged_in ? 'Connected' : 'Disconnected';

  return (
    <div className="space-y-6 fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-[var(--text-primary)]">
          Polymarket Account
        </h1>
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-2 text-sm">
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: statusColor }}
            />
            <span style={{ color: statusColor }}>{statusText}</span>
          </span>
          {lastRefresh && (
            <span className="text-xs text-[var(--text-muted)]">
              {lastRefresh.toLocaleTimeString()}
            </span>
          )}
        </div>
      </div>

      {/* Balance Cards */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <p className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1">USDC Cash</p>
          <p className="text-2xl font-mono text-[var(--text-primary)]">
            ${balance?.usdc?.toFixed(2) || '0.00'}
          </p>
        </div>
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <p className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1">Positions</p>
          <p className="text-2xl font-mono text-[var(--text-primary)]">
            ${balance?.positions_value?.toFixed(2) || '0.00'}
          </p>
        </div>
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <p className="text-xs text-[var(--text-muted)] uppercase tracking-wider mb-1">Total</p>
          <p className="text-2xl font-mono text-[var(--accent-cyan)]">
            ${balance?.total?.toFixed(2) || '0.00'}
          </p>
        </div>
      </div>

      {/* Redeemable + Redeem Button */}
      {redeemable.length > 0 && (
        <div className="bg-[var(--card)] border border-[var(--accent-purple)]/30 rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-[var(--accent-purple)]">
              Redeemable Positions ({redeemable.length})
            </h2>
            <button
              onClick={handleRedeem}
              disabled={redeeming}
              className="px-4 py-1.5 rounded text-sm bg-[var(--accent-purple)] text-white hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {redeeming ? 'Redeeming...' : 'Redeem All'}
            </button>
          </div>
          <div className="space-y-2">
            {redeemable.map((r, i) => (
              <div key={i} className="flex justify-between text-sm">
                <span className="text-[var(--text-secondary)]">{r.market}</span>
                <span className="text-[var(--profit)] font-mono">
                  {r.value ? `$${r.value}` : ''}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Live Browser Preview */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-[var(--text-secondary)]">Live Preview</h2>
          <button
            onClick={refreshScreenshot}
            className="px-3 py-1 rounded text-xs text-[var(--text-muted)] border border-[var(--border)] hover:text-[var(--text-secondary)] transition-colors"
          >
            Refresh
          </button>
        </div>
        {screenshotUrl ? (
          <img
            src={screenshotUrl}
            alt="Polymarket Dashboard"
            className="w-full rounded border border-[var(--border)]"
          />
        ) : (
          <div className="w-full aspect-video bg-black/20 rounded flex items-center justify-center text-[var(--text-muted)] text-sm">
            No screenshot available
          </div>
        )}
      </div>

      {/* Positions Table */}
      {positions.length > 0 && (
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <h2 className="text-sm font-medium text-[var(--text-secondary)] mb-3">
            Positions ({positions.length})
          </h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--text-muted)] border-b border-[var(--border)]">
                <th className="text-left py-2">Market</th>
                <th className="text-left py-2">Outcome</th>
                <th className="text-right py-2">Value</th>
                <th className="text-right py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => (
                <tr key={i} className="border-b border-[var(--border)]/50">
                  <td className="py-2 text-[var(--text-primary)]">{p.market}</td>
                  <td className="py-2 text-[var(--text-secondary)]">{p.outcome}</td>
                  <td className="py-2 text-right font-mono text-[var(--text-primary)]">
                    ${typeof p.value === 'number' ? p.value.toFixed(2) : p.value || '0.00'}
                  </td>
                  <td className="py-2 text-right">
                    <StatusBadge status={p.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Order History */}
      {history.length > 0 && (
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <h2 className="text-sm font-medium text-[var(--text-secondary)] mb-3">
            Recent Activity ({history.length})
          </h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--text-muted)] border-b border-[var(--border)]">
                <th className="text-left py-2">Market</th>
                <th className="text-left py-2">Side</th>
                <th className="text-right py-2">Amount</th>
                <th className="text-right py-2">Date</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h, i) => (
                <tr key={i} className="border-b border-[var(--border)]/50">
                  <td className="py-2 text-[var(--text-primary)]">{h.market}</td>
                  <td className="py-2 text-[var(--text-secondary)]">{h.side}</td>
                  <td className="py-2 text-right font-mono text-[var(--text-primary)]">
                    ${typeof h.amount === 'number' ? h.amount.toFixed(2) : h.amount || '0.00'}
                  </td>
                  <td className="py-2 text-right text-[var(--text-muted)]">{h.date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }) {
  const styles = {
    active: { bg: 'var(--accent-cyan)', text: 'Active' },
    settled: { bg: 'var(--profit)', text: 'Settled' },
    redeemable: { bg: 'var(--accent-purple)', text: 'Redeemable' },
  };
  const s = styles[status] || { bg: 'var(--text-muted)', text: status || 'Unknown' };
  return (
    <span
      className="px-2 py-0.5 rounded text-xs font-medium"
      style={{ backgroundColor: `${s.bg}20`, color: s.bg }}
    >
      {s.text}
    </span>
  );
}
