/**
 * ModelGenealogy.jsx
 *
 * Embeds the model genealogy + 15m diagnostic HTML map.
 * Source HTML lives at frontend/public/docs/15m_winning_strategy/model_genealogy.html
 * Companion markdown docs live in the worktree under docs/15m_winning_strategy/.
 *
 * The HTML is self-contained (inline CSS, no external assets) so iframe
 * embedding is the simplest and most robust integration. It also keeps the
 * canonical render identical across the worktree, GitHub preview, and the FE.
 *
 * Owner: 15m_winning_strategy worktree (PR #456)
 * Refs: hub note #311, audit task #343, audit task #347
 */
import React from 'react';

const SOURCE_PATH = '/docs/15m_winning_strategy/model_genealogy.html';
const REPO_PATH =
  'docs/15m_winning_strategy/model_genealogy.html';
const RETRAIN_SPEC_PATH =
  'docs/15m_winning_strategy/v12xl_15m_retrain_spec.md';

export default function ModelGenealogy() {
  const githubBlobUrl = `https://github.com/billybrichards/novakash/blob/develop/${REPO_PATH}`;
  const retrainSpecUrl = `https://github.com/billybrichards/novakash/blob/develop/${RETRAIN_SPEC_PATH}`;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid var(--border, #1f2638)',
          background: 'var(--card, #131826)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <div>
          <div style={{ fontSize: 14, fontWeight: 600 }}>
            TimesFM Model Genealogy + 15m Diagnostic
          </div>
          <div
            style={{
              fontSize: 11,
              color: 'var(--muted, #8590a8)',
              marginTop: 2,
            }}
          >
            Map of every classifier + LGB version, why 15m is dodgy, and the
            v12-XL-15m retrain spec — companion to PR #456 / hub note #311.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, fontSize: 12 }}>
          <a
            href={SOURCE_PATH}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: 'var(--accent, #5fb3f5)',
              textDecoration: 'none',
              padding: '4px 10px',
              border: '1px solid var(--border, #1f2638)',
              borderRadius: 4,
            }}
          >
            Open in new tab ↗
          </a>
          <a
            href={githubBlobUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: 'var(--accent, #5fb3f5)',
              textDecoration: 'none',
              padding: '4px 10px',
              border: '1px solid var(--border, #1f2638)',
              borderRadius: 4,
            }}
          >
            View on GitHub ↗
          </a>
          <a
            href={retrainSpecUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              color: 'var(--accent, #5fb3f5)',
              textDecoration: 'none',
              padding: '4px 10px',
              border: '1px solid var(--border, #1f2638)',
              borderRadius: 4,
            }}
          >
            v12-XL-15m retrain spec ↗
          </a>
        </div>
      </div>
      <iframe
        src={SOURCE_PATH}
        title="Model Genealogy + 15m Diagnostic"
        style={{
          flex: 1,
          minHeight: 600,
          width: '100%',
          border: 'none',
          background: 'var(--bg, #0a0e1a)',
        }}
      />
    </div>
  );
}
