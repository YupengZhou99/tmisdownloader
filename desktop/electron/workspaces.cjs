// Each workspace owns one worker, one queue lock and one browser. No shared task pool.
const { EventEmitter } = require('node:events');
const { randomUUID } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { redact } = require('./bridge.cjs');
const MAX_WORKSPACES = 4;
const emptyState = () => ({ tasks: [], batches: [], counts: {}, mode: 'idle', current: null,
  session_ready: false, headless: false, pending_headless: null, switching: false,
  importing: false, state_dir: '', version: '6.1.0' });
function stateDirectory(env = process.env, platform = process.platform, home = os.homedir()) {
  if (env.TMIS_STATE_DIR) return path.resolve(env.TMIS_STATE_DIR.replace(/^~(?=\/|$)/, home));
  if (platform === 'darwin') return path.join(home, 'Library/Application Support/TMISDLer');
  if (platform === 'win32') return path.join(env.LOCALAPPDATA || home, 'TMISDLer');
  return path.join(env.XDG_STATE_HOME || path.join(home, '.local/state'), 'tmis-dler');
}
function safeName(name) {
  if (typeof name !== 'string' || !name.trim() || name.trim().length > 40 ||
      /https?:\/\/|(?:token|ticket|password|authorization)\s*[:=]|[\x00-\x1f]/i.test(name))
    throw new Error('工作区名称应为 1～40 个字符，不要填写登录链接或凭据');
  return name.trim();
}
class WorkspaceManager extends EventEmitter {
  constructor({ directory = stateDirectory(), createBridge }) {
    super(); this.directory = directory; this.createBridge = createBridge;
    this.entries = new Map(); this.closing = false; this.importTail = Promise.resolve();
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.registryPath = path.join(directory, 'workspaces.json');
    if (fs.existsSync(this.registryPath)) {
      const registry = JSON.parse(fs.readFileSync(this.registryPath, 'utf8'));
      if (registry.version !== 1 || !Array.isArray(registry.items)) throw new Error('工作区配置不兼容；原文件未修改');
      for (const item of registry.items) {
        if (!/^(default|[a-f0-9]{32})$/.test(item.id) || this.entries.has(item.id) ||
            typeof item.root !== 'string' || (item.root && !path.isAbsolute(item.root))) throw new Error('工作区配置损坏；原文件未修改');
        this.addEntry({ id: item.id, name: safeName(item.name), root: item.root,
          archived: !!item.archived, folder: this.folder(item.id), browserPath: typeof item.browserPath === 'string' && (!item.browserPath || path.isAbsolute(item.browserPath)) ? item.browserPath : '' });
      }
      if (this.active().length > MAX_WORKSPACES) throw new Error('此版本最多支持 4 个活动工作区');
    } else {
      this.addEntry({ id: 'default', name: '默认工作区', root: '', archived: false, folder: this.folder('default'), browserPath: '' });
      this.save();
    }
  }
  folder(id) { return id === 'default' ? '默认工作区_default' : '工作区_' + id.slice(0, 8); }
  addEntry(meta) { this.entries.set(meta.id, { meta, state: emptyState(), logs: [], error: '', confirmed: false, bridge: null, online: false, refreshing: null, timer: null, dirty: false, previews: new Map(), busy: null }); }
  active() { return [...this.entries.values()].filter(e => !e.meta.archived); }
  get(id) {
    if (typeof id !== 'string' || !this.entries.has(id)) throw new Error('请指定有效的工作区');
    return this.entries.get(id);
  }
  save() {
    const temporary = this.registryPath + '.' + randomUUID() + '.tmp';
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try {
      fs.writeFileSync(fd, JSON.stringify({ version: 1, items: [...this.entries.values()].map(e => e.meta) }, null, 2));
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, this.registryPath);
  }
  changed() { this.emit('change'); }
  statePath(id) { return id === 'default' ? this.directory : path.join(this.directory, 'workspaces', id); }
  async initialize() { await Promise.allSettled(this.active().map(e => this.ensure(e.meta.id))); }
  async ensure(id) {
    if (this.closing) throw new Error('正在退出应用');
    const e = this.get(id);
    if (e.meta.archived) throw new Error('请先恢复此工作区');
    if (e.bridge && !e.bridge.closed) return e.bridge;
    const bridge = this.createBridge(id, this.statePath(id), id === 'default');
    e.bridge = bridge; e.error = ''; e.online = false; e.confirmed = false;
    e.previews.clear();
    bridge.on('event', message => {
      if (e.bridge !== bridge) return;
      if (message.event === 'ready') { e.online = true; e.error = ''; }
      if (message.event === 'log') e.logs = [...e.logs, { ...message, time: new Date().toLocaleTimeString('zh-CN', { hour12: false }) }].slice(-300);
      if (['error', 'fatal'].includes(message.event)) e.error = redact(message.message);
      if (message.event === 'offline') {
        e.online = false; e.confirmed = false; e.error = redact(message.message);
        const tasks = e.state.tasks.map(t => t.status === 'running' ? { ...t, status: 'interrupted', error: '此后台已退出，重新登录后可恢复' } : t);
        const counts = tasks.reduce((sum, t) => { sum[t.status] = (sum[t.status] || 0) + 1; return sum; }, {});
        e.state = { ...e.state, tasks, counts, session_ready: false, current: null, mode: 'waiting_login', switching: false, pending_headless: null };
      }
      this.emit('event', { ...message, workspaceId: id });
      if (['ready', 'changed', 'status', 'completed'].includes(message.event)) this.schedule(id);
      this.changed();
    });
    try { await this.refresh(id); }
    catch (error) { e.error = redact(error.message); this.changed(); throw error; }
    return bridge;
  }
  schedule(id) {
    const e = this.get(id); e.dirty = true;
    if (e.timer || this.closing) return;
    e.timer = setTimeout(() => { e.timer = null; void this.refresh(id).catch(() => {}); }, 120);
  }
  async refresh(id) {
    const e = this.get(id);
    if (!e.bridge || e.bridge.closed) return e.state;
    if (e.refreshing) { e.dirty = true; return e.refreshing; }
    const bridge = e.bridge; e.dirty = false;
    e.refreshing = (async () => {
      const state = await bridge.call('snapshot');
      if (bridge !== e.bridge || bridge.closed) return e.state;
      e.state = state; e.online = true;
      this.emit('event', { event: 'snapshot', workspaceId: id, data: { ...state, logs: e.logs } });
      this.changed(); return state;
    })().finally(() => { e.refreshing = null; if (e.dirty && !this.closing) this.schedule(id); });
    return e.refreshing;
  }
  snapshot() {
    const workspaces = [...this.entries.values()].map(e => ({ ...e.meta, online: e.online,
      confirmed: e.confirmed, error: e.error, busy: e.busy, worker_pid: e.online ? e.bridge?.child.pid : null, state: e.state, logs: e.logs }));
    return { version: '6.1.0', limit: MAX_WORKSPACES, workspaces, closing: this.closing };
  }
  create(name = '新工作区') {
    if (this.closing || this.active().length >= MAX_WORKSPACES) throw new Error('最多同时保留 4 个活动工作区；可先归档不用的队列');
    const id = randomUUID().replaceAll('-', '');
    this.addEntry({ id, name: safeName(name), root: '', archived: false, folder: this.folder(id), browserPath: '' });
    this.save(); this.changed(); return id;
  }
  update(id, data) {
    const e = this.get(id);
    if (!data || Object.keys(data).some(k => !['name', 'root', 'browserPath'].includes(k))) throw new Error('工作区设置无效');
    const next = { ...e.meta };
    if ('name' in data) next.name = safeName(data.name);
    for (const key of ['root', 'browserPath']) if (key in data) {
      if (typeof data[key] !== 'string' || (data[key] && !path.isAbsolute(data[key]))) throw new Error('请选择有效的本机路径');
      next[key] = data[key];
    }
    e.meta = next; this.save(); this.changed(); return next;
  }
  async confirm(ids) {
    const pending = this.active().filter(e => !e.confirmed);
    if (!Array.isArray(ids) || new Set(ids).size !== ids.length || ids.length !== pending.length ||
        pending.some(e => !ids.includes(e.meta.id))) throw new Error('请确认本轮全部会话；失败项需重试或明确移出本轮');
    await Promise.all(pending.map(e => this.refresh(e.meta.id)));
    const current = this.active().filter(e => !e.confirmed);
    if (current.length !== pending.length || current.some(e => !ids.includes(e.meta.id))) throw new Error('本轮会话列表已变化，请重新确认');
    if (!pending.length || pending.some(e => !e.online || !e.state.session_ready || e.busy)) throw new Error('本轮所有会话登录成功后，才能配置参数');
    pending.forEach(e => { e.confirmed = true; }); this.changed();
  }
  requireConfirmed(e) {
    if (!e.confirmed) throw new Error('请先完成本轮全部登录，并点击“确认并配置任务”');
  }
  async login(id, data) {
    const e = this.get(id);
    if (e.busy) throw new Error('此工作区正在操作，请稍候');
    if (typeof data.url !== 'string' || data.url.length > 16384 || !/^https?:\/\/\S+$/i.test(data.url)) throw new Error('请输入完整的 HTTP(S) 登录链接');
    e.busy = 'login'; e.error = ''; this.changed();
    try {
      const bridge = await this.ensure(id);
      const result = await bridge.call('login', { url: data.url, browser_path: data.browser_path ?? e.meta.browserPath });
      await this.refresh(id);
      if (!e.state.session_ready) throw new Error(e.error || '尚未进入 TMIS 工作界面，请检查此登录链接');
      return result;
    } finally { e.busy = null; this.changed(); }
  }
  otherDuplicates(id, fingerprints) {
    return [...this.entries.values()].filter(e => e.meta.id !== id &&
      e.state.batches.some(b => fingerprints.includes(b.fingerprint))).map(e => e.meta.name);
  }
  async call(id, command, data = {}) {
    if (this.closing && command !== 'snapshot') throw new Error('正在退出应用，不再接受新操作');
    const e = this.get(id);
    if (e.meta.archived) throw new Error('此工作区已归档');
    if (e.busy === 'archive' && command !== 'snapshot') throw new Error('此工作区正在归档');
    if (command === 'login') return this.login(id, data);
    if (command === 'snapshot') { await this.refresh(id); return { ...e.state, logs: e.logs }; }
    if (['preview', 'import_preview', 'start', 'retry'].includes(command)) this.requireConfirmed(e);
    if (!e.bridge || e.bridge.closed) throw new Error('此工作区后台已停止；请重新登录以恢复，其他工作区不受影响');
    if (command === 'import_preview') return this.import(id, data);
    const result = await e.bridge.call(command, data);
    if (command === 'preview') for (const p of result) if (p.id) {
      const duplicates = this.otherDuplicates(id, [p.fingerprint]);
      e.previews.set(p.id, p.fingerprint);
      p.duplicate_workspaces = duplicates;
      p.duplicate = !!p.duplicate || duplicates.length > 0;
    }
    if (command === 'discard_preview') for (const key of data.ids || []) e.previews.delete(key);
    if (command === 'disconnect') e.confirmed = false;
    await this.refresh(id); this.changed(); return result;
  }
  async import(id, data) {
    // Serialize the *commit* of imports, not the downloads, so duplicate checks
    // against another workspace cannot race simultaneous confirmations.
    const previous = this.importTail;
    let release; this.importTail = new Promise(resolve => { release = resolve; });
    await previous;
    try {
      if (this.closing) throw new Error('正在退出应用，未提交的导入已取消');
      const e = this.get(id); this.requireConfirmed(e);
      if (!Array.isArray(data.ids) || data.ids.some(key => !e.previews.has(key))) throw new Error('预览不属于此工作区或已经失效');
      const names = this.otherDuplicates(id, data.ids.map(key => e.previews.get(key)));
      if (names.length && data.allow_duplicate !== true) throw new Error('参数已存在于工作区“' + names.join('、') + '”；请重新检查并确认重复导入');
      const root = path.resolve(data.root);
      if (!fs.statSync(root).isDirectory()) throw new Error('保存根目录不存在');
      const scopedRoot = path.join(root, e.meta.folder);
      fs.mkdirSync(scopedRoot, { recursive: true, mode: 0o700 });
      const result = await e.bridge.call('import_preview', { ...data, root: scopedRoot });
      // The backend consumes successful preview IDs. Retain failed previews only.
      const failed = new Set(result.errors.map(x => x.id));
      for (const key of data.ids) if (!failed.has(key)) e.previews.delete(key);
      if (!e.meta.root && result.accepted.length) this.update(id, { root });
      await this.refresh(id); return result;
    } finally { release(); }
  }
  async all(command) {
    if (!['start', 'pause'].includes(command)) throw new Error('未知批量操作');
    const results = await Promise.all(this.active().map(async e => {
      try {
        if (command === 'start' && (!e.confirmed || !e.state.session_ready)) throw new Error('未确认登录或需要重新登录，已跳过');
        await this.call(e.meta.id, command); return { id: e.meta.id, name: e.meta.name, ok: true };
      } catch (error) { return { id: e.meta.id, name: e.meta.name, ok: false, error: redact(error.message) }; }
    }));
    return results;
  }
  async stop(id) {
    const e = this.get(id); const bridge = e.bridge;
    clearTimeout(e.timer); e.timer = null;
    if (bridge && !bridge.closed) {
      try {
        await bridge.call('shutdown', {}, 30000);
        bridge.child.stdin.end();
        await new Promise(resolve => {
          if (bridge.closed) return resolve();
          const timer = setTimeout(resolve, 10000);
          bridge.child.once('exit', () => { clearTimeout(timer); resolve(); });
        });
      } catch {}
      if (!bridge.closed) bridge.child.kill('SIGTERM');
    }
    e.online = false; e.confirmed = false; e.state.session_ready = false;
  }
  async archive(id) {
    const e = this.get(id);
    if (e.busy) throw new Error('此工作区正在操作，请稍候');
    e.busy = 'archive'; this.changed();
    try {
      await this.refresh(id);
      if (e.state.current || e.state.importing || e.state.switching || e.state.pending_headless !== null || ['running', 'pausing', 'logging_in'].includes(e.state.mode))
        throw new Error('请先暂停并等待此队列当前操作结束，再归档');
      await this.stop(id); e.meta.archived = true; this.save();
    } finally { e.busy = null; this.changed(); }
  }
  restore(id) {
    const e = this.get(id);
    if (!e.meta.archived) throw new Error('此工作区已经处于活动状态');
    if (this.active().length >= MAX_WORKSPACES) throw new Error('最多 4 个活动工作区');
    e.meta.archived = false; e.confirmed = false;
    this.save(); this.changed();
  }
  async shutdown() {
    this.closing = true; this.changed();
    await Promise.allSettled([...this.entries.keys()].map(id => this.stop(id)));
  }
}
module.exports = { WorkspaceManager, stateDirectory, safeName, MAX_WORKSPACES };
