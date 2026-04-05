/**
 * AnalysisLibrary.jsx — Clean document-style analysis library.
 *
 * Visual style: emerald green accent, serif titles, clean tables,
 * minimal chrome. Inspired by technical documentation design.
 */

import React, { useState, useEffect } from 'react';

// ── Design Tokens (document style) ──────────────────────────────────────────

const C = {
  primary: '#00B476',
  primaryLight: '#E6F7F0',
  primaryDim: 'rgba(0,180,118,0.08)',
  textDark: '#111827',
  textBody: '#374151',
  textMuted: '#6B7280',
  border: '#E5E7EB',
  bg: '#FFFFFF',
  bgPage: '#F9FAFB',
  loss: '#DC2626',
  lossLight: '#FEE2E2',
  warn: '#D97706',
  warnLight: '#FEF3C7',
  profit: '#059669',
  profitLight: '#D1FAE5',
};

const F = {
  serif: "'Playfair Display', Georgia, 'Times New Roman', serif",
  sans: "'Inter', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
  mono: "'SF Mono', 'Fira Code', Consolas, 'Courier New', monospace",
};

// ── Status Badge ────────────────────────────────────────────────────────────

const STATUS = {
  critical: { bg: C.lossLight, border: '#FECACA', text: C.loss, label: '⚠️ CRITICAL' },
  warning: { bg: C.warnLight, border: '#FDE68A', text: C.warn, label: '⚡ WARNING' },
  draft: { bg: '#F3F4F6', border: '#D1D5DB', text: C.textMuted, label: 'DRAFT' },
  published: { bg: C.profitLight, border: '#A7F3D0', text: C.profit, label: '✓ PUBLISHED' },
};

function StatusBadge({ status }) {
  const s = STATUS[status] || STATUS.draft;
  return (
    <span style={{
      padding: '3px 10px', borderRadius: 4,
      background: s.bg, border: `1px solid ${s.border}`,
      fontSize: 11, fontFamily: F.sans, fontWeight: 600, color: s.text,
      letterSpacing: '0.02em',
    }}>
      {s.label}
    </span>
  );
}

function Tag({ label }) {
  return (
    <span style={{
      padding: '2px 8px', borderRadius: 4,
      background: C.primaryDim, border: `1px solid rgba(0,180,118,0.2)`,
      fontSize: 11, fontFamily: F.mono, color: C.primary, fontWeight: 500,
    }}>
      {label}
    </span>
  );
}

// ── Markdown Renderer (document style) ──────────────────────────────────────

function DocMarkdown({ content }) {
  if (!content) return null;
  const lines = content.split('\n');
  const elements = [];
  let inTable = false, tableRows = [], inCode = false, codeLines = [];

  const flushTable = () => {
    if (tableRows.length < 2) { tableRows = []; return; }
    const headerCells = tableRows[0].split('|').filter(c => c.trim()).map(c => c.trim());
    const separator = tableRows[1];
    const dataRows = tableRows.slice(2);
    elements.push(
      <table key={`t${elements.length}`} style={{
        width: '100%', borderCollapse: 'collapse', margin: '16px 0',
        fontSize: 14, fontFamily: F.sans,
      }}>
        <thead>
          <tr>{headerCells.map((h, i) => (
            <th key={i} style={{
              padding: '10px 14px', textAlign: 'left',
              background: C.primaryLight, color: C.primary,
              fontWeight: 600, fontSize: 12, letterSpacing: '0.03em',
              borderBottom: `2px solid ${C.primaryLight}`,
            }}>{h.replace(/\*\*/g, '')}</th>
          ))}</tr>
        </thead>
        <tbody>
          {dataRows.map((row, ri) => {
            const cells = row.split('|').filter(c => c.trim()).map(c => c.trim());
            return (
              <tr key={ri}>{cells.map((c, ci) => {
                const clean = c.replace(/\*\*/g, '');
                const isPositive = clean.match(/^\+?\$[\d,]+/) || clean.includes('100.0%') || clean.match(/9[5-9]\.\d%/);
                const isNegative = clean.startsWith('-$');
                const isBold = c.startsWith('**');
                return (
                  <td key={ci} style={{
                    padding: '10px 14px',
                    borderBottom: `1px solid ${C.border}`,
                    color: isPositive ? C.profit : isNegative ? C.loss : C.textBody,
                    fontWeight: isBold ? 600 : 400,
                    fontFamily: clean.startsWith('$') || clean.startsWith('+$') || clean.startsWith('-$') ? F.mono : F.sans,
                  }}>{clean}</td>
                );
              })}</tr>
            );
          })}
        </tbody>
      </table>
    );
    tableRows = [];
  };

  const flushCode = () => {
    if (codeLines.length > 0) {
      elements.push(
        <pre key={`c${elements.length}`} style={{
          background: '#F9FAFB', border: `1px solid ${C.border}`,
          borderRadius: 8, padding: '16px 20px', margin: '12px 0',
          fontSize: 13, fontFamily: F.mono, color: C.textDark,
          lineHeight: 1.7, overflow: 'auto',
        }}>
          {codeLines.join('\n')}
        </pre>
      );
      codeLines = [];
    }
  };

  const renderInline = (text) => {
    return text
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/`(.+?)`/g, '<code>$1</code>');
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith('```')) {
      if (inCode) { flushCode(); inCode = false; } else { inCode = true; }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    if (line.includes('|') && line.trim().startsWith('|')) {
      if (!inTable) inTable = true;
      tableRows.push(line);
      continue;
    } else if (inTable) { flushTable(); inTable = false; }

    if (line.startsWith('# ')) {
      elements.push(
        <h1 key={i} style={{
          fontFamily: F.serif, fontSize: 28, fontWeight: 700,
          color: C.textDark, margin: '32px 0 8px', lineHeight: 1.3,
        }}>{line.slice(2)}</h1>
      );
    } else if (line.startsWith('## ')) {
      elements.push(
        <div key={i} style={{ margin: '28px 0 14px' }}>
          <div style={{
            fontFamily: F.sans, fontSize: 18, fontWeight: 700,
            textTransform: 'uppercase', letterSpacing: '0.06em',
            color: C.primary, display: 'flex', alignItems: 'center', gap: 10,
          }}>
            <span style={{ color: C.primary, fontSize: 14 }}>■</span>
            {line.slice(3)}
          </div>
        </div>
      );
    } else if (line.startsWith('### ')) {
      elements.push(
        <h3 key={i} style={{
          fontFamily: F.sans, fontSize: 15, fontWeight: 600,
          color: C.textDark, margin: '18px 0 8px',
        }}>{line.slice(4)}</h3>
      );
    } else if (line.startsWith('- ') || line.startsWith('* ')) {
      const text = line.slice(2);
      elements.push(
        <div key={i} style={{
          paddingLeft: 20, fontSize: 14, color: C.textBody,
          margin: '4px 0', lineHeight: 1.7, fontFamily: F.sans,
        }}>
          <span dangerouslySetInnerHTML={{ __html: `<span style="color:${C.primary};margin-right:10px">•</span>${renderInline(text)}` }} />
        </div>
      );
    } else if (line.startsWith('---')) {
      elements.push(<hr key={i} style={{ border: 'none', borderTop: `1px solid ${C.border}`, margin: '24px 0' }} />);
    } else if (line.trim()) {
      elements.push(
        <p key={i} style={{
          fontSize: 14, color: C.textBody, margin: '6px 0',
          lineHeight: 1.7, fontFamily: F.sans,
        }}>
          <span dangerouslySetInnerHTML={{ __html: renderInline(line) }} />
        </p>
      );
    }
  }

  if (inTable) flushTable();
  if (inCode) flushCode();
  return <>{elements}</>;
}

// ── Main Page ───────────────────────────────────────────────────────────────

export default function AnalysisLibrary() {
  const [docs, setDocs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem('access_token');
    if (!token) return;
    fetch('/api/analysis', { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.json())
      .then(data => {
        setDocs(data.docs || []);
        setLoading(false);
        // Auto-select first doc
        if (data.docs?.length > 0) loadDoc(data.docs[0].doc_id);
      })
      .catch(() => setLoading(false));
  }, []);

  const loadDoc = (docId) => {
    const token = localStorage.getItem('access_token');
    fetch(`/api/analysis/${docId}`, { headers: { Authorization: `Bearer ${token}` } })
      .then(r => r.json())
      .then(data => setSelected(data))
      .catch(() => {});
  };

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 52px)', background: C.bgPage, overflow: 'hidden' }}>
      {/* ── Left Panel: Doc List ──────────────────────────────────────── */}
      <div style={{
        width: 320, flexShrink: 0, borderRight: `1px solid ${C.border}`,
        overflow: 'auto', padding: '20px 16px', background: C.bg,
      }}>
        <div style={{
          fontSize: 20, fontFamily: F.serif, fontWeight: 700,
          color: C.textDark, marginBottom: 4,
        }}>
          Analysis Library
        </div>
        <div style={{ fontSize: 13, color: C.textMuted, marginBottom: 20, fontFamily: F.sans }}>
          Research reports &amp; backtests
        </div>

        {loading && <div style={{ color: C.textMuted, fontSize: 13 }}>Loading...</div>}

        {docs.map(doc => (
          <div
            key={doc.doc_id}
            onClick={() => loadDoc(doc.doc_id)}
            style={{
              padding: '14px 16px', marginBottom: 8, borderRadius: 8, cursor: 'pointer',
              background: selected?.doc_id === doc.doc_id ? C.primaryDim : C.bg,
              border: `1px solid ${selected?.doc_id === doc.doc_id ? 'rgba(0,180,118,0.3)' : C.border}`,
              transition: 'all 200ms ease-out',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <StatusBadge status={doc.status} />
              <span style={{ fontSize: 11, color: C.textMuted, fontFamily: F.mono }}>
                {doc.data_period}
              </span>
            </div>
            <div style={{ fontSize: 14, fontWeight: 600, color: C.textDark, marginBottom: 4, fontFamily: F.sans }}>
              {doc.title}
            </div>
            {doc.summary && (
              <div style={{ fontSize: 12, color: C.textMuted, lineHeight: 1.5, fontFamily: F.sans }}>
                {doc.summary}
              </div>
            )}
            {doc.tags?.length > 0 && (
              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 8 }}>
                {doc.tags.map(t => <Tag key={t} label={t} />)}
              </div>
            )}
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 8, fontFamily: F.mono }}>
              {doc.created_at ? new Date(doc.created_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''}
              {doc.author ? ` · ${doc.author}` : ''}
            </div>
          </div>
        ))}

        {!loading && docs.length === 0 && (
          <div style={{ color: C.textMuted, fontSize: 13, textAlign: 'center', marginTop: 60 }}>
            No analysis docs yet
          </div>
        )}
      </div>

      {/* ── Right Panel: Document Reader ──────────────────────────────── */}
      <div style={{ flex: 1, overflow: 'auto', background: C.bg }}>
        {selected ? (
          <div style={{ maxWidth: 760, margin: '0 auto', padding: '40px 32px' }}>
            {/* Document header */}
            <div style={{ textAlign: 'center', marginBottom: 32 }}>
              <h1 style={{
                fontFamily: F.serif, fontSize: 30, fontWeight: 700,
                color: C.textDark, lineHeight: 1.3, margin: '0 0 8px',
              }}>
                {selected.title}
              </h1>
              {selected.summary && (
                <div style={{
                  fontFamily: F.serif, fontStyle: 'italic', fontWeight: 600,
                  fontSize: 16, color: C.textBody, marginBottom: 8,
                }}>
                  {selected.summary}
                </div>
              )}
              <div style={{ fontSize: 13, color: C.textMuted, fontFamily: F.sans }}>
                {selected.created_at ? new Date(selected.created_at).toLocaleDateString('en-GB', {
                  weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
                }) : ''}
                {' | '}By {selected.author}
                {selected.data_period ? ` | Data: ${selected.data_period}` : ''}
              </div>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 12 }}>
                <StatusBadge status={selected.status} />
                {selected.tags?.map(t => <Tag key={t} label={t} />)}
              </div>
              <div style={{
                fontSize: 10, color: C.textMuted, fontFamily: F.mono,
                marginTop: 8,
              }}>
                {selected.doc_id}
              </div>
            </div>

            {/* Document content */}
            <DocMarkdown content={selected.content} />

            {/* Footer */}
            <div style={{
              textAlign: 'center', marginTop: 48, paddingTop: 16,
              borderTop: `1px solid ${C.border}`,
              fontStyle: 'italic', fontSize: 12, color: C.textMuted,
              fontFamily: F.sans,
            }}>
              Generated by Novakash Analysis Engine · {selected.doc_id}
            </div>
          </div>
        ) : (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            height: '100%', color: C.textMuted, fontSize: 14, fontFamily: F.sans,
          }}>
            ← Select an analysis report
          </div>
        )}
      </div>

      <style>{`
        code {
          background: ${C.primaryLight};
          padding: 1px 5px;
          border-radius: 3px;
          font-family: ${F.mono};
          font-size: 13px;
          color: ${C.primary};
        }
        b { color: ${C.textDark}; }
      `}</style>
    </div>
  );
}
