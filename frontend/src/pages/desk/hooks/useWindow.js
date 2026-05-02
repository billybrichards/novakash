// /desk — window clock hook.
//
// Hits GET /api/windows/current on a 10s cadence (cheap; just the Chainlink
// target price), and ticks a client-side countdown every 1s so the UI
// doesn't depend on the network for T-MM:SS updates.
//
// When /api/windows/current hasn't responded yet we fall back to computing
// the window from local UTC. That way the FE clock runs even when the hub
// is briefly unreachable.

import { useEffect, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

const WINDOW_SECONDS = 300;

function floorWindow(nowS) {
  return Math.floor(nowS / WINDOW_SECONDS) * WINDOW_SECONDS;
}

export function useWindow(asset = 'BTC') {
  const api = useApi();
  const [server, setServer] = useState(null);
  const [serverError, setServerError] = useState(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  // Poll server every 10s — just to refresh target_price_chainlink + confirm
  // our local window math matches the hub's.
  useEffect(() => {
    let cancel = false;
    const fetchOnce = async () => {
      try {
        const r = await api.get(`/api/windows/current?asset=${encodeURIComponent(asset)}`);
        if (!cancel) {
          setServer(r?.data ?? r);
          setServerError(null);
        }
      } catch (e) {
        if (!cancel) setServerError(e.message || 'window fetch failed');
      }
    };
    fetchOnce();
    const h = setInterval(fetchOnce, 10_000);
    return () => { cancel = true; clearInterval(h); };
  }, [api, asset]);

  // Client tick every 1s.
  useEffect(() => {
    const h = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(h);
  }, []);

  const tOpen = server?.t_open_ts ?? floorWindow(now);
  const tClose = server?.t_close_ts ?? tOpen + WINDOW_SECONDS;
  const secondsRemaining = Math.max(0, tClose - now);
  const targetPriceChainlink = server?.target_price_chainlink ?? null;

  return {
    windowEpoch: server?.window_epoch ?? tOpen,
    tOpen,
    tClose,
    secondsRemaining,
    targetPriceChainlink,
    asset,
    error: serverError,
  };
}

export function fmtTRemaining(seconds) {
  const s = Math.max(0, seconds | 0);
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return `T-${mm}:${ss}`;
}
