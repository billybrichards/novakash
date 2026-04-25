// /desk — "Your Play" card.
//
// Three buttons (UP / DOWN / SKIP), an optional notes box, and a hard
// T-00:10 client-side lockout. On click we POST /api/desk/picks and
// stash a localStorage echo so a reload mid-window still shows the
// operator's last call.
//
// The hub endpoint is UPSERT so flipping UP → DOWN before the lockout
// just updates the row.

import React, { useEffect, useState } from 'react';
import { T } from '../../../theme/tokens.js';
import { LOCKOUT_SECONDS, readCachedPick } from '../hooks/usePicks.js';

export default function YourPlay({
  windowEpoch,
  secondsRemaining,
  submitPick,
  latestPickFromServer,
}) {
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [localPick, setLocalPick] = useState(null);

  // Hydrate from localStorage on mount / when window rotates.
  useEffect(() => {
    const cached = readCachedPick(windowEpoch);
    setLocalPick(cached?.pick ?? null);
    setNotes(''); // fresh window → fresh notes
    setErr(null);
  }, [windowEpoch]);

  // Prefer the server's view if it's present for this window.
  const servedPick = latestPickFromServer?.window_epoch === windowEpoch
    ? latestPickFromServer?.pick
    : null;
  const shownPick = servedPick ?? localPick;

  const lockedOut = secondsRemaining <= LOCKOUT_SECONDS;

  const click = async (pick) => {
    if (lockedOut || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await submitPick({
        windowEpoch,
        pick,
        tRemainingS: secondsRemaining,
        notes: notes.trim() || null,
      });
      setLocalPick(pick);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || 'submit failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      border: `1px solid ${T.borderStrong}`,
      padding: 10,
      background: T.card,
      marginBottom: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ fontSize: 10, color: T.label, letterSpacing: '0.1em' }}>YOUR PLAY</div>
        {lockedOut ? (
          <span style={{ fontSize: 10, color: T.loss, letterSpacing: '0.1em' }}>
            LOCKED · T-{String(Math.max(0, secondsRemaining)).padStart(2, '0')}
          </span>
        ) : null}
      </div>

      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        <PickButton
          label="▲ UP"
          colour={T.profit}
          active={shownPick === 'UP'}
          disabled={lockedOut || busy}
          onClick={() => click('UP')}
        />
        <PickButton
          label="▼ DOWN"
          colour={T.loss}
          active={shownPick === 'DOWN'}
          disabled={lockedOut || busy}
          onClick={() => click('DOWN')}
        />
        <PickButton
          label="SKIP"
          colour={T.label2}
          active={shownPick === 'SKIP'}
          disabled={lockedOut || busy}
          onClick={() => click('SKIP')}
        />
      </div>

      <textarea
        value={notes}
        onChange={e => setNotes(e.target.value.slice(0, 2000))}
        placeholder="notes (optional, 2000 char max)…"
        rows={2}
        disabled={lockedOut}
        style={{
          width: '100%',
          background: 'transparent',
          border: `1px solid ${T.border}`,
          color: T.text,
          fontFamily: T.font,
          fontSize: 11,
          padding: 6,
          resize: 'vertical',
          boxSizing: 'border-box',
        }}
      />

      {err ? <div style={{ color: T.loss, fontSize: 11, marginTop: 6 }}>{err}</div> : null}
      {shownPick ? (
        <div style={{ fontSize: 10, color: T.label2, marginTop: 6 }}>
          logged: you → <span style={{ color: T.text }}>{shownPick}</span>
          {' '}@ window {windowEpoch}
        </div>
      ) : null}
    </div>
  );
}

function PickButton({ label, colour, active, disabled, onClick }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      style={{
        flex: 1,
        padding: '10px 8px',
        fontSize: 13,
        letterSpacing: '0.08em',
        fontFamily: T.font,
        background: active ? colour : 'transparent',
        color: active ? '#000' : colour,
        border: `1px solid ${colour}`,
        borderRadius: 2,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
      }}
    >{label}</button>
  );
}
