import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';
import StatusBadge from '../components/StatusBadge';
import { formatTimestamp } from '../lib/utils';

export default function System() {
  const api = useApi();
  const [status, setStatus] = useState(null);
  const [killConfirm, setKillConfirm] = useState(false);

  const fetchStatus = () => {
    api.get('/api/system/status')
      .then(res => setStatus(res.data))
      .catch(err => console.error('[System] status fetch error:', err));
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [api]);

  const handleKill = async () => {
    if (!killConfirm) { setKillConfirm(true); return; }
    await api.post('/api/system/kill');
    setKillConfirm(false);
    fetchStatus();
  };

  const handleResume = async () => {
    await api.post('/api/system/resume');
    fetchStatus();
  };

  const handlePaperToggle = async () => {
    const newMode = status?.engine_status !== 'paper';
    await api.post('/api/system/paper-mode', { enabled: newMode });
    fetchStatus();
  };

  const feeds = status ? [
    { name: 'Binance', connected: status.binance_connected },
    { name: 'CoinGlass', connected: status.coinglass_connected },
    { name: 'Chainlink', connected: status.chainlink_connected },
    { name: 'Polymarket', connected: status.polymarket_connected },
    { name: 'Opinion', connected: status.opinion_connected },
  ] : [];

  return (
    <div className="space-y-6">
      <div className="mb-2">
        <div className="flex items-center gap-3 mb-1">
          <h2 className="text-xl font-semibold text-white">System Status</h2>
          <span className="text-[7px] font-bold tracking-wider px-1.5 py-0.5 rounded border" style={{ background: 'rgba(100,116,139,0.1)', color: '#64748b', borderColor: 'rgba(100,116,139,0.3)' }}>SYSTEM</span>
        </div>
        <p className="text-[10px] text-white/35 max-w-2xl leading-relaxed">
          Polymarket engine health, kill switch, data feed connections, and system toggles.
          Covers both the Polymarket 5m binary options engine and global infrastructure state.
          <span className="text-white/20 ml-2">Data: GET /api/system/status</span>
        </p>
      </div>

      {/* Engine Status */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <h3 className="text-lg font-medium text-white">Engine</h3>
            <StatusBadge status={status?.engine_status || 'unknown'} />
          </div>
          <div className="text-xs text-white/30">
            Last heartbeat: {status?.last_heartbeat ? formatTimestamp(status.last_heartbeat) : '—'}
          </div>
        </div>

        {/* Controls */}
        <div className="flex flex-wrap gap-3">
          <button
            onClick={handleKill}
            className={`px-4 py-2 rounded text-sm font-medium transition-colors ${
              killConfirm
                ? 'bg-red-600 text-white animate-pulse'
                : 'bg-red-600/20 text-red-400 hover:bg-red-600/30 border border-red-600/30'
            }`}
          >
            {killConfirm ? '⚠️ CONFIRM KILL SWITCH' : '🛑 Kill Switch'}
          </button>
          {killConfirm && (
            <button
              onClick={() => setKillConfirm(false)}
              className="px-4 py-2 rounded text-sm text-white/40 hover:text-white/60"
            >
              Cancel
            </button>
          )}
          <button
            onClick={handleResume}
            className="px-4 py-2 bg-green-600/20 text-green-400 hover:bg-green-600/30 border border-green-600/30 rounded text-sm font-medium transition-colors"
          >
            ▶️ Resume
          </button>
          <button
            onClick={handlePaperToggle}
            className="px-4 py-2 bg-[var(--card)] border border-[var(--border)] rounded text-sm text-white/60 hover:text-white/80 transition-colors"
          >
            {status?.engine_status === 'paper' ? '🔴 Switch to Live' : '📝 Switch to Paper'}
          </button>
        </div>
      </div>

      {/* Feed Connections */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-lg font-medium text-white mb-4">Data Feeds</h3>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          {feeds.map(feed => (
            <div key={feed.name} className="flex items-center gap-2 p-3 bg-white/[0.02] rounded border border-[var(--border)]">
              <div className={`w-2 h-2 rounded-full ${feed.connected ? 'bg-[var(--profit)]' : 'bg-[var(--loss)]'}`} />
              <span className="text-sm text-white/70">{feed.name}</span>
            </div>
          ))}
        </div>
      </div>

      {/* System Info */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-lg font-medium text-white mb-4">Info</h3>
        <div className="grid grid-cols-2 gap-y-2 text-sm">
          <span className="text-white/40">Active Positions</span>
          <span className="text-white/80">{status?.active_positions ?? '—'}</span>
          <span className="text-white/40">Last Trade</span>
          <span className="text-white/80">{status?.last_trade_at ? formatTimestamp(status.last_trade_at) : 'None'}</span>
          <span className="text-white/40">Last VPIN</span>
          <span className="text-white/80">{status?.last_vpin?.toFixed(4) ?? '—'}</span>
          <span className="text-white/40">Cascade State</span>
          <span className="text-white/80">{status?.last_cascade_state ?? 'IDLE'}</span>
        </div>
      </div>
    </div>
  );
}
