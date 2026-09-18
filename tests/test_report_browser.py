"""本地合成页面回归测试。启用 TMIS_BROWSER_TESTS=1，在 CI 使用虚拟桌面。"""

import asyncio
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pandas as pd
from playwright.async_api import async_playwright, Download
from test_report_core import APP, ROOT, PLAN
from tmis_runtime import normalize_task_row
from tmis_queue import QueueStore
from tmis_service import BrowserSession, QueueController, SessionExpired
from test_report_queue import OPTIONS, plan_fixture


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.respond(parse_qs(data))

    def do_GET(self):
        self.respond(parse_qs(urlsplit(self.path).query))

    def transform_fixture(self, content):
        return content

    def respond(self, params):
        route = urlsplit(self.path).path
        if route == "/report":
            serial = int(params.get("serial", ["0"])[0])
            disabled = params.get("mode", ["normal"])[0] == "disabled"
            content = ("<meta charset='utf-8'><p id='result'>Report %d</p>" % serial
                       + "<a class='fr-btn ui-state-disabled' widgetname='ExcelO' href='/download' download='report.xlsx'>原样导出</a>"
                       + ("" if disabled else "<script>setTimeout(()=>document.querySelector('a').className='fr-btn ui-state-enabled', 100)</script>")).encode()
        elif route == "/download":
            content = (ROOT / PLAN["output_file"]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="report.xlsx"')
            self.end_headers()
            self.wfile.write(content)
            return
        else:
            content = (ROOT / "tests/fixtures/stock-query.html").read_bytes()
            kind = params.get('kind', ['库存'])[0]
            frame_name = APP.REPORT_CONFIGS.get(kind, APP.REPORT_CONFIGS['库存'])['iframe_name']
            content = content.replace(b'fineReportTsasRpt6040', frame_name.encode())
            content = self.transform_fixture(content)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content)


@unittest.skipUnless(os.environ.get("TMIS_BROWSER_TESTS") == "1", "set TMIS_BROWSER_TESTS=1")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/login?token=local-test-token" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    async def asyncSetUp(self):
        APP.stop_flag = False
        self.pw = await async_playwright().start()
        self.browser_path = os.environ.get('TMIS_TEST_BROWSER_PATH', self.pw.chromium.executable_path)
        self.browser = await self.pw.chromium.launch(headless=True, executable_path=self.browser_path)
        self.page = await self.browser.new_page(accept_downloads=True)
        await self.page.goto(self.url)
        self.app = APP.ReportEngine()
        self.app.run_options = {"start_date": "", "end_date": "", "naming_mode": "param", "browser_path": self.pw.chromium.executable_path, "postprocess": False}
        self.app.current_nav_type = None
        self.app._active_browser = None
        self.app.target_url = self.url
        self.messages = []
        self.app.log = lambda msg, level="INFO": self.messages.append(str(msg))

    async def asyncTearDown(self):
        APP.stop_flag = False
        await self.browser.close()
        await self.pw.stop()

    async def test_two_consecutive_queries_fill_city_and_province_and_export_new_report(self):
        rows = pd.read_excel(ROOT / PLAN["output_file"], sheet_name="库存参数", dtype=str).fillna("")
        with tempfile.TemporaryDirectory() as directory:
            for serial, (_, raw) in enumerate(rows.iloc[[0, 279]].iterrows(), 1):
                row = normalize_task_row(raw.rename(index=APP.COLUMN_MAPPING))
                await self.app._navigate_sidebar_smart(self.page, "库存")
                await self.app._fill_form(self.page, row, "库存")
                for field in ("pTreCode", "pStartDate", "pEndDate", "pBookSbt", "pShowScope"):
                    locator = self.page.locator('.el-form-item:has(label[for="%s"]) input' % field)
                    self.assertEqual(await locator.input_value(), row[field])
                await self.app._click_query_and_wait(self.page, "库存", timeout_seconds=3)
                self.assertEqual(await self.page.frame(name="fineReportTsasRpt6040").locator("#result").text_content(), "Report %d" % serial)
                output = await self.app._export_and_save(self.page, directory, raw["文件名称"], "库存", False)
                self.assertEqual(Path(output).read_bytes(), (ROOT / PLAN["output_file"]).read_bytes())
            self.assertEqual(len(list(Path(directory).glob("*.xlsx"))), 2)
            self.assertFalse(list(Path(directory).glob("*.part")))

    async def test_mode_switch_moves_cookie_local_and_session_storage_only_in_memory(self):
        session = BrowserSession(APP.ReportEngine, lambda *args: None, headless=True)
        try:
            await session.login(self.url, self.browser_path)
            await session.context.add_cookies([{'name': 'session', 'value': 'RAM_COOKIE', 'url': self.url}])
            await session.page.evaluate("""() => {
                localStorage.setItem('session', 'RAM_LOCAL');
                sessionStorage.setItem('session', 'RAM_SESSION');
            }""")
            original = session.browser
            await session.switch_mode(False)
            self.assertIsNot(session.browser, original)
            self.assertFalse(session.headless)
            self.assertTrue(session.ready)
            self.assertNotIn('token=', session.page.url)
            self.assertEqual(await session.page.evaluate("sessionStorage.getItem('session')"), 'RAM_SESSION')
            self.assertEqual(await session.page.evaluate("localStorage.getItem('session')"), 'RAM_LOCAL')
            self.assertTrue(any(c['value'] == 'RAM_COOKIE' for c in await session.context.cookies()))
            for _ in range(3):
                prior = session.browser
                await session.switch_mode(session.headless, force=True)
                self.assertIsNot(session.browser, prior)
                self.assertFalse(prior.is_connected())
                self.assertEqual(await session.page.evaluate("sessionStorage.getItem('session')"), 'RAM_SESSION')
            await session.set_window('minimize')
            await session.set_window('restore')
            await session.switch_mode(True)
            self.assertTrue(session.headless)
            self.assertEqual(await session.page.evaluate("sessionStorage.getItem('session')"), 'RAM_SESSION')
            with self.assertRaises(ValueError):
                await session.set_window('restore')
            with tempfile.TemporaryDirectory() as directory:
                store = QueueStore(Path(directory) / 'state')
                try:
                    store.add_batch(plan_fixture(1, 'after-switch'), directory)
                    task = store.claim_next()
                    saved = await session.execute(task, lambda *args: None)
                    self.assertTrue(Path(saved).is_file())
                    self.assertNotIn('RAM_', '\n'.join(store.connection.iterdump()))
                finally:
                    store.close()
        finally:
            await session.close()

    async def test_delayed_dropdown_is_not_misreported_as_missing_option(self):
        await self.app._navigate_sidebar_smart(self.page, '库存')
        await self.page.evaluate("""() => {
            const field = document.querySelector('label[for="pRptDbType"]').parentNode;
            const input = field.querySelector('input'), menu = field.querySelector('ul');
            input.value = '0 -- 预处理库';
            input.onclick = () => setTimeout(() => { menu.style.display = 'block'; }, 1100);
        }""")
        await self.app._fill_dropdown(self.page, 'pRptDbType', '1 -- 报表库')
        self.assertEqual(await self.page.locator('.el-form-item:has(label[for="pRptDbType"]) input').input_value(), '1 -- 报表库')

    async def test_unopened_dropdown_is_an_interaction_error_not_missing_parameter(self):
        from tmis_runtime import FormInteractionError
        await self.app._navigate_sidebar_smart(self.page, '库存')
        await self.page.evaluate("""() => {
            const input = document.querySelector('label[for="pRptDbType"]').parentNode.querySelector('input');
            input.value = '0 -- 预处理库'; input.onclick = () => {};
        }""")
        with self.assertRaisesRegex(FormInteractionError, '未正常展开'):
            await self.app._fill_dropdown(self.page, 'pRptDbType', '1 -- 报表库')

    async def test_old_enabled_export_does_not_satisfy_a_new_query(self):
        frame = self.page.frame(name="fineReportTsasRpt6040")
        await frame.locator(".ui-state-enabled").wait_for()
        await self.page.evaluate("window.queryMode='no-navigation'")
        with self.assertRaises(Exception):
            await self.app._click_query_and_wait(self.page, "库存", timeout_seconds=0.4)
        self.assertNotIn("  数据查询完成，导出按钮已可用", self.messages)

    async def test_new_report_with_disabled_export_times_out(self):
        await self.page.evaluate("window.queryMode='disabled'")
        with self.assertRaises(TimeoutError):
            await self.app._click_query_and_wait(self.page, "库存", timeout_seconds=0.5)

    async def test_missing_option_stops_query_instead_of_using_old_selection(self):
        with self.assertRaises(ValueError):
            await self.app._fill_dropdown(self.page, "pShowScope", "8 -- 不存在")
        self.assertEqual(await self.page.locator("#serial").input_value(), "0")

    async def test_existing_download_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'existing.xlsx'
            original.write_bytes(b'earlier-user-report')
            with self.assertRaises(FileExistsError):
                await self.app._export_and_save(self.page, directory, 'existing', '库存', False)
            self.assertEqual(original.read_bytes(), b'earlier-user-report')
            self.assertFalse(list(Path(directory).glob('*.part')))

    async def test_export_deletes_browser_temporary_copy_after_durable_save(self):
        saved_stages, removed = [], []
        original_delete = Download.delete
        async def delete(download):
            internal = await download.path()
            self.assertTrue(saved_stages and Path(saved_stages[0]).is_file())
            await original_delete(download)
            removed.append(internal)
            self.assertFalse(Path(internal).exists())
        with tempfile.TemporaryDirectory() as directory, patch.object(Download, 'delete', delete):
            result = await self.app._export_and_save(self.page, directory, 'kept', '库存', False,
                                                     on_saved=saved_stages.append)
            self.assertEqual(saved_stages, [result])
            self.assertEqual(len(removed), 1)
            self.assertFalse(Path(removed[0]).exists())
            self.assertGreater(Path(result).stat().st_size, 0)

    async def test_income_and_expense_city_province_fields_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ('收入', '支出'):
                await self.page.goto(self.url + '&kind=' + kind)
                self.app.current_nav_type = None
                for province in (False, True):
                    raw = dict(APP.REPORT_CONFIGS[kind]['sample'])
                    raw.update({'报表类型': '1 -- 日', '起始日期': '20260801', '终止日期': '20260831',
                                '国库选择': '1000000000' if province else '1015000000', '预算级次': '0',
                                '辖属标志': '1 -- 本级' if province else '0 -- 全辖',
                                '展示范围': '1 -- 下级' if province else '0 -- 全部', '金额单位': '0 -- 元',
                                '分地区': '1'})
                    raw['预算科目'] = ('T01,T010401,1050402' if province else 'T01,T010401,1101102') if kind == '收入' else 'T02,T020401,205,208,210,221,222'
                    row = normalize_task_row(pd.Series(raw).rename(index=APP.COLUMN_MAPPING))
                    await self.app._navigate_sidebar_smart(self.page, kind)
                    await self.app._fill_form(self.page, row, kind)
                    for category in ('dropdown_fields', 'date_fields', 'text_fields'):
                        for field in APP.REPORT_CONFIGS[kind][category]:
                            self.assertEqual(await self.page.locator('.el-form-item:has(label[for="%s"]) input' % field).input_value(), row[field])
                    for label in APP.REPORT_CONFIGS[kind]['checkbox_fields']:
                        checkbox = self.page.locator('label.el-checkbox').filter(has_text=label)
                        self.assertEqual('is-checked' in await checkbox.get_attribute('class'), row[label] == '1')
                    await self.app._click_query_and_wait(self.page, kind, timeout_seconds=3)
                    name = kind + ('-province' if province else '-city')
                    saved = await self.app._export_and_save(self.page, directory, name, kind, False)
                    self.assertEqual(Path(saved).read_bytes(), (ROOT / PLAN['output_file']).read_bytes())
            self.assertEqual(len(list(Path(directory).glob('*.xlsx'))), 4)

    async def until(self, predicate, timeout=90):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.03)
        await asyncio.wait_for(wait(), timeout)

    async def test_login_first_pause_append_continue_keeps_browser_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = QueueStore(root / 'state')
            events = []
            session = BrowserSession(APP.ReportEngine, lambda *args: None, headless=True, query_timeout=3)
            controller = QueueController(store, session, APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, APP.ReportEngine,
                                         lambda event, **data: events.append(dict(data, event=event)))
            runner = asyncio.create_task(controller.serve())
            try:
                await controller.login(self.url, self.browser_path)
                self.assertTrue(session.ready)
                self.assertEqual(store.tasks(), [])
                browser = session.browser
                with patch('tmis_service.parse_parameter_file', return_value=plan_fixture(1, 'first')):
                    await controller.import_files(['first.xlsx'], root, OPTIONS)
                await controller.start()
                await self.until(lambda: any(r['stage'] == '填写参数' for r in store.tasks()))
                await controller.pause()
                with patch('tmis_service.parse_parameter_file', return_value=plan_fixture(1, 'appended')):
                    await controller.import_files(['appended.xlsx'], root, OPTIONS)
                await self.until(lambda: controller.current is None)
                self.assertEqual(store.counts(), {'pending': 1, 'succeeded': 1})
                self.assertEqual(controller.mode, 'paused')
                self.assertTrue(browser.is_connected())
                await controller.start()
                await self.until(lambda: controller.mode == 'idle' and store.counts().get('succeeded') == 2)
                self.assertIs(session.browser, browser)
                self.assertFalse(runner.done())
                for row in store.tasks():
                    self.assertEqual(Path(row['saved_path']).read_bytes(), (ROOT / PLAN['output_file']).read_bytes())
                    self.assertEqual(row['attempt_count'], 1)
                self.assertEqual(len({r['output_dir'] for r in store.tasks()}), 2)
                self.assertNotIn('local-test-token', '\n'.join(store.connection.iterdump()))
                self.assertNotIn('local-test-token', json.dumps(events, default=str))
                for batch in store.batches():
                    with (Path(batch['output_dir']) / '下载结果.csv').open(encoding='utf-8-sig', newline='') as stream:
                        self.assertEqual(next(csv.DictReader(stream))['状态'], '成功')
            finally:
                await controller.shutdown()
                await asyncio.wait_for(runner, 20)
                self.assertFalse(session.ready)
                self.assertIsNone(session.browser)
                store.close()

    async def test_timeout_retry_uses_same_browser_and_retains_both_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = QueueStore(root / 'state')
            session = BrowserSession(APP.ReportEngine, lambda *args: None, headless=True, query_timeout=0.5)
            controller = QueueController(store, session, APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, APP.ReportEngine, lambda *args, **kwargs: None)
            runner = asyncio.create_task(controller.serve())
            original_query = session.engine._click_query_and_wait
            modes = iter(['disabled', 'normal'])
            async def query(page, kind, **kwargs):
                mode = next(modes)
                await page.evaluate('(mode) => window.queryMode=mode', mode)
                await original_query(page, kind, timeout_seconds=0.5 if mode == 'disabled' else 3)
            session.engine._click_query_and_wait = query
            try:
                await controller.login(self.url, self.browser_path)
                browser = session.browser
                store.add_batch(plan_fixture(1), root)
                await controller.start()
                await self.until(lambda: controller.mode == 'idle' and store.counts().get('timed_out') == 1)
                self.assertFalse(list(root.glob('**/*.xlsx')))
                await controller.retry()
                await self.until(lambda: controller.mode == 'idle' and store.counts().get('succeeded') == 1)
                task = store.tasks()[0]
                self.assertEqual([a['status'] for a in store.history(task['id'])], ['timed_out', 'succeeded'])
                self.assertTrue(Path(task['saved_path']).is_file())
                self.assertIs(session.browser, browser)
                await session.page.set_content('<input type="password">')
                with self.assertRaises(SessionExpired):
                    await session.check()
                await controller.login(self.url, self.browser_path)
                self.assertTrue(session.ready)
                await session.browser.close()
                self.assertFalse(session.ready)
            finally:
                await controller.shutdown()
                await asyncio.wait_for(runner, 20)
                store.close()


if __name__ == "__main__":
    unittest.main()
