// Parse the `model_version` string embedded in a /v4/snapshot timescale
// payload. Spec §5.1.
//
// Format: `<hash>@v<reg>/<asset>/<tf_id>/<hash2>/<date>`
// Example: `a547c3d@v2/btc/btc_5m/a547c3d/2026-04-16T04-48-24Z`

const MODEL_VERSION_RE =
  /^(?<hash>[a-f0-9]+)@v(?<reg>\d+)\/(?<asset>\w+)\/(?<tf_id>[\w_]+)\/(?<hash2>[a-f0-9]+)\/(?<date>[\w\-T:Z]+)$/;

/**
 * Parse a model_version string.
 * @param {string | null | undefined} version
 * @returns {{ hash: string, reg: string, asset: string, tf_id: string, hash2: string, date: string, raw: string } | null}
 *          null if the input is empty or malformed.
 */
export function parseModelVersion(version) {
  if (!version || typeof version !== 'string') return null;
  const m = version.match(MODEL_VERSION_RE);
  if (!m || !m.groups) return null;
  return { ...m.groups, raw: version };
}

/**
 * Render the parsed date fragment (ISO-ish with dashes where `:` normally sit)
 * as a compact UTC display, e.g. `2026-04-16 04:48Z`. Returns the raw string
 * if it can't be parsed.
 */
export function formatModelDate(dateStr) {
  if (!dateStr) return '—';
  // The spec format uses dashes in the time: 2026-04-16T04-48-24Z
  // Convert to a real ISO so Date() parses cleanly.
  const iso = dateStr.replace(
    /^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z$/,
    '$1T$2:$3:$4Z',
  );
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return dateStr;
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}Z`;
}
