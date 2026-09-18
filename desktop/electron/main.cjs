const { app, BrowserWindow, ipcMain, dialog, shell, screen, nativeTheme, Menu, powerSaveBlocker } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const { WorkerBridge, redact } = require('./bridge.cjs');
const { WorkspaceManager } = require('./workspaces.cjs');
const { prepareRuntime, cleanupRuntime, diagnosticLog, systemResources, pressureCapacity, BoundedPublisher } = require('./stability.cjs');
let runtimeDirectory, startupError;

if (process.env.TMIS_DESKTOP_DATA_DIR) {
  fs.mkdirSync(process.env.TMIS_DESKTOP_DATA_DIR, { recursive: true });
  app.setPath('userData', process.env.TMIS_DESKTOP_DATA_DIR);
}
// Kylin floating-window geometry relies on X11 (or XWayland). No sandbox flags.
if (process.platform === 'linux') app.commandLine.appendSwitch('ozone-platform', 'x11');
const pageURL = pathToFileURL(path.join(__dirname, '../dist/index.html')).href;
const devURL = !app.isPackaged && process.env.TMIS_DEV_URL === 'http://127.0.0.1:5173' ? process.env.TMIS_DEV_URL : null;
const allowedFiles = new Set(), allowedRoots = new Set();
let mainWindow, floatWindow, manager, snapshot, quitting = false, asking = false, compact = false;
let refreshTimer, blocker, prefs = {}, resourceTimer, resourceBusy = false, capacity = 4, healthySamples = 0;
let diagnostic = () => {};
const publishers = new Map();
const troubledWindows = new Set();
const windows = () => [mainWindow, floatWindow].filter(win => win && !win.isDestroyed());
const broadcast = message => windows().forEach(win => publishers.get(win)?.push(message));
ipcMain.on('tmis:ack', (event, seq) => {
  if (!Number.isInteger(seq)) return;
  const win = windows().find(w => w.webContents === event.sender && event.senderFrame === event.sender.mainFrame);
  if (win) publishers.get(win)?.ack(seq);
});
const prefsPath = () => path.join(app.getPath('userData'), 'window-preferences.json');
function savePrefs() { try { fs.writeFileSync(prefsPath(), JSON.stringify(prefs), { mode: 0o600 }); } catch {} }
function authorize(event) {
  if (!windows().some(win => win.webContents === event.sender) || event.senderFrame !== event.sender.mainFrame)
    throw new Error('不受信任的窗口');
  const url = event.senderFrame.url.split('?')[0];
  if (url !== pageURL && url !== devURL + '/') throw new Error('不受信任的页面');
}
async function refresh() {
  if (!manager || quitting) return;
  snapshot = manager.snapshot();
  broadcast({ event: 'workspaces', data: snapshot });
  const active = snapshot.workspaces.some(w => !w.archived && (w.busy || ['running', 'pausing', 'logging_in', 'switching'].includes(w.state.mode)));
  if (active && blocker === undefined) blocker = powerSaveBlocker.start('prevent-app-suspension');
  if (!active && blocker !== undefined) { powerSaveBlocker.stop(blocker); blocker = undefined; }
}
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; void refresh(); }, 120);
}
function safeBounds(bounds) {
  const area = screen.getDisplayMatching(bounds).workArea;
  return { x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - 368)),
    y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - 420)) };
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
    safeBounds({ x: prefs.x, y: prefs.y, width: 368, height: 420 }) : {};
  const win = new BrowserWindow({
    ...bounds, width: floating ? 368 : 1380, height: floating ? 420 : 880,
    minWidth: floating ? 368 : 990, minHeight: floating ? 420 : 660,
    frame: !floating, resizable: !floating, show: false, backgroundColor: '#f5f7fb',
    title: floating ? 'TMIS · 下载进度' : 'TMIS · 数据工作台',
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      sandbox: true, nodeIntegration: false, webSecurity: true, partition: 'tmis-ui',
      backgroundThrottling: true, devTools: !app.isPackaged }
  });
  publishers.set(win, new BoundedPublisher(win, { compact: floating }));
  win.on('hide', () => publishers.get(win)?.reset());
  win.on('closed', () => { publishers.get(win)?.reset(); publishers.delete(win); troubledWindows.delete(win); });
  win.on('show', () => { publishers.get(win)?.reset(); void refresh(); });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  let recoveries = [], recoveryTimer;
  const recoverUI = details => {
    if (quitting || win.isDestroyed()) return;
    troubledWindows.add(win);
    diagnostic('renderer-fault', { floating, ...details });
    recoveries = recoveries.filter(t => Date.now() - t < 60000);
    if (recoveries.length >= 2) return;
    recoveries.push(Date.now());
    publishers.get(win)?.reset();
    clearTimeout(recoveryTimer);
    recoveryTimer = setTimeout(() => {
      if (!quitting && !win.isDestroyed()) {
        diagnostic('renderer-reload', { floating, crashed: win.webContents.isCrashed() });
        win.reload();
      }
    }, 2000);
  };
  win.webContents.on('render-process-gone', (_event, details) => recoverUI(details));
  win.on('unresponsive', () => recoverUI({ reason: 'unresponsive' }));
  win.on('responsive', () => { troubledWindows.delete(win); clearTimeout(recoveryTimer); });
  win.on('closed', () => clearTimeout(recoveryTimer));
  win.webContents.on('did-finish-load', () => {
    if (troubledWindows.has(win)) diagnostic('renderer-recovered', { floating, pid: win.webContents.getOSProcessId() });
    troubledWindows.delete(win); publishers.get(win)?.reset(); void refresh();
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
    defaultId: 0, cancelId: 0, message: '退出会关闭所有工作区的浏览器和服务',
    detail: '所有队列都会保存，正在执行的任务标记为中断，下次启动可恢复。若只是收起界面，请切换悬浮窗。'
  });
  asking = false;
  if (response === 1) setCompact(true);
  if (response === 2) await shutdown();
}
async function shutdown() {
  if (quitting) return;
  quitting = true;
  clearInterval(resourceTimer);
  broadcast({ event: 'closing' });
  if (manager) await manager.shutdown();
  if (blocker !== undefined) powerSaveBlocker.stop(blocker);
  app.quit();
}
const commands = new Set(['snapshot', 'preview', 'import_preview', 'discard_preview', 'details',
  'tasks', 'clear_completed', 'login', 'start', 'pause', 'disconnect', 'retry', 'remove', 'request_mode', 'cancel_mode', 'browser_window']);
const grantKey = (id, value) => id + '\0' + value;
function rootAllowed(id, root) { return typeof root === 'string' && path.isAbsolute(root) && (allowedRoots.has(grantKey(id, root)) || manager.get(id).meta.root === root); }
ipcMain.handle('tmis:call', async (event, command, data = {}, workspaceId) => {
  authorize(event);
  manager.get(workspaceId);
  if (!commands.has(command) || !data || typeof data !== 'object' || Array.isArray(data)) throw new Error('无效操作');
  if (command === 'preview' && (!Array.isArray(data.paths) || data.paths.some(p => !allowedFiles.has(grantKey(workspaceId, p)))))
    throw new Error('请通过文件选择或拖放导入参数表');
  if (command === 'import_preview' && !rootAllowed(workspaceId, data.root)) throw new Error('请先为此工作区选择下载目录');
  const result = await manager.call(workspaceId, command, data);
  if (!['tasks', 'details'].includes(command)) scheduleRefresh();
  return result;
});
ipcMain.handle('tmis:workspaces', async (event, action, data = {}) => {
  authorize(event);
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('无效工作区操作');
  let result;
  if (action === 'snapshot') return { ...manager.snapshot(), top: prefs.top !== false };
  if (action === 'create') result = manager.create(data.name);
  else if (action === 'update') {
    if (data.settings && 'root' in data.settings && !rootAllowed(data.id, data.settings.root)) throw new Error('请先选择此工作区的保存目录');
    result = manager.update(data.id, data.settings);
  } else if (action === 'confirm') result = await manager.confirm(data.ids);
  else if (action === 'archive') result = await manager.archive(data.id);
  else if (action === 'restore') { manager.restore(data.id); await manager.ensure(data.id); }
  else if (action === 'start_all' || action === 'pause_all') result = await manager.all(action === 'start_all' ? 'start' : 'pause');
  else if (action === 'login_all') {
    if (!Array.isArray(data.entries) || data.entries.length < 1 || data.entries.length > 4 || new Set(data.entries.map(e => e.id)).size !== data.entries.length) throw new Error('请选择 1～4 个不同工作区登录');
    data.entries.forEach(e => manager.get(e.id));
    result = await Promise.all(data.entries.map(async e => {
      try { await manager.login(e.id, { url: e.url, headless: e.headless }); return { id: e.id, ok: true }; }
      catch (error) { return { id: e.id, ok: false, error: redact(error.message) }; }
    }));
  } else throw new Error('未知工作区操作');
  scheduleRefresh(); return result;
});
ipcMain.handle('tmis:files', async (event, id) => {
  authorize(event);
  manager.get(id);
  const result = await dialog.showOpenDialog(mainWindow, { properties: ['openFile', 'multiSelections'],
    filters: [{ name: 'Excel 参数表', extensions: ['xlsx', 'xls'] }] });
  result.filePaths.forEach(p => allowedFiles.add(grantKey(id, p)));
  return result.filePaths;
});
ipcMain.handle('tmis:drop', (event, files, id) => {
  authorize(event);
  manager.get(id);
  if (!Array.isArray(files) || files.length > 30 || files.some(p => typeof p !== 'string' || !path.isAbsolute(p) || !/\.xlsx?$/i.test(p) || !fs.statSync(p).isFile()))
    throw new Error('请拖入 Excel 参数表（一次最多 30 份）');
  files.forEach(p => allowedFiles.add(grantKey(id, p)));
  return files;
});
ipcMain.handle('tmis:directory', async (event, id) => {
  authorize(event);
  manager.get(id);
  const result = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory', 'createDirectory'] });
  const root = result.filePaths[0] || '';
  if (root) allowedRoots.add(grantKey(id, root));
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
  else if (action === 'workspace' && typeof value === 'string') {
    manager.get(value); setCompact(false); broadcast({ event: 'navigate', workspaceId: value });
  }
  else if (action === 'top' && typeof value === 'boolean') {
    prefs.top = value; savePrefs(); floatWindow.setAlwaysOnTop(value);
    broadcast({ event: 'window', compact, top: value });
  } else throw new Error('未知窗口操作');
});
ipcMain.handle('tmis:open', async (event, target, id) => {
  authorize(event);
  const fresh = await manager.call(id, 'snapshot');
  const permitted = [manager.get(id).meta.root, fresh.state_dir, ...fresh.batches.map(b => b.output_dir)];
  if (typeof target !== 'string' || !permitted.includes(target)) throw new Error('只能打开已选择的输出目录或任务目录');
  const error = await shell.openPath(target);
  if (error) throw new Error(redact(error));
});
if (!app.requestSingleInstanceLock()) app.quit();
else {
  try { runtimeDirectory = prepareRuntime(app); } catch (error) { startupError = error; }
  app.on('will-quit', () => cleanupRuntime(runtimeDirectory));
  app.on('second-instance', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (troubledWindows.has(mainWindow) || mainWindow.webContents.isCrashed()) {
      publishers.get(mainWindow)?.reset(); mainWindow.reload();
    }
    setCompact(false);
  });
  app.on('activate', () => { if (mainWindow) setCompact(false); });
  app.on('before-quit', event => { if (!quitting && mainWindow) { event.preventDefault(); void confirmExit(); } });
  app.whenReady().then(() => {
    if (startupError) throw startupError;
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
    manager = new WorkspaceManager({ createBridge: (_id, stateDir, legacy) => {
      const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend/tmis-worker') :
        (process.env.TMIS_PYTHON || path.join(root, '.venv/bin/python'));
      const args = app.isPackaged ? [] : ['-u', path.join(root, 'tmis_worker.py')];
      return new WorkerBridge(spawn(executable, args, { cwd: app.isPackaged ? process.resourcesPath : root,
        env: { ...process.env, TMIS_STATE_DIR: stateDir, TMIS_BACKUP_LEGACY: legacy ? '1' : '0', PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' },
        stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true }));
    } });
    diagnostic = diagnosticLog(path.join(manager.directory, 'diagnostics'));
    app.on('child-process-gone', (_event, details) => diagnostic('child-process-gone', details));
    resourceTimer = setInterval(async () => {
      if (quitting || resourceBusy || process.platform !== 'linux') return;
      resourceBusy = true;
      try {
        const sample = systemResources(runtimeDirectory || app.getPath('temp'));
        await Promise.allSettled(manager.active().filter(e => e.online && e.bridge && !e.bridge.closed).map(async e => {
          e.state.resources = await e.bridge.call('resources', {}, 10000);
        }));
        diagnostic('resources', { ...sample, capacity,
          electron: app.getAppMetrics().map(p => ({ pid: p.pid, type: p.type, memory: p.memory })),
          workspaces: manager.active().map(e => ({ id: e.meta.id, resources: e.state.resources })) });
        const proposed = pressureCapacity(sample);
        if (proposed < capacity) { capacity = proposed; healthySamples = 0; }
        else if (proposed > capacity && ++healthySamples >= 12) { capacity = Math.min(proposed, capacity + 1); healthySamples = 0; }
        else if (proposed === capacity) healthySamples = 0;
        const candidates = manager.active().filter(e => e.online && (e.state.session_ready || e.state.resources?.parked))
          .sort((a, b) => Number(!!b.state.run_requested) - Number(!!a.state.run_requested));
        await Promise.allSettled(candidates.map((e, i) => {
          const hold = i >= capacity;
          if (!!e.state.resource_hold === hold) return Promise.resolve();
          return e.bridge.call('resource_gate', { hold, reason: '系统内存、共享内存或临时磁盘空间达到保护阈值' }, 10000);
        }));
        broadcast({ event: 'resources', data: { ...sample, capacity } });
      } catch (error) { diagnostic('monitor-error', { message: error.message }); }
      finally { resourceBusy = false; }
    }, 5000);
    resourceTimer.unref();
    manager.on('event', message => { if (!quitting || message.event !== 'offline') broadcast(message); });
    manager.on('change', scheduleRefresh);
    void manager.initialize().then(scheduleRefresh);
    mainWindow.once('ready-to-show', () => mainWindow.show());
    mainWindow.webContents.on('did-finish-load', () => {
      void refresh();
    });
  }).catch(error => { dialog.showErrorBox('启动失败', redact(error.message)); quitting = true; app.quit(); });
}
module.exports = { shutdown };
