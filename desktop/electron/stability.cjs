// Bounded diagnostics and IPC. No URLs, report content or authentication state.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { redact } = require('./bridge.cjs');
function redactValue(value) {
  if (typeof value === 'string') return redact(value);
  if (Array.isArray(value)) return value.map(redactValue);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, redactValue(v)]));
  return value;
}

function prepareRuntime(app, env = process.env, platform = process.platform) {
  if (platform !== 'linux') return null;
  const base = env.TMIS_RUNTIME_DIR || path.join(env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'), 'tmis-dler', 'runtime');
  fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(base);
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid()) throw new Error('浏览器临时目录必须为当前用户拥有的独立目录');
  const disk = fs.statfsSync(base);
  if ([0x01021994, 0x858458f6].includes(disk.type)) throw new Error('浏览器临时目录位于内存文件系统。请用 TMIS_RUNTIME_DIR 指定磁盘上的目录。');
  if (disk.bavail * disk.bsize < 512 * 1024**2) throw new Error('浏览器临时目录可用磁盘空间不足 512 MB，请先释放空间');
  const directory = fs.mkdtempSync(path.join(base, 'run-'));
  fs.chmodSync(directory, 0o700);
  env.TMPDIR = directory; env.TMP = directory; env.TEMP = directory;
  app.setPath('temp', directory);
  app.commandLine.appendSwitch('disable-dev-shm-usage');
  return directory;
}

function cleanupRuntime(directory) {
  if (!directory || !/^run-[a-zA-Z0-9]+$/.test(path.basename(directory))) return;
  try {
    const info = fs.lstatSync(directory);
    if (info.isDirectory() && !info.isSymbolicLink() && info.uid === process.getuid())
      fs.rmSync(directory, { recursive: true, force: true });
  } catch {} // Only this run's private temporary directory; never /dev/shm.
}

function diagnosticLog(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const file = path.join(directory, 'stability.jsonl');
  return (event, details = {}) => {
    try {
      if (fs.existsSync(file) && fs.statSync(file).size > 2 * 1024**2) {
        const oldest = file + '.3';
        if (fs.existsSync(oldest)) fs.unlinkSync(oldest);
        for (let i = 2; i >= 0; i--) {
          const from = i ? file + '.' + i : file;
          if (fs.existsSync(from)) fs.renameSync(from, file + '.' + (i + 1));
        }
      }
      fs.appendFileSync(file, JSON.stringify({ time: new Date().toISOString(), event,
        details: redactValue(details) }) + '\n', { mode: 0o600 });
    } catch {} // Disk-full diagnostics must not take down the queue manager.
  };
}

function filesystemUse(directory) {
  try {
    const s = fs.statfsSync(directory);
    return { size: s.blocks * s.bsize, free: s.bavail * s.bsize,
      used: (s.blocks - s.bfree) * s.bsize, inodes: s.files, free_inodes: s.ffree };
  } catch { return null; }
}
function systemResources(directory, proc = '/proc') {
  let available = null, total = null;
  try {
    const text = fs.readFileSync(path.join(proc, 'meminfo'), 'utf8');
    const get = name => { const m = text.match(new RegExp('^' + name + ':\\s+(\\d+)', 'm')); return m ? Number(m[1]) * 1024 : null; };
    available = get('MemAvailable'); total = get('MemTotal');
  } catch {}
  return { available, total, shm: filesystemUse('/dev/shm'), disk: filesystemUse(directory), main_rss: process.memoryUsage().rss };
}
function pressureCapacity(sample) {
  const gb = 1024**3, shm = sample.shm;
  const ratio = shm && shm.size ? shm.used / shm.size : 0;
  if ((sample.disk && sample.disk.free < 512 * 1024**2) || (shm && shm.free_inodes === 0) ||
      ratio >= .9 || (sample.available !== null && sample.available < gb)) return 0;
  if (ratio >= .8 || (sample.available !== null && sample.available < 2 * gb)) return 1;
  if (sample.available !== null && sample.available < 3 * gb) return 2;
  return 4;
}

class BoundedPublisher {
  constructor(win, { compact = false, delay = 100 } = {}) {
    this.win = win; this.compact = compact; this.delay = delay;
    this.pending = new Map(); this.logs = []; this.serial = 0; this.inflight = null; this.timer = null;
  }
  push(message) {
    if (this.win.isDestroyed() || !this.win.isVisible()) return;
    if (this.compact && !['workspaces', 'window', 'closing', 'navigate', 'resources'].includes(message.event)) return;
    if (message.event === 'log') this.logs = [...this.logs, message].slice(-100);
    else {
      if (this.compact && message.event === 'workspaces') message = { ...message, data: { ...message.data,
        workspaces: message.data.workspaces.map(w => ({ ...w, logs: [], state: { ...w.state, tasks: [], batches: [] } })) } };
      this.pending.set(message.event + ':' + (message.workspaceId || ''), message);
    }
    this.schedule();
  }
  schedule() {
    if (this.timer || this.inflight !== null) return;
    this.timer = setTimeout(() => { this.timer = null; this.flush(); }, this.delay);
  }
  flush() {
    if (this.inflight !== null || (!this.pending.size && !this.logs.length)) return;
    if (this.win.isDestroyed() || !this.win.isVisible()) { this.reset(); return; }
    const items = [...this.pending.values(), ...this.logs]; this.pending.clear(); this.logs = [];
    this.inflight = ++this.serial;
    try { this.win.webContents.send('tmis:event', { event: 'bundle', seq: this.inflight, items }); }
    catch { this.reset(); }
  }
  ack(seq) { if (seq === this.inflight) { this.inflight = null; this.schedule(); } }
  reset() { clearTimeout(this.timer); this.timer = null; this.inflight = null; this.pending.clear(); this.logs = []; }
}
module.exports = { prepareRuntime, cleanupRuntime, diagnosticLog, systemResources, pressureCapacity, BoundedPublisher };
