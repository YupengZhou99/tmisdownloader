"""Real Tk widgets and callbacks; no network or user task database involved."""
import os
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from test_report_core import APP
from test_report_queue import plan_fixture
import tmis_ui


class FakeService:
    def __init__(self, *args):
        self.events = queue.Queue()
        self.calls = []
        self.alive = True
        self.thread = self

    def is_alive(self):
        return self.alive

    def submit(self, command, *args, **kwargs):
        self.calls.append((command, args, kwargs))

    def shutdown(self):
        self.alive = False


@unittest.skipUnless(os.environ.get('TMIS_GUI_TESTS') == '1', 'set TMIS_GUI_TESTS=1 with a desktop or Xvfb')
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = APP.TMISAutoApp(state_dir=self.root / 'state', service_factory=FakeService)
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        if not self.app._closed_store:
            self.app.after_cancel(self.app._poll_id)
            self.app.service.shutdown()
            self.app.store.close()
            self.app.destroy()
        self.temp.cleanup()

    def drain(self):
        self.app.after_cancel(self.app._poll_id)
        self.app._poll()
        if not self.app._closed_store:
            self.app.update()

    def test_login_before_any_parameters_or_folder(self):
        with patch.object(tmis_ui.simpledialog, 'askstring', return_value='https://example.invalid/?token=memory-only'):
            self.app.login_btn.invoke()
        self.assertEqual(self.app.service.calls[0][0], 'login')
        self.assertEqual(self.app.store.tasks(), [])
        self.assertEqual(self.app.download_folder.get(), '')
        self.assertNotIn('memory-only', '\n'.join(self.app.store.connection.iterdump()))

    def test_multi_select_and_append_remain_enabled_while_running(self):
        self.app._update_status(dict(mode='running', session_ready=True, current='current'))
        self.assertEqual(self.app.select_btn['state'], 'normal')
        self.assertEqual(self.app.import_btn['state'], 'normal')
        self.assertEqual(self.app.login_btn['state'], 'disabled')
        with patch.object(tmis_ui.filedialog, 'askopenfilenames', return_value=('/example/income.xlsx', '/example/expense.xlsx')):
            self.app.select_btn.invoke()
        self.app.download_folder.set(str(self.root))
        self.app.import_btn.invoke()
        command, args, kwargs = self.app.service.calls[-1]
        self.assertEqual(command, 'import_files')
        self.assertEqual(args[0], ['/example/income.xlsx', '/example/expense.xlsx'])
        self.assertFalse(kwargs['start_after'])
        self.app.enable_postprocess.set(True)
        self.assertFalse(args[2]['postprocess'], 'previous import options must not follow later UI edits')
        self.app.import_btn.invoke()
        self.assertEqual(len(self.app.service.calls), 1, 'do not double-submit an active import')

    def test_failed_submission_does_not_lock_staged_files(self):
        self.app.selected_files = ['test.xlsx']
        self.app.download_folder.set(str(self.root))
        with patch.object(self.app.service, 'submit', side_effect=RuntimeError('service stopped')), patch.object(tmis_ui.messagebox, 'showerror'):
            self.app._enqueue_selected()
        self.assertEqual(self.app._import_submitted, set())

    def test_clear_staged_selection_does_not_remove_queued_tasks(self):
        self.app.store.add_batch(plan_fixture(1), self.root)
        self.app.selected_files = ['wrong.xlsx', 'importing.xlsx']
        self.app._import_submitted.add('importing.xlsx')
        self.app._clear_selected()
        self.assertEqual(self.app.selected_files, ['importing.xlsx'])
        self.assertEqual(self.app.store.counts(), {'pending': 1})

    def test_completed_round_shows_failure_list_and_selected_retry(self):
        self.app.store.add_batch(plan_fixture(2), self.root)
        failed = self.app.store.claim_next()
        self.app.store.finish(failed['id'], 'timed_out', 'local fixture timeout')
        success = self.app.store.claim_next()
        self.app.store.finish(success['id'], 'succeeded')
        self.app.service.events.put(dict(event='completed', counts={'succeeded': 1, 'timed_out': 1}))
        self.drain()
        self.assertEqual(self.app.notebook.select(), str(self.app.failure_tab))
        self.assertEqual(self.app.failure_tree.get_children(), (failed['id'],))
        self.app.failure_tree.selection_set(failed['id'])
        with patch.object(tmis_ui.messagebox, 'askyesno', return_value=True):
            self.app._retry_selected(True)
        self.assertEqual(self.app.service.calls[-1], ('retry', ([failed['id']],), {}))

    def test_pause_does_not_shutdown_and_exit_requires_confirmation(self):
        self.app._stop_task()
        self.assertTrue(self.app.service.is_alive())
        self.assertEqual(self.app.service.calls[-1][0], 'pause')
        with patch.object(tmis_ui.messagebox, 'askyesno', return_value=False):
            self.app._on_close()
        self.assertFalse(self.app._closing)
        with patch.object(tmis_ui.messagebox, 'askyesno', return_value=True):
            self.app._on_close()
        self.assertFalse(self.app.service.is_alive())
        self.drain()
        self.assertTrue(self.app._closed_store)


if __name__ == '__main__':
    unittest.main()
