// Fixed-capacity ring buffer: O(1) push, oldest entries are dropped (and counted). Pure.

export class RingBuffer {
  constructor(capacity = 1000) {
    if (!Number.isInteger(capacity) || capacity < 1) throw new RangeError('capacity must be a positive integer');
    this.capacity = capacity;
    this.items = new Array(capacity);
    this.start = 0;
    this.length = 0;
    this.dropped = 0;
    this.total = 0;
  }

  push(item) {
    this.total++;
    if (this.length < this.capacity) {
      this.items[(this.start + this.length) % this.capacity] = item;
      this.length++;
    } else {
      this.items[this.start] = item;
      this.start = (this.start + 1) % this.capacity;
      this.dropped++;
    }
    return this;
  }

  pushAll(list) {
    for (const x of list) this.push(x);
    return this;
  }

  get size() {
    return this.length;
  }

  at(i) {
    if (i < 0) i += this.length;
    if (i < 0 || i >= this.length) return undefined;
    return this.items[(this.start + i) % this.capacity];
  }

  last() {
    return this.at(-1);
  }

  first() {
    return this.at(0);
  }

  /** Oldest -> newest. */
  toArray() {
    const out = new Array(this.length);
    for (let i = 0; i < this.length; i++) out[i] = this.items[(this.start + i) % this.capacity];
    return out;
  }

  *[Symbol.iterator]() {
    for (let i = 0; i < this.length; i++) yield this.items[(this.start + i) % this.capacity];
  }

  filter(fn) {
    const out = [];
    for (const x of this) if (fn(x)) out.push(x);
    return out;
  }

  find(fn) {
    for (const x of this) if (fn(x)) return x;
    return undefined;
  }

  clear() {
    this.items = new Array(this.capacity);
    this.start = 0;
    this.length = 0;
    this.dropped = 0;
  }
}
