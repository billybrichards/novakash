import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { useAuth } from '../../../auth/AuthContext.jsx';

// ── Mock Data ────────────────────────────────────────────────────────────────
// Realistic fallbacks so the page renders without API connectivity.

const MOCK_SYSTEM = {
  engine_running: true,
  paper_mode: false,
  uptime_seconds: 86432,
  connections: {
    binance_spot: true,
    binance_futures: true,
    chainlink: true,
    tiingo: true,
    coinglass: true,
    gamma: true,
    clob: true,
    timesfm: true,
  },
  feeds: {
    binance_spot: { connected: true, last_ts: Date.now() - 1200, last_price: 94283.42 },
    binance_futures: { connected: true, last_ts: Date.now() - 800, last_price: 94291.10 },
    chainlink: { connected: true, last_ts: Date.now() - 4800, delta_pct: 0.032 },
    tiingo: { connected: true, last_ts: Date.now() - 1900, bid: 94280.5, ask: 94286.3 },
    coinglass: { connected: true, last_ts: Date.now() - 14200, oi_change_pct: -0.8 },
    gamma: { connected: true, last_ts: Date.now() - 900, window_id: 'W-2026-04-23-14:05', state: 'TRADING' },
    clob: { connected: true, last_ts: Date.now() - 9500, up_ask: 0.52, down_ask: 0.51 },
    timesfm: { connected: true, last_ts: Date.now() - 950, probability_up: 0.63, model_version: 'v4.2' },
  },
};

const MOCK_SIGNALS = {
  vpin: { value: 0.58, bucket_fill: 0.72 },
  regime: { label: 'chop', confidence: 0.81 },
  data_surface: { freshness_ms: 1200, assembled_at: Date.now() - 1200 },
  ensemble: { p_up: 0.63, dist_from_half: 0.13, direction: 'UP' },
};

const MOCK_STRATEGY = {
  v8_champion: {
    status: 'LIVE',
    last_decision: 'TRADE',
    last_gate_blocked: null,
    window_position: 'T-87',
    gates: [
      { name: 'agreement', passed: true },
      { name: 'vpin', passed: true },
      { name: 'delta', passed: true },
      { name: 'cg_veto', passed: true },
      { name: 'macro', passed: true },
      { name: 'divergence', passed: false },
      { name: 'floor', passed: true },
      { name: 'cap', passed: true },
      { name: 'confidence', passed: true },
    ],
  },
  ghosts: { count: 3, last_shadow: 'SKIP (cap gate)' },
};

const MOCK_EXECUTION = {
  fak_executor: { last_fill_price: 0.54, success_rate: 0.87, last_fill_ts: Date.now() - 45000 },
  risk_manager: { bankroll: 412.83, exposure_pct: 0.12, daily_pnl: 8.42 },
  trade_claims: { active_count: 2 },
};

const MOCK_POST_TRADE = {
  clob_reconciler: { status: 'ok', wins_today: 14, losses_today: 5 },
  sot_reconciler: { status: 'ok', last_pass_agrees: true, last_pass_ts: Date.now() - 30000 },
  redeemer: { status: 'idle', pending_wins_usd: 6.82, on_chain_transport: 'polygon' },
  on_chain_scanner: { connected_rpcs: 2, last_scan_result: 'clean', last_scan_ts: Date.now() - 15000 },
};

const MOCK_WALLET = {
  usdc_balance: 412.83,
  matic_balance: 2.14,
  pending_wins_usd: 6.82,
};

const MOCK_WINDOW = {
  window_id: 'W-2026-04-23-14:05',
  window_ts: (Date.now() / 1000) - 187,
  countdown_seconds: 113,
};

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useEngineStatus() {
  const api = useApi();
  const { token } = useAuth();

  const [system, setSystem] = useState(null);
  const [wallet, setWallet] = useState(null);
  const [trades, setTrades] = useState([]);
  const [signals, setSignals] = useState(null);
  const [strategy, setStrategy] = useState(null);
  const [execution, setExecution] = useState(null);
  const [postTrade, setPostTrade] = useState(null);
  const [window, setWindow] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  const [wsConnected, setWsConnected] = useState(false);
  const wsRef = useRef(null);

  // Tick counter for clock and countdowns
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // ── REST polling ──────────────────────────────────────────────────────────
  const fetchAll = useCallback(async () => {
    try {
      const [sysRes, walletRes, tradesRes, hqRes] = await Promise.allSettled([
        api.get('/system/status'),
        api.get('/wallet/snapshot'),
        api.get('/trades?limit=5'),
        api.get('/v58/execution-hq?limit=5'),
      ]);

      if (sysRes.status === 'fulfilled') {
        const d = sysRes.value?.data;
        setSystem(d || null);
      }
      if (walletRes.status === 'fulfilled') {
        const d = walletRes.value?.data;
        setWallet(d || null);
      }
      if (tradesRes.status === 'fulfilled') {
        const d = tradesRes.value?.data;
        setTrades(Array.isArray(d) ? d : d?.trades || []);
      }
      if (hqRes.status === 'fulfilled') {
        const hq = hqRes.value?.data;
        if (hq) {
          // Extract signals/strategy/execution/post-trade from HQ endpoint if available
          if (hq.signals) setSignals(hq.signals);
          if (hq.strategy) setStrategy(hq.strategy);
          if (hq.execution) setExecution(hq.execution);
          if (hq.post_trade) setPostTrade(hq.post_trade);
          if (hq.current_window) setWindow(hq.current_window);

          // Derive some data from windows array
          if (hq.windows?.length > 0) {
            const latest = hq.windows[0];
            // Build window info
            setWindow(prev => prev || {
              window_id: latest.window_id || `W-${latest.window_ts}`,
              window_ts: typeof latest.window_ts === 'number'
                ? latest.window_ts
                : new Date(latest.window_ts).getTime() / 1000,
            });

            // Build strategy info from latest window
            const gates = [];
            const gateNames = ['agreement', 'vpin', 'delta', 'cg_veto', 'macro', 'divergence', 'floor', 'cap', 'confidence'];
            for (const g of gateNames) {
              const key = `gate_${g}`;
              if (latest[key] !== undefined) {
                gates.push({ name: g, passed: !!latest[key] });
              }
            }
            if (gates.length > 0) {
              setStrategy(prev => ({
                ...prev,
                v8_champion: {
                  status: 'LIVE',
                  last_decision: latest.trade_placed ? 'TRADE' : 'SKIP',
                  last_gate_blocked: latest.skip_reason || null,
                  window_position: latest.eval_offset ? `T-${latest.eval_offset}` : null,
                  gates,
                },
              }));
            }
          }

          // Build system info from HQ system data
          if (hq.system) {
            setSystem(prev => ({ ...prev, ...hq.system }));
          }
        }
      }

      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch engine status');
    }
    setLoading(false);
  }, [api]);

  // Initial fetch + polling
  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 8000);
    return () => clearInterval(id);
  }, [fetchAll]);

  // ── WebSocket for real-time updates ───────────────────────────────────────
  useEffect(() => {
    if (!token) return;

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const host = import.meta.env.VITE_API_URL
      ? new URL(import.meta.env.VITE_API_URL).host
      : window.location.host;
    const wsUrl = `${proto}://${host}/ws/feed?token=${token}`;

    let ws;
    let reconnectTimer;

    const connect = () => {
      try {
        ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => setWsConnected(true);
        ws.onclose = () => {
          setWsConnected(false);
          reconnectTimer = setTimeout(connect, 5000);
        };
        ws.onerror = () => {
          setWsConnected(false);
        };
        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            // Update relevant state based on message type
            if (msg.type === 'system') {
              setSystem(prev => ({ ...prev, ...msg.data }));
            } else if (msg.type === 'trade') {
              setTrades(prev => [msg.data, ...prev].slice(0, 5));
            } else if (msg.type === 'signal') {
              setSignals(prev => ({ ...prev, ...msg.data }));
            }
          } catch { /* ignore parse errors */ }
        };
      } catch { /* ignore connection errors in dev */ }
    };

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, [token]);

  // ── Merge real data with mocks ────────────────────────────────────────────
  const merged = useMemo(() => {
    const sys = system || MOCK_SYSTEM;
    const sig = signals || MOCK_SIGNALS;
    const strat = strategy || MOCK_STRATEGY;
    const exec = execution || MOCK_EXECUTION;
    const pt = postTrade || MOCK_POST_TRADE;
    const wal = wallet || MOCK_WALLET;
    const win = window || MOCK_WINDOW;

    // Normalize feed connections from various API shapes
    const conn = sys.connections || sys.feeds || {};
    const feedDetails = sys.feeds || {};

    const feedList = [
      {
        id: 'binance_spot',
        name: 'Binance Spot',
        connected: conn.binance_spot ?? conn.binance_connected ?? conn.binance ?? null,
        last_ts: feedDetails.binance_spot?.last_ts || null,
        metric_label: 'Last Price',
        metric_value: feedDetails.binance_spot?.last_price,
        metric_fmt: (v) => v ? `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : null,
        freq: '1-3 Hz',
      },
      {
        id: 'binance_futures',
        name: 'Binance Futures',
        connected: conn.binance_futures ?? conn.binance_futures_connected ?? null,
        last_ts: feedDetails.binance_futures?.last_ts || null,
        metric_label: 'Last Price',
        metric_value: feedDetails.binance_futures?.last_price,
        metric_fmt: (v) => v ? `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : null,
        freq: '1-3 Hz',
      },
      {
        id: 'chainlink',
        name: 'Chainlink Oracle',
        connected: conn.chainlink ?? conn.chainlink_connected ?? null,
        last_ts: feedDetails.chainlink?.last_ts || null,
        metric_label: 'BTC Delta',
        metric_value: feedDetails.chainlink?.delta_pct,
        metric_fmt: (v) => v != null ? `${v > 0 ? '+' : ''}${(v * 100).toFixed(3)}%` : null,
        freq: '5s',
      },
      {
        id: 'tiingo',
        name: 'Tiingo Top-of-Book',
        connected: conn.tiingo ?? conn.tiingo_connected ?? null,
        last_ts: feedDetails.tiingo?.last_ts || null,
        metric_label: 'BTC Bid/Ask',
        metric_value: feedDetails.tiingo,
        metric_fmt: (v) => v?.bid ? `$${Number(v.bid).toFixed(0)} / $${Number(v.ask).toFixed(0)}` : null,
        freq: '2s',
      },
      {
        id: 'coinglass',
        name: 'CoinGlass',
        connected: conn.coinglass ?? conn.coinglass_connected ?? null,
        last_ts: feedDetails.coinglass?.last_ts || null,
        metric_label: 'OI Change',
        metric_value: feedDetails.coinglass?.oi_change_pct,
        metric_fmt: (v) => v != null ? `${v > 0 ? '+' : ''}${Number(v).toFixed(1)}%` : null,
        freq: '15s',
      },
      {
        id: 'gamma',
        name: 'Polymarket 5min',
        connected: conn.gamma ?? conn.gamma_connected ?? null,
        last_ts: feedDetails.gamma?.last_ts || null,
        metric_label: 'Window',
        metric_value: feedDetails.gamma,
        metric_fmt: (v) => v?.state ? `${v.state}` : null,
        freq: '1s',
      },
      {
        id: 'clob',
        name: 'CLOB Order Book',
        connected: conn.clob ?? conn.clob_connected ?? null,
        last_ts: feedDetails.clob?.last_ts || null,
        metric_label: 'UP/DOWN Ask',
        metric_value: feedDetails.clob,
        metric_fmt: (v) => (v?.up_ask != null) ? `$${Number(v.up_ask).toFixed(2)} / $${Number(v.down_ask).toFixed(2)}` : null,
        freq: '10s',
      },
      {
        id: 'timesfm',
        name: 'TimesFM ML',
        connected: conn.timesfm ?? conn.timesfm_connected ?? null,
        last_ts: feedDetails.timesfm?.last_ts || null,
        metric_label: 'P(UP)',
        metric_value: feedDetails.timesfm,
        metric_fmt: (v) => v?.probability_up != null ? `${(v.probability_up * 100).toFixed(1)}% (${v.model_version || '?'})` : null,
        freq: '1s',
      },
    ];

    // If no real feed data, apply mock connectivity
    const hasFeedData = Object.keys(feedDetails).length > 0 || Object.keys(conn).length > 0;
    if (!hasFeedData) {
      const mockFeeds = MOCK_SYSTEM.feeds;
      feedList.forEach(f => {
        const mock = mockFeeds[f.id];
        if (mock) {
          f.connected = mock.connected;
          f.last_ts = mock.last_ts;
          if (!f.metric_value) f.metric_value = mock;
        }
      });
    }

    return {
      system: sys,
      feeds: feedList,
      signals: sig,
      strategy: strat,
      execution: exec,
      postTrade: pt,
      wallet: wal,
      window: win,
      trades,
      loading,
      error,
      tick,
      wsConnected,
      isUsingMocks: !system,
      refresh: fetchAll,
    };
  }, [system, signals, strategy, execution, postTrade, wallet, window, trades, loading, error, tick, wsConnected, fetchAll]);

  return merged;
}
