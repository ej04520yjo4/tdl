"""Offline close/reopen acceptance using real Qt workers and session storage."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from desktop.app import BackendBridge, MainWindow
from desktop.backend import Runner


CORE = r'''
import json, pathlib, sys
runtime, output = map(pathlib.Path, sys.argv[1:3])
output.mkdir(parents=True, exist_ok=True)
marker = runtime / 'fixture-checkpoint.json'
def emit(data): print('@@TDL_GUI@@' + json.dumps(data), flush=True)
if not marker.exists():
    (output / 'first.mp4').write_bytes(b'completed-file')
    marker.write_text('completed-first-file')
    emit({'event':'progress','file_id':'1','filename':'first.mp4','state':'completed','current':14,'total':14,'percent':100})
    emit({'event':'progress','file_id':'2','filename':'second.mp4','state':'downloading','current':5,'total':20,'percent':25})
    sys.stdin.readline()
else:
    (output / 'second.mp4').write_bytes(b'remaining-file')
    emit({'event':'progress','file_id':'2','filename':'second.mp4','state':'completed','current':14,'total':14,'percent':100})
'''


class RestartUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "GUI workflow timed out")

    def clean_window(self, window):
        from shiboken6 import isValid
        if not isValid(window):
            return
        window.close()
        deadline = time.monotonic() + 15
        while window._background_work_running() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()

    def test_close_reopen_auto_resumes_same_account_and_job(self):
        self._restart_case(explicit_cancel=False)

    def test_manual_cancel_is_remembered_without_auto_restart(self):
        self._restart_case(explicit_cancel=True)

    def test_close_or_cancel_while_manifest_is_being_saved_never_starts_core(self):
        root = Path(__file__).resolve().parents[2]
        for explicit_cancel in (False, True):
            with self.subTest(explicit_cancel=explicit_cancel), tempfile.TemporaryDirectory(dir=root / ".build") as directory:
                with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                    bridge = BackendBridge()
                    account = bridge.store.add_account("儲存期間關閉測試")
                    bridge.store.update_account(account.id, authenticated=True)
                    entered, release = threading.Event(), threading.Event()
                    original_prepare = bridge.prepare_download

                    def delayed_prepare(job, options):
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError("fixture release timed out")
                        return original_prepare(job, options)

                    bridge.prepare_download = delayed_prepare
                    with patch.object(bridge, "download") as download:
                        window = MainWindow(bridge)
                        self.addCleanup(self.clean_window, window)
                        window.show()
                        self.pump(lambda: window._accounts_loaded and window._queue_loaded)
                        window._enqueue_downloads(["https://t.me/telegram/193"], {
                            "account_id": account.id, "output_dir": str(Path(directory) / "downloads"),
                        })
                        self.pump(entered.is_set)
                        job = next(iter(window.jobs.values()))
                        if explicit_cancel:
                            window.cancel_job(job.id)
                        window.close()
                        release.set()
                        self.pump(lambda: window.isHidden() and not window._background_work_running())
                        self.assertEqual(bridge.store.get_job(job.backend_job_id).status, "cancelled" if explicit_cancel else "paused")
                        download.assert_not_called()
                        window.deleteLater()
                        self.app.processEvents()

    def _restart_case(self, *, explicit_cancel):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=root / ".build") as directory:
            state = Path(directory)
            script = state / "fake_core.py"
            script.write_text(CORE, encoding="utf-8")
            output = state / "downloads"
            built_args = []

            class FixtureRunner(Runner):
                def _build_args(self, request, runtime):
                    built_args.append(super()._build_args(request, runtime))
                    return [sys.executable, "-u", str(script), str(runtime), str(output)]

            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": str(state)}):
                bridge = BackendBridge()
                account = bridge.store.add_account("重新啟動測試")
                bridge.store.update_account(account.id, authenticated=True)
                other = bridge.store.add_account("目前選取的另一個帳號")
                bridge.store.update_account(other.id, authenticated=True)
                bridge.store.set_active_account(other.id)
                bridge.runner = FixtureRunner(bridge.store, executable=sys.executable)
                window = MainWindow(bridge)
                self.addCleanup(self.clean_window, window)
                window.show()
                self.pump(lambda: window._accounts_loaded and window._queue_loaded)
                window._enqueue_downloads(["https://t.me/telegram/193"], {
                    "account_id": account.id, "output_dir": str(output),
                    "continue_download": True, "skip_existing": True,
                })
                self.pump(lambda: bool(window.jobs) and bool(next(iter(window.jobs.values())).file_progress.get("2")))
                job = next(iter(window.jobs.values()))
                saved_id = job.backend_job_id
                self.assertTrue(saved_id)
                first_stamp = (output / "first.mp4").stat().st_mtime_ns
                if explicit_cancel:
                    window.cancel_job(job.id)
                    self.pump(lambda: not window._threads)
                window.close()
                self.pump(lambda: window.isHidden() and not window._background_work_running())
                self.assertEqual(bridge.store.get_job(saved_id).status, "cancelled" if explicit_cancel else "paused")
                self.assertEqual(bridge.store.get_job(saved_id).file_progress["1"]["state"], "completed")
                window.deleteLater()
                self.app.processEvents()

                reopened_bridge = BackendBridge()
                reopened_bridge.runner = FixtureRunner(reopened_bridge.store, executable=sys.executable)
                reopened = MainWindow(reopened_bridge)
                self.addCleanup(self.clean_window, reopened)
                reopened.show()
                self.pump(lambda: reopened._accounts_loaded and reopened._queue_loaded)
                self.assertEqual(reopened.home_page.account_combo.currentData(), other.id)
                restored = next(iter(reopened.jobs.values()))
                self.assertEqual(restored.file_progress["1"]["state"], "completed")
                if explicit_cancel:
                    self.assertFalse(reopened._threads)
                    self.assertFalse(reopened._preparing_jobs)
                    self.assertFalse((output / "second.mp4").exists())
                else:
                    self.pump(lambda: reopened_bridge.store.get_job(saved_id).status == "completed" and not reopened._threads)
                    self.assertTrue((output / "second.mp4").exists())
                    self.assertEqual((output / "first.mp4").stat().st_mtime_ns, first_stamp)
                    self.assertIn("--continue", built_args[-1])
                self.assertEqual(len(reopened_bridge.store.list_queue()), 1)
                self.assertEqual(len(reopened_bridge.store.list_history()), 1)
                self.assertEqual(reopened_bridge.store.get_job(saved_id).account_id, account.id)
                reopened.close()
                self.pump(lambda: reopened.isHidden() and not reopened._background_work_running())
                reopened.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
