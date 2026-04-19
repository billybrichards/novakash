// Fixed-capacity FIFO ring buffer with a cheap external-store subscription
// model so React components can mount via `useSyncExternalStore` without
// re-rendering on every 2s snapshot push.
//
// Keep the buffer OUT of React state — 1800 samples × every-2s = 30/s React
// reconciles otherwise. We notify subscribers on each `push()` and they can
// decide when to re-read (e.g. via a coarse 1s throttled selector).

export class RingBuffer {
  constructor(capacity = 1800) {
    this.capacity = capacity;
    this.items = [];
    this._listeners = new Set();
    this._version = 0;
  }

  push(item) {
    this.items.push(item);
    if (this.items.length > this.capacity) {
      // Drop oldest. Splice rather than shift on arrays this size is fine —
      // shift is O(n) but at 1800 items that's ~microseconds every 2s.
      this.items.splice(0, this.items.length - this.capacity);
    }
    this._version += 1;
    this._listeners.forEach((fn) => {
      try { fn(); } catch (_) { /* swallow */ }
    });
  }

  size() {
    return this.items.length;
  }

  snapshot() {
    // Return the underlying array reference — callers MUST treat it as
    // read-only. Cheap for useSyncExternalStore.
    return this.items;
  }

  getVersion() {
    return this._version;
  }

  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  clear() {
    this.items = [];
    this._version += 1;
    this._listeners.forEach((fn) => {
      try { fn(); } catch (_) { /* swallow */ }
    });
  }
}
