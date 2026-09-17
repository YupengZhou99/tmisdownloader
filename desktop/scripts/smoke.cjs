// Real Electron + Python + Chromium acceptance; no production server or data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { createInterface } = require('node:readline');
const root = path.resolve(__dirname, '../..');
const playwrightPath = process.env.TMIS_PLAYWRIGHT_NODE || path.join(root, '.venv/lib/python3.9/site-packages/playwright/driver/package');
const { _electron } = require(playwrightPath);
const python = process.env.TMIS_PYTHON || path.join(root, '.venv/bin/python');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tmis-desktop-smoke-'));
const artifacts = path.join(root, 'output/playwright');
fs.mkdirSync(artifacts, { recursive: true });
const server = spawn(python, [path.join(root, 'tests/desktop_fixture_server.py'), temporary], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let application;
async function waitForState(page, predicate, timeout = 90000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await page.evaluate(() => window.tmis.call('snapshot'));
    if (predicate(result)) return result;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error('Timed out waiting for backend state');
}
async function run() {
  const config = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture start timeout')), 20000);
    const lines = createInterface({ input: server.stdout });
    lines.once('line', line => { clearTimeout(timer); resolve(JSON.parse(line)); });
    server.once('exit', code => reject(new Error('fixture exited: ' + code)));
    server.stderr.on('data', data => process.stderr.write(data));
  });
  const packaged = process.env.TMIS_DESKTOP_EXECUTABLE;
  const args = packaged ? [] : [path.join(root, 'desktop')];
  if (process.env.TMIS_CI_ROOT === '1') args.push('--no-sandbox'); // CI container only, never the shipped launcher
  application = await _electron.launch({ executablePath: packaged || process.env.TMIS_ELECTRON_PATH || require('electron'), args, timeout: 60000,
    env: { ...process.env, TMIS_STATE_DIR: path.join(temporary, 'state'),
      TMIS_DESKTOP_DATA_DIR: path.join(temporary, 'desktop'), TMIS_PYTHON: python } });
  await application.firstWindow();
  let main;
  for (let attempt = 0; attempt < 200; attempt++) {
    main = application.windows().find(p => p.url().includes('index.html') && !p.url().includes('view=compact'));
    if (main) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(main, 'main workbench window loaded');
  const pageErrors = [];
  main.on('pageerror', error => pageErrors.push(error.message));
  await main.getByRole('heading', { name: '下载队列', exact: true }).waitFor();
  await waitForState(main, state => state.version === '6.0.0');
  await main.screenshot({ path: path.join(artifacts, 'workbench-empty.png') });
  // Native picker is stubbed in the isolated test process; subsequent user UI,
  // IPC validation, Python parsing, snapshots and downloads are real.
  await application.evaluate(({ dialog }, { directory }) => {
    dialog.showOpenDialog = async (_window, options) => ({
      canceled: false, filePaths: options.properties.includes('openDirectory') ?
        [directory] : [directory + '/库存.xlsx']
    });
  }, config);
  if (process.env.TMIS_TEST_BROWSER_PATH) {
    await main.getByRole('button', { name: '偏好与浏览器', exact: true }).click();
    await main.getByPlaceholder('默认使用随包浏览器 / 系统 Chrome').fill(process.env.TMIS_TEST_BROWSER_PATH);
    await main.getByRole('button', { name: '完成', exact: true }).click();
  }
  await main.getByRole('button', { name: '连接 TMIS', exact: true }).click();
  await main.getByPlaceholder('粘贴含登录信息的完整 http:// 或 https:// 链接').fill(config.url);
  await main.getByRole('button', { name: '连接工作界面' }).click();
  await waitForState(main, s => s.session_ready);
  process.stdout.write('PASS independent login\n');
  assert.equal((await main.evaluate(() => window.tmis.call('snapshot'))).tasks.length, 0, 'login works before file selection');
  async function importFile(files = ['库存.xlsx']) {
    await application.evaluate(({ dialog }, { directory, files }) => {
      dialog.showOpenDialog = async (_window, options) => ({
        canceled: false, filePaths: options.properties.includes('openDirectory') ?
          [directory] : files.map(file => directory + '/' + file)
      });
    }, { directory: config.directory, files });
    await main.getByRole('button', { name: '添加参数表', exact: true }).click();
    await main.getByRole('button', { name: /选择 Excel 参数表/ }).click();
    await main.getByRole('button', { name: '选择下载目录', exact: true }).click();
    await main.getByRole('button', { name: '检查参数', exact: true }).click();
    await main.getByText('条待导入任务 / ' + files.length + ' 份参数表').waitFor();
  }
  await importFile();
  await main.screenshot({ path: path.join(artifacts, 'import-preview.png') });
  await main.getByRole('button', { name: '加入队列', exact: true }).click();
  process.stdout.write('PASS parameter preview and import\n');
  assert.equal((await main.evaluate(() => window.tmis.call('snapshot'))).tasks.length, 3);
  await main.getByRole('button', { name: '开始 / 继续' }).click();
  await waitForState(main, s => !!s.current);
  const pid = await application.evaluate(() => process.pid);
  await main.getByRole('button', { name: '悬浮窗', exact: true }).click();
  const floating = application.windows().find(page => page.url().includes('view=compact'));
  assert.ok(floating, 'floating window exists');
  await floating.getByRole('button', { name: '返回工作台', exact: true }).first().waitFor();
  await floating.screenshot({ path: path.join(artifacts, 'floating-window.png') });
  await floating.getByRole('button', { name: '返回工作台', exact: true }).first().click();
  assert.equal(await application.evaluate(() => process.pid), pid);
  // Queue mode switch during an active task. It must never cancel that task.
  await main.getByRole('button', { name: '偏好与浏览器', exact: true }).click();
  await main.getByRole('switch', { name: '后台无头模式' }).click();
  await main.getByRole('button', { name: '完成', exact: true }).click();
  await importFile(['追加1.xlsx', '追加2.xlsx']);
  await main.getByRole('button', { name: '加入队列', exact: true }).click();
  assert.equal((await main.evaluate(() => window.tmis.call('snapshot'))).tasks.length, 5);
  const finished = await waitForState(main, s => s.counts.succeeded === 5 && s.mode === 'idle', 180000);
  assert.equal(finished.headless, true);
  assert.equal(finished.session_ready, true, 'session kept alive after queue completion');
  process.stdout.write('PASS floating mode, headless transition and running multi-file append\n');
  for (const task of finished.tasks) {
    assert.equal(task.attempt_count, 1);
    assert.ok(fs.existsSync(task.saved_path));
  }
  await main.screenshot({ path: path.join(artifacts, 'workbench-completed.png') });
  // Retry a successful row is ignored, never downloaded again.
  await main.evaluate(id => window.tmis.call('retry', { ids: [id] }), finished.tasks[0].id);
  const after = await main.evaluate(() => window.tmis.call('snapshot'));
  assert.equal(after.tasks[0].attempt_count, 1);
  assert.equal(after.counts.succeeded, 5);
  await importFile(['重试.xlsx']);
  await main.getByRole('button', { name: '加入队列', exact: true }).click();
  const control = new URL('/__test/fail-next', config.url);
  assert.ok((await fetch(control)).ok);
  await main.getByRole('button', { name: '开始 / 继续' }).click();
  const failed = await waitForState(main, s => s.counts.failed === 1 && s.mode === 'idle');
  await main.getByRole('button', { name: '重试', exact: true }).click();
  const recovered = await waitForState(main, s => s.counts.succeeded === 6 && s.mode === 'idle');
  const retried = recovered.tasks.find(task => task.output_name === '失败后重试');
  assert.equal(retried.attempt_count, 2);
  const details = await main.evaluate(id => window.tmis.call('details', { id }), retried.id);
  assert.deepEqual(details.history.map(item => item.status), ['failed', 'succeeded']);
  process.stdout.write('PASS failed-row retry with preserved history\n');
  await main.getByRole('button', { name: '下载队列', exact: true }).click();
  await main.screenshot({ path: path.join(artifacts, 'workbench-completed.png') });
  assert.deepEqual(pageErrors, []);
  const report = { status: 'ok', platform: process.platform, arch: process.arch,
    electron: await application.evaluate(() => process.versions.electron), downloads: 6,
    login_before_parameters: true, floating_during_execution: true, mode_switch_boundary: true,
    persistent_session: true, successful_retry_skipped: true, multi_file_append: true,
    failed_row_retry: true, packaged: !!packaged, temporary };
  fs.writeFileSync(path.join(artifacts, 'desktop-smoke.json'), JSON.stringify(report, null, 2));
  process.stdout.write(JSON.stringify(report) + '\n');
  await main.evaluate(() => window.tmis.call('pause'));
  await application.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 2 }); });
  await main.getByRole('button', { name: '退出应用', exact: true }).click();
  await new Promise((resolve, reject) => {
    if (application.process().exitCode !== null) return resolve();
    const timer = setTimeout(() => reject(new Error('app did not shut down')), 30000);
    application.process().once('exit', () => { clearTimeout(timer); resolve(); });
  });
  application = null;
  const files = fs.readdirSync(path.join(temporary, 'state'));
  for (const filename of files) {
    const file = path.join(temporary, 'state', filename);
    if (fs.statSync(file).isFile()) assert.ok(!fs.readFileSync(file).includes(Buffer.from('LOCAL_TEST_ONLY')), 'no login token on disk');
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(async () => {
    if (application) {
      const page = application.windows().find(p => !p.url().includes('view=compact'));
      if (page) await page.screenshot({ path: path.join(artifacts, 'last-state.png') }).catch(() => {});
      if (page) fs.writeFileSync(path.join(artifacts, 'last-state.txt'), await page.locator('body').innerText().catch(() => ''));
      await application.evaluate(({ dialog }) => { dialog.showMessageBox = async () => ({ response: 2 }); }).catch(() => {});
      await application.close().catch(() => {});
    }
    server.kill('SIGTERM');
  });
