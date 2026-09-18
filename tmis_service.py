"""Persistent browser session and serial queue consumer, independent of Tk."""

import asyncio
from collections import Counter
import contextlib
import json
from pathlib import Path
import queue
import re
import threading
import time
import psutil
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from tmis_queue import DuplicateBatch, parse_parameter_file
from tmis_runtime import (
    browser_environment, browser_launch_options, bundled_browser_path,
    redact_urls, validate_login_url, FormInteractionError,
)


class SessionExpired(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, engine_factory, log, headless=False, query_timeout=360):
        self.engine = engine_factory()
        self.engine.log = log
        self.engine.current_nav_type = None
        self.log = log
        self.headless = headless
        self.query_timeout = query_timeout
        self.ready = False
        self.pw = self.browser = self.context = self.page = None
        self.browser_path = None
        self.auth_lost = False
        self.origin = None
        self.previous_task = None
        self.switching = False
        self.state_callback = lambda: None
        self.completed_since_launch = 0
        self.launched_at = time.monotonic()
        self.parked_state = None
        self._resource_time = 0
        self._resources = {}
        self._owned_driver = None

    async def _start_playwright(self):
        before = {p.pid for p in psutil.Process().children()}
        self.pw = await async_playwright().start()
        created = [p for p in psutil.Process().children() if p.pid not in before]
        self._owned_driver = created[0] if len(created) == 1 else None

    def resources(self):
        if time.monotonic() - self._resource_time >= 5:
            rss, children = 0, 0
            try:
                owned = self._owned_driver.children(recursive=True) + [self._owned_driver] if self._owned_driver and self._owned_driver.is_running() else []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                owned = []
            for process in owned:
                try:
                    rss += process.memory_info().rss
                    children += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._resources = dict(worker_rss=psutil.Process().memory_info().rss, browser_tree_rss=rss, child_processes=children,
                                   completed_since_launch=self.completed_since_launch, parked=self.parked_state is not None)
            self._resource_time = time.monotonic()
        return dict(self._resources, completed_since_launch=self.completed_since_launch,
                    parked=self.parked_state is not None)

    def needs_recycle(self):
        return self.ready and self.completed_since_launch > 0 and (self.completed_since_launch >= 10 or
            time.monotonic() - self.launched_at >= 1200 or self.resources().get('browser_tree_rss', 0) > 1536 * 1024**2)

    def task_finished(self):
        self.completed_since_launch += 1

    def _disconnected(self):
        self.ready = False
        if not self.switching:
            self.state_callback()

    def _response(self, response):
        # A report-specific 403 is not automatically an expired login.
        if response.status == 401 and urlsplit(response.url).netloc == self.origin:
            self.auth_lost = True

    async def login(self, url, browser_path='', headless=None):
        url = validate_login_url(url)  # kept in memory only for this navigation
        if self.parked_state is not None:
            await self.close()  # A fresh URL supersedes the suspended credentials.
        if headless is not None:
            if type(headless) is not bool:
                raise ValueError('浏览器模式无效')
            if headless != self.headless and self.browser:
                await self.close()
            self.headless = headless
        self.ready = False
        if self.browser and (not self.browser.is_connected() or browser_path != self.browser_path):
            await self.close()
        if self.pw is None:
            await self._start_playwright()
        if self.browser is None:
            kwargs = browser_launch_options(browser_path, bundled_browser_path())
            kwargs.update(headless=self.headless, env=browser_environment(), args=[
                '--ignore-certificate-errors', '--ignore-ssl-errors', '--no-first-run',
                '--disable-popup-blocking', '--start-maximized',
            ])
            self.browser = await self.pw.chromium.launch(**kwargs)
            self.browser.on('disconnected', self._disconnected)
            self.browser_path = browser_path
            self.context = await self.browser.new_context(ignore_https_errors=True,
                viewport={'width': 1440, 'height': 1000} if self.headless else None, accept_downloads=True)
            self.context.on('response', self._response)
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            self.page.on('close', self._disconnected)
        self.auth_lost = False
        self.origin = urlsplit(url).netloc
        self.engine.current_nav_type = None
        self.previous_task = None
        try:
            await self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except BrowserTimeout:
            self.log('登录页加载较慢，继续检查是否已经进入工作界面', 'WARN')
        await self.page.get_by_text('固定报表', exact=True).first.wait_for(state='visible', timeout=30000)
        self.origin = urlsplit(self.page.url).netloc
        self.auth_lost = False
        self.ready = True
        self.completed_since_launch = 0
        self.launched_at = time.monotonic()
        self._resource_time = 0
        self.log('已进入 TMIS 工作界面；可添加参数表或开始/继续队列', 'SUCCESS')

    async def check(self):
        if not self.ready or not self.browser or not self.browser.is_connected() or not self.page or self.page.is_closed():
            raise SessionExpired('浏览器或登录会话不可用，请重新登录')
        if self.auth_lost:
            self.ready = False
            raise SessionExpired('服务器返回未登录状态，请重新登录后继续队列')
        for frame in self.page.frames:
            try:
                password = frame.locator('input[type="password"]:visible')
                expired = frame.get_by_text(re.compile(r'登录(?:已)?过期|会话(?:已)?(?:失效|超时)|请重新登录|登录超时'))
                if await password.count() or await expired.first.is_visible():
                    self.ready = False
                    raise SessionExpired('页面要求重新登录，已暂停队列')
            except SessionExpired:
                raise
            except Exception:
                # Frames may disappear during normal navigation.
                continue

    async def _watch_auth(self):
        while True:
            await asyncio.sleep(1)
            await self.check()
            await self.check_window()

    async def check_window(self):
        if self.headless or not self.ready:
            return
        client = await self.context.new_cdp_session(self.page)
        try:
            window = await client.send('Browser.getWindowForTarget')
            if window.get('bounds', {}).get('windowState') == 'minimized':
                raise FormInteractionError('有头浏览器已最小化，暂停此队列以免误判参数；请还原窗口或切换无头模式后继续')
        finally:
            await client.detach()

    async def execute(self, task, stage):
        await self.check()
        await self.check_window()
        operation = asyncio.create_task(self._execute(task, stage))
        watcher = asyncio.create_task(self._watch_auth())
        try:
            done, _ = await asyncio.wait((operation, watcher), return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                return await operation
            if watcher in done:
                await watcher
            return await operation
        finally:
            for work in (operation, watcher):
                if not work.done():
                    work.cancel()
            await asyncio.gather(operation, watcher, return_exceptions=True)

    async def _execute(self, task, stage):
        engine, row, kind = self.engine, task['params'], task['kind']
        engine.run_options = dict(task['options'])
        # A failed post-processing attempt already owns a complete download.
        # Retry only that step; do not download again or overwrite another file.
        if (task.get('previous_stage') in ('数据后处理', '下载已保存') and task.get('saved_path')
                and Path(task['saved_path']).is_file() and Path(task['saved_path']).stat().st_size > 0):
            if task['options']['postprocess']:
                stage('数据后处理', task['saved_path'])
                await asyncio.to_thread(engine._process_excel_data, task['saved_path'], row, kind)
            return task['saved_path']
        previous = self.previous_task
        # A new file must not inherit form selections from the preceding file.
        # Blank parameters retain fresh-page defaults, not a previous row's value.
        clear_needed = previous and any(previous['params'].get(key) and not row.get(key)
                                       for key in set(previous['params']) | set(row))
        if previous is None or previous['batch_id'] != task['batch_id'] or clear_needed:
            stage('重置查询页面')
            await self.recover()
        self.previous_task = task
        stage('打开查询页')
        await engine._navigate_sidebar_smart(self.page, kind)
        stage('填写参数')
        await engine._fill_form(self.page, row, kind)
        stage('等待查询')
        await engine._click_query_and_wait(self.page, kind, timeout_seconds=self.query_timeout)
        await self.check()
        stage('下载报表')
        saved = await engine._export_and_save(self.page, task['output_dir'], task['output_name'], kind,
            engine._should_keep_original_name(row), on_saved=lambda path: stage('下载已保存', path))
        stage('下载已保存', saved)
        if task['options']['postprocess']:
            stage('数据后处理', saved)
            await asyncio.to_thread(engine._process_excel_data, saved, row, kind)
        return saved

    async def recover(self):
        """Reload the owned page to discard late frames from a timed-out query."""
        await self.check()
        self.engine.current_nav_type = None
        self.previous_task = None
        await self.page.reload(wait_until='domcontentloaded', timeout=30000)
        await self.page.get_by_text('固定报表', exact=True).first.wait_for(state='visible', timeout=15000)
        await self.check()

    async def set_window(self, action):
        if action not in ('minimize', 'restore'):
            raise ValueError('未知浏览器窗口操作')
        await self.check()
        if self.headless:
            raise ValueError('当前为无头模式，没有可最小化或还原的浏览器窗口')
        client = await self.context.new_cdp_session(self.page)
        try:
            window = await client.send('Browser.getWindowForTarget')
            await client.send('Browser.setWindowBounds', {
                'windowId': window['windowId'],
                'bounds': {'windowState': 'minimized' if action == 'minimize' else 'normal'},
            })
            if action == 'restore':
                await self.page.bring_to_front()
        finally:
            await client.detach()

    async def switch_mode(self, headless, force=False):
        """Only called at a task boundary. Credentials never leave memory."""
        if headless == self.headless and not force:
            return
        if self.parked_state is not None:
            self.headless = headless
            return
        if not self.browser or not self.ready:
            if self.browser:
                await self.close()
            self.headless = headless
            return
        await self.park()
        self.headless = headless
        await self.unpark()
        self.log('浏览器已回收重建，登录状态检查通过' if force else '浏览器模式已切换，登录状态检查通过', 'SUCCESS')

    async def park(self):
        if self.parked_state is not None or not self.ready:
            return
        await self.check()
        storage = await self.context.storage_state()
        session_storage = {}
        for frame in self.page.frames:
            try:
                origin, values = await frame.evaluate(
                    '() => [location.origin, Object.fromEntries(Object.entries(sessionStorage))]')
                if origin != 'null':
                    session_storage[origin] = values
            except Exception:
                pass
        # Do not replay a one-use login token. Navigate to the current work route
        # with its non-secret parameters; applications requiring other state ask
        # for a fresh link instead of silently discarding queued tasks.
        current = urlsplit(self.page.url)
        secret = re.compile(r'token|ticket|authorization|password|secret|^code$', re.I)
        query = urlencode([(k, v) for k, v in parse_qsl(current.query) if not secret.search(k)])
        fragment = current.fragment
        if '?' in fragment:
            route, args = fragment.split('?', 1)
            fragment = route + '?' + urlencode([(k, v) for k, v in parse_qsl(args) if not secret.search(k)])
        work_url = urlunsplit((current.scheme, current.netloc, current.path, query, fragment))
        browser_path = self.browser_path
        self.switching = True
        try:
            await self.close()
            self.parked_state = (storage, session_storage, work_url, browser_path, current.netloc)
        finally:
            self.switching = False

    async def unpark(self):
        if self.parked_state is None:
            return
        storage, session_storage, work_url, browser_path, origin = self.parked_state
        self.parked_state = None
        self.switching = True
        try:
            await self._start_playwright()
            kwargs = browser_launch_options(browser_path, bundled_browser_path())
            kwargs.update(headless=self.headless, env=browser_environment(), args=['--no-first-run', '--disable-popup-blocking'])
            self.browser = await self.pw.chromium.launch(**kwargs)
            self.browser_path = browser_path
            self.browser.on('disconnected', self._disconnected)
            self.context = await self.browser.new_context(
                storage_state=storage, ignore_https_errors=True,
                viewport={'width': 1440, 'height': 1000} if self.headless else None, accept_downloads=True)
            self.context.on('response', self._response)
            await self.context.add_init_script('(() => { const all = ' + json.dumps(session_storage) +
                '; for (const [k,v] of Object.entries(all[location.origin] || {})) sessionStorage.setItem(k,v); })()')
            self.page = await self.context.new_page()
            self.page.on('close', self._disconnected)
            self.auth_lost = False
            self.origin = origin
            await self.page.goto(work_url, wait_until='domcontentloaded', timeout=30000)
            await self.page.get_by_text('固定报表', exact=True).first.wait_for(state='visible', timeout=15000)
            self.ready = True
            await self.check()
            self.previous_task = None
            self.completed_since_launch = 0
            self.launched_at = time.monotonic()
            self._resource_time = 0
        except Exception as error:
            self.ready = False
            await self.close()
            raise SessionExpired('浏览器回收/切换后未能恢复会话，请粘贴新的登录链接；任务已保留') from error
        finally:
            storage.clear()
            session_storage.clear()
            self.switching = False

    async def close(self):
        self.ready = False
        owned_driver = self._owned_driver
        # Capture Process identities before graceful close: the driver may exit
        # first and leave children reparented. psutil guards against PID reuse.
        remaining = []
        if owned_driver:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                remaining = owned_driver.children(recursive=True) + [owned_driver]
        if self.parked_state is not None:
            self.parked_state[0].clear()
            self.parked_state[1].clear()
            self.parked_state = None
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.browser.close(), timeout=8)
        if self.pw is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.pw.stop(), timeout=8)
        self.pw = self.browser = self.context = self.page = None
        self._owned_driver = None
        if remaining:
            # Kill only descendants of the exact driver we created, never a
            # name-based list of the user's other Chrome/Electron processes.
            try:
                for process in remaining:
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        if process.is_running():
                            process.terminate()
                _, alive = await asyncio.to_thread(psutil.wait_procs, remaining, timeout=2)
                for process in alive:
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.engine.current_nav_type = None


class QueueController:
    """All orchestration runs on one asyncio loop; imports do not block queries."""

    def __init__(self, store, session, configs, mapping, engine_factory, emit):
        self.store, self.session = store, session
        self.configs, self.mapping, self.engine_factory = configs, mapping, engine_factory
        self.emit = emit
        self.wake = asyncio.Event()
        self.run_requested = False
        self.mode = 'idle'
        self.current = None
        self.closing = False
        self.inflight = self.login_job = None
        self.pending_headless = None
        self.mode_job = None
        self.resource_hold = False
        self.import_jobs = set()
        self.round_results = Counter()
        self.import_lock = asyncio.Lock()
        self.session.state_callback = self._session_changed

    def _session_changed(self):
        if not self.session.ready and not self.login_job and not self.mode_job and not self.closing:
            if self.run_requested:
                self.mode = 'waiting_login'
            self.wake.set()
        self.status()

    def log(self, message, level='INFO'):
        self.emit('log', text=redact_urls(message), level=level)

    def status(self):
        self.emit('status', mode=self.mode, session_ready=self.session.ready,
                  current=self.current, importing=bool(self.import_jobs),
                  headless=getattr(self.session, 'headless', False),
                  pending_headless=self.pending_headless, switching=bool(self.mode_job))

    async def request_mode(self, headless):
        if not isinstance(headless, bool):
            raise ValueError('浏览器模式必须为开关值')
        if self.login_job or self.mode_job or self.closing:
            raise ValueError('正在登录或切换，请完成后再操作')
        self.pending_headless = None if headless == self.session.headless else headless
        self.log('已预约浏览器模式切换，将在当前条结束后执行' if self.current else '正在准备切换浏览器模式')
        self.status()
        self.wake.set()

    async def cancel_mode(self):
        if self.mode_job:
            raise ValueError('切换已经开始，不能中途取消')
        self.pending_headless = None
        self.status()

    async def browser_window(self, action):
        if self.login_job or self.mode_job:
            raise ValueError('请等待登录或模式切换完成')
        await self.session.set_window(action)

    async def _apply_mode(self):
        target, self.pending_headless = self.pending_headless, None
        self.mode = 'switching'
        self.mode_job = asyncio.create_task(self.session.switch_mode(target))
        self.status()
        try:
            await self.mode_job
            self.mode = ('running' if self.session.ready else 'waiting_login') if self.run_requested else 'idle'
        except Exception as error:
            self.mode = 'waiting_login'
            self.session.ready = False
            self.log(str(error), 'WARN')
            self.emit('error', message=redact_urls(error))
        finally:
            self.mode_job = None
            self.status()

    async def import_files(self, paths, root, options, allow_duplicate=False, start_after=False):
        if self.closing:
            return
        job = asyncio.current_task()
        self.import_jobs.add(job)
        accepted, duplicates, errors = [], [], []
        self.status()
        try:
            async with self.import_lock:
                for path in paths:
                    if self.closing:
                        break
                    try:
                        plan = await asyncio.to_thread(parse_parameter_file, path, self.configs, self.mapping, options, self.engine_factory)
                        batch = self.store.add_batch(plan, root, allow_duplicate)
                        self.export(batch)
                        accepted.append(path)
                        self.log(f"已加入 {plan['source_name']}：{len(plan['rows'])} 条任务", 'SUCCESS')
                        self.emit('changed')
                        self.wake.set()
                    except DuplicateBatch:
                        duplicates.append(path)
                    except Exception as error:
                        errors.append({'path': path, 'error': redact_urls(error)})
                        self.log(f'参数表导入失败：{path}；{error}', 'ERROR')
        finally:
            self.import_jobs.discard(job)
            self.emit('imported', accepted=accepted, duplicates=duplicates, errors=errors,
                      root=root, options=options, start_after=start_after)
            if start_after and accepted and not self.closing:
                await self.start()
            self.status()
            self.wake.set()

    async def login(self, url, browser_path='', headless=None):
        if self.current or self.login_job or self.mode_job or self.pending_headless is not None or self.closing:
            raise ValueError('请先等待当前条完成并暂停，再重新登录')
        self.login_job = asyncio.current_task()
        self.mode = 'logging_in'
        self.status()
        try:
            if headless is None:
                await self.session.login(url, browser_path)
            else:
                await self.session.login(url, browser_path, headless=headless)
            self.mode = 'running' if self.run_requested else 'idle'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.session.ready = False
            self.mode = 'waiting_login'
            self.log(f'登录未完成：{error}；请粘贴新的有效链接', 'ERROR')
            self.emit('error', message='登录未完成，请检查内网、浏览器路径和登录链接。具体原因见日志。')
        finally:
            self.login_job = None
            self.status()
            self.wake.set()

    async def start(self, restore_interrupted=True):
        if self.closing:
            return
        if restore_interrupted:
            affected = {r['batch_id'] for r in self.store.task_refs(('interrupted',))}
            self.store.resume_interrupted()
            for batch in affected:
                self.export(batch)
        if not self.current and not self.store.counts().get('pending') and not self.import_jobs:
            self.log('没有待执行任务；可添加参数表，或在失败列表中选择重试', 'WARN')
            return
        if not self.run_requested:
            self.round_results.clear()
        self.run_requested = True
        if not self.login_job and not self.mode_job:
            self.mode = 'running' if self.session.ready else 'waiting_login'
        if not self.session.ready:
            self.log('任务已保留，等待登录；登录成功后继续队列', 'WARN')
        self.emit('changed')
        self.status()
        self.wake.set()

    async def pause(self):
        self.run_requested = False
        if not self.mode_job and not self.login_job:
            self.mode = 'pausing' if self.current else 'paused'
        self.log('将在当前条完成后暂停，浏览器保持开启' if self.current else '队列已暂停，浏览器保持开启', 'WARN')
        self.status()
        self.wake.set()

    async def resource_gate(self, hold, reason=''):
        if type(hold) is not bool or not isinstance(reason, str) or len(reason) > 200:
            raise ValueError('资源保护参数无效')
        if self.resource_hold == hold:
            return
        self.resource_hold = hold
        self.log(('资源保护：将在当前条结束后暂缓此工作区，释放浏览器。' + reason) if hold else '资源恢复：允许此工作区继续调度', 'WARN' if hold else 'INFO')
        self.status()
        self.wake.set()

    async def disconnect(self):
        if self.current or self.login_job or self.mode_job or self.pending_headless is not None:
            raise ValueError('请暂停并等待当前任务、登录或模式切换结束后断开连接')
        self.run_requested = False
        await self.session.close()
        self.mode = 'waiting_login'
        self.status()

    async def retry(self, ids=None):
        candidates = self.store.task_refs(('failed', 'timed_out', 'interrupted'))
        if ids is None:
            ids = [r['id'] for r in candidates]
        selected = set(ids)
        affected = {r['batch_id'] for r in candidates if r['id'] in selected}
        count = self.store.retry(ids)
        for batch in affected:
            self.export(batch)
        self.emit('changed')
        self.log(f'已将 {count} 条失败/中断任务加入队尾；不会重跑成功任务')
        if count:
            await self.start(restore_interrupted=False)

    async def remove(self, ids):
        selected = set(ids)
        affected = {r['batch_id'] for r in self.store.task_refs(('pending', 'interrupted')) if r['id'] in selected}
        count = self.store.cancel_pending(ids)
        for batch in affected:
            self.export(batch)
        self.log(f'已移除 {count} 条待执行任务；历史记录保留')
        self.emit('changed')
        self.wake.set()

    def export(self, batch):
        try:
            self.store.export_results(batch)
        except Exception as error:
            self.log(f'结果 CSV 更新失败（任务状态仍保存在本机任务库）：{error}', 'ERROR')

    async def _one(self, task):
        def stage(name, saved_path=''):
            self.store.stage(task['id'], name, saved_path)
            self.emit('changed')
        try:
            saved = await self.session.execute(task, stage)
            self.store.finish(task['id'], 'succeeded', saved_path=saved)
            self.round_results['succeeded'] += 1
            self.log(f"成功：{task['output_name']}", 'SUCCESS')
        except SessionExpired as error:
            self.store.finish(task['id'], 'waiting_login', str(error))
            self.session.ready = False
            self.mode = 'waiting_login'
            self.log(str(error) + '；当前条和后续任务保留，重新登录后继续', 'WARN')
        except FormInteractionError as error:
            self.store.finish(task['id'], 'interrupted', str(error))
            self.run_requested = False
            self.mode = 'paused'
            self.session.previous_task = None
            self.log(str(error) + '；仅暂停此工作区，后续任务未判失败', 'WARN')
        except asyncio.CancelledError:
            self.store.finish(task['id'], 'interrupted', '用户退出程序；任务已保存，重新登录后可恢复')
            raise
        except Exception as error:
            status = 'timed_out' if isinstance(error, (TimeoutError, BrowserTimeout)) else 'failed'
            self.store.finish(task['id'], status, str(error))
            self.round_results[status] += 1
            self.log(f"任务{('超时' if status == 'timed_out' else '失败')}：{task['output_name']}；{error}", 'ERROR')
            try:
                await self.session.recover()
            except Exception as recovery_error:
                self.session.ready = False
                self.mode = 'waiting_login'
                self.log(f'查询页面未恢复，已暂停后续任务：{recovery_error}；请检查网络并重新登录', 'WARN')
        finally:
            if hasattr(self.session, 'task_finished'):
                self.session.task_finished()
            self.export(task['batch_id'])
            self.emit('changed')

    async def serve(self):
        try:
            self.status()
            while not self.closing:
                self.wake.clear()
                if self.pending_headless is not None and not self.login_job:
                    await self._apply_mode()
                    continue
                if self.resource_hold:
                    if self.session.ready and not self.login_job:
                        self.mode = 'resource_paused'
                        self.mode_job = asyncio.create_task(self.session.park())
                        try:
                            await self.mode_job
                        except Exception as error:
                            self.session.ready = False
                            self.log(f'资源回收未完成：{error}；保留队列等待处理', 'WARN')
                        finally:
                            self.mode_job = None
                            self.status()
                    await self.wake.wait()
                    continue
                parked = getattr(self.session, 'parked_state', None) is not None
                needs_browser = self.store.counts().get('pending', 0) > 0
                if self.run_requested and needs_browser and not self.login_job and (parked or getattr(self.session, 'needs_recycle', lambda: False)()):
                    self.mode = 'switching'
                    self.mode_job = asyncio.create_task(self.session.unpark() if parked else self.session.switch_mode(self.session.headless, force=True))
                    self.status()
                    try:
                        await self.mode_job
                        self.mode = 'running' if self.run_requested else 'paused'
                    except Exception as error:
                        self.mode = 'waiting_login'
                        self.session.ready = False
                        self.log(f'浏览器回收后等待重新登录：{error}', 'WARN')
                    finally:
                        self.mode_job = None
                        self.status()
                    continue
                if self.run_requested and self.session.ready and self.mode == 'running':
                    try:
                        await self.session.check()
                    except SessionExpired as error:
                        self.mode = 'waiting_login'
                        self.session.ready = False
                        self.log(str(error), 'WARN')
                        self.status()
                        continue
                    task = self.store.claim_next()
                    if task is None:
                        if self.import_jobs:
                            await self.wake.wait()
                            continue
                        self.run_requested = False
                        self.mode = 'idle'
                        self.emit('completed', counts=dict(self.round_results))
                        self.log('本轮队列已结束，浏览器与执行服务保持待命', 'SUCCESS')
                        self.status()
                    else:
                        self.current = task['id']
                        self.emit('changed')
                        self.status()
                        self.inflight = asyncio.create_task(self._one(task))
                        try:
                            await self.inflight
                        except asyncio.CancelledError:
                            if not self.closing:
                                raise
                        finally:
                            if self.closing:
                                self.store.finish(task['id'], 'interrupted', '退出时当前任务未完成；下次登录后可恢复')
                                self.export(task['batch_id'])
                            self.current = None
                            self.inflight = None
                        if not self.run_requested and self.mode != 'waiting_login':
                            self.mode = 'paused'
                        self.status()
                        continue
                await self.wake.wait()
        finally:
            self.closing = True
            if self.login_job:
                self.login_job.cancel()
                await asyncio.gather(self.login_job, return_exceptions=True)
            if self.import_jobs:
                await asyncio.gather(*list(self.import_jobs), return_exceptions=True)
            await self.session.close()
            self.emit('closed')

    async def shutdown(self):
        self.closing = True
        self.run_requested = False
        if self.inflight:
            self.inflight.cancel()
        if self.mode_job:
            self.mode_job.cancel()
        self.wake.set()


class QueueService:
    """Tk calls this facade; no Tk object is touched by its worker thread."""

    def __init__(self, store, configs, mapping, engine_factory, session_factory=None):
        self.events = queue.Queue()
        self.store = store
        self.booted = threading.Event()
        self.loop = None
        self.controller = None
        self.thread = threading.Thread(target=self._worker, args=(configs, mapping, engine_factory, session_factory), name='TMIS-queue-service', daemon=False)
        self.thread.start()
        if not self.booted.wait(5):
            raise RuntimeError('后台任务服务启动超时')
        if self.controller is None:
            raise RuntimeError('后台任务服务未能初始化，请查看启动环境')

    def emit(self, event, **data):
        self.events.put(dict(data, event=event))

    def _worker(self, configs, mapping, engine_factory, session_factory):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        def log(message, level='INFO'):
            self.emit('log', text=redact_urls(message), level=level)
        try:
            session = session_factory(log) if session_factory else BrowserSession(engine_factory, log)
            self.controller = QueueController(self.store, session, configs, mapping, engine_factory, self.emit)
            self.booted.set()
            self.loop.run_until_complete(self.controller.serve())
        except Exception as error:
            self.emit('fatal', message=redact_urls(error))
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()
            self.booted.set()

    def submit(self, command, *args, **kwargs):
        if not self.thread.is_alive() or not self.controller or self.controller.closing:
            raise RuntimeError('后台服务已停止，请重新启动程序')
        async def handle():
            try:
                return await getattr(self.controller, command)(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.emit('error', message=redact_urls(error))
        return asyncio.run_coroutine_threadsafe(handle(), self.loop)

    def shutdown(self):
        if self.thread.is_alive() and self.controller and not self.controller.closing:
            return self.submit('shutdown')
