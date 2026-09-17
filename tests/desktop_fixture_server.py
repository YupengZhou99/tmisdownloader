"""Synthetic localhost service and workbook for desktop acceptance tests only."""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).parent))
from test_report_browser import FixtureHandler
from test_report_core import APP
import pandas as pd


class DesktopFixture(FixtureHandler):
    def transform_fixture(self, content):
        if self.path.startswith('/login') and getattr(self.server, 'missing_next', False):
            self.server.missing_next = False
            content = content.replace(
                "['pShowScope', '展示范围', ['0 -- 全部', '1 -- 下级']]".encode(),
                "['pShowScope', '展示范围', ['1 -- 下级']]".encode())
        return content

    def respond(self, params):
        if self.path.startswith('/__test/fail-next'):
            self.server.missing_next = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
            return
        if self.path.startswith('/report'):
            time.sleep(1)
        if self.path.startswith('/auth-required'):
            content = b'<input type="password" aria-label="expired login">'
            self.send_response(401)
            self.end_headers()
            self.wfile.write(content)
            return
        super().respond(params)


if __name__ == '__main__':
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ('库存', '收入', '支出'):
        rows = []
        for i in range(3):
            row = dict(APP.REPORT_CONFIGS[kind]['sample'])
            row.update({'文件名称': f'本机验收_{kind}_{i + 1}', '是否追加日期': '0', '保留原文件名': '0',
                        '报表类型': '1 -- 日', '起始日期': '20250101', '终止日期': '20250131',
                        '国库选择': '1002000000', '展示范围': '0 -- 全部'})
            rows.append(row)
        pd.DataFrame(rows).to_excel(directory / f'{kind}.xlsx', sheet_name=kind + '参数', index=False)
        if kind == '库存':
            for j in (1, 2):
                row = dict(rows[0], 文件名称=f'运行中追加_{j}')
                pd.DataFrame([row]).to_excel(directory / f'追加{j}.xlsx', sheet_name='库存参数', index=False)
            row = dict(rows[0], 文件名称='失败后重试')
            pd.DataFrame([row]).to_excel(directory / '重试.xlsx', sheet_name='库存参数', index=False)
    server = ThreadingHTTPServer(('127.0.0.1', 0), DesktopFixture)
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/login?token=LOCAL_TEST_ONLY',
                      'directory': str(directory)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
