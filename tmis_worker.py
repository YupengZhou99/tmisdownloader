"""Electron's private JSON-lines worker. No listening port or credential files."""
import asyncio
from collections import Counter
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

from tmis_queue import QueueStore, DuplicateBatch, parse_parameter_file
from tmis_runtime import redact_urls
from tmis_service import BrowserSession, QueueController


def load_engine():
    # Keep the proven engine and literal report configs shared with the legacy
    # app and parameter generator. Importing it does NOT instantiate a Tk window.
    source = Path(__file__).parent / 'TMIS数据批量抓取工具V5.0_副本.py'
    spec = importlib.util.spec_from_file_location('tmis_legacy_engine', source)
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
    return module


class DesktopAPI:
    def __init__(self, controller, store):
        self.controller, self.store = controller, store
        self.previews = {}

    def snapshot(self):
        c = self.controller
        page = self.store.task_page()
        current = self.store.task_page(task_id=c.current)['tasks'] if c.current else []
        return dict(tasks=page['tasks'], task_total=page['total'], current_task=current[0] if current else None,
                    batches=self.store.batches(limit=200), counts=self.store.counts(),
                    mode=c.mode, current=c.current, session_ready=c.session.ready,
                    headless=c.session.headless, pending_headless=c.pending_headless,
                    switching=bool(c.mode_job), importing=bool(c.import_jobs),
                    resources=getattr(c.session, 'resources', lambda: {})(), resource_hold=c.resource_hold, run_requested=c.run_requested,
                    state_dir=str(self.store.directory), version='6.2.0')

    async def preview(self, paths, options):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 30:
            raise ValueError('一次请选择 1～30 份参数表')
        c, result = self.controller, []
        self.previews = {key: value for key, value in self.previews.items()
                         if time.monotonic() - value[0] < 900}
        known = {b['fingerprint'] for b in self.store.batches()}
        for path in paths:
            try:
                if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).suffix.lower() not in ('.xlsx', '.xls'):
                    raise ValueError('请选择本机 Excel 参数表')
                plan = await asyncio.to_thread(parse_parameter_file, path, c.configs, c.mapping, options, c.engine_factory)
                if sum(len(p[1]['rows']) for p in self.previews.values()) + len(plan['rows']) > 50000:
                    raise ValueError('暂存预览超过 50000 条，请先导入或关闭预览后重试')
                key = uuid4().hex
                self.previews[key] = (time.monotonic(), plan)
                dates = [r['params']['pStartDate'] for r in plan['rows']]
                ends = [r['params']['pEndDate'] for r in plan['rows']]
                result.append(dict(id=key, name=plan['source_name'], fingerprint=plan['fingerprint'], count=len(plan['rows']),
                                   kinds=dict(Counter(r['kind'] for r in plan['rows'])),
                                   start=min(dates), end=max(ends), duplicate=plan['fingerprint'] in known,
                                   sample=plan['rows'][:3]))
                known.add(plan['fingerprint'])
            except Exception as error:
                result.append(dict(name=Path(str(path)).name, error=redact_urls(error)))
        return result

    async def import_preview(self, ids, root, allow_duplicate=False):
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise ValueError('请选择有效的预览参数表')
        if not isinstance(allow_duplicate, bool):
            raise ValueError('重复确认必须为开关值')
        if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
            raise ValueError('请先选择有效的下载目录')
        accepted, errors = [], []
        # All plans validated before mutating anything. A preview is immutable:
        # modifying/deleting the original workbook never changes these tasks.
        for key in ids:
            if key not in self.previews or time.monotonic() - self.previews[key][0] >= 900:
                raise ValueError('预览已失效，请重新选择参数表')
        for key in ids:
            plan = self.previews[key][1]
            try:
                batch = self.store.add_batch(plan, root, allow_duplicate)
                self.controller.export(batch)
                accepted.append(dict(id=batch, name=plan['source_name'], count=len(plan['rows'])))
                del self.previews[key]
            except DuplicateBatch:
                errors.append(dict(id=key, name=plan['source_name'], error='重复参数表：请明确勾选允许重复导入'))
            except Exception as error:
                errors.append(dict(id=key, name=plan['source_name'], error=redact_urls(error)))
        self.controller.emit('changed')
        self.controller.wake.set()
        return dict(accepted=accepted, errors=errors)

    async def dispatch(self, command, data):
        if not isinstance(data, dict):
            raise ValueError('请求参数必须为对象')
        c = self.controller
        fields = {
            'snapshot': (), 'preview': ('paths', 'options'), 'import_preview': ('ids', 'root', 'allow_duplicate'),
            'discard_preview': ('ids',), 'details': ('id',), 'login': ('url', 'browser_path', 'headless'),
            'tasks': ('offset', 'limit', 'search', 'kind', 'batch', 'failed'), 'clear_completed': (),
            'has_fingerprints': ('fingerprints',), 'fingerprints': (),
            'resource_gate': ('hold', 'reason'), 'resources': (),
            'start': (), 'pause': (), 'disconnect': (), 'retry': ('ids',), 'remove': ('ids',),
            'request_mode': ('headless',), 'cancel_mode': (), 'browser_window': ('action',), 'shutdown': (),
        }
        if command not in fields or any(k not in fields[command] for k in data):
            raise ValueError('不支持的操作或参数')
        if c.closing and command != 'snapshot':
            raise ValueError('服务正在退出')
        if 'ids' in data and (not isinstance(data['ids'], list) or len(data['ids']) > 50000
                              or any(not isinstance(x, str) for x in data['ids'])):
            raise ValueError('任务选择无效')
        if command == 'snapshot':
            return self.snapshot()
        if command == 'tasks':
            return self.store.task_page(**data)
        if command == 'resources':
            return getattr(c.session, 'resources', lambda: {})()
        if command == 'clear_completed':
            count = self.store.clear_completed()
            c.emit('changed')
            c.log(f'已清除 {count} 条成功任务的队列显示；报表与历史记录保留')
            return count
        if command == 'has_fingerprints':
            values = data.get('fingerprints', [])
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(v, str) or len(v) > 256 for v in values):
                raise ValueError('参数指纹无效')
            with self.store.lock:
                return any(self.store.connection.execute('SELECT 1 FROM batches WHERE fingerprint=?', (v,)).fetchone() for v in values)
        if command == 'fingerprints':
            with self.store.lock:
                return [r[0] for r in self.store.connection.execute('SELECT DISTINCT fingerprint FROM batches')]
        if command == 'preview':
            return await self.preview(**data)
        if command == 'import_preview':
            return await self.import_preview(**data)
        if command == 'discard_preview':
            for key in data.get('ids', []):
                self.previews.pop(key, None)
            return
        if command == 'details':
            if not isinstance(data.get('id'), str) or len(data['id']) > 128:
                raise ValueError('任务标识无效')
            tasks = self.store.tasks(task_id=data['id'])
            if not tasks:
                raise ValueError('任务不存在')
            task = tasks[0]
            return dict(task=task, history=self.store.history(task['id']))
        if command == 'login':
            if not isinstance(data.get('url'), str) or len(data['url']) > 16384:
                raise ValueError('请输入有效的登录链接')
            if not isinstance(data.get('browser_path', ''), str):
                raise ValueError('浏览器路径无效')
            if 'headless' in data and type(data['headless']) is not bool:
                raise ValueError('浏览器模式无效')
        return await getattr(c, command)(**data)


async def run():
    protocol = sys.stdout
    sys.stdout = sys.stderr  # incidental library prints cannot corrupt IPC
    def send(payload):
        try:
            protocol.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
            protocol.flush()
        except (BrokenPipeError, OSError):
            pass
    store = QueueStore(backup_legacy=os.environ.get('TMIS_BACKUP_LEGACY') == '1')
    app = load_engine()
    def emit(event, **data):
        send(dict(event=event, **data))
    session = BrowserSession(app.ReportEngine, lambda message, level='INFO':
                             emit('log', text=redact_urls(message), level=level))
    controller = QueueController(store, session, app.REPORT_CONFIGS, app.COLUMN_MAPPING, app.ReportEngine, emit)
    api = DesktopAPI(controller, store)
    loop, incoming = asyncio.get_running_loop(), asyncio.Queue()
    def reader():
        try:
            for line in sys.stdin:
                if len(line) > 2 * 1024 * 1024:
                    loop.call_soon_threadsafe(incoming.put_nowait, '')
                    continue
                loop.call_soon_threadsafe(incoming.put_nowait, line)
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(incoming.put_nowait, None)
    threading.Thread(target=reader, name='desktop-stdin', daemon=True).start()
    jobs = set()
    async def request(line):
        request_id = None
        try:
            message = json.loads(line)
            request_id = message.get('id')
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                raise ValueError('请求标识无效')
            result = await api.dispatch(message.get('command'), message.get('data', {}))
            send(dict(id=request_id, result=result))
        except Exception as error:
            send(dict(id=request_id, error=redact_urls(error)))
    consumer = asyncio.create_task(controller.serve())
    send(dict(event='ready', version='6.2.0'))
    try:
        while not controller.closing and not consumer.done():
            reading = asyncio.create_task(incoming.get())
            done, _ = await asyncio.wait((reading, consumer), return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                break
            line = reading.result()
            if line is None:
                break
            job = asyncio.create_task(request(line))
            jobs.add(job)
            job.add_done_callback(jobs.discard)
    finally:
        await controller.shutdown()
        await asyncio.gather(consumer, return_exceptions=True)
        if jobs:
            await asyncio.gather(*list(jobs), return_exceptions=True)
        store.close()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as error:
        print(redact_urls(error), file=sys.stderr)
        raise SystemExit(1)
