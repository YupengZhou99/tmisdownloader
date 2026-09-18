const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { WorkspaceManager, stateDirectory, safeName } = require('./workspaces.cjs');
class FakeBridge extends EventEmitter {
  constructor(id) {
    super(); this.id = id; this.calls = []; this.closed = false;
    this.child = new EventEmitter(); this.child.pid = 100;
    this.child.stdin = { end: () => this.exit() }; this.child.kill = () => this.exit();
    this.state = { tasks: [], batches: [], counts: {}, current: null, mode: 'idle', pending_headless: null, session_ready: false, state_dir: id };
  }
  exit() { this.closed = true; this.emit('event', { event: 'offline', message: 'closed' }); this.child.emit('exit', 0); }
  async call(command, data = {}) {
    this.calls.push({ command, data });
    if (command === 'snapshot') return structuredClone(this.state);
    if (command === 'has_fingerprints') return this.state.batches.some(b => data.fingerprints.includes(b.fingerprint));
    if (command === 'fingerprints') return this.state.batches.map(b => b.fingerprint);
    if (command === 'login') { if (this.gate) await this.gate; this.state.session_ready = !data.url.includes('invalid'); return; }
    if (command === 'start') this.state.mode = 'running';
    if (command === 'pause') this.state.mode = 'paused';
    if (command === 'disconnect') { this.state.session_ready = false; this.state.mode = 'waiting_login'; }
    if (command === 'preview') return data.paths.map(p => ({ id: this.id + p, fingerprint: p, name: p, count: 1 }));
    if (command === 'import_preview') {
      const accepted = data.ids.map(id => {
        const fingerprint = id.slice(this.id.length);
        this.state.batches.push({ id, fingerprint, output_dir: data.root });
        this.state.tasks.push({ id, output_dir: data.root, status: 'pending' });
        return { id, name: fingerprint, count: 1 };
      });
      return { accepted, errors: [] };
    }
  }
}
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-workspace-unit-'));
  const created = [];
  const createBridge = (id, statePath, legacy) => { const b = new FakeBridge(id); created.push({ id, statePath, legacy, b }); return b; };
  const manager = new WorkspaceManager({ directory, createBridge });
  t.after(async () => { await manager.shutdown(); fs.rmSync(directory, { recursive: true, force: true }); });
  return { manager, directory, created, createBridge };
}
async function ready(manager, ids) { for (const id of ids) await manager.login(id, { url: 'https://fixture.invalid/?token=RAM_ONLY' }); }

test('legacy state directory matches Python on Linux and macOS; unsafe labels rejected', () => {
  assert.equal(stateDirectory({}, 'linux', '/users/a'), '/users/a/.local/state/tmis-dler');
  assert.equal(stateDirectory({}, 'darwin', '/users/a'), '/users/a/Library/Application Support/TMISDLer');
  assert.equal(stateDirectory({ TMIS_STATE_DIR: '/tmp/private' }), '/tmp/private');
  for (const name of ['', 'https://host/?token=secret', 'a\nname', 'token=private']) assert.throws(() => safeName(name));
});
test('four workspaces have isolated state paths and a fifth is rejected', async t => {
  const { manager, directory, created } = fixture(t);
  const ids = ['default', manager.create('收入'), manager.create('支出'), manager.create('库存')];
  assert.throws(() => manager.create('第五个'), /4/);
  await manager.initialize();
  assert.equal(new Set(created.map(c => c.statePath)).size, 4);
  assert.equal(created[0].statePath, directory); assert.equal(created[0].legacy, true);
  assert.ok(created.slice(1).every(c => c.statePath.startsWith(path.join(directory, 'workspaces')) && !c.legacy));
  assert.deepEqual(manager.active().map(e => e.meta.id), ids);
});
test('one successful login unlocks only that queue without waiting for other workspaces', async t => {
  const { manager } = fixture(t), ids = ['default', manager.create('收入')];
  await manager.login('default', { url: 'https://fixture.test/ok' });
  assert.equal((await manager.call('default', 'preview', { paths: ['A'] })).length, 1);
  await manager.confirm(['default']);
  await manager.call('default', 'start');
  assert.equal(manager.get('default').state.mode, 'running');
  await assert.rejects(() => manager.call(ids[1], 'start'), /登录此/);
  await assert.rejects(() => manager.confirm(ids), /登录成功/);
  await manager.login(ids[1], { url: 'https://fixture.test/ok' });
  await manager.confirm(ids);
  assert.ok(manager.active().every(e => e.confirmed));
  const added = manager.create('新增');
  assert.ok(manager.get('default').confirmed); assert.equal(manager.get(added).confirmed, false);
  assert.equal((await manager.call('default', 'preview', { paths: ['A'] })).length, 1);
});
test('headless login preference is persisted without URLs; log events do not broadcast full snapshots', async t => {
  const { manager, created, directory } = fixture(t);
  await manager.login('default', { url: 'https://fixture.test/?token=PRIVATE', headless: true });
  assert.equal(created[0].b.calls.find(c => c.command === 'login').data.headless, true);
  const saved = fs.readFileSync(path.join(directory, 'workspaces.json'), 'utf8');
  assert.ok(saved.includes('"headless": true')); assert.ok(!saved.includes('PRIVATE'));
  let changes = 0; manager.on('change', () => changes++);
  for (let i = 0; i < 1000; i++) created[0].b.emit('event', { event: 'log', text: 'bounded', level: 'INFO' });
  assert.equal(changes, 0); assert.equal(manager.get('default').logs.length, 300);
});
test('login calls run concurrently and preserve per-workspace labels without credentials', async t => {
  const { manager, created, directory } = fixture(t), ids = ['default', manager.create('二号')];
  await manager.initialize(); let release;
  created[0].b.gate = new Promise(r => { release = r; });
  const first = manager.login(ids[0], { url: 'https://host/?token=RAM_ONLY' });
  await manager.login(ids[1], { url: 'https://host/?token=RAM_TWO' });
  assert.equal(manager.get(ids[1]).state.session_ready, true);
  assert.equal(manager.get(ids[0]).busy, 'login'); release(); await first;
  assert.ok(!fs.readFileSync(path.join(directory, 'workspaces.json'), 'utf8').includes('RAM_'));
});
test('commands and events remain tagged; a worker crash cannot mark another offline', async t => {
  const { manager, created } = fixture(t), id = manager.create('二号');
  await manager.initialize(); const events = []; manager.on('event', e => events.push(e));
  created[1].b.emit('event', { event: 'log', text: 'second', level: 'INFO' });
  assert.equal(events.at(-1).workspaceId, id);
  assert.equal(manager.get('default').logs.length, 0);
  created[1].b.exit();
  assert.equal(manager.get(id).online, false); assert.equal(manager.get('default').online, true);
  await assert.rejects(() => manager.call(id, 'pause'), /后台已停止/);
  await manager.call('default', 'pause');
  assert.equal(manager.get('default').state.mode, 'paused');
});
test('worker exit preserves aggregate counts beyond the first page', async t => {
  const { manager, created } = fixture(t);
  await manager.initialize();
  manager.get('default').state.counts = { succeeded: 180, pending: 200, running: 1 };
  manager.get('default').state.tasks = [{ id: 'first-page', status: 'succeeded' }];
  created[0].b.exit();
  assert.deepEqual(manager.get('default').state.counts, { succeeded: 180, pending: 200, interrupted: 1 });
});
test('imports cannot use another workspace preview; scoped output roots never cross', async t => {
  const { manager, directory } = fixture(t), id = manager.create('二号');
  await manager.login('default', { url: 'https://host/ok' }); await manager.login(id, { url: 'https://host/ok' });
  await manager.confirm(['default', id]);
  const a = await manager.call('default', 'preview', { paths: ['A'] });
  const b = await manager.call(id, 'preview', { paths: ['B'] });
  await assert.rejects(() => manager.call(id, 'import_preview', { ids: [a[0].id], root: directory }), /不属于/);
  await Promise.all([manager.call('default', 'import_preview', { ids: [a[0].id], root: directory }), manager.call(id, 'import_preview', { ids: [b[0].id], root: directory })]);
  assert.notEqual(manager.get(id).state.tasks[0].output_dir, manager.get('default').state.tasks[0].output_dir);
  assert.equal(manager.get(id).meta.root, directory);
  const original = manager.get(id).state.tasks[0].output_dir;
  manager.update(id, { root: path.join(directory, 'later'), name: '改名' });
  assert.equal(manager.get(id).state.tasks[0].output_dir, original);
});
test('simultaneous duplicate imports require confirmation in the second workspace', async t => {
  const { manager, directory } = fixture(t), id = manager.create('二号');
  await manager.login('default', { url: 'https://host/ok' }); await manager.login(id, { url: 'https://host/ok' }); await manager.confirm(['default', id]);
  const a = await manager.call('default', 'preview', { paths: ['same'] }); const b = await manager.call(id, 'preview', { paths: ['same'] });
  const results = await Promise.allSettled([manager.call('default', 'import_preview', { ids: [a[0].id], root: directory }), manager.call(id, 'import_preview', { ids: [b[0].id], root: directory })]);
  assert.deepEqual(results.map(r => r.status), ['fulfilled', 'rejected']);
  await manager.call(id, 'import_preview', { ids: [b[0].id], root: directory, allow_duplicate: true });
  const again = await manager.call(id, 'preview', { paths: ['same'] });
  assert.deepEqual(again[0].duplicate_workspaces, ['默认工作区']);
});
test('single pause, global start, disconnect and confirm affect the intended queues', async t => {
  const { manager } = fixture(t), id = manager.create('二号');
  await manager.login('default', { url: 'https://host/ok' }); await manager.login(id, { url: 'https://host/ok' }); await manager.confirm(['default', id]);
  assert.ok((await manager.all('start')).every(r => r.ok));
  await manager.call('default', 'pause');
  assert.equal(manager.get(id).state.mode, 'running');
  await manager.call('default', 'disconnect');
  assert.equal(manager.get('default').confirmed, false); assert.equal(manager.get(id).confirmed, true);
  assert.deepEqual((await manager.all('start')).map(r => r.ok), [false, true]);
});
test('archiving preserves metadata and state files; restart requires fresh login confirmation', async t => {
  const { manager, directory, createBridge } = fixture(t);
  await manager.initialize(); manager.update('default', { name: '旧队列', root: directory });
  fs.writeFileSync(path.join(directory, 'keep.txt'), 'history');
  await manager.archive('default'); assert.equal(manager.active().length, 0);
  assert.equal(fs.readFileSync(path.join(directory, 'keep.txt'), 'utf8'), 'history');
  manager.restore('default'); await manager.ensure('default');
  const reloaded = new WorkspaceManager({ directory, createBridge });
  assert.equal(reloaded.get('default').meta.name, '旧队列');
  assert.equal(reloaded.get('default').meta.root, directory);
  assert.equal(reloaded.get('default').confirmed, false);
  await reloaded.shutdown();
});
