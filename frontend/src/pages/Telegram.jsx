import React from 'react';
import PositionSnapshotBar from './telegram/PositionSnapshotBar.jsx';
import { T } from './polymarket/components/theme.js';

/**
 * Telegram (page)
 * ----------------
 * Minimal landing page that hosts the live PositionSnapshotBar at the top
 * (see docs/superpowers/plans/2026-04-16-telegram-redemption-visibility.md
 * — Task 10). Notification feed UI from the older 2026-04-14 dashboard plan
 * is not yet wired; this page will grow to host it next.
 */
export default function TelegramPage() {
  return (
    <div style={{ minHeight: '100vh', background: T.bg, color: T.text }}>
      <PositionSnapshotBar />
      <div
        style={{
          padding: 24,
          fontFamily: T.mono,
          color: T.textMuted,
          fontSize: 13,
          lineHeight: 1.6,
        }}
      >
        <h2
          style={{
            color: T.text,
            fontWeight: 500,
            marginBottom: 8,
            fontSize: 18,
          }}
        >
          Telegram Feed
        </h2>
        <p style={{ marginBottom: 6 }}>
          Position snapshot above is live (5s polling). Notification feed coming next.
        </p>
        <p style={{ color: T.textDim, fontSize: 11 }}>
          Source: <code style={{ color: T.cyan }}>GET /api/positions/snapshot</code>
        </p>
      </div>
    </div>
  );
}
