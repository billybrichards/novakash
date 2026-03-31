/**
 * Format a number as USD currency.
 * @param {number} value
 * @returns {string}
 */
export function formatUSD(value) {
  if (value == null) return '—';
  const sign = value < 0 ? '-' : '';
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

/**
 * Format a decimal as percentage.
 * @param {number} value - Decimal (e.g. 0.54 = 54%)
 * @returns {string}
 */
export function formatPercent(value) {
  if (value == null) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * Format an ISO timestamp to a readable local string.
 * @param {string} iso
 * @returns {string}
 */
export function formatTimestamp(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-GB', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/**
 * Format a decimal number to fixed places.
 * @param {number} value
 * @param {number} places
 * @returns {string}
 */
export function formatDecimal(value, places = 4) {
  if (value == null) return '—';
  return value.toFixed(places);
}

/**
 * Merge class names, filtering falsy values.
 * @param  {...string} classes
 * @returns {string}
 */
export function classNames(...classes) {
  return classes.filter(Boolean).join(' ');
}
