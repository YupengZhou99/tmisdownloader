"""Queue durability, admission, credentials and GUI-independent scheduling."""
import asyncio
import copy
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock, Mock

import pandas as pd

from test_report_core import APP, ROOT, PLAN
from tmis_queue import QueueStore, DuplicateBatch, parse_parameter_file
from tmis_service import BrowserSession, QueueController, QueueService, SessionExpired


OPTIONS = {'start_date': '', 'end_date': '', 'naming_mode': 'param', 'postprocess': False}


def plan_fixture(count=2, tag='one'):
    engine = APP.ReportEngine()
    engine.run_options = OPTIONS
    rows = []
    for i in range(count):
        values = dict(APP.REPORT_CONFIGS['库存']['sample'])
        values.update({'文件名称': f'{tag}_{i}', '是否追加日期': '0', '保留原文件名': '0',
                       '报表类型': '1 -- 日', '起始日期': '20250101', '终止日期': '20250131', '国库选择': '1002000000'})
        params = dict(pd.Series(values).rename(index=APP.COLUMN_MAPPING))
        rows.append({'kind': '库存', 'sheet': '库存参数', 'row_number': i + 2,
                     'params': params, 'output_name': engine._build_filename(params, '库存')})
    return {'source': '/parameters/' + tag + '.xlsx', 'source_name': tag + '.xlsx',
            'file_hash': tag, 'fingerprint': tag, 'options': dict(OPTIONS), 'rows': rows}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root / 'state')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, count=2, tag='one'):
        return self.store.add_batch(plan_fixture(count, tag), self.root)

    def test_import_real_inventory_all_280_rows(self):
        parsed = parse_parameter_file(ROOT / PLAN['output_file'], APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, OPTIONS, APP.ReportEngine)
        self.assertEqual(len(parsed['rows']), 280)
        self.assertEqual(parsed['rows'][-1]['params']['pEndDate'], '20260831')
        self.assertEqual(parsed['rows'][0]['row_number'], 2)
        self.assertEqual(parsed['rows'][-1]['row_number'], 281)
        self.store.add_batch(parsed, self.root)
        self.assertEqual(self.store.counts(), {'pending': 280})

    def parse_frames(self, sheets, options=None):
        with patch('tmis_queue.pd.read_excel', return_value=sheets):
            return parse_parameter_file(ROOT / PLAN['output_file'], APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, options or OPTIONS, APP.ReportEngine)

    def test_income_and_expense_t_subjects_and_dates_are_text(self):
        sheets = {}
        for kind, subjects in [('收入', 'T01,T010401,1101102'), ('支出', 'T02,T020401,205,208,210,221,222')]:
            row = dict(APP.REPORT_CONFIGS[kind]['sample'])
            row.update({'预算科目': subjects, '报表类型': '1 -- 日', '起始日期': '2025-02-01 00:00:00', '终止日期': '2025-02-28 00:00:00'})
            sheets[kind + '参数'] = pd.DataFrame([row])
        parsed = self.parse_frames(sheets)
        self.assertEqual([r['kind'] for r in parsed['rows']], ['收入', '支出'])
        self.assertEqual(parsed['rows'][0]['params']['pStartDate'], '20250201')
        self.assertEqual(parsed['rows'][1]['params']['pSbtCode'], 'T02,T020401,205,208,210,221,222')

    def test_import_rejects_whole_file_on_bad_row_and_marks_source(self):
        rows = [dict(APP.REPORT_CONFIGS['库存']['sample']) for _ in range(2)]
        rows[1]['分地区'] = 'yes'
        with self.assertRaisesRegex(ValueError, '库存参数 第 3 行'):
            self.parse_frames({'库存参数': pd.DataFrame(rows)})
        self.assertEqual(self.store.tasks(), [])

    def test_blank_rows_ignored_and_physical_excel_row_numbers_preserved(self):
        row = dict(APP.REPORT_CONFIGS['库存']['sample'])
        parsed = self.parse_frames({'库存参数': pd.DataFrame([{}, row]), '使用说明': pd.DataFrame([{'说明': '不是任务'}])})
        self.assertEqual(len(parsed['rows']), 1)
        self.assertEqual(parsed['rows'][0]['row_number'], 3)

    def test_mangled_duplicate_excel_headers_are_rejected(self):
        row = dict(APP.REPORT_CONFIGS['库存']['sample'])
        row['国库选择.1'] = '1000000000'
        with self.assertRaisesRegex(ValueError, '重复字段表头'):
            self.parse_frames({'库存参数': pd.DataFrame([row])})

    def test_same_output_name_rejected_but_automatic_names_are_disambiguated(self):
        row = dict(APP.REPORT_CONFIGS['库存']['sample'])
        sheets = {'库存参数': pd.DataFrame([row, row])}
        with self.assertRaisesRegex(ValueError, '文件名重复'):
            self.parse_frames(sheets)
        parsed = self.parse_frames(sheets, dict(OPTIONS, naming_mode='auto'))
        self.assertNotEqual(parsed['rows'][0]['output_name'], parsed['rows'][1]['output_name'])

    def test_duplicate_batch_requires_confirmation_and_gets_a_new_directory(self):
        self.add()
        with self.assertRaises(DuplicateBatch):
            self.add()
        self.store.add_batch(plan_fixture(), self.root, allow_duplicate=True)
        self.assertEqual(len({b['output_dir'] for b in self.store.batches()}), 2)

    def test_queue_snapshot_and_output_directory_survive_restart(self):
        plan = plan_fixture()
        self.store.add_batch(plan, self.root)
        plan['rows'][0]['params']['pTreCode'] = 'mutated-after-admission'
        before = self.store.tasks()
        self.store.close()
        self.store = QueueStore(self.root / 'state')
        self.assertEqual(self.store.tasks(), before)
        self.assertEqual(self.store.tasks()[0]['params']['pTreCode'], '1002000000')

    def test_interrupted_attempt_recovered_without_repeating_success(self):
        self.add()
        first = self.store.claim_next()
        self.store.finish(first['id'], 'succeeded', saved_path='/result/a.xlsx')
        second = self.store.claim_next()
        self.store.stage(second['id'], '等待查询')
        self.store.close()
        self.store = QueueStore(self.root / 'state')
        self.assertEqual(self.store.counts(), {'interrupted': 1, 'succeeded': 1})
        self.assertEqual(self.store.history(second['id'])[0]['status'], 'interrupted')
        self.store.resume_interrupted()
        retried = self.store.claim_next()
        self.assertEqual(retried['id'], second['id'])
        self.assertEqual(retried['attempt_count'], 2)

    def test_retry_appends_to_tail_and_does_not_repeat_success_or_running(self):
        self.add(4)
        first = self.store.claim_next()
        self.store.finish(first['id'], 'timed_out', 'timeout')
        second = self.store.claim_next()
        self.store.finish(second['id'], 'succeeded')
        third = self.store.claim_next()
        self.assertEqual(self.store.retry([first['id'], second['id'], third['id']]), 1)
        self.store.finish(third['id'], 'succeeded')
        fourth = self.store.claim_next()
        self.assertNotEqual(fourth['id'], first['id'])
        self.store.finish(fourth['id'], 'succeeded')
        self.assertEqual(self.store.claim_next()['id'], first['id'])
        self.assertEqual(self.store.history(first['id'])[0]['status'], 'timed_out')

    def test_remove_is_soft_and_only_affects_pending_items(self):
        self.add()
        running = self.store.claim_next()
        all_ids = [r['id'] for r in self.store.tasks()]
        self.assertEqual(self.store.cancel_pending(all_ids), 1)
        self.assertEqual(self.store.counts(), {'cancelled': 1, 'running': 1})
        self.assertEqual(self.store.history(running['id'])[0]['status'], 'running')

    def test_second_process_lock_fails_without_resetting_running_task(self):
        self.add()
        self.store.claim_next()
        with self.assertRaisesRegex(RuntimeError, '另一个程序'):
            QueueStore(self.root / 'state')
        self.assertEqual(self.store.counts()['running'], 1)

    def test_csv_and_database_redact_credentials_and_preserve_attempts(self):
        batch = self.add(1)
        row = self.store.claim_next()
        self.store.finish(row['id'], 'failed', 'goto https://example.invalid/login?token=TOPSECRET\ntoken=BARESECRET')
        self.store.retry([row['id']])
        self.store.claim_next()
        self.store.finish(row['id'], 'succeeded', saved_path='/result/a.xlsx')
        self.store.export_results(batch)
        folder = Path(self.store.batches()[0]['output_dir'])
        with (folder / '下载结果.csv').open(encoding='utf-8-sig', newline='') as stream:
            result = list(csv.DictReader(stream))
        self.assertEqual(result[0]['状态'], '成功')
        self.assertEqual(result[0]['尝试次数'], '2')
        history = (folder / '重试记录.csv').read_text(encoding='utf-8-sig')
        self.assertIn('失败', history)
        self.assertNotIn('TOPSECRET', history)
        self.assertNotIn('BARESECRET', history)
        dump = '\n'.join(self.store.connection.iterdump())
        self.assertNotIn('TOPSECRET', dump)
        self.assertNotIn('BARESECRET', dump)

    def test_credentials_not_admitted_as_query_parameters_or_options(self):
        row = dict(APP.REPORT_CONFIGS['库存']['sample'])
        row['会计账户名称'] = 'https://example.invalid/?token=secret'
        with self.assertRaisesRegex(ValueError, '不能包含'):
            self.parse_frames({'库存参数': pd.DataFrame([row])})
        row['会计账户名称'] = ''
        with self.assertRaisesRegex(ValueError, '不能包含'):
            self.parse_frames({'库存参数': pd.DataFrame([row])}, dict(OPTIONS, start_date='token=secret'))


class FakeSession:
    def __init__(self):
        self.ready = False
        self.calls = []
        self.logins = 0
        self.closed = False
        self.outcomes = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.recovery_error = None

    async def login(self, url, browser_path=''):
        self.logins += 1
        self.ready = True

    async def check(self):
        if not self.ready:
            raise SessionExpired('登录失效')

    async def execute(self, task, stage):
        self.calls.append(task['id'])
        stage('等待查询')
        self.entered.set()
        await self.release.wait()
        if self.outcomes:
            result = self.outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
        return str(Path(task['output_dir']) / (task['output_name'] + '.xlsx'))

    async def recover(self):
        if self.recovery_error:
            raise self.recovery_error

    async def close(self):
        self.ready = False
        self.closed = True


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root / 'state')
        self.session = FakeSession()
        self.events = []
        self.controller = QueueController(self.store, self.session, APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, APP.ReportEngine,
                                          lambda event, **data: self.events.append(dict(data, event=event)))
        self.runner = asyncio.create_task(self.controller.serve())

    async def asyncTearDown(self):
        await self.controller.shutdown()
        await asyncio.wait_for(self.runner, 3)
        self.store.close()
        self.temp.cleanup()

    def add(self, count=2, tag='one'):
        return self.store.add_batch(plan_fixture(count, tag), self.root)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 4)

    async def test_login_without_parameters_and_finish_keeps_service_and_session(self):
        await self.controller.login('https://example.invalid/?token=RAM_ONLY')
        self.assertTrue(self.session.ready)
        self.assertEqual(self.store.tasks(), [])
        self.add()
        await self.controller.start()
        await self.until(lambda: self.controller.mode == 'idle' and len(self.session.calls) == 2)
        self.assertFalse(self.runner.done())
        self.assertFalse(self.session.closed)
        self.assertNotIn('RAM_ONLY', '\n'.join(self.store.connection.iterdump()))

    async def test_start_before_login_waits_and_resumes_after_login(self):
        self.add(1)
        await self.controller.start()
        await asyncio.sleep(0.02)
        self.assertEqual(self.controller.mode, 'waiting_login')
        self.assertEqual(self.store.counts(), {'pending': 1})
        await self.controller.login('https://example.invalid')
        await self.until(lambda: self.store.counts().get('succeeded') == 1)

    async def test_pause_finishes_current_task_and_retains_added_work(self):
        self.add(2)
        self.session.release.clear()
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.session.entered.wait()
        await self.controller.pause()
        self.add(1, 'later')
        self.session.release.set()
        await self.until(lambda: self.controller.current is None)
        self.assertEqual(self.controller.mode, 'paused')
        self.assertEqual(self.store.counts(), {'pending': 2, 'succeeded': 1})
        self.assertFalse(self.session.closed)
        await self.controller.start()
        await self.until(lambda: self.store.counts().get('succeeded') == 3)
        self.assertEqual(len(set(self.session.calls)), 3)

    async def test_import_during_last_query_is_appended_even_if_parsing_finishes_later(self):
        self.add(1)
        self.session.release.clear()
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.session.entered.wait()
        def slow_parse(*args):
            time.sleep(0.12)
            return plan_fixture(1, 'later')
        with patch('tmis_service.parse_parameter_file', side_effect=slow_parse):
            importing = asyncio.create_task(self.controller.import_files(['later.xlsx'], str(self.root), OPTIONS))
            await asyncio.sleep(0.02)
            self.session.release.set()
            await importing
        await self.until(lambda: self.controller.mode == 'idle' and len(self.session.calls) == 2)
        self.assertEqual(len([e for e in self.events if e['event'] == 'completed']), 1)

    async def test_import_while_idle_does_not_automatically_start(self):
        await self.controller.login('https://example.invalid')
        with patch('tmis_service.parse_parameter_file', return_value=plan_fixture(1)):
            await self.controller.import_files(['one.xlsx'], str(self.root), OPTIONS)
        await asyncio.sleep(0.02)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.store.counts(), {'pending': 1})

    async def test_failed_file_does_not_discard_other_selected_files(self):
        with patch('tmis_service.parse_parameter_file', side_effect=[ValueError('bad row'), plan_fixture(1, 'good')]):
            await self.controller.import_files(['bad.xlsx', 'good.xlsx'], str(self.root), OPTIONS)
        event = next(e for e in self.events if e['event'] == 'imported')
        self.assertEqual(event['accepted'], ['good.xlsx'])
        self.assertEqual(len(event['errors']), 1)
        self.assertEqual(self.store.counts(), {'pending': 1})

    async def test_expired_login_pauses_whole_queue_and_relogin_resumes_original_task(self):
        self.add(2)
        self.session.outcomes = [SessionExpired('token=NO_LOG'), None, None]
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.until(lambda: self.controller.mode == 'waiting_login')
        self.assertEqual(self.store.counts(), {'pending': 2})
        self.assertEqual(len(self.session.calls), 1)
        await self.controller.login('https://example.invalid')
        await self.until(lambda: self.store.counts().get('succeeded') == 2)
        self.assertEqual(self.session.calls[0], self.session.calls[1])
        self.assertNotIn('NO_LOG', json.dumps(self.events))
        self.assertEqual(self.store.history(self.session.calls[0])[0]['status'], 'waiting_login')

    async def test_timeout_and_failure_retry_only_selected_failed_task(self):
        self.add(3)
        self.session.outcomes = [TimeoutError('query timed out'), ValueError('bad option'), None]
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.until(lambda: self.controller.mode == 'idle' and len(self.session.calls) == 3)
        tasks = self.store.tasks()
        self.assertEqual(self.store.counts(), {'failed': 1, 'succeeded': 1, 'timed_out': 1})
        await self.controller.retry([tasks[0]['id'], tasks[2]['id']])
        await self.until(lambda: self.controller.mode == 'idle' and len(self.session.calls) == 4)
        self.assertEqual(self.session.calls.count(tasks[2]['id']), 1)
        self.assertEqual(self.store.counts(), {'failed': 1, 'succeeded': 2})
        await self.controller.retry()
        await self.until(lambda: self.store.counts().get('succeeded') == 3)
        self.assertEqual(self.session.logins, 1)

    async def test_unrecoverable_page_does_not_fail_all_remaining_tasks(self):
        self.add(3)
        self.session.outcomes = [TimeoutError('query timeout')]
        self.session.recovery_error = RuntimeError('browser gone')
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.until(lambda: self.controller.mode == 'waiting_login')
        self.assertEqual(self.store.counts(), {'pending': 2, 'timed_out': 1})

    async def test_selected_retry_does_not_resume_other_interrupted_tasks(self):
        self.add(2)
        one = self.store.claim_next()
        self.store.finish(one['id'], 'failed', 'bad option')
        two = self.store.claim_next()
        self.store.finish(two['id'], 'interrupted', 'old interrupted task')
        await self.controller.login('https://example.invalid')
        await self.controller.retry([one['id']])
        await self.until(lambda: self.controller.mode == 'idle' and len(self.session.calls) == 1)
        self.assertEqual(self.store.counts(), {'interrupted': 1, 'succeeded': 1})

    async def test_browser_disconnection_while_waiting_unblocks_relogin(self):
        await self.controller.login('https://example.invalid')
        self.add(1)
        self.controller.run_requested = True
        self.controller.mode = 'running'
        self.session.ready = False
        self.session.state_callback()
        await asyncio.sleep(0.02)
        self.assertEqual(self.controller.mode, 'waiting_login')
        self.assertEqual(self.store.counts(), {'pending': 1})
        await self.controller.login('https://example.invalid')
        await self.until(lambda: self.store.counts().get('succeeded') == 1)

    async def test_retry_saved_postprocessing_does_not_issue_new_query(self):
        plan = plan_fixture(1)
        plan['options']['postprocess'] = True
        self.store.add_batch(plan, self.root)
        first = self.store.claim_next()
        saved = Path(first['output_dir']) / 'already-downloaded.xlsx'
        saved.write_bytes(b'owned-report-fixture')
        self.store.stage(first['id'], '数据后处理', str(saved))
        self.store.finish(first['id'], 'failed', 'postprocess error')
        self.store.retry([first['id']])
        retry = self.store.claim_next()
        session = BrowserSession(APP.ReportEngine, lambda *args: None)
        session.engine._process_excel_data = Mock()
        session.engine._click_query_and_wait = AsyncMock()
        result = await session._execute(retry, lambda *args: None)
        self.assertEqual(result, str(saved))
        session.engine._process_excel_data.assert_called_once()
        session.engine._click_query_and_wait.assert_not_called()

    async def test_shutdown_interrupts_current_preserves_pending_and_closes_browser(self):
        self.add(2)
        self.session.release.clear()
        await self.controller.login('https://example.invalid')
        await self.controller.start()
        await self.session.entered.wait()
        await self.controller.shutdown()
        await asyncio.wait_for(self.runner, 3)
        self.assertTrue(self.session.closed)
        self.assertEqual(self.store.counts(), {'interrupted': 1, 'pending': 1})


class ThreadServiceTests(unittest.TestCase):
    def test_service_thread_stays_alive_until_explicit_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory) / 'state')
            service = QueueService(store, APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, APP.ReportEngine, session_factory=lambda log: FakeSession())
            try:
                service.submit('login', 'https://example.invalid/?token=MEMORY_ONLY').result(3)
                self.assertTrue(service.thread.is_alive())
                self.assertTrue(service.controller.session.ready)
                self.assertNotIn('MEMORY_ONLY', '\n'.join(store.connection.iterdump()))
            finally:
                service.shutdown()
                service.thread.join(3)
                store.close()
            self.assertFalse(service.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
