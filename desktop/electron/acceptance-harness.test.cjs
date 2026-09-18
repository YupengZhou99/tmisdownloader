const test = require('node:test');
const assert = require('node:assert/strict');
const { withDeadline } = require('../scripts/acceptance-harness.cjs');

test('acceptance deadline also bounds a protocol promise that never settles', async () => {
  await assert.rejects(withDeadline(new Promise(() => {}), 25, 'renderer probe'), /renderer probe exceeded 25 ms/);
});

test('acceptance deadline preserves successful results and original errors', async () => {
  assert.equal(await withDeadline(Promise.resolve(42), 1000, 'probe'), 42);
  const failure = new Error('renderer crashed');
  await assert.rejects(withDeadline(Promise.reject(failure), 1000, 'probe'), error => error === failure);
});
