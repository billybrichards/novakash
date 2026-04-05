/**
 * AnalysisLibrary.jsx — Analysis document library.
 *
 * Browse, read, and search analysis reports.
 * Markdown rendered inline with status badges and tags.
 */

import React, { useState, useEffect } from 'react';

const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80',
  loss: '#f87171',
  warning: '#f59e0b',
  purple: '#a855f7',
  cyan: '#06b6d4',
  text: 'rgba(255,255,255,0.92)',
  textSec: 'rgba(255,255,255,0.45)',
  label: 'rgba(255,255,255,0.25)',
  mono: "'IBM Plex Mono', monospace",
};

const STATUS_COLORS = {
  critical: { bg: 'rgba(248,113,113,0.1)', border: 'rgba(248,113,113,0.4)', text: '#f87171' },
  warning: { bg: 'rgba(245,158,11,0.1)', border: 'rgba(245,158,11,0.4)', text: '#f59e0b' },
  draft: { bg: 'rgba(255,255,255,0.05)', border: 'rgba(255,255,255,0.15)', text: 'rgba(255,255,255,0.5)' },
  published: { bg: 'rgba(74,222,128,0.1)', border: 'rgba(74,222,128,0.4)', text: '#4ade80' },
};

function StatusBadge({ status }) {
  const s = STATUS_COLORS[status] || STATUS_COLORS.draft;
  return (
    <span style={{
      padding: '2px 8px', borderRadius: 12,
      background: s.bg, border: `1px solid ${s.border}`,
      fontSize: 10, fontFamily: T.mono, fontWeight: 600, color: s.text,
      textTransform: 'uppercase', letterSpacing: '0.05em',
    }}>
      {status}
    </span>
  );
}

function Tag({ label }) {
  return (
    <span style={{
      padding: '1px 6px', borderRadius: 8,
      background: 'rgba(168,85,247,0.1)', border: '1px solid rgba(168,85,247,0.25)',
      fontSize: 9, fontFamily: T.mono, color: T.purple,
    }}>
      {label}
    </span>
  );
}

function SimpleMarkdown({ content }) {
  if (!content) return null;

  const lines = content.split('\n');
  const elements = [];

  let inTable = false;
  let tableRows = [];
  let inCode = false;
  let codeLines = [];

  const flushTable = () => {
    if (tableRows.length > 0) {
      const headerCells = tableRows[0].split('|').filter(c => c.trim()).map(c => c.trim());
      const dataRows = tableRows.slice(2);
      elements.push(
        <div key={`table-${elements.length}`} style={{ overflowX: 'auto', margin: '12px 0' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontFamily: T.mono }}>
            <thead>
              <tr>
                {headerCells.map((h, i) => (
                  <th key={i} style={{ padding: '6px 10px', borderBottom: `1px solid ${T.border}`, textAlign: 'left', color: T.textSec, fontSize: 10, letterSpacing: '0.05em' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dataRows.map((row, ri) => {
                const cells = row.split('|').filter(c => c.trim()).map(c => c.trim());
                return (
                  <tr key={ri}>
                    {cells.map((c, ci) => {
                      const isPositive = c.startsWith('+$') || c.includes('100.0%') || c.includes('99.');
                      const isNegative = c.startsWith('-$');
                      const color = isPositive ? T.profit : isNegative ? T.loss : T.text;
                      return (
                        <td key={ci} style={{ padding: '5px 10px', borderBottom: `1px solid rgba(255,255,255,0.03)`, color }}>
                          {c.replace(/\*\*/g, '')}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      );
      tableRows = [];
    }
  };

  const flushCode = () => {
    if (codeLines.length > 0) {
      elements.push(
        <pre key={`code-${elements.length}`} style={{
          background: 'rgba(255,255,255,0.03)', border: `1px solid ${T.border}`,
          borderRadius: 8, padding: '12px 16px', margin: '8px 0',
          fontSize: 11, fontFamily: T.mono, color: T.textSec, overflow: 'auto',
        }}>
          {codeLines.join('\n')}
        </pre>
      );
      codeLines = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith('```')) {
      if (inCode) { flushCode(); inCode = false; }
      else { inCode = true; }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    if (line.includes('|') && line.trim().startsWith('|')) {
      if (!inTable) inTable = true;
      tableRows.push(line);
      continue;
    } else if (inTable) {
      flushTable();
      inTable = false;
    }

    if (line.startsWith('# ')) {
      elements.push(<h1 key={i} style={{ fontSize: 20, fontWeight: 700, color: T.text, margin: '20px 0 8px' }}>{line.slice(2)}</h1>);
    } else if (line.startsWith('## ')) {
      elements.push(<h2 key={i} style={{ fontSize: 16, fontWeight: 700, color: T.text, margin: '16px 0 6px', borderBottom: `1px solid ${T.border}`, paddingBottom: 4 }}>{line.slice(3)}</h2>);
    } else if (line.startsWith('### ')) {
      elements.push(<h3 key={i} style={{ fontSize: 14, fontWeight: 600, color: T.textSec, margin: '12px 0 4px' }}>{line.slice(4)}</h3>);
    } else if (line.startsWith('- ') || line.startsWith('* ')) {
      const text = line.slice(2).replace(/\*\*(.+?)\*\*/g, '$1').replace(/`(.+?)`/g, '$1');
      elements.push(
        <div key={i} style={{ paddingLeft: 16, fontSize: 12, color: T.text, margin: '2px 0', lineHeight: 1.6 }}>
          <span style={{ color: T.purple, marginRight: 8 }}>•</span>{text}
        </div>
      );
    } else if (line.startsWith('---')) {
      elements.push(<hr key={i} style={{ border: 'none', borderTop: `1px solid ${T.border}`, margin: '16px 0' }} />);
    } else if (line.trim()) {
      const text = line.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`(.+?)`/g, '$1');
      elements.push(<p key={i} style={{ fontSize: 12, color: T.text, margin: '4px 0', lineHeight: 1.6 }}>{text}</p>);
    }
  }

  if (inTable) flushTable();
  if (inCode) flushCode();

  return <>{elements}</>;
}

export default function AnalysisLibrary() {
  const [docs, setDocs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem('token');
    if (!token) return;
    fetch('/api/analysis', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.json())
      .then(data => { setDocs(data.docs || []); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  const loadDoc = (docId) => {
    const token = localStorage.getItem('token');
    fetch(`/api/analysis/${docId}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.json())
      .then(data => setSelected(data))
      .catch(() => {});
  };

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 52px)', background: T.bg, overflow: 'hidden' }}>
      {/* Left: Doc list */}
      <div style={{
        width: 340, flexShrink: 0, borderRight: `1px solid ${T.border}`,
        overflow: 'auto', padding: '16px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
          <span style={{ fontSize: 18 }}>📚</span>
          <span style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color: T.text }}>Analysis Library</span>
        </div>

        {loading && <div style={{ color: T.label, fontSize: 12 }}>Loading...</div>}

        {docs.map(doc => (
          <div
            key={doc.doc_id}
            onClick={() => loadDoc(doc.doc_id)}
            style={{
              padding: '12px 14px', marginBottom: 8, borderRadius: 10, cursor: 'pointer',
              background: selected?.doc_id === doc.doc_id ? 'rgba(168,85,247,0.08)' : T.card,
              border: `1px solid ${selected?.doc_id === doc.doc_id ? 'rgba(168,85,247,0.3)' : T.border}`,
              transition: 'all 200ms ease-out',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <StatusBadge status={doc.status} />
              <span style={{ fontSize: 9, color: T.label, fontFamily: T.mono }}>
                {doc.created_at ? new Date(doc.created_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : ''}
              </span>
            </div>
            <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 4 }}>
              {doc.title}
            </div>
            {doc.summary && (
              <div style={{ fontSize: 11, color: T.textSec, lineHeight: 1.4 }}>
                {doc.summary}
              </div>
            )}
            {doc.tags?.length > 0 && (
              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 6 }}>
                {doc.tags.map(t => <Tag key={t} label={t} />)}
              </div>
            )}
          </div>
        ))}

        {!loading && docs.length === 0 && (
          <div style={{ color: T.label, fontSize: 12, textAlign: 'center', marginTop: 40 }}>
            No analysis docs yet
          </div>
        )}
      </div>

      {/* Right: Doc content */}
      <div style={{ flex: 1, overflow: 'auto', padding: '24px 32px' }}>
        {selected ? (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
              <StatusBadge status={selected.status} />
              <span style={{ fontSize: 10, color: T.label, fontFamily: T.mono }}>
                {selected.doc_id}
              </span>
              <span style={{ fontSize: 10, color: T.label, fontFamily: T.mono }}>
                {selected.data_period}
              </span>
            </div>
            <h1 style={{ fontSize: 22, fontWeight: 700, color: T.text, marginBottom: 4 }}>{selected.title}</h1>
            <div style={{ fontSize: 11, color: T.textSec, marginBottom: 20 }}>
              By {selected.author} · {selected.created_at ? new Date(selected.created_at).toLocaleString('en-GB') : ''}
            </div>
            {selected.tags?.length > 0 && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 20 }}>
                {selected.tags.map(t => <Tag key={t} label={t} />)}
              </div>
            )}
            <div style={{
              background: T.card, border: `1px solid ${T.border}`, borderRadius: 12,
              padding: '24px 28px',
            }}>
              <SimpleMarkdown content={selected.content} />
            </div>
          </>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: T.label, fontSize: 13 }}>
            Select an analysis doc from the left
          </div>
        )}
      </div>
    </div>
  );
}
