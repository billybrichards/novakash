import { describe, it, expect, vi } from 'vitest';
import { RingBuffer } from '../RingBuffer.js';

describe('RingBuffer', () => {
  it('push / size / snapshot work at small capacity', () => {
    const b = new RingBuffer(3);
    expect(b.size()).toBe(0);
    b.push('a'); b.push('b'); b.push('c');
    expect(b.size()).toBe(3);
    expect(b.snapshot()).toEqual(['a', 'b', 'c']);
  });

  it('evicts oldest when over capacity', () => {
    const b = new RingBuffer(3);
    b.push(1); b.push(2); b.push(3); b.push(4); b.push(5);
    expect(b.size()).toBe(3);
    expect(b.snapshot()).toEqual([3, 4, 5]);
  });

  it('enforces cap 1800 (production size)', () => {
    const b = new RingBuffer(1800);
    for (let i = 0; i < 2500; i++) b.push(i);
    expect(b.size()).toBe(1800);
    // Newest in, oldest out: snapshot should start at 700.
    expect(b.snapshot()[0]).toBe(700);
    expect(b.snapshot()[1799]).toBe(2499);
  });

  it('notifies subscribers on each push', () => {
    const b = new RingBuffer(10);
    const fn = vi.fn();
    const unsub = b.subscribe(fn);
    b.push('x'); b.push('y');
    expect(fn).toHaveBeenCalledTimes(2);
    unsub();
    b.push('z');
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('version increments monotonically', () => {
    const b = new RingBuffer(10);
    const v0 = b.getVersion();
    b.push('a');
    expect(b.getVersion()).toBe(v0 + 1);
    b.push('b');
    expect(b.getVersion()).toBe(v0 + 2);
  });

  it('clear empties the buffer and notifies', () => {
    const b = new RingBuffer(10);
    const fn = vi.fn();
    b.subscribe(fn);
    b.push(1); b.push(2);
    expect(b.size()).toBe(2);
    b.clear();
    expect(b.size()).toBe(0);
    expect(fn).toHaveBeenCalledTimes(3); // 2 pushes + 1 clear
  });
});
