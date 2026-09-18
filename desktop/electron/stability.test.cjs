const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { BoundedPublisher, pressureCapacity, diagnosticLog, systemResources, prepareRuntime, cleanupRuntime } = require('./stability.cjs');
test('Linux temp files use a private disk directory; cleanup rejects a symlink', t => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-runtime-test-'));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const env = { TMIS_RUNTIME_DIR: path.join(base, 'runtime') }, settings = {};
  const app = { setPath: (key, value) => { settings[key] = value; }, commandLine: { appendSwitch: key => { settings[key] = true; } } };
  const directory = prepareRuntime(app, env, 'linux');
  assert.equal(env.TMPDIR, directory); assert.equal(settings.temp, directory);
  assert.ok(settings['disable-dev-shm-usage']);
  assert.equal(fs.statSync(directory).mode & 0o777, 0o700);
  const outside = path.join(base, 'keep'); fs.mkdirSync(outside); fs.writeFileSync(path.join(outside, 'report.xlsx'), 'user file');
  const link = path.join(base, 'run-link'); fs.symlinkSync(outside, link);
  cleanupRuntime(link); assert.ok(fs.existsSync(path.join(outside, 'report.xlsx')));
  cleanupRuntime(directory); assert.ok(!fs.existsSync(directory));
});
test('unresponsive renderer retains one inflight message and only latest state plus 100 logs', () => {
  const sent = [], win = { isDestroyed: () => false, isVisible: () => true, webContents: { send: (_topic, packet) => sent.push(packet) } };
  const channel = new BoundedPublisher(win, { delay: 10000 });
  channel.push({ event: 'snapshot', workspaceId: 'a', data: 0 }); channel.flush();
  for (let i = 0; i < 10000; i++) {
    channel.push({ event: 'snapshot', workspaceId: 'a', data: i });
    channel.push({ event: 'log', text: String(i) });
  }
  assert.equal(sent.length, 1); assert.equal(channel.pending.size, 1); assert.equal(channel.logs.length, 100);
  channel.ack(sent[0].seq); channel.flush();
  assert.equal(sent[1].items[0].data, 9999);
  assert.equal(sent[1].items.length, 101); channel.reset();
});
test('hidden windows receive nothing; floating window never receives full task or log arrays', () => {
  const sent = [], win = { isDestroyed: () => false, isVisible: () => false, webContents: { send: (_, value) => sent.push(value) } };
  const c = new BoundedPublisher(win, { compact: true, delay: 10000 });
  c.push({ event: 'log', text: 'hidden' }); assert.equal(c.logs.length, 0);
  win.isVisible = () => true;
  c.push({ event: 'workspaces', data: { workspaces: [{ logs: [1], state: { tasks: [1], batches: [1], counts: { pending: 8 } } }] } });
  c.flush(); const w = sent[0].items[0].data.workspaces[0];
  assert.deepEqual(w.logs, []); assert.deepEqual(w.state.tasks, []); assert.equal(w.state.counts.pending, 8); c.reset();
});
test('memory, shared memory and disk pressure limit dispatch without destructive cleanup', () => {
  const gb = 1024**3, s = { available: 8 * gb, disk: { free: 10 * gb }, shm: { size: gb, used: 0, free_inodes: 100 } };
  assert.equal(pressureCapacity(s), 4);
  assert.equal(pressureCapacity({ ...s, available: 2.5 * gb }), 2);
  assert.equal(pressureCapacity({ ...s, available: 1.5 * gb }), 1);
  assert.equal(pressureCapacity({ ...s, available: .5 * gb }), 0);
  assert.equal(pressureCapacity({ ...s, shm: { ...s.shm, used: .95 * gb } }), 0);
  assert.equal(pressureCapacity({ ...s, shm: { ...s.shm, free_inodes: 0 } }), 0);
  assert.equal(pressureCapacity({ ...s, disk: { free: 1 } }), 0);
});
test('diagnostics redact URLs and are rotated; unavailable system memory is not treated as zero', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-log-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const log = diagnosticLog(dir);
  log('error', { message: 'https://example.test/?token=PRIVATE' });
  assert.ok(!fs.readFileSync(path.join(dir, 'stability.jsonl'), 'utf8').includes('PRIVATE'));
  fs.appendFileSync(path.join(dir, 'stability.jsonl'), 'x'.repeat(2 * 1024**2)); log('rotated');
  assert.ok(fs.existsSync(path.join(dir, 'stability.jsonl.1')));
  assert.equal(systemResources(dir, path.join(dir, 'missing')).available, null);
});
