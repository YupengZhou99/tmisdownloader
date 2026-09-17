const { EventEmitter } = require('node:events');
const { createInterface } = require('node:readline');
function redact(value) {
  return String(value).replace(/https?:\/\/[^\s"'<>]+/gi, '[登录链接已隐藏]')
    .replace(/\b(?:access_token|refresh_token|token|authorization|cookie|password)\s*[:=]\s*[^\s,;]+/gi, '[凭据已隐藏]');
}
class WorkerBridge extends EventEmitter {
  constructor(child) {
    super();
    this.child = child;
    this.pending = new Map();
    this.serial = 0;
    this.closed = false;
    const reader = createInterface({ input: child.stdout });
    reader.on('line', line => {
      try {
        const message = JSON.parse(line);
        if (Number.isInteger(message.id)) {
          const pending = this.pending.get(message.id);
          if (pending) {
            clearTimeout(pending.timer);
            this.pending.delete(message.id);
            if (message.error) pending.reject(new Error(redact(message.error)));
            else pending.resolve(message.result);
          }
        } else if (message.event) this.emit('event', message);
      } catch { this.emit('event', { event: 'log', level: 'WARN', text: '后台返回了无法解析的消息' }); }
    });
    let remainder = '';
    child.stderr.on('data', data => {
      remainder += data.toString();
      const lines = remainder.split('\n');
      remainder = lines.pop().slice(-4000);
      for (const line of lines) if (line.trim()) this.emit('event', { event: 'log', level: 'WARN', text: redact(line).slice(0, 2000) });
    });
    child.stdin.on('error', () => this.fail('后台通信已中断，任务保存在本机'));
    child.on('error', error => this.fail('后台启动失败：' + redact(error.message)));
    child.on('exit', (code, signal) => this.fail('后台服务已退出（' + (signal || code) + '），任务保存在本机'));
  }
  fail(message) {
    if (this.closed) return;
    this.closed = true;
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(new Error(message)); }
    this.pending.clear();
    this.emit('event', { event: 'offline', message });
  }
  call(command, data = {}, timeout = 180000) {
    if (this.closed) return Promise.reject(new Error('后台服务未运行，请重启应用恢复任务'));
    const id = ++this.serial;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('操作响应超时；请查看当前状态，不要重复导入'));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, command, data }) + '\n', error => {
        if (error) this.fail('后台通信失败，任务保存在本机');
      });
    });
  }
}
module.exports = { WorkerBridge, redact };
