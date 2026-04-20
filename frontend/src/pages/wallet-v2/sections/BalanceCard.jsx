import React from 'react';
import { T } from '../../../theme/tokens.js';
import SotBadge from '../sotBadge.jsx';
import { fmtUsd } from '../reconcile.js';

/**
 * Four-column balance card per spec #189 §5.2.
 *
 * `balance` shape comes out of `mapWalletSnapshotToBalance` or the DB-only
 * fallback via `mapPositionsSnapshotToBalance`. Either way, numbers carry
 * their `rank` so the SotBadge can tell the operator which source they
 * came from.
 */
export default function BalanceCard({ balance, rank = 'db-only' }) {
  if (!balance) return null;
  const delta24 = balance.delta_24h;
  const deltaSess = balance.delta_session;
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
      gap: 12,
      padding: 14,
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 2,
      marginBottom: 14,
    }}>
      <Stat
        lbl="CASH USDC"
        val={fmtUsd(balance.cash_usdc)}
        rank={rank}
        sub={rank === 'onchain' ? 'on-chain · 2-of-3' : rank === 'db-only' ? 'db-only (audit #217)' : rank}
      />
      <Stat
        lbl="PENDING WINS"
        val={fmtUsd(balance.pending_wins_value)}
        rank={rank}
        sub={balance.redeemable_now != null ? `${balance.redeemable_now} redeemable now` : null}
      />
      <Stat
        lbl="OPEN MARK"
        val={fmtUsd(balance.open_positions_mark)}
        rank={rank}
        sub={balance.open_count != null ? `${balance.open_count} open` : null}
      />
      <Stat
        lbl="EFFECTIVE TOTAL"
        val={fmtUsd(balance.effective_total)}
        rank={rank}
        sub={delta24 != null ? `${fmtUsd(delta24, { signed: true })} (24h)` : deltaSess != null ? `${fmtUsd(deltaSess, { signed: true })} (session)` : null}
      />
    </div>
  );
}

function Stat({ lbl, val, sub, rank }) {
  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4,
      }}>
        <div style={{
          fontSize: 10,
          letterSpacing: '0.12em',
          color: T.label,
          textTransform: 'uppercase',
        }}>{lbl}</div>
        <SotBadge rank={rank} compact />
      </div>
      <div style={{ fontSize: 22, fontWeight: 500, color: T.text, fontFamily: T.font }}>
        {val}
      </div>
      {sub ? (
        <div style={{ fontSize: 11, color: T.label2, marginTop: 2 }}>{sub}</div>
      ) : null}
    </div>
  );
}
