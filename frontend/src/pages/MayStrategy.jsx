import React from 'react';

/**
 * MayStrategy — static report viewer for /docs/analysis/2026-05-01-strategy-forecast.html
 *
 * The HTML file lives at frontend/public/may-strategy.html (served as static asset
 * by Vite). Charting + heatmap rendering uses Chart.js via CDN inside the HTML, so
 * we just iframe-embed it here for a zero-conversion-cost viewer.
 *
 * Route: /may-strategy
 * Source-of-truth file: docs/analysis/2026-05-01-strategy-forecast.html
 *
 * If the report becomes a live dashboard, convert to a React component reading from
 * /api/v58/strategy-comparison (audit #340 — when fixed, the FE pages StrategyLab,
 * Evaluate, StrategyConfigs, StrategyCommand will populate too).
 */
export default function MayStrategy() {
  return (
    <div style={{ position: 'absolute', inset: 0, background: '#0d1117' }}>
      <iframe
        src="/may-strategy.html"
        title="May Strategy Forecast 2026-05-01"
        style={{
          width: '100%',
          height: '100%',
          border: 'none',
          display: 'block',
        }}
      />
    </div>
  );
}
