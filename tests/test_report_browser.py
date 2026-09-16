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
from playwright.async_api import async_playwright
from test_report_core import APP, ROOT, PLAN
from tmis_runtime import normalize_task_row


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.respond(parse_qs(data))

    def do_GET(self):
        self.respond(parse_qs(urlsplit(self.path).query))

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
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page(accept_downloads=True)
        await self.page.goto(self.url)
        self.app = object.__new__(APP.TMISAutoApp)
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

    async def test_pasted_url_startup_without_sniper(self):
        self.app.is_running = False
        class Button:
            def config(self, **kwargs): pass
        class Value:
            def __init__(self, value): self.value = value
            def get(self): return self.value
        self.app.start_btn = self.app.stop_btn = self.app.login_btn = self.app.sniper_btn = Button()
        with tempfile.TemporaryDirectory() as directory:
            self.app.excel_path = Value(str(ROOT / PLAN["output_file"]))
            self.app.download_folder = Value(directory)
            self.app.chrome_path_var = Value(self.pw.chromium.executable_path)
            self.app.start_date_var = self.app.end_date_var = Value("")
            self.app.naming_mode = Value("param")
            self.app.enable_postprocess = Value(False)
            self.app.target_url = None
            with patch.object(APP.simpledialog, "askstring", return_value=self.url):
                self.app._paste_login_url()
            with patch.object(APP.threading, "Thread") as worker:
                self.app._start_task()
                worker.return_value.start.assert_called_once()
            self.assertEqual(self.app.target_url, self.url)
            self.assertTrue(self.app.is_running)

    async def test_stop_closes_inflight_browser(self):
        closed = asyncio.Event()
        class Browser:
            async def close(self): closed.set()
        async def pending(*args):
            self.app._active_browser = Browser()
            await closed.wait()
        self.app._run_automation = pending
        run = asyncio.create_task(self.app._run_with_stop("unused", "unused"))
        await asyncio.sleep(0.05)
        APP.stop_flag = True
        await asyncio.wait_for(run, timeout=2)
        self.assertTrue(closed.is_set())


if __name__ == "__main__":
    unittest.main()
