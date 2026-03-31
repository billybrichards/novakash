import { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi';

const DEFAULTS = {
  bet_fraction: 0.025,
  max_drawdown_kill: 0.45,
  daily_loss_limit_pct: 0.10,
  max_open_exposure_pct: 0.30,
  consecutive_loss_cooldown: 3,
  cooldown_seconds: 900,
  vpin_informed_threshold: 0.55,
  vpin_cascade_threshold: 0.70,
  cascade_oi_drop_threshold: 0.02,
  cascade_liq_volume_threshold: 5000000,
  arb_min_spread: 0.015,
  arb_max_position: 50.0,
  arb_enabled: true,
  vpin_cascade_enabled: true,
};

export default function Config() {
  const api = useApi();
  const [config, setConfig] = useState(DEFAULTS);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.get('/api/config').then(res => setConfig({ ...DEFAULTS, ...res.data }));
  }, []);

  const handleChange = (key, value) => {
    setConfig(prev => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const handleSave = async () => {
    try {
      await api.put('/api/config', config);
      setSaved(true);
      setError(null);
      setTimeout(() => setSaved(false), 3000);
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to save');
    }
  };

  const handleReset = () => {
    setConfig(DEFAULTS);
    setSaved(false);
  };

  const Field = ({ label, field, type = 'number', step = '0.001', suffix = '' }) => (
    <div className="flex items-center justify-between py-2 border-b border-[var(--border)]">
      <label className="text-sm text-white/60">{label}</label>
      <div className="flex items-center gap-2">
        <input
          type={type}
          step={step}
          value={config[field]}
          onChange={e => handleChange(field, type === 'number' ? parseFloat(e.target.value) : e.target.value)}
          className="w-28 bg-white/[0.03] border border-[var(--border)] rounded px-2 py-1 text-sm text-right text-white/80 focus:outline-none focus:border-[var(--accent-purple)]"
        />
        {suffix && <span className="text-xs text-white/30 w-8">{suffix}</span>}
      </div>
    </div>
  );

  const Toggle = ({ label, field }) => (
    <div className="flex items-center justify-between py-2 border-b border-[var(--border)]">
      <label className="text-sm text-white/60">{label}</label>
      <button
        onClick={() => handleChange(field, !config[field])}
        className={`w-10 h-5 rounded-full transition-colors ${
          config[field] ? 'bg-[var(--accent-purple)]' : 'bg-white/10'
        }`}
      >
        <div className={`w-4 h-4 rounded-full bg-white transition-transform mx-0.5 ${
          config[field] ? 'translate-x-5' : 'translate-x-0'
        }`} />
      </button>
    </div>
  );

  return (
    <div className="space-y-6 max-w-2xl">
      <h2 className="text-xl font-semibold text-white">Configuration</h2>

      {/* Strategy Toggles */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-sm font-medium text-white/40 uppercase tracking-wider mb-4">Strategies</h3>
        <Toggle label="Sub-$1 Arbitrage" field="arb_enabled" />
        <Toggle label="VPIN Cascade Filter" field="vpin_cascade_enabled" />
      </div>

      {/* Risk Parameters */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-sm font-medium text-white/40 uppercase tracking-wider mb-4">Risk Management</h3>
        <Field label="Bet Fraction (Kelly)" field="bet_fraction" suffix="%" />
        <Field label="Max Drawdown Kill" field="max_drawdown_kill" suffix="%" />
        <Field label="Daily Loss Limit" field="daily_loss_limit_pct" suffix="%" />
        <Field label="Max Open Exposure" field="max_open_exposure_pct" suffix="%" />
        <Field label="Consecutive Loss Cooldown" field="consecutive_loss_cooldown" step="1" />
        <Field label="Cooldown Duration" field="cooldown_seconds" step="60" suffix="s" />
      </div>

      {/* VPIN Thresholds */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-sm font-medium text-white/40 uppercase tracking-wider mb-4">VPIN Thresholds</h3>
        <Field label="Informed Threshold" field="vpin_informed_threshold" />
        <Field label="Cascade Threshold" field="vpin_cascade_threshold" />
      </div>

      {/* Cascade Detection */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-sm font-medium text-white/40 uppercase tracking-wider mb-4">Cascade Detection</h3>
        <Field label="OI Drop Threshold" field="cascade_oi_drop_threshold" suffix="%" />
        <Field label="Liquidation Volume Threshold" field="cascade_liq_volume_threshold" step="100000" suffix="$" />
      </div>

      {/* Arb Settings */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-lg p-6">
        <h3 className="text-sm font-medium text-white/40 uppercase tracking-wider mb-4">Arbitrage</h3>
        <Field label="Min Spread" field="arb_min_spread" suffix="%" />
        <Field label="Max Position" field="arb_max_position" step="5" suffix="$" />
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          className="px-6 py-2 bg-[var(--accent-purple)] text-white rounded text-sm font-medium hover:bg-[var(--accent-purple)]/80 transition-colors"
        >
          {saved ? '✓ Saved' : 'Save Configuration'}
        </button>
        <button
          onClick={handleReset}
          className="px-6 py-2 bg-[var(--card)] border border-[var(--border)] text-white/40 rounded text-sm hover:text-white/60 transition-colors"
        >
          Reset to Defaults
        </button>
        {error && <span className="text-sm text-[var(--loss)]">{error}</span>}
      </div>
    </div>
  );
}
