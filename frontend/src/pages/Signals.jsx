import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';
import VPINChart from '../components/VPINChart';
import { formatTimestamp } from '../lib/utils';

const TABS = ['VPIN', 'Cascade', 'Arb'];

export default function Signals() {
  const api = useApi();
  const [activeTab, setActiveTab] = useState('VPIN');
  const [vpinData, setVpinData] = useState([]);
  const [cascadeEvents, setCascadeEvents] = useState([]);
  const [arbOpportunities, setArbOpportunities] = useState([]);

  useEffect(() => {
    if (activeTab === 'VPIN') {
      api.get('/api/signals/vpin?limit=2000')
        .then(res => setVpinData(res.data.items || []))
        .catch(err => console.error('[Signals] VPIN fetch error:', err));
    } else if (activeTab === 'Cascade') {
      api.get('/api/signals/cascade?limit=100')
        .then(res => setCascadeEvents(res.data.items || []))
        .catch(err => console.error('[Signals] Cascade fetch error:', err));
    } else if (activeTab === 'Arb') {
      api.get('/api/signals/arb?limit=500')
        .then(res => setArbOpportunities(res.data.items || []))
        .catch(err => console.error('[Signals] Arb fetch error:', err));
    }
  }, [api, activeTab]);

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold text-white">Signal History</h2>

      {/* Tabs */}
      <div className="flex gap-1 bg-[var(--card)] border border-[var(--border)] rounded-lg p-1 w-fit">
        {TABS.map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-1.5 rounded text-sm transition-colors ${
              activeTab === tab
                ? 'bg-[var(--accent-purple)] text-white'
                : 'text-white/40 hover:text-white/60'
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      {activeTab === 'VPIN' && (
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <h3 className="text-sm font-medium text-white/60 mb-4">VPIN History (Zoomable)</h3>
          <div className="h-[400px]">
            <VPINChart data={vpinData} fullHistory />
          </div>
        </div>
      )}

      {activeTab === 'Cascade' && (
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <h3 className="text-sm font-medium text-white/60 mb-4">Cascade Events Timeline</h3>
          <div className="space-y-3">
            {cascadeEvents.length === 0 && (
              <p className="text-white/30 text-sm">No cascade events recorded yet.</p>
            )}
            {cascadeEvents.map((event, i) => (
              <div key={i} className="flex items-start gap-4 p-3 bg-white/[0.02] rounded border border-[var(--border)]">
                <div className={`w-2 h-2 rounded-full mt-1.5 ${
                  event.metadata?.trade_placed ? 'bg-[var(--profit)]' : 'bg-white/20'
                }`} />
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-white">
                      {event.metadata?.direction === 'down' ? '📉' : '📈'} {event.metadata?.direction || 'unknown'} cascade
                    </span>
                    <span className="text-xs text-white/30">{formatTimestamp(event.created_at)}</span>
                  </div>
                  <div className="text-xs text-white/40 mt-1">
                    Duration: {event.metadata?.duration_s || '?'}s · 
                    Trade: {event.metadata?.trade_placed ? 'YES' : 'NO'} · 
                    Outcome: {event.metadata?.outcome || 'N/A'}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'Arb' && (
        <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-4">
          <h3 className="text-sm font-medium text-white/60 mb-4">Arb Opportunities</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-white/40 border-b border-[var(--border)]">
                  <th className="text-left py-2 font-medium">Time</th>
                  <th className="text-right py-2 font-medium">Combined</th>
                  <th className="text-right py-2 font-medium">Spread</th>
                  <th className="text-right py-2 font-medium">Net Profit</th>
                  <th className="text-center py-2 font-medium">Taken</th>
                </tr>
              </thead>
              <tbody>
                {arbOpportunities.map((opp, i) => (
                  <tr key={i} className="border-b border-[var(--border)] hover:bg-white/[0.02]">
                    <td className="py-2 text-white/60">{formatTimestamp(opp.created_at)}</td>
                    <td className="py-2 text-right text-white/80">${opp.value?.toFixed(4)}</td>
                    <td className="py-2 text-right text-[var(--accent-cyan)]">
                      {((1 - opp.value) * 100).toFixed(2)}%
                    </td>
                    <td className="py-2 text-right text-[var(--profit)]">
                      ${opp.metadata?.net_profit?.toFixed(4) || '—'}
                    </td>
                    <td className="py-2 text-center">
                      {opp.metadata?.taken ? '🟢' : '⚫'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
