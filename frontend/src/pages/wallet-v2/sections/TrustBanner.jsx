import React from 'react';
import { T } from '../../../theme/tokens.js';

/**
 * Red banner shown at the top of the wallet page when:
 *   - hub is serving DB-only data (spec #217 unshipped), OR
 *   - redeemer is behind (spec #189 §5.2 — wins_missed_by_engine_24h > 0).
 *
 * Renders nothing when neither condition fires.
 */
export default function TrustBanner({ rank, redeemerHealth, missingEndpoints = [] }) {
  const dbDegraded = rank === 'db-only';
  const missed = redeemerHealth?.wins_missed_24h;
  const behind = Number.isFinite(Number(missed)) && Number(missed) > 0;
  const missing = Array.isArray(missingEndpoints) && missingEndpoints.length > 0;

  if (!dbDegraded && !behind && !missing) return null;

  const bits = [];
  if (behind) {
    bits.push(
      <span key="behind">
        Engine redeemer is behind — {missed} wins needed manual action in last 24h. See audit-task #252.
      </span>
    );
  }
  if (dbDegraded) {
    bits.push(
      <span key="db">
        Balance shown is DB-only (source rank 5). `/api/wallet/snapshot` has not shipped — audit-task #217. Cross-check with <code style={codeStyle}>scripts/ops/wallet_truth.py</code> before acting.
      </span>
    );
  }
  if (missing) {
    bits.push(
      <span key="missing">
        Hub endpoints missing: {missingEndpoints.join(', ')}. Fallbacks in use.
      </span>
    );
  }

  return (
    <div style={{
      background: 'rgba(248, 113, 113, 0.08)',
      border: `1px solid ${T.loss}`,
      borderRadius: 2,
      padding: '10px 12px',
      marginBottom: 14,
      fontSize: 12,
      color: T.loss,
      fontFamily: T.font,
      lineHeight: 1.6,
    }}>
      <div style={{
        fontSize: 10,
        letterSpacing: '0.12em',
        textTransform: 'uppercase',
        fontWeight: 600,
        marginBottom: 4,
      }}>
        Trust warning
      </div>
      {bits.map((b, i) => (
        <div key={i} style={{ color: T.text }}>{b}</div>
      ))}
    </div>
  );
}

const codeStyle = {
  background: T.bg,
  border: `1px solid ${T.border}`,
  padding: '1px 5px',
  borderRadius: 2,
  fontSize: 11,
  color: T.label2,
};
