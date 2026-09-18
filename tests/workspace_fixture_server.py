"""Same-origin, cookie-isolated report server for four-workspace acceptance."""
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4
import pandas as pd
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).parent))
from test_report_browser import FixtureHandler
from test_report_core import APP, ROOT

SPECS = [('A', '库存', '1002000000', '20250101', '20250131'),
         ('B', '收入', '1003000000', '20250201', '20250228'),
         ('C', '支出', '1004000000', '20250301', '20250331'),
         ('D', '库存', '1005000000', '20250401', '20250430')]

class WorkspaceFixture(FixtureHandler):
    def slot(self):
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        return cookie['workspace_slot'].value if 'workspace_slot' in cookie else 'NONE'

    def end_headers(self):
        if urlsplit(self.path).path == '/login':
            slot = parse_qs(urlsplit(self.path).query).get('slot', ['NONE'])[0]
            if slot in 'ABCD' and len(slot) == 1:
                self.send_header('Set-Cookie', 'workspace_slot=' + slot + '; Path=/; HttpOnly; SameSite=Lax')
        super().end_headers()

    def transform_fixture(self, content):
        slot = parse_qs(urlsplit(self.path).query).get('slot', [self.slot()])[0]
        with self.server.guard:
            missing = slot in self.server.fail_next
            self.server.fail_next.discard(slot)
        if missing:
            content = content.replace(b"['pShowScope', '\xe5\xb1\x95\xe7\xa4\xba\xe8\x8c\x83\xe5\x9b\xb4', ['0 -- \xe5\x85\xa8\xe9\x83\xa8', '1 -- \xe4\xb8\x8b\xe7\xba\xa7']]",
                                      "['pShowScope', '展示范围', ['1 -- 下级']]".encode())
        return content.replace(b"document.getElementById('reportForm').submit();", b"""
          let payload = document.getElementById('payload');
          if (!payload) { payload=document.createElement('input'); payload.id='payload'; payload.name='payload'; document.getElementById('reportForm').append(payload); }
          payload.value=JSON.stringify(Object.fromEntries([...document.querySelectorAll('.el-form-item > label')].map(l => [l.htmlFor,l.parentNode.querySelector('input').value])));
          document.getElementById('reportForm').submit();""")

    def respond(self, params):
        route = urlsplit(self.path).path
        if route.startswith('/__test/'):
            with self.server.guard:
                if route == '/__test/fail':
                    self.server.fail_next.add(params['slot'][0])
                payload = json.dumps({'queries': self.server.queries, 'max_overlap': self.server.max_overlap}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(payload); return
        if route == '/report':
            rid = uuid4().hex
            values = json.loads(params.get('payload', ['{}'])[0])
            entry = {'slot': self.slot(), 'params': values, 'rid': rid}
            if values:
                with self.server.guard:
                    self.server.queries.append(entry); self.server.active += 1
                    self.server.max_overlap = max(self.server.max_overlap, self.server.active)
                try:
                    time.sleep(float(os.environ.get('TMIS_FIXTURE_DELAY', '2')))
                finally:
                    with self.server.guard:
                        self.server.active -= 1
            with self.server.guard:
                self.server.reports[rid] = entry
            content = ("<meta charset='utf-8'><p id='result'>Report " + rid + "</p>"
                       + "<a class='fr-btn ui-state-disabled' widgetname='ExcelO' href='/download?rid=" + rid + "' download='report.xlsx'>原样导出</a>"
                       + "<script>setTimeout(()=>document.querySelector('a').className='fr-btn ui-state-enabled',100)</script>").encode()
            if os.environ.get('TMIS_FIXTURE_LARGE') == '1':
                content += b'''<script>const canvas=document.createElement('canvas');canvas.width=4096;canvas.height=2048;
                  canvas.style.cssText='position:fixed;pointer-events:none;opacity:0.01';document.body.append(canvas);
                  canvas.getContext('2d').fillRect(0,0,4096,2048);
                  const table=document.createElement('table');table.innerHTML=Array.from({length:1000},(_,i)=>'<tr><td>synthetic</td><td>'+i+'</td></tr>').join('');document.body.append(table);</script>'''
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write(content); return
        if route == '/download':
            with self.server.guard:
                entry = self.server.reports[params['rid'][0]]
            book = Workbook(); sheet = book.active
            sheet.append(['workspace_cookie', 'pTreCode', 'pStartDate', 'pEndDate', 'pShowScope', 'download_cookie'])
            sheet.append([entry['slot'], entry['params'].get('pTreCode'), entry['params'].get('pStartDate'), entry['params'].get('pEndDate'), entry['params'].get('pShowScope'), self.slot()])
            output = BytesIO(); book.save(output)
            self.send_response(200); self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'); self.send_header('Content-Disposition', 'attachment; filename="report.xlsx"'); self.end_headers(); self.wfile.write(output.getvalue()); return
        super().respond(params)

if __name__ == '__main__':
    directory = Path(sys.argv[1]); directory.mkdir(parents=True, exist_ok=True)
    sources = []
    for slot, kind, treasury, start, end in SPECS:
        source = directory / (slot + '.xlsx'); rows = []
        for n in range(int(os.environ.get('TMIS_FIXTURE_TASKS', '3'))):
            row = dict(APP.REPORT_CONFIGS[kind]['sample'])
            row.update({'文件名称': slot + '_任务_' + str(n + 1), '是否追加日期': '0', '保留原文件名': '0', '报表类型': '1 -- 日',
                        '起始日期': start, '终止日期': end, '国库选择': treasury, '展示范围': '0 -- 全部'})
            rows.append(row)
        pd.DataFrame(rows).to_excel(source, sheet_name=kind + '参数', index=False)
        append = directory / (slot + '_追加.xlsx')
        pd.DataFrame([dict(rows[0], 文件名称=slot + '_运行追加')]).to_excel(append, sheet_name=kind + '参数', index=False)
        sources.append(dict(slot=slot, kind=kind, treasury=treasury, start=start, end=end, source=str(source), append=str(append)))
    server = ThreadingHTTPServer(('127.0.0.1', 0), WorkspaceFixture)
    server.guard = threading.Lock(); server.queries = []; server.reports = {}; server.fail_next = set(); server.active = server.max_overlap = 0
    base = 'http://127.0.0.1:' + str(server.server_port)
    for row in sources:
        row['url'] = base + '/login?slot=' + row['slot'] + '&kind=' + row['kind'] + '&token=WORKSPACE_SECRET_' + row['slot']
    print(json.dumps({'base': base, 'sources': sources, 'directory': str(directory)}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
