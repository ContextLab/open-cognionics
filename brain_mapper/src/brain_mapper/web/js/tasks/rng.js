// Seeded random numbers for the tasks: mulberry32, one stream per (seed, labels).
//
// `rng(seed, ...labels)` hashes the JSON text of `[seed, ...labels]` (UTF-8 bytes, FNV-1a,
// 32 bits) and starts a mulberry32 generator from that number. The same seed and labels give
// the same numbers in every browser and every run; another label gives another stream, so each
// task and block has its own: `rng(state.seed, "nback", "block1")`. Labels are strings or whole
// numbers. `tests/brain_mapper/test_tasks_browser.py` has the same algorithm in Python and checks
// fixed values.

const ENCODER = new TextEncoder();

/** FNV-1a over the UTF-8 bytes of `text`, as an unsigned 32-bit number. */
function fnv1a(text) {
  let hash = 0x811c9dc5;
  for (const byte of ENCODER.encode(text)) {
    hash ^= byte;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

/** mulberry32: returns a function giving the next number in [0, 1). */
function mulberry32(start) {
  let a = start >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * A stream of random numbers from the session seed and labels.
 * `next()` a number in [0, 1); `int(n)` a whole number in [0, n); `pick(list)` one item;
 * `shuffle(list)` a shuffled copy (Fisher-Yates); the list given is not changed.
 */
export function rng(seed, ...labels) {
  if (!Number.isInteger(seed)) throw new Error("rng needs the session seed, a whole number");
  for (const label of labels) {
    if (typeof label !== "string" && !Number.isInteger(label)) {
      throw new Error("rng labels are strings or whole numbers");
    }
  }
  const next = mulberry32(fnv1a(JSON.stringify([seed, ...labels])));
  const int = (n) => {
    if (!Number.isInteger(n) || n < 1) throw new Error("int(n) needs a whole number n >= 1");
    return Math.floor(next() * n);
  };
  return {
    next,
    int,
    pick: (list) => list[int(list.length)],
    shuffle(list) {
      const out = Array.from(list);
      for (let i = out.length - 1; i > 0; i -= 1) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
}
