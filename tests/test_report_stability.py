"""Resource safety, bounded queue projection and non-destructive history clearing."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from test_report_core import APP
from test_report_queue import plan_fixture
import test_report_queue as queue_tests
from tmis_queue import QueueStore, DuplicateBatch
from tmis_runtime import FormInteractionError
from tmis_service import BrowserSession


class ProjectionTests(unittest.TestCase):
    def test_completed_clear_preserves_files_history_and_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = QueueStore(root / 'state')
            try:
                batch = store.add_batch(plan_fixture(201), root)
                task = store.claim_next()
                report = root / 'do-not-delete.xlsx'
                report.write_bytes(b'report')
                store.finish(task['id'], 'succeeded', saved_path=str(report))
                self.assertEqual(len(store.task_page()['tasks']), 60)
                self.assertEqual(store.task_page(offset=180)['total'], 201)
                self.assertEqual(len(store.task_page(offset=180)['tasks']), 21)
                self.assertEqual(store.clear_completed(), 1)
                self.assertEqual(store.clear_completed(), 0)
                self.assertEqual(store.counts(), {'pending': 200})
                self.assertEqual(report.read_bytes(), b'report')
                self.assertEqual(store.history(task['id'])[0]['status'], 'succeeded')
                with self.assertRaises(DuplicateBatch):
                    store.add_batch(plan_fixture(201), root)
                store.export_results(batch)
                self.assertIn(task['id'], (Path(task['output_dir']) / '下载结果.csv').read_text(encoding='utf-8-sig'))
                store.close()
                store = QueueStore(root / 'state')
                self.assertEqual(store.task_page()['total'], 200)
            finally:
                store.close()

    def test_headless_never_checks_window_and_recycle_does_not_loop_before_any_task(self):
        session = BrowserSession(APP.ReportEngine, lambda *args: None, headless=True)
        session.ready = True
        session.context = object()
        asyncio.run(session.check_window())
        session.resources = lambda: {'browser_tree_rss': 8 * 1024**3}
        self.assertFalse(session.needs_recycle())
        session.task_finished()
        self.assertTrue(session.needs_recycle())

    def test_minimized_headed_window_pauses_instead_of_marking_bad_parameters(self):
        session = BrowserSession(APP.ReportEngine, lambda *args: None)
        session.ready = True
        client = AsyncMock()
        client.send.return_value = {'bounds': {'windowState': 'minimized'}}
        session.context = AsyncMock()
        session.context.new_cdp_session.return_value = client
        with self.assertRaises(FormInteractionError):
            asyncio.run(session.check_window())
        client.detach.assert_awaited_once()


class ResourceControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = queue_tests.ControllerTests.asyncSetUp
    asyncTearDown = queue_tests.ControllerTests.asyncTearDown
    add = queue_tests.ControllerTests.add
    until = queue_tests.ControllerTests.until
    async def test_hidden_dropdown_is_interrupted_and_remaining_tasks_stay_pending(self):
        self.add(4)
        await self.controller.login('https://fixture.invalid/')
        self.session.outcomes = [FormInteractionError('下拉框未展开')]
        await self.controller.start()
        await self.until(lambda: self.controller.mode == 'paused')
        self.assertEqual(self.store.counts(), {'interrupted': 1, 'pending': 3})
        self.assertEqual(len(self.session.calls), 1)
        await self.controller.start()
        await self.until(lambda: self.store.counts().get('succeeded') == 4)

    async def test_pressure_waits_for_boundary_then_parks_and_resumes_without_relogin(self):
        self.add(3)
        await self.controller.login('https://fixture.invalid/')
        async def park():
            self.session.ready = False
            self.session.parked_state = {'in_memory_only': True}
        async def unpark():
            self.session.parked_state = None
            self.session.ready = True
        self.session.park = AsyncMock(side_effect=park)
        self.session.unpark = AsyncMock(side_effect=unpark)
        self.session.release.clear()
        await self.controller.start()
        await self.session.entered.wait()
        await self.controller.resource_gate(True, 'test pressure')
        self.session.park.assert_not_awaited()
        self.session.release.set()
        await self.until(lambda: self.controller.mode == 'resource_paused')
        await self.until(lambda: getattr(self.session, 'parked_state', None) is not None)
        self.assertEqual(self.store.counts(), {'succeeded': 1, 'pending': 2})
        await self.controller.resource_gate(False)
        await self.until(lambda: self.store.counts().get('succeeded') == 3)
        self.assertEqual(self.session.logins, 1)

    async def test_manual_pause_is_not_undone_when_pressure_clears(self):
        self.add(2)
        await self.controller.login('https://fixture.invalid/')
        self.session.release.clear()
        await self.controller.start()
        await self.session.entered.wait()
        self.session.park = AsyncMock()
        await self.controller.resource_gate(True)
        await self.controller.pause()
        self.session.release.set()
        await self.until(lambda: self.controller.current is None)
        await self.controller.resource_gate(False)
        await asyncio.sleep(.05)
        self.assertFalse(self.controller.run_requested)
        self.assertEqual(self.store.counts(), {'succeeded': 1, 'pending': 1})

    async def test_retry_all_includes_interrupted(self):
        self.add(1)
        task = self.store.claim_next()
        self.store.finish(task['id'], 'interrupted')
        await self.controller.login('https://fixture.invalid/')
        await self.controller.retry()
        await self.until(lambda: self.store.counts().get('succeeded') == 1)
