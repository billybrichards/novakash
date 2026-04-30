import { describe, it, expect } from 'vitest';
import { inExitWindow, EXIT_WINDOW_OPEN_S, EXIT_WINDOW_CLOSE_S } from '../lib/exitWindow.js';

describe('inExitWindow', () => {
  it('returns false when secondsRemaining is null', () => {
    expect(inExitWindow(null)).toBe(false);
  });

  it('returns false for NaN', () => {
    expect(inExitWindow(NaN)).toBe(false);
  });

  it('returns false when above exit window (T > 48)', () => {
    expect(inExitWindow(49)).toBe(false);
    expect(inExitWindow(100)).toBe(false);
    expect(inExitWindow(300)).toBe(false);
  });

  it('returns false when below exit window (T < 30)', () => {
    expect(inExitWindow(29)).toBe(false);
    expect(inExitWindow(0)).toBe(false);
  });

  it('returns true at lower boundary (T = 30)', () => {
    expect(inExitWindow(EXIT_WINDOW_CLOSE_S)).toBe(true);
  });

  it('returns true at upper boundary (T = 48)', () => {
    expect(inExitWindow(EXIT_WINDOW_OPEN_S)).toBe(true);
  });

  it('returns true inside the window (T = 40)', () => {
    expect(inExitWindow(40)).toBe(true);
  });
});
