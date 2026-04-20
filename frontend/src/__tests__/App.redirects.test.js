/**
 * Audit test for App.jsx Navigate redirect rules.
 *
 * Policy:
 *   KEEP  — pages that may appear in Telegram alerts or saved bookmarks
 *   REMOVE — internal-only dev pages that were never externally linked
 *
 * This test reads App.jsx as text and checks for the presence/absence of
 * specific route path strings, avoiding the need for a full React render stack.
 */
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { describe, it, expect } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const appSource = readFileSync(resolve(__dirname, '../App.jsx'), 'utf8');

// Helper: does App.jsx declare a <Navigate> route for this path?
function hasRedirect(fromPath) {
  // Match pattern: path="/foo" ... <Navigate
  // The path attr and Navigate element appear on the same <Route ... /> line.
  const escaped = fromPath.replace(/\//g, '\\/').replace(/\./g, '\\.');
  const re = new RegExp(`path="${escaped}"[^>]*<Navigate`);
  return re.test(appSource);
}

describe('App.jsx redirect audit — externally-linked URLs (KEEP)', () => {
  const kept = [
    ['/dashboard',                    'main dashboard — most-bookmarked URL'],
    ['/live',                         'live trading — likely in Telegram trade alerts'],
    ['/paper',                        'paper dashboard — operational bookmark'],
    ['/positions',                    'positions page — operational bookmark'],
    ['/risk',                         'risk page — operational bookmark'],
    ['/execution-hq',                 'execution monitoring — operational bookmark'],
    ['/windows',                      'window results — operational'],
    ['/v58',                          'v58 monitor — referenced in operational flow'],
    ['/strategy',                     'strategy analysis — user bookmark'],
    ['/timesfm',                      'ML forecast monitor — may appear in alerts'],
    ['/telegram',                     'telegram config — may be bookmarked'],
    ['/trading-config',               'redirects to active /config Tier-1 page'],
    ['/polymarket',                   'main polymarket entry — commonly bookmarked'],
    ['/polymarket/monitor',           'polymarket monitor — operational'],
    ['/polymarket/overview',          'polymarket overview — operational'],
    ['/polymarket/floor',             'polymarket floor — operational'],
    ['/polymarket/strategy-history',  'strategy history — trade result bookmarks'],
    ['/polymarket/strategies',        'strategy configs — operational'],
    ['/polymarket/gate-monitor',      'gate pipeline monitor — may have Telegram links'],
    ['/polymarket/command',           'strategy command — operational'],
  ];

  for (const [path, reason] of kept) {
    it(`keeps redirect for ${path} (${reason})`, () => {
      expect(hasRedirect(path)).toBe(true);
    });
  }
});

describe('App.jsx redirect audit — internal-only dev pages (REMOVED)', () => {
  const removed = [
    ['/playwright',               'Playwright test dashboard — dev tool only'],
    ['/factory',                  'Factory Floor — internal signal monitoring'],
    ['/composite',                'Composite Signals — internal dev tool'],
    ['/margin',                   'Margin Engine — internal dev subsystem'],
    ['/margin-strategies',        'Margin Strategies — internal dev'],
    ['/legacy-config',            'Legacy Config — superseded internal config'],
    ['/setup',                    'Setup — one-time bootstrap, dev only'],
    ['/notes',                    'Notes — developer notes, internal'],
    ['/schema',                   'Schema — DB schema inspector, dev tool'],
    ['/deployments',              'Deployments — CI/CD surface, dev only'],
    ['/signal-comparison',        'Signal Comparison — dev comparison tool'],
    ['/ops',                      'Agent Ops — internal dev tool'],
    ['/polymarket/evaluate',      'Polymarket Evaluate — internal analysis tool'],
    ['/polymarket/strategy-lab',  'Strategy Lab — experimental/dev tool'],
    ['/polymarket/data-health',   'Data Health — internal data monitoring'],
    ['/polymarket/down-only',     'DOWN Strategy Floor — internal strategy floor'],
    ['/polymarket/up-asian',      'UP Strategy Floor — internal strategy floor'],
    ['/polymarket/15min',         '15-min monitor — internal monitoring tool'],
    ['/data/v1',                  'Data Surface V1 — raw data inspector, dev only'],
    ['/data/v2',                  'Data Surface V2 — raw data inspector, dev only'],
    ['/data/v3',                  'Data Surface V3 — raw data inspector, dev only'],
    ['/data/v4',                  'Data Surface V4 — raw data inspector, dev only'],
    ['/data/assembler1',          'Assembler1 — pipeline assembler, dev only'],
  ];

  for (const [path, reason] of removed) {
    it(`has no redirect for ${path} (${reason})`, () => {
      expect(hasRedirect(path)).toBe(false);
    });
  }
});
