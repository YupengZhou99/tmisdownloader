const test = require('node:test');
const assert = require('node:assert/strict');
const { PassThrough } = require('node:stream');
const { EventEmitter } = require('node:events');
const { WorkerBridge, redact } = require('./bridge.cjs');
function fakeChild() {
  return Object.assign(new EventEmitter(), { stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough() });
}
test('responses are correlated, credentials are redacted, and exit rejects pending calls', async () => {
  const child = fakeChild(), bridge = new WorkerBridge(child), events = [];
  bridge.on('event', item => events.push(item));
  const one = bridge.call('snapshot'), two = bridge.call('pause');
  child.stdout.write('{"id":2,"result":"second"}\n{"id":1,"result":{"tasks":[]}}\n');
  assert.equal(await two, 'second');
  assert.deepEqual(await one, { tasks: [] });
  child.stderr.write('https://private.invalid/?token=SECRET cookie=ALSO_SECRET\n');
  assert.ok(!JSON.stringify(events).includes('SECRET'));
  const pending = bridge.call('start');
  child.emit('exit', 1, null);
  await assert.rejects(pending, /退出/);
  await assert.rejects(bridge.call('start'), /未运行/);
  assert.equal(bridge.pending.size, 0);
});
test('invalid messages cannot resolve another request', async () => {
  const child = fakeChild(), bridge = new WorkerBridge(child);
  const waiting = bridge.call('snapshot', {}, 20);
  child.stdout.write('not json\n{"id":44,"result":"wrong"}\n');
  await assert.rejects(waiting, /超时/);
  assert.equal(bridge.pending.size, 0);
  child.emit('exit', 0, null);
});
test('login URLs and common credentials never reach logs', () => {
  assert.ok(!redact('token=abc cookie=xyz authorization=Bearer url=https://host/a?x=5').includes('abc'));
  assert.ok(!redact('https://host/a?token=secret').includes('host'));
});
