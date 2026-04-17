/**
 * Data integrity tests for navigation.js archived pages.
 * Run with: node src/nav/navigation.test.mjs
 */
import { ARCHIVED_PAGES, ARCHIVED_STRATEGY_FLOORS } from './navigation.js';

const VALID_CATEGORIES = new Set(['trading', 'polymarket', 'data', 'ops']);
let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (condition) {
    console.log(`  ✓ ${message}`);
    passed++;
  } else {
    console.error(`  ✗ ${message}`);
    failed++;
  }
}

console.log('\nnavigation.js — archive data integrity\n');

// ── Category field: all entries must have a valid category ──────────────────
console.log('Category fields:');
const allEntries = [...ARCHIVED_PAGES, ...ARCHIVED_STRATEGY_FLOORS];

for (const entry of allEntries) {
  assert(
    VALID_CATEGORIES.has(entry.category),
    `${entry.path} — category is "${entry.category}" (valid: ${[...VALID_CATEGORIES].join(' | ')})`,
  );
}

// ── No "other" or "margin" or "system" categories (old names removed) ───────
console.log('\nNo stale category names:');
const staleCategories = allEntries.filter(e =>
  ['other', 'margin', 'system'].includes(e.category),
);
assert(staleCategories.length === 0, `No entries use old category names (other/margin/system); found: ${staleCategories.map(e => e.path).join(', ') || 'none'}`);

// ── Setup page removed ───────────────────────────────────────────────────────
console.log('\nRemoved pages:');
assert(
  !ARCHIVED_PAGES.some(p => p.path === '/archive/setup'),
  '/archive/setup (one-time bootstrap) is not in ARCHIVED_PAGES',
);

// ── replacedBy: null entries must have a non-empty note ─────────────────────
console.log('\nNull replacedBy entries have notes:');
const nullWithoutNote = ARCHIVED_PAGES.filter(
  p => p.replacedBy === null && (!p.note || p.note.trim() === ''),
);
assert(
  nullWithoutNote.length === 0,
  `All replacedBy:null entries have a note; missing: ${nullWithoutNote.map(e => e.path).join(', ') || 'none'}`,
);

// ── Data surface v1-v3 now have replacedBy pointers ─────────────────────────
console.log('\nData surface v1-v3 replacedBy pointers:');
for (const v of ['/archive/data/v1', '/archive/data/v2', '/archive/data/v3']) {
  const entry = ARCHIVED_PAGES.find(p => p.path === v);
  assert(entry && entry.replacedBy !== null, `${v} has a replacedBy pointer`);
}

// ── No duplicate paths ───────────────────────────────────────────────────────
console.log('\nNo duplicate paths:');
const paths = allEntries.map(e => e.path);
const dupes = paths.filter((p, i) => paths.indexOf(p) !== i);
assert(dupes.length === 0, `No duplicate archive paths; dupes: ${dupes.join(', ') || 'none'}`);

// ── Required fields present on all entries ───────────────────────────────────
console.log('\nRequired fields:');
const missingFields = ARCHIVED_PAGES.filter(
  p => !p.path || !p.label || !p.importName || !p.category,
);
assert(missingFields.length === 0, `All ARCHIVED_PAGES have path, label, importName, category`);

// ── Strategy floors have category field ─────────────────────────────────────
const floorsWithoutCategory = ARCHIVED_STRATEGY_FLOORS.filter(f => !f.category);
assert(floorsWithoutCategory.length === 0, `All ARCHIVED_STRATEGY_FLOORS have category field`);

// ── Summary ──────────────────────────────────────────────────────────────────
console.log(`\n${'─'.repeat(50)}`);
console.log(`Total: ${passed + failed}  Passed: ${passed}  Failed: ${failed}`);
if (failed > 0) {
  console.error('\nSome assertions failed.');
  process.exit(1);
} else {
  console.log('\nAll assertions passed.');
}
