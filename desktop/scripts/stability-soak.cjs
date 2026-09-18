// Isolated four-workspace endurance test of the actual packaged application.
// Only the local fixture server is contacted. Does not read a production queue.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn, spawnSync } = require('node:child_process');
const { createInterface } = require('node:readline');
const { withDeadline, captureProcessOutput, collectDiagnostics, closeApplication } = require('./acceptance-harness.cjs');
const root = path.resolve(__dirname, '../..');
const python = process.env.TMIS_PYTHON || path.join(root, '.venv/bin/python');
const { _electron } = require(process.env.TMIS_PLAYWRIGHT_NODE || path.join(root, '.venv/lib/python3.9/site-packages/playwright/driver/package'));
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-soak-'));
const artifacts = path.join(root, 'output/playwright'); fs.mkdirSync(artifacts, { recursive: true });
const duration = Number(process.env.TMIS_SOAK_SECONDS || 14400) * 1000;
const minimum = Number(process.env.TMIS_SOAK_DOWNLOADS || 1000);
const perBatch = Math.max(30, Math.ceil(minimum / 4));
const server = spawn(python, [path.join(root, 'tests/workspace_fixture_server.py'), temporary], {
  cwd: root, env: { ...process.env, TMIS_FIXTURE_TASKS: String(perBatch), TMIS_FIXTURE_LARGE: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
let application, main;
const samples = [], faults = [], sleep = ms => new Promise(r => setTimeout(r, ms));
async function hub(action, data = {}) { return withDeadline(main.evaluate(({ action, data }) => window.tmis.workspaces(action, data), { action, data }), action === 'snapshot' ? 15000 : 150000, 'soak workspaces ' + action); }
async function call(id, command, data = {}) { return withDeadline(main.evaluate(({ id, command, data }) => window.tmis.call(command, data, id), { id, command, data }), 90000, 'soak ' + command); }
async function importBatch(w, file, directory) {
  await application.evaluate(({ dialog }, file) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [file] }); }, file);
  const paths = await main.evaluate(id => window.tmis.files(id), w.id);
  const previews = await call(w.id, 'preview', { paths, options: { start_date: '', end_date: '', naming_mode: 'param', postprocess: false } });
  assert.ok(previews.length === 1 && previews[0].id, JSON.stringify(previews));
  await application.evaluate(({ dialog }, directory) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [directory] }); }, directory);
  const output = await main.evaluate(id => window.tmis.directory(id), w.id);
  await call(w.id, 'import_preview', { ids: [previews[0].id], root: output, allow_duplicate: true });
}
function percentile(list, ratio) { const values = [...list].sort((a,b) => a-b); return values[Math.floor((values.length - 1) * ratio)] || 0; }
async function run() {
  const config = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture timeout')), 60000);
    createInterface({ input: server.stdout }).once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
    server.stderr.on('data', data => process.stderr.write(data)); server.once('exit', code => reject(new Error('fixture exit ' + code)));
  });
  const packaged = process.env.TMIS_DESKTOP_EXECUTABLE;
  const args = packaged ? [] : [path.join(root, 'desktop')];
  if (process.env.TMIS_CI_ROOT === '1') args.push('--no-sandbox');
  application = await _electron.launch({ executablePath: packaged || process.env.TMIS_ELECTRON_PATH || require('electron'), args, timeout: 60000,
    env: { ...process.env, TMIS_STATE_DIR: path.join(temporary, 'state'), TMIS_DESKTOP_DATA_DIR: path.join(temporary, 'desktop'),
      TMIS_RUNTIME_DIR: path.join(temporary, 'runtime'), TMIS_PYTHON: python } });
  captureProcessOutput(application, artifacts, 'stability-soak');
  application.context().setDefaultTimeout(30000);
  await application.firstWindow();
  for (let i=0; i<200; i++) {
    main = application.windows().find(p => p.url().includes('index.html') && !p.url().includes('view=compact'));
    if (main) break; await sleep(100);
  }
  assert.ok(main); main.on('pageerror', error => faults.push(error.message));
  await main.getByRole('heading', { name: '每一路，各司其职。', exact: true }).waitFor();
  for (const name of ['长期收入', '长期支出', '长期库存']) await hub('create', { name });
  const ws = (await hub('snapshot')).workspaces;
  for (let i=0; i<4; i++) {
    if (process.env.TMIS_TEST_BROWSER_PATH) await hub('update', { id: ws[i].id, settings: { browserPath: process.env.TMIS_TEST_BROWSER_PATH } });
    const result = await hub('login_all', { entries: [{ id: ws[i].id, url: config.sources[i].url, headless: true }] });
    assert.ok(result[0].ok, JSON.stringify(result));
    await importBatch(ws[i], config.sources[i].source, config.directory);
  }
  const started = Date.now(); let lastReport = 0, lastProgress = Date.now(), lastDone = 0;
  await hub('start_all');
  while (true) {
    const state = await hub('snapshot');
    const done = state.workspaces.reduce((n,w) => n + (w.state.counts.succeeded || 0), 0);
    for (const w of state.workspaces) {
      assert.ok(w.online, w.error || 'worker offline');
      assert.equal((w.state.counts.failed || 0) + (w.state.counts.timed_out || 0) + (w.state.counts.interrupted || 0), 0, w.name + ': ' + JSON.stringify(w.state.counts));
      assert.notEqual(w.state.mode, 'waiting_login', 'fixture session must survive recycling');
    }
    const elapsed = Date.now() - started;
    if (done > lastDone) { lastProgress = Date.now(); lastDone = done; }
    assert.ok(Date.now() - lastProgress < 600000, 'no download progress for ten minutes');
    const sample = await withDeadline(application.evaluate(({ app }) => ({
      electron: app.getAppMetrics().reduce((n,p)=>n+(p.memory?.workingSetSize || 0)*1024,0)
    })), 15000, 'soak memory sample');
    sample.shm = null;
    try { const s=fs.statfsSync('/dev/shm'); sample.shm=(s.blocks-s.bfree)*s.bsize; } catch {}
    sample.elapsed = elapsed; sample.done = done;
    sample.browsers = state.workspaces.map(w => w.state.resources?.browser_tree_rss || 0);
    samples.push(sample);
    if (elapsed - lastReport > 60000) {
      lastReport = elapsed;
      console.log(JSON.stringify({ minutes: Math.floor(elapsed / 60000), downloads: done, ...sample, elapsed: undefined }));
      fs.writeFileSync(path.join(artifacts, 'stability-soak-progress.json'), JSON.stringify({ status: 'running', elapsed_seconds: elapsed / 1000, downloads: done, samples }));
    }
    if (elapsed >= duration && done >= minimum) break;
    for (let i=0; i<4; i++) if (!state.workspaces[i].state.counts.pending && !state.workspaces[i].state.current) {
      await importBatch(ws[i], config.sources[i].source, config.directory); await call(ws[i].id, 'start');
    }
    await sleep(5000);
  }
  await hub('pause_all');
  for (let i=0; i<120 && (await hub('snapshot')).workspaces.some(w=>w.state.current); i++) await sleep(1000);
  const final = await hub('snapshot'); assert.ok(final.workspaces.every(w=>!w.state.current));
  const warm = samples.filter(s => s.elapsed >= Math.min(600000, duration / 4));
  const portion = Math.max(1, Math.floor(warm.length / 4));
  const early = warm.slice(0, portion), late = warm.slice(-portion);
  const growth = key => percentile(late.map(s=>s[key] || 0), .5) - percentile(early.map(s=>s[key] || 0), .5);
  if (duration >= 1800000) {
    assert.ok(growth('electron') < 192 * 1024**2, 'Electron retained memory grew by more than 192 MiB after warmup');
    assert.ok(growth('shm') < 32 * 1024**2, '/dev/shm retained growth exceeds 32 MiB');
    for (let i=0;i<4;i++) assert.ok(percentile(late.map(s=>s.browsers[i]), .9) - percentile(early.map(s=>s.browsers[i]), .9) < 256 * 1024**2, 'browser retained growth in workspace ' + i);
  }
  // Clearing completed entries must preserve saved files and audit histories.
  const exports = [];
  for (const [i, w] of final.workspaces.entries()) {
    const page = await call(w.id, 'tasks', { limit: 200 });
    const saved = page.tasks.filter(t=>t.status==='succeeded'); assert.ok(saved.length);
    for (const task of saved) assert.ok(fs.statSync(task.saved_path).size > 0);
    exports.push(...[saved[0], saved.at(-1)].map(t => ({ path: t.saved_path, expected: [config.sources[i].slot,
      config.sources[i].treasury, config.sources[i].start, config.sources[i].end, '0 -- 全部', config.sources[i].slot] })));
    await call(w.id, 'clear_completed');
    const after = await call(w.id, 'snapshot'); assert.equal(after.counts.succeeded || 0, 0);
    for (const task of saved) assert.ok(fs.existsSync(task.saved_path));
  }
  const verification = spawnSync(python, ['-c', 'import json,sys; from openpyxl import load_workbook\nfor entry in json.load(sys.stdin):\n book=load_workbook(entry["path"],read_only=True); row=list(book.active.values)[1]; book.close()\n assert list(row)==entry["expected"], (row,entry["expected"])'], { input: JSON.stringify(exports), encoding: 'utf8' });
  assert.equal(verification.status, 0, verification.stderr);
  assert.deepEqual(faults, []);
  await main.screenshot({ path: path.join(artifacts, 'stability-soak-final.png') });
  const result = { status: 'ok', version: '6.2.0', platform: process.platform, arch: process.arch,
    duration_seconds: (Date.now()-started)/1000,
    downloads: samples.at(-1).done, workspaces: 4, packaged: !!packaged,
    electron_retained_growth: growth('electron'),
    shm_retained_growth: samples.some(s => s.shm !== null) ? growth('shm') : null, samples };
  fs.writeFileSync(path.join(artifacts, 'stability-soak.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({ ...result, samples: undefined }));
}
withDeadline(run(), duration + 20 * 60000, 'endurance phase').catch(error=>{
  console.error(error); process.exitCode=1;
  fs.writeFileSync(path.join(artifacts, 'stability-soak-failure.json'), JSON.stringify({ status: 'failed', error: error.stack, last_sample: samples.at(-1) }, null, 2));
}).finally(async()=>{
  fs.writeFileSync(path.join(artifacts, 'stability-soak-samples.json'), JSON.stringify(samples));
  if (application) {
    await closeApplication(application).catch(error=>{ console.error(error); process.exitCode=1; });
  }
  collectDiagnostics(temporary, artifacts, 'stability-soak');
  server.kill('SIGTERM');
});
