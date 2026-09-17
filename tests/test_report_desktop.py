"""Desktop protocol, immutable previews and safe browser-mode transitions."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from test_report_core import APP, ROOT, PLAN
from test_report_queue import FakeSession, OPTIONS, plan_fixture
from tmis_queue import QueueStore
from tmis_service import QueueController, SessionExpired
from tmis_worker import DesktopAPI


class ModeSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.headless = False
        self.transitions = []
        self.switch_failure = False

    async def switch_mode(self, headless):
        self.transitions.append((headless, len(self.calls)))
        self.headless = headless
        if self.switch_failure:
            self.ready = False
            raise SessionExpired('需要新的登录链接')


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = QueueStore(self.root / 'state')
        self.session = ModeSession()
        self.controller = QueueController(self.store, self.session, APP.REPORT_CONFIGS,
                                          APP.COLUMN_MAPPING, APP.ReportEngine, lambda *a, **k: None)
        self.api = DesktopAPI(self.controller, self.store)
        self.runner = asyncio.create_task(self.controller.serve())

    async def asyncTearDown(self):
        await self.controller.shutdown()
        await asyncio.wait_for(self.runner, 3)
        self.store.close()
        self.temp.cleanup()

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 4)

    async def begin(self):
        self.store.add_batch(plan_fixture(2), self.root)
        await self.controller.login('https://fixture.invalid/?token=NEVER_PERSIST')
        self.session.release.clear()
        await self.controller.start()
        await self.session.entered.wait()

    async def test_switch_is_deferred_until_task_finishes_and_never_repeats_success(self):
        await self.begin()
        await self.controller.request_mode(True)
        self.assertEqual(self.session.transitions, [])
        self.assertIs(self.controller.pending_headless, True)
        self.session.release.set()
        await self.until(lambda: self.store.counts().get('succeeded') == 2)
        self.assertEqual(self.session.transitions, [(True, 1)])
        self.assertEqual(len(set(self.session.calls)), 2)
        self.assertNotIn('NEVER_PERSIST', '\n'.join(self.store.connection.iterdump()))
        self.assertFalse(self.runner.done())

    async def test_pending_switch_can_be_cancelled_without_restarting_browser(self):
        await self.begin()
        await self.controller.request_mode(True)
        await self.controller.cancel_mode()
        self.session.release.set()
        await self.until(lambda: self.store.counts().get('succeeded') == 2)
        self.assertEqual(self.session.transitions, [])

    async def test_failed_migration_preserves_queue_and_resumes_after_manual_login(self):
        await self.begin()
        self.session.switch_failure = True
        await self.controller.request_mode(True)
        self.session.release.set()
        await self.until(lambda: self.controller.mode == 'waiting_login')
        self.assertEqual(self.store.counts(), {'pending': 1, 'succeeded': 1})
        await self.controller.login('https://fixture.invalid/?token=NEW_LINK')
        await self.until(lambda: self.store.counts().get('succeeded') == 2)
        self.assertEqual(len(self.session.calls), 2)

    async def test_pause_while_switching_does_not_resume_queue(self):
        await self.begin()
        gate = asyncio.Event()
        async def switch(target):
            await gate.wait()
            self.session.headless = target
        self.session.switch_mode = switch
        await self.controller.request_mode(True)
        self.session.release.set()
        await self.until(lambda: self.controller.mode == 'switching')
        await self.controller.pause()
        gate.set()
        await self.until(lambda: self.controller.mode == 'idle')
        self.assertEqual(self.store.counts(), {'pending': 1, 'succeeded': 1})
        self.assertFalse(self.controller.run_requested)

    async def test_relogin_cannot_race_pending_mode_switch(self):
        await self.controller.request_mode(True)
        with self.assertRaises(ValueError):
            await self.controller.login('https://fixture.invalid/')

    async def test_preview_is_immutable_and_consumed_once(self):
        source = self.root / '库存.xlsx'
        source.write_bytes((ROOT / PLAN['output_file']).read_bytes())
        plans = await self.api.dispatch('preview', {'paths': [str(source)], 'options': OPTIONS})
        self.assertEqual(plans[0]['count'], 280)
        source.write_bytes(b'changed-after-preview')
        result = await self.api.dispatch('import_preview', {'ids': [plans[0]['id']], 'root': str(self.root)})
        self.assertEqual(result['accepted'][0]['count'], 280)
        with self.assertRaises(ValueError):
            await self.api.dispatch('import_preview', {'ids': [plans[0]['id']], 'root': str(self.root)})
        self.assertFalse(self.controller.run_requested)
        self.assertEqual(self.store.counts(), {'pending': 280})

    async def test_preview_duplicate_errors_invalid_commands_and_private_snapshot(self):
        args = {'paths': [str(ROOT / PLAN['output_file'])], 'options': OPTIONS}
        first = await self.api.dispatch('preview', args)
        await self.api.dispatch('import_preview', {'ids': [first[0]['id']], 'root': str(self.root)})
        second = await self.api.dispatch('preview', args)
        self.assertTrue(second[0]['duplicate'])
        result = await self.api.dispatch('import_preview', {'ids': [second[0]['id']], 'root': str(self.root)})
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(len(self.store.batches()), 1)
        snap = await self.api.dispatch('snapshot', {})
        self.assertNotIn('params', snap['tasks'][0])
        self.assertEqual(snap['tasks'][0]['treasury'], '1002000000')
        for command, data in [('__getattribute__', {}), ('login', {'url': 4}), ('start', {'restore_interrupted': False}),
                              ('remove', {'ids': 'all'}), ('request_mode', {'headless': 'yes'})]:
            with self.assertRaises(ValueError):
                await self.api.dispatch(command, data)


class WorkerProcessTests(unittest.TestCase):
    def test_legacy_backup_precedes_recovery_and_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(directory)
            batch = store.add_batch(plan_fixture(2), directory)
            succeeded = store.claim_next()
            store.finish(succeeded['id'], 'succeeded', saved_path='already-downloaded.xlsx')
            interrupted = store.claim_next()
            store.close()
            upgraded = QueueStore(directory, backup_legacy=True)
            backup = Path(directory) / 'queue.pre-workspaces.sqlite3'
            with sqlite3.connect(str(backup)) as copy:
                self.assertEqual(copy.execute('SELECT status FROM tasks WHERE id=?', (interrupted['id'],)).fetchone()[0], 'running')
                self.assertEqual(copy.execute('SELECT status FROM tasks WHERE id=?', (succeeded['id'],)).fetchone()[0], 'succeeded')
            self.assertEqual(upgraded.counts(), {'interrupted': 1, 'succeeded': 1})
            original = backup.read_bytes()
            with self.assertRaises(RuntimeError):
                QueueStore(directory, backup_legacy=True)
            upgraded.close()
            again = QueueStore(directory, backup_legacy=True)
            again.close()
            self.assertEqual(backup.read_bytes(), original)

    def test_disconnect_keeps_queue_store_usable(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                store = QueueStore(directory)
                session = ModeSession()
                controller = QueueController(store, session, APP.REPORT_CONFIGS, APP.COLUMN_MAPPING, APP.ReportEngine, lambda *a, **k: None)
                store.add_batch(plan_fixture(1), directory)
                controller.current = 'busy'
                with self.assertRaises(ValueError):
                    await controller.disconnect()
                controller.current = None
                await controller.disconnect()
                self.assertEqual(store.counts(), {'pending': 1})
                self.assertFalse(session.ready)
                store.close()
        asyncio.run(check())

    def test_stdio_protocol_and_eof_release_queue_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, TMIS_STATE_DIR=directory, PYTHONUNBUFFERED='1')
            child = subprocess.Popen([sys.executable, str(ROOT / 'tmis_worker.py')], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            stdout, stderr = child.communicate('{"id":1,"command":"snapshot"}\n{"id":2,"command":"not_allowed"}\n', timeout=15)
            messages = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(child.returncode, 0, stderr)
            self.assertTrue(any(m.get('event') == 'ready' for m in messages))
            self.assertTrue(any(m.get('id') == 2 and m.get('error') for m in messages))
            store = QueueStore(directory)
            self.assertEqual(store.tasks(), [])
            store.close()
