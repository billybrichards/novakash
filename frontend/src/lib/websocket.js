/**
 * websocket.js — TimesFM WebSocket client.
 *
 * Connects to VITE_TIMESFM_WS_URL (set in .env.production).
 * Falls back gracefully to mock data when the backend isn't available.
 *
 * Usage:
 *   const ws = createTimesFMClient({ onForecast, onStatus, onError });
 *   ws.connect();
 *   ws.disconnect();
 */

const WS_URL = import.meta.env.VITE_TIMESFM_WS_URL || 'ws://16.52.14.182:8080/ws/forecast';
const RECONNECT_DELAY_BASE = 1500;
const RECONNECT_DELAY_MAX = 30_000;
const RECONNECT_MAX_ATTEMPTS = 10;

export const WS_STATUS = {
  DISCONNECTED: 'DISCONNECTED',
  CONNECTING: 'CONNECTING',
  CONNECTED: 'CONNECTED',
  RECONNECTING: 'RECONNECTING',
  FAILED: 'FAILED',
};

/**
 * createTimesFMClient — factory for a managed WebSocket connection.
 *
 * @param {{ onForecast, onStatus, onError, onMessage }} callbacks
 * @returns {{ connect, disconnect, send, getStatus }}
 */
export function createTimesFMClient({ onForecast, onStatus, onError, onMessage } = {}) {
  let ws = null;
  let status = WS_STATUS.DISCONNECTED;
  let attempts = 0;
  let reconnectTimer = null;
  let destroyed = false;

  function setStatus(s) {
    status = s;
    onStatus?.(s);
  }

  function handleMessage(event) {
    try {
      const msg = JSON.parse(event.data);
      onMessage?.(msg);

      // Route by message type
      switch (msg.type) {
        case 'forecast':
          onForecast?.(msg.payload);
          break;
        case 'heartbeat':
          // ignore
          break;
        case 'error':
          onError?.(new Error(msg.message || 'Server error'));
          break;
        default:
          // pass through raw
          onForecast?.(msg);
      }
    } catch (err) {
      console.error('[TimesFM WS] Failed to parse message:', err);
    }
  }

  function scheduleReconnect() {
    if (destroyed || attempts >= RECONNECT_MAX_ATTEMPTS) {
      setStatus(WS_STATUS.FAILED);
      return;
    }
    const delay = Math.min(
      RECONNECT_DELAY_BASE * Math.pow(1.5, attempts),
      RECONNECT_DELAY_MAX
    );
    setStatus(WS_STATUS.RECONNECTING);
    reconnectTimer = setTimeout(() => {
      if (!destroyed) connect();
    }, delay);
  }

  function connect() {
    if (destroyed) return;
    if (ws && ws.readyState === WebSocket.OPEN) return;

    clearTimeout(reconnectTimer);
    setStatus(WS_STATUS.CONNECTING);

    try {
      ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        attempts = 0;
        setStatus(WS_STATUS.CONNECTED);
        // Request initial forecast snapshot
        ws.send(JSON.stringify({ type: 'subscribe', channel: 'btc_updown' }));
      };

      ws.onmessage = handleMessage;

      ws.onerror = (event) => {
        console.warn('[TimesFM WS] Connection error — backend may not be running yet');
        onError?.(new Error('WebSocket connection failed'));
      };

      ws.onclose = (event) => {
        if (!destroyed) {
          attempts++;
          scheduleReconnect();
        } else {
          setStatus(WS_STATUS.DISCONNECTED);
        }
      };
    } catch (err) {
      console.error('[TimesFM WS] Failed to create WebSocket:', err);
      attempts++;
      scheduleReconnect();
    }
  }

  function disconnect() {
    destroyed = true;
    clearTimeout(reconnectTimer);
    if (ws) {
      ws.onclose = null; // prevent reconnect loop
      ws.close();
      ws = null;
    }
    setStatus(WS_STATUS.DISCONNECTED);
  }

  function send(payload) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function getStatus() {
    return status;
  }

  return { connect, disconnect, send, getStatus };
}

/**
 * useTimesFMStatus — returns a human-readable status string + colour.
 */
export function formatWSStatus(status) {
  switch (status) {
    case WS_STATUS.CONNECTED:
      return { label: 'LIVE', color: '#4ade80', dot: true };
    case WS_STATUS.CONNECTING:
      return { label: 'CONNECTING', color: '#f59e0b', dot: false };
    case WS_STATUS.RECONNECTING:
      return { label: 'RECONNECTING', color: '#f59e0b', dot: false };
    case WS_STATUS.FAILED:
      return { label: 'FAILED', color: '#f87171', dot: false };
    default:
      return { label: 'MOCK', color: '#a855f7', dot: false };
  }
}
