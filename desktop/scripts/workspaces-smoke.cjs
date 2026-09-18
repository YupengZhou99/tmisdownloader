// Four *real* Python workers and Chromium sessions against the same origin.
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
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-four-workspaces-'));
const artifacts = path.join(root, 'output/playwright'); fs.mkdirSync(artifacts, { recursive: true });
const server = spawn(python, [path.join(root, 'tests/workspace_fixture_server.py'), temporary], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let application, main;
const errors = [];
const packaged = process.env.TMIS_DESKTOP_EXECUTABLE;
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function launch() {
  const args = packaged ? [] : [path.join(root, 'desktop')];
  if (process.env.TMIS_CI_ROOT === '1') args.push('--no-sandbox');
  application = await _electron.launch({ executablePath: packaged || process.env.TMIS_ELECTRON_PATH || require('electron'), args, timeout: 60000,
    env: { ...process.env, TMIS_STATE_DIR: path.join(temporary, 'state'), TMIS_DESKTOP_DATA_DIR: path.join(temporary, 'desktop'), TMIS_PYTHON: python } });
  captureProcessOutput(application, artifacts, 'workspaces-smoke');
  application.context().setDefaultTimeout(30000);
  await application.firstWindow();
  for (let i = 0; i < 200; i++) {
    main = application.windows().find(p => p.url().includes('index.html') && !p.url().includes('view=compact'));
    if (main) break; await sleep(100);
  }
  assert.ok(main); main.on('pageerror', e => errors.push(e.message));
  await main.getByRole('heading', { name: '每一路，各司其职。', exact: true }).waitFor();
}
async function snapshot() { return withDeadline(main.evaluate(() => window.tmis.workspaces('snapshot')), 15000, 'workspaces snapshot'); }
async function until(predicate, timeout = 150000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const s = await snapshot(); if (predicate(s)) return s; await sleep(150); }
  const latest = await snapshot();
  throw new Error('Workspace state timeout: ' + JSON.stringify(latest.workspaces.map(w => ({ name: w.name, mode: w.state.mode, counts: w.state.counts, online: w.online, error: w.error }))));
}
async function call(id, command, data = {}) { return withDeadline(main.evaluate(({ id, command, data }) => window.tmis.call(command, data, id), { id, command, data }), 90000, 'workspace ' + command); }
async function overview() { const button = main.getByRole('button', { name: '所有工作区', exact: true }); if (await button.count()) await button.click(); }
async function select(w) {
  const selector = main.getByRole('combobox', { name: '切换工作区', exact: true });
  if (await selector.count()) await selector.selectOption(w.id);
  else await main.getByRole('button', { name: '进入队列 ' + w.name, exact: true }).click();
  await main.getByRole('combobox', { name: '切换工作区', exact: true }).waitFor();
}
async function importUI(w, file, directory) {
  await select(w);
  await application.evaluate(({ dialog }, { file, directory }) => {
    dialog.showOpenDialog = async (_window, options) => ({ canceled: false,
      filePaths: options.properties.includes('openDirectory') ? [directory] : [file] });
  }, { file, directory });
  await main.getByRole('button', { name: '添加参数表', exact: true }).click();
  await main.getByText('目标工作区：' + w.name, { exact: true }).waitFor();
  await main.getByRole('button', { name: '选择 Excel 参数表', exact: true }).click();
  await main.getByRole('button', { name: '选择下载目录', exact: true }).click();
  await main.getByRole('button', { name: '检查参数', exact: true }).click();
  await main.getByText('条待导入任务 / 1 份参数表', { exact: true }).waitFor();
  await main.getByRole('button', { name: '加入队列', exact: true }).click();
  await main.getByRole('dialog').waitFor({ state: 'hidden' });
}
async function close() {
  if (!application) return;
  await closeApplication(application); application = null;
}
async function run() {
  const config = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture timeout')), 20000);
    createInterface({ input: server.stdout }).once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
    server.stderr.on('data', data => process.stderr.write(data)); server.once('exit', code => reject(new Error('fixture exit ' + code)));
  });
  await launch();
  await main.getByRole('button', { name: '管理登录', exact: true }).click();
  await main.getByPlaceholder('每行一个完整登录 URL，分配后可分别核对').fill(config.sources.map(s => s.url).join('\n'));
  await main.getByRole('button', { name: '分配到工作区', exact: true }).click();
  const created = await until(s => s.workspaces.length === 4);
  const ws = created.workspaces;
  if (process.env.TMIS_TEST_BROWSER_PATH) for (const w of ws) await main.evaluate(({ id, browserPath }) => window.tmis.workspaces('update', { id, settings: { browserPath } }), { id: w.id, browserPath: process.env.TMIS_TEST_BROWSER_PATH });
  await assert.rejects(() => call(ws[0].id, 'start'), /登录/);
  assert.equal(await main.getByRole('button', { name: '进入已登录工作区' }).isEnabled(), false);
  await main.locator('[data-workspace-id="' + ws[0].id + '"]').getByRole('button', { name: '单独登录', exact: true }).click();
  await until(s => s.workspaces[0].confirmed);
  await call(ws[0].id, 'start');
  assert.ok((await snapshot()).workspaces.slice(1).every(w => !w.confirmed));
  await main.getByRole('combobox', { name: '登录模式 ' + ws[3].name, exact: true }).selectOption('headless');
  await main.getByRole('button', { name: '全部登录', exact: true }).click();
  await until(s => s.workspaces.every(w => w.state.session_ready));
  await main.screenshot({ path: path.join(artifacts, 'workspaces-login-confirm.png'), animations: 'disabled' });
  assert.ok((await snapshot()).workspaces.every(w => w.confirmed && w.state.tasks.length === 0));
  assert.equal((await snapshot()).workspaces[3].state.headless, true);
  await main.getByRole('button', { name: '进入已登录工作区', exact: true }).click();
  await until(s => s.workspaces.every(w => w.confirmed));
  process.stdout.write('PASS independent login/start, headless login and four same-origin sessions\n');
  for (let i = 0; i < 4; i++) await importUI(ws[i], config.sources[i].source, config.directory);
  const staged = await snapshot();
  assert.ok(staged.workspaces.every(w => w.state.tasks.length === 3 && w.state.mode === 'idle'));
  assert.equal(new Set(staged.workspaces.map(w => w.state.state_dir)).size, 4);
  assert.equal(new Set(staged.workspaces.map(w => w.worker_pid)).size, 4);
  assert.equal(new Set(staged.workspaces.map(w => w.state.batches[0].output_dir)).size, 4);
  await assert.rejects(() => call(ws[1].id, 'preview', { paths: [config.sources[0].source], options: {} }), /文件选择/);
  await overview();
  await main.screenshot({ path: path.join(artifacts, 'workspaces-configured.png'), animations: 'disabled' });
  await fetch(config.base + '/__test/fail?slot=A');
  await main.getByRole('button', { name: '全部开始', exact: true }).click();
  const simultaneous = await until(s => s.workspaces.every(w => !!w.state.current));
  process.stdout.write('PASS four real tasks simultaneously active with four distinct workers\n');
  // Kill exactly the isolated test's C worker; other queues must keep running.
  const target = simultaneous.workspaces[2].worker_pid;
  assert.ok(Number.isInteger(target) && target > 1);
  process.kill(target, 'SIGKILL');
  await until(s => !s.workspaces[2].online);
  await select(ws[1]);
  await main.getByRole('button', { name: '暂停本队列', exact: true }).click();
  await select(ws[0]);
  await main.getByRole('button', { name: '偏好与浏览器', exact: true }).click();
  await main.getByRole('switch', { name: '后台无头模式', exact: true }).click();
  await main.getByRole('button', { name: '完成', exact: true }).click();
  const override = path.join(temporary, 'D-override'); fs.mkdirSync(override);
  await importUI(ws[3], config.sources[3].append, override);
  await overview();
  await main.getByRole('button', { name: '悬浮窗', exact: true }).click();
  const floating = application.windows().find(p => p.url().includes('view=compact'));
  await floating.getByRole('button', { name: '打开工作区 ' + ws[0].name, exact: true }).waitFor();
  await floating.screenshot({ path: path.join(artifacts, 'workspaces-floating.png'), animations: 'disabled' });
  await floating.getByRole('button', { name: '打开工作区 ' + ws[1].name, exact: true }).click();
  assert.equal(await main.getByRole('combobox', { name: '切换工作区' }).inputValue(), ws[1].id);
  const partial = await until(s => s.workspaces[0].state.counts.failed === 1 && s.workspaces[0].state.counts.succeeded === 2 && s.workspaces[1].state.mode === 'paused' && s.workspaces[3].state.counts.succeeded === 4, 200000);
  assert.equal(partial.workspaces[0].state.headless, true);
  assert.equal(partial.workspaces[1].state.headless, false);
  assert.ok((partial.workspaces[1].state.counts.succeeded || 0) < 3);
  assert.equal(partial.workspaces[3].root, config.directory, 'one-batch override must not change default');
  assert.ok(partial.workspaces[3].state.tasks.filter(t => t.output_name.includes('任务')).every(t => t.output_dir.startsWith(fs.realpathSync(path.join(config.directory, ws[3].folder)) + path.sep)));
  assert.ok(partial.workspaces[3].state.tasks.find(t => t.output_name.includes('追加')).output_dir.startsWith(fs.realpathSync(override) + path.sep));
  process.stdout.write('PASS failure and worker-crash isolation, scoped pause/mode and running append\n');
  await main.getByRole('button', { name: '开始本队列', exact: true }).click();
  await overview();
  await main.getByRole('button', { name: '管理登录', exact: true }).click();
  await main.getByRole('textbox', { name: '登录链接 ' + ws[2].name, exact: true }).fill(config.sources[2].url);
  await main.getByRole('button', { name: '全部登录', exact: true }).click();
  await until(s => s.workspaces[2].state.session_ready);
  await main.getByRole('button', { name: '进入已登录工作区', exact: true }).click();
  const recovered = await until(s => s.workspaces[2].confirmed);
  assert.equal(recovered.workspaces[2].state.tasks.length, 3);
  assert.equal(recovered.workspaces[2].state.counts.interrupted, 1);
  await select(ws[2]); await main.getByRole('button', { name: '开始本队列', exact: true }).click();
  await select(ws[0]); await main.getByRole('button', { name: /^待处理任务/ }).click();
  await main.getByRole('button', { name: '重试', exact: true }).click();
  const finished = await until(s => s.workspaces.every((w, i) => w.state.counts.succeeded === (i === 3 ? 4 : 3) && w.state.mode === 'idle'), 200000);
  assert.ok(finished.workspaces.every(w => w.state.session_ready && w.online));
  const saved = finished.workspaces.flatMap((w, i) => w.state.tasks.map(t => ({ path: t.saved_path, expected: [config.sources[i].slot, config.sources[i].treasury, config.sources[i].start, config.sources[i].end, '0 -- 全部', config.sources[i].slot] })));
  assert.equal(new Set(saved.map(s => s.path)).size, 13);
  const verification = spawnSync(python, ['-c', 'import json,sys; from openpyxl import load_workbook\nfor entry in json.load(sys.stdin):\n row=list(load_workbook(entry["path"],read_only=True).active.values)[1]\n assert list(row)==entry["expected"], (entry["path"],row,entry["expected"])\nprint("13 exported workbooks match their workspace cookie and query fields")'], { input: JSON.stringify(saved), encoding: 'utf8' });
  assert.equal(verification.status, 0, verification.stderr); process.stdout.write(verification.stdout);
  for (const w of finished.workspaces) for (const task of w.state.tasks) {
    const d = await call(w.id, 'details', { id: task.id });
    assert.equal(d.history.filter(h => h.status === 'succeeded').length, 1);
    assert.ok(w.logs.filter(l => l.text.startsWith('成功：')).every(l => l.text.includes(config.sources[finished.workspaces.indexOf(w)].slot + '_')));
  }
  await overview(); await main.screenshot({ path: path.join(artifacts, 'workspaces-completed.png'), animations: 'disabled' });
  const identity = finished.workspaces.map(w => ({ id: w.id, root: w.root, tasks: w.state.tasks.map(t => [t.id, t.status, t.attempt_count]) }));
  await importUI(ws[0], config.sources[0].append, config.directory);
  assert.equal((await snapshot()).workspaces[0].state.counts.pending, 1);
  await close();
  await launch();
  const restored = await until(s => s.workspaces.length === 4 && s.workspaces.every((w, i) => w.state.counts.succeeded === (i === 3 ? 4 : 3)));
  assert.deepEqual(restored.workspaces.map(w => ({ id: w.id, root: w.root, tasks: w.state.tasks.filter(t => t.status === 'succeeded').map(t => [t.id, t.status, t.attempt_count]) })), identity);
  assert.ok(restored.workspaces.every(w => !w.confirmed && !w.state.session_ready));
  await main.getByRole('button', { name: '管理登录', exact: true }).click();
  await main.getByRole('textbox', { name: '登录链接 ' + ws[0].name, exact: true }).fill(config.sources[0].url);
  await main.getByRole('combobox', { name: '登录模式 ' + ws[0].name, exact: true }).selectOption('headless');
  await main.locator('[data-workspace-id="' + ws[0].id + '"]').getByRole('button', { name: '单独登录', exact: true }).click();
  await until(s => s.workspaces[0].confirmed);
  await main.getByRole('button', { name: '进入已登录工作区', exact: true }).click();
  await select(ws[0]);
  await main.getByRole('button', { name: '开始本队列', exact: true }).click();
  const independentlyResumed = await until(s => s.workspaces[0].state.counts.succeeded === 4 && s.workspaces[0].state.mode === 'idle');
  assert.ok(independentlyResumed.workspaces.slice(1).every(w => !w.confirmed && !w.state.session_ready));
  main.once('dialog', dialog => dialog.accept());
  await main.getByRole('button', { name: '清空已完成', exact: true }).click();
  await until(s => (s.workspaces[0].state.counts.succeeded || 0) === 0);
  assert.ok(independentlyResumed.workspaces[0].state.tasks.every(t => fs.existsSync(t.saved_path)));
  const retained = await call(ws[0].id, 'details', { id: independentlyResumed.workspaces[0].state.tasks[0].id });
  assert.ok(retained.history.some(h => h.status === 'succeeded'));
  process.stdout.write('PASS restart with only one login resumes its queue; completed clearing preserves files and history\n');
  await close();
  for (const entry of fs.readdirSync(path.join(temporary, 'state'), { recursive: true, withFileTypes: true })) {
    if (entry.isFile()) assert.ok(!fs.readFileSync(path.join(entry.parentPath || entry.path, entry.name)).includes(Buffer.from('WORKSPACE_SECRET_')), 'no login tokens in saved state');
  }
  assert.deepEqual(errors, []);
  const report = { status: 'ok', version: '6.2.0', platform: process.platform, arch: process.arch, packaged: !!packaged,
    workspaces: 4, downloads: 14, same_origin_cookie_isolation: true, independent_login_confirmation: true,
    single_relogin_resume: true, headless_login: true, completed_clear_preserves_files: true,
    independent_paths: true, four_simultaneous_tasks: true, scoped_pause_and_mode: true,
    worker_crash_isolation: true, running_append: true, failure_retry: true, restart_preserves_all_queues: true, credentials_not_persisted: true };
  fs.writeFileSync(path.join(artifacts, 'workspaces-smoke.json'), JSON.stringify(report, null, 2));
  process.stdout.write(JSON.stringify(report) + '\n');
}
withDeadline(run(), 14 * 60000, 'workspaces smoke phase').catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (application) {
    if (main) {
      await withDeadline(main.screenshot({ path: path.join(artifacts, 'workspaces-last-state.png'), timeout: 5000 }), 6000, 'failure screenshot').catch(() => {});
      fs.writeFileSync(path.join(artifacts, 'workspaces-last-state.txt'), await withDeadline(main.locator('body').innerText({ timeout: 3000 }), 4000, 'failure text').catch(() => 'Renderer unavailable'));
    }
    await close().catch(error => { console.error(error); process.exitCode = 1; });
  }
  collectDiagnostics(temporary, artifacts, 'workspaces-smoke');
  server.kill('SIGTERM');
});
