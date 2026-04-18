#!/usr/bin/env node
// Emits frontend/public/release-notes.json from the last N merge commits
// on the current branch. The FE release-notes panel loads this at runtime.
//
// Runs in `prebuild` so every `npm run build` ships a fresh file. If git
// isn't available (unlikely in prod CI but possible in exotic sandboxes)
// we write an empty array rather than fail the build.
//
// Uses execFileSync with an argv array — no shell, no injection surface.

import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const LIMIT = 30;
const REPO_URL = 'https://github.com/billybrichards/novakash';

const __dirname = dirname(fileURLToPath(import.meta.url));
const outPath = resolve(__dirname, '..', 'public', 'release-notes.json');

function parseLine(line) {
  // Expected format: <sha>|<iso-date>|<subject>
  const [sha, date, ...rest] = line.split('|');
  const subject = rest.join('|');
  const prMatch = subject.match(/\(#(\d+)\)\s*$/);
  const prNumber = prMatch ? Number(prMatch[1]) : null;
  const title = subject.replace(/\s*\(#\d+\)\s*$/, '');
  return {
    sha: sha.slice(0, 7),
    date: date.slice(0, 10),
    title,
    prNumber,
    prUrl: prNumber ? `${REPO_URL}/pull/${prNumber}` : null,
    commitUrl: `${REPO_URL}/commit/${sha}`,
  };
}

function main() {
  let entries = [];
  try {
    // --first-parent keeps only the merge line on develop, skipping the
    // per-branch squashed internals. Works whether PRs land as squash
    // merges (single commit with "(#N)" suffix) or merge commits.
    const out = execFileSync(
      'git',
      ['log', '--first-parent', `-n`, String(LIMIT), '--pretty=format:%H|%cI|%s'],
      { encoding: 'utf8' },
    ).trim();
    entries = out
      .split('\n')
      .filter(Boolean)
      .map(parseLine)
      // Only keep rows that look like a PR squash-merge or merge commit.
      // Plain non-PR commits on develop (rare) are excluded to avoid
      // noise in the panel.
      .filter((e) => e.prNumber !== null);
  } catch (err) {
    console.warn('[gen-release-notes] git read failed — writing empty list', err.message);
  }

  const payload = {
    generated_at: new Date().toISOString(),
    count: entries.length,
    entries,
  };

  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(payload, null, 2));
  console.log(`[gen-release-notes] wrote ${entries.length} entries → ${outPath}`);
}

main();
