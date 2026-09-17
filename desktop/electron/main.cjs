const { app, BrowserWindow, ipcMain, dialog, shell, screen, nativeTheme, Menu, powerSaveBlocker } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { WorkerBridge, redact } = require('./bridge.cjs');

if (process.env.TMIS_DESKTOP_DATA_DIR) {
  fs.mkdirSync(process.env.TMIS_DESKTOP_DATA_DIR, { recursive: true });
  app.setPath('userData', process.env.TMIS_DESKTOP_DATA_DIR);
}
// Kylin floating-window geometry relies on X11 (or XWayland). No sandbox flags.
if (process.platform === 'linux') app.commandLine.appendSwitch('ozone-platform', 'x11');
const pageURL = pathToFileURL(path.join(__dirname, '../dist/index.html')).href;
const devURL = !app.isPackaged && process.env.TMIS_DEV_URL === 'http://127.0.0.1:5173' ? process.env.TMIS_DEV_URL : null;
const allowedFiles = new Set(), allowedRoots = new Set();
let mainWindow, floatWindow, bridge, snapshot, quitting = false, asking = false, compact = false;
let refreshTimer, refreshing = false, refreshAgain = false, blocker, prefs = {}, logs = [];
const windows = () => [mainWindow, floatWindow].filter(win => win && !win.isDestroyed());
const broadcast = message => windows().forEach(win => win.webContents.send('tmis:event', message));
const prefsPath = () => path.join(app.getPath('userData'), 'window-preferences.json');
function savePrefs() { try { fs.writeFileSync(prefsPath(), JSON.stringify(prefs), { mode: 0o600 }); } catch {} }
function authorize(event) {
  if (!windows().some(win => win.webContents === event.sender) || event.senderFrame !== event.sender.mainFrame)
    throw new Error('不受信任的窗口');
  const url = event.senderFrame.url.split('?')[0];
  if (url !== pageURL && url !== devURL + '/') throw new Error('不受信任的页面');
}
async function refresh() {
  if (!bridge || bridge.closed || quitting) return;
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    snapshot = await bridge.call('snapshot');
    broadcast({ event: 'snapshot', data: snapshot });
    const active = ['running', 'pausing', 'logging_in', 'switching'].includes(snapshot.mode);
    if (active && blocker === undefined) blocker = powerSaveBlocker.start('prevent-app-suspension');
    if (!active && blocker !== undefined) { powerSaveBlocker.stop(blocker); blocker = undefined; }
  } catch (error) { broadcast({ event: 'error', message: redact(error.message) }); }
  finally { refreshing = false; if (refreshAgain) { refreshAgain = false; scheduleRefresh(); } }
}
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; void refresh(); }, 120);
}
function safeBounds(bounds) {
  const area = screen.getDisplayMatching(bounds).workArea;
  return { x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - 368)),
    y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - 264)) };
}
function setCompact(value) {
  compact = value;
  const show = value ? floatWindow : mainWindow;
  show.show(); show.focus();
  (value ? mainWindow : floatWindow).hide();
  broadcast({ event: 'window', compact, top: !!prefs.top });
}
function makeWindow(floating) {
  const bounds = floating && Number.isFinite(prefs.x) && Number.isFinite(prefs.y) ?
    safeBounds({ x: prefs.x, y: prefs.y, width: 368, height: 264 }) : {};
  const win = new BrowserWindow({
    ...bounds, width: floating ? 368 : 1320, height: floating ? 264 : 850,
    minWidth: floating ? 368 : 990, minHeight: floating ? 264 : 660,
    frame: !floating, resizable: !floating, show: false, backgroundColor: '#f5f7fb',
    title: floating ? 'TMIS · 下载进度' : 'TMIS · 数据工作台',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      sandbox: true, nodeIntegration: false, webSecurity: true, partition: 'tmis-ui',
      backgroundThrottling: false, devTools: !app.isPackaged }
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  win.webContents.on('render-process-gone', () => {
    if (!quitting) { win.reload(); win.show(); }
  });
  win.on('close', event => {
    if (quitting) return;
    event.preventDefault();
    if (floating) setCompact(false);
    else void confirmExit();
  });
  if (floating) {
    win.setAlwaysOnTop(prefs.top !== false);
    let moving;
    win.on('move', () => {
      clearTimeout(moving);
      moving = setTimeout(() => {
        if (win.isDestroyed()) return;
        const b = win.getBounds(), area = screen.getDisplayMatching(b).workArea;
        let x = b.x, y = b.y;
        if (Math.abs(x - area.x) < 20) x = area.x;
        if (Math.abs(area.x + area.width - x - b.width) < 20) x = area.x + area.width - b.width;
        if (Math.abs(y - area.y) < 20) y = area.y;
        if (Math.abs(area.y + area.height - y - b.height) < 20) y = area.y + area.height - b.height;
        const clamped = safeBounds({ ...b, x, y });
        prefs = { ...prefs, ...clamped }; savePrefs();
        if (clamped.x !== b.x || clamped.y !== b.y) win.setPosition(clamped.x, clamped.y);
      }, 250);
    });
  }
  win.loadURL((devURL || pageURL) + (floating ? '?view=compact' : ''));
  return win;
}
async function confirmExit() {
  if (asking || quitting) return;
  asking = true;
  const { response } = await dialog.showMessageBox(compact ? floatWindow : mainWindow, {
    type: 'question', title: '离开工作台', buttons: ['继续运行', '切换悬浮窗', '退出应用'],
    defaultId: 0, cancelId: 0, message: '退出会关闭抓取浏览器和服务',
    detail: '当前未完成任务会保存为中断状态，下次启动可恢复。若只是收起界面，请切换悬浮窗。'
  });
  asking = false;
  if (response === 1) setCompact(true);
  if (response === 2) await shutdown();
}
async function shutdown() {
  if (quitting) return;
  quitting = true;
  broadcast({ event: 'closing' });
  if (bridge && !bridge.closed) {
    try {
      await bridge.call('shutdown', {}, 30000);
      bridge.child.stdin.end();
      await new Promise(resolve => {
        if (bridge.closed) return resolve();
        const timer = setTimeout(resolve, 20000);
        bridge.child.once('exit', () => { clearTimeout(timer); resolve(); });
      });
    } catch {}
    if (!bridge.closed) bridge.child.kill('SIGTERM');
  }
  if (blocker !== undefined) powerSaveBlocker.stop(blocker);
  app.quit();
}
const commands = new Set(['snapshot', 'preview', 'import_preview', 'discard_preview', 'details',
  'login', 'start', 'pause', 'retry', 'remove', 'request_mode', 'cancel_mode', 'browser_window']);
ipcMain.handle('tmis:call', async (event, command, data = {}) => {
  authorize(event);
  if (!commands.has(command) || !data || typeof data !== 'object' || Array.isArray(data)) throw new Error('无效操作');
  if (command === 'preview' && (!Array.isArray(data.paths) || data.paths.some(p => !allowedFiles.has(p))))
    throw new Error('请通过文件选择或拖放导入参数表');
  if (command === 'import_preview' && !allowedRoots.has(data.root)) throw new Error('请先选择下载目录');
  if (command === 'snapshot') return { ...await bridge.call(command), logs, top: prefs.top !== false };
  const result = await bridge.call(command, data);
  scheduleRefresh();
  return result;
});
ipcMain.handle('tmis:files', async event => {
  authorize(event);
  const result = await dialog.showOpenDialog(mainWindow, { properties: ['openFile', 'multiSelections'],
    filters: [{ name: 'Excel 参数表', extensions: ['xlsx', 'xls'] }] });
  result.filePaths.forEach(p => allowedFiles.add(p));
  return result.filePaths;
});
ipcMain.handle('tmis:drop', (event, files) => {
  authorize(event);
  if (!Array.isArray(files) || files.length > 30 || files.some(p => typeof p !== 'string' || !path.isAbsolute(p) || !/\.xlsx?$/i.test(p) || !fs.statSync(p).isFile()))
    throw new Error('请拖入 Excel 参数表（一次最多 30 份）');
  files.forEach(p => allowedFiles.add(p));
  return files;
});
ipcMain.handle('tmis:directory', async event => {
  authorize(event);
  const result = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'] });
  const root = result.filePaths[0] || '';
  if (root) allowedRoots.add(root);
  return root;
});
ipcMain.handle('tmis:browser', async event => {
  authorize(event);
  const result = await dialog.showOpenDialog(mainWindow, { properties: ['openFile'], title: '选择 Chromium / Chrome 可执行文件' });
  return result.filePaths[0] || '';
});
ipcMain.handle('tmis:window', (event, action, value) => {
  authorize(event);
  if (action === 'compact') setCompact(true);
  else if (action === 'expand') setCompact(false);
  else if (action === 'exit') void confirmExit();
  else if (action === 'top' && typeof value === 'boolean') {
    prefs.top = value; savePrefs(); floatWindow.setAlwaysOnTop(value);
    broadcast({ event: 'window', compact, top: value });
  } else throw new Error('未知窗口操作');
});
ipcMain.handle('tmis:open', async (event, target) => {
  authorize(event);
  const fresh = await bridge.call('snapshot');
  const permitted = [...allowedRoots, fresh.state_dir, ...fresh.batches.map(b => b.output_dir)];
  if (typeof target !== 'string' || !permitted.includes(target)) throw new Error('只能打开已选择的输出目录或任务目录');
  const error = await shell.openPath(target);
  if (error) throw new Error(redact(error));
});
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (mainWindow) setCompact(false); });
  app.on('activate', () => { if (mainWindow) setCompact(false); });
  app.on('before-quit', event => { if (!quitting && mainWindow) { event.preventDefault(); void confirmExit(); } });
  app.whenReady().then(() => {
    nativeTheme.themeSource = 'light';
    Menu.setApplicationMenu(process.platform === 'darwin' ? Menu.buildFromTemplate([
      { label: 'TMIS', submenu: [{ role: 'about' }, { type: 'separator' },
        { label: '退出 TMIS', accelerator: 'CmdOrCtrl+Q', click: () => void confirmExit() }] },
      { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] }
    ]) : null);
    try { prefs = JSON.parse(fs.readFileSync(prefsPath(), 'utf8')); } catch {}
    mainWindow = makeWindow(false);
    floatWindow = makeWindow(true);
    const root = path.resolve(__dirname, '../..');
    const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend/tmis-worker') :
      (process.env.TMIS_PYTHON || path.join(root, '.venv/bin/python'));
    const args = app.isPackaged ? [] : ['-u', path.join(root, 'tmis_worker.py')];
    bridge = new WorkerBridge(spawn(executable, args, { cwd: app.isPackaged ? process.resourcesPath : root,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true }));
    bridge.on('event', message => {
      if (quitting && message.event === 'offline') return;
      if (message.event === 'log') {
        logs.push({ ...message, time: new Date().toLocaleTimeString('zh-CN', { hour12: false }) });
        logs = logs.slice(-300);
      }
      broadcast(message);
      if (['ready', 'changed', 'status', 'completed'].includes(message.event)) scheduleRefresh();
    });
    mainWindow.once('ready-to-show', () => mainWindow.show());
    mainWindow.webContents.on('did-finish-load', () => {
      if (snapshot) broadcast({ event: 'snapshot', data: snapshot });
    });
  }).catch(error => { dialog.showErrorBox('启动失败', redact(error.message)); quitting = true; app.quit(); });
}
module.exports = { shutdown };
