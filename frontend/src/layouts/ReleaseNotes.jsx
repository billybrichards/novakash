import React, { useEffect, useRef, useState } from 'react';
import { T } from '../theme/tokens.js';

// Fetches /release-notes.json (built at npm prebuild time from git log) and
// renders a "what's new" dropdown in the AppShell top bar. Entries link
// back to the GitHub PR and commit.

const MAX_VISIBLE = 25;

export default function ReleaseNotes() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    let cancelled = false;
    fetch('/release-notes.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((j) => {
        if (!cancelled) setData(j);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const onClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const onEsc = (e) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    window.addEventListener('keydown', onEsc);
    return () => {
      window.removeEventListener('mousedown', onClick);
      window.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  const entries = (data?.entries || []).slice(0, MAX_VISIBLE);
  const latestPr = entries[0]?.prNumber;

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          background: open ? 'rgba(168,85,247,0.12)' : 'transparent',
          border: `1px solid ${T.border}`,
          color: T.label,
          fontFamily: T.font,
          fontSize: 11,
          letterSpacing: '0.08em',
          padding: '4px 10px',
          borderRadius: 2,
          cursor: 'pointer',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
        }}
        title="Recent merges on develop"
      >
        <span style={{ fontSize: 10, opacity: 0.75 }}>RELEASES</span>
        {latestPr ? (
          <span style={{ color: '#a855f7' }}>#{latestPr}</span>
        ) : (
          <span style={{ opacity: 0.4 }}>—</span>
        )}
      </button>

      {open && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            right: 0,
            width: 420,
            maxHeight: 480,
            overflowY: 'auto',
            background: T.bg,
            border: `1px solid ${T.border}`,
            borderRadius: 4,
            padding: '8px 0',
            zIndex: 100,
            boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
          }}
        >
          <div
            style={{
              padding: '4px 12px 8px',
              borderBottom: `1px solid ${T.border}`,
              fontSize: 10,
              letterSpacing: '0.18em',
              color: T.label,
              display: 'flex',
              justifyContent: 'space-between',
            }}
          >
            <span>RECENT MERGES · DEVELOP</span>
            {data?.generated_at && (
              <span style={{ opacity: 0.5 }}>
                built {data.generated_at.slice(0, 10)}
              </span>
            )}
          </div>
          {error && (
            <div style={{ padding: '12px', fontSize: 11, color: '#f87171' }}>
              release-notes.json load failed: {error}
            </div>
          )}
          {!error && entries.length === 0 && (
            <div style={{ padding: '12px', fontSize: 11, color: T.label }}>
              No entries yet. Run <code>npm run build</code> to regenerate.
            </div>
          )}
          {entries.map((e) => (
            <a
              key={e.sha}
              href={e.prUrl || e.commitUrl}
              target="_blank"
              rel="noreferrer"
              style={{
                display: 'block',
                padding: '8px 12px',
                borderBottom: `1px solid ${T.border}`,
                textDecoration: 'none',
                color: T.text,
                fontSize: 11,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 2 }}>
                <span style={{ opacity: 0.5, fontSize: 10, letterSpacing: '0.08em' }}>{e.date}</span>
                {e.prNumber && (
                  <span style={{ color: '#a855f7', fontSize: 10 }}>#{e.prNumber}</span>
                )}
              </div>
              <div style={{ lineHeight: 1.35, whiteSpace: 'normal' }}>{e.title}</div>
              <div style={{ opacity: 0.4, fontSize: 10, marginTop: 2, fontFamily: T.font }}>
                {e.sha}
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
