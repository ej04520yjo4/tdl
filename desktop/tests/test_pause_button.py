"""1.6.4: pause one download from the queue, keep a manual pause across
restarts, and cancel a paused job.  Never contacts Telegram."""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from desktop import backend
from desktop.app import MANUAL_PAUSE_MESSAGE, BackendBridge, DownloadJob, MainWindow, QueueRow
from desktop.backend import CLOSING_PAUSE_MESSAGE, Runner, Store

BUILD = Path(__file__).resolve().parents[2] / ".build"


class RowButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.rows: list[QueueRow] = []

    def tearDown(self) -> None:
        # A row left for interpreter shutdown is freed after QApplication and
        # crashes the test process on exit (0xC0000005 / 0xC0000409).
        for row in self.rows:
            row.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def row(self, status: str) -> QueueRow:
        row = QueueRow(DownloadJob(id="j", url="https://t.me/telegram/1", title="影片", output_dir=str(BUILD), status=status))
        row.set_state(status)
        self.rows.append(row)
        return row

    def test_buttons_follow_the_job_state(self) -> None:
        for status, pause, cancel, retry in (
            ("下載中", True, True, False),
            ("等待中", False, True, False),
            ("處理中", False, True, False),
            ("已暫停", False, True, True),
            ("失敗", False, False, True),
            ("已完成", False, False, False),
        ):
            with self.subTest(status=status):
                row = self.row(status)
                self.assertEqual(not row.pause_button.isHidden(), pause)
                self.assertEqual(not row.cancel_button.isHidden(), cancel)
                self.assertEqual(not row.retry_button.isHidden(), retry)
        self.assertEqual(self.row("已暫停").retry_button.text(), "繼續下載")

    def test_pause_button_asks_for_its_own_job(self) -> None:
        row = self.row("下載中")
        asked: list[str] = []
        row.pause_requested.connect(asked.append)
        row.pause_button.click()
        self.assertEqual(asked, ["j"])


class _WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="tdl-pause-test-", dir=BUILD)
        self.env = patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": self.directory})
        self.env.start()
        self.bridge = BackendBridge()
        self.account = self.bridge.store.add_account("暫停測試")
        self.bridge.store.update_account(self.account.id, authenticated=True)
        self.bridge.store.set_active_account(self.account.id)
        self.window = MainWindow(self.bridge, resume_downloads=False)
        self.window.show()
        self.pump(lambda: self.window._accounts_loaded and self.window._queue_loaded)

    def tearDown(self) -> None:
        if self.window.isVisible():
            self.window.close()
            self.pump(lambda: self.window.isHidden() and not self.window._background_work_running())
        self.window.deleteLater()
        self.app.processEvents()
        self.env.stop()
        shutil.rmtree(self.directory, ignore_errors=True)

    def pump(self, predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for the UI")


class WindowPauseTests(_WindowCase):
    def test_pause_button_pauses_the_running_download_manually(self) -> None:
        calls: list[bool] = []

        class Worker:
            def pause(self, *, manual: bool = False) -> None:
                calls.append(manual)

        self.window._workers["job-1"] = Worker()  # type: ignore[assignment]
        self.window.pause_job("job-1")
        self.assertEqual(calls, [True])
        self.window._workers.pop("job-1")

    def test_a_paused_job_can_be_cancelled_and_its_parts_go(self) -> None:
        store = self.bridge.store
        output = Path(self.directory) / "downloads"
        chat = output / "頻道"
        chat.mkdir(parents=True)
        saved = store.create_download_job(self.account.id, ["https://t.me/telegram/1"], output_dir=output)
        kept = chat / "1_1_a.mp4.tmp"
        part_list = chat / "1_1_a.mp4.tmp.part"
        kept.write_bytes(b"x")
        part_list.write_bytes(b"{}")
        store.update_job_progress(saved.id, file_progress={
            "1": {"filename": "1_1_a.mp4", "path": str(chat), "state": "downloading", "current": 1, "total": 9},
        })
        store.finish_job(saved.id, "paused", error=MANUAL_PAUSE_MESSAGE)

        job = DownloadJob(
            id=saved.id, url=saved.links[0], title="影片", output_dir=str(output), status="已暫停",
            account_id=self.account.id, links=list(saved.links), backend_job_id=saved.id,
        )
        self.window.jobs[job.id] = job
        self.window.home_page.add_jobs([job])
        row = self.window.home_page.rows[job.id]
        self.assertFalse(row.cancel_button.isHidden(), "a paused job offers 取消")

        row.cancel_button.click()
        self.pump(lambda: store.get_job(saved.id).status == "cancelled")
        self.assertEqual(job.status, "已取消")
        self.assertFalse(job.resume_on_start)
        self.assertFalse(kept.exists())
        self.assertFalse(part_list.exists())

    def test_a_waiting_job_without_a_download_can_be_cancelled(self) -> None:
        store = self.bridge.store
        saved = store.create_download_job(self.account.id, ["https://t.me/telegram/1"], output_dir=Path(self.directory) / "downloads")
        job = DownloadJob(
            id=saved.id, url=saved.links[0], title="影片", output_dir=str(Path(self.directory) / "downloads"),
            status="等待中", account_id=self.account.id, links=list(saved.links), backend_job_id=saved.id,
            resume_on_start=True,
        )
        self.window.jobs[job.id] = job
        self.window.home_page.add_jobs([job])
        row = self.window.home_page.rows[job.id]
        self.assertTrue(row.pause_button.isHidden(), "nothing is running yet")
        row.cancel_button.click()
        self.pump(lambda: store.get_job(saved.id).status == "cancelled")
        self.assertEqual(job.status, "已取消")
        self.assertFalse(job.resume_on_start)
        self.assertIn(job.id, self.window._resume_attempted)


class RestoreTests(unittest.TestCase):
    def test_a_manual_pause_waits_and_a_closing_pause_resumes(self) -> None:
        base = {"id": "a", "links": ["https://t.me/telegram/1"], "status": "paused"}
        manual = MainWindow._restore_job(dict(base, error=MANUAL_PAUSE_MESSAGE), 0)
        closing = MainWindow._restore_job(dict(base, error=CLOSING_PAUSE_MESSAGE), 0)
        interrupted = MainWindow._restore_job(dict(base, status="interrupted", error=""), 0)
        self.assertFalse(manual.resume_on_start)
        self.assertTrue(closing.resume_on_start)
        self.assertTrue(interrupted.resume_on_start)
        self.assertEqual(manual.status, "已暫停")

    def test_the_front_end_and_backend_agree_on_the_text(self) -> None:
        self.assertEqual(MANUAL_PAUSE_MESSAGE, backend.MANUAL_PAUSE_MESSAGE)


class _FixtureRunner(Runner):
    def __init__(self, store: Store, script: str) -> None:
        super().__init__(store, executable=sys.executable)
        self.script = script

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


class BackendPauseTests(unittest.TestCase):
    script = (
        "import json, sys\n"
        f"print({backend.EVENT_PREFIX!r} + json.dumps({{'event': 'progress', 'percent': 5}}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    if json.loads(line).get('command') in ('pause', 'cancel'):\n"
        "        raise SystemExit(0)\n"
    )

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-pause-test-", dir=BUILD))
        self.store = Store(self.root)
        self.account = self.store.add_account("暫停測試")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def stopped_job(self, *, manual: bool):  # type: ignore[no-untyped-def]
        handle = _FixtureRunner(self.store, self.script).start_download(
            self.account.id, ["https://t.me/telegram/1"], output_dir=self.root / "out"
        )
        self.assertIsNotNone(handle.get_event(timeout=5))
        handle.pause(manual=manual)
        result = handle.wait(timeout=10)
        self.assertTrue(result.paused)
        return self.store.get_job(result.job_id)

    def test_the_saved_reason_tells_the_two_pauses_apart(self) -> None:
        manual = self.stopped_job(manual=True)
        self.assertEqual((manual.status, manual.error), ("paused", MANUAL_PAUSE_MESSAGE))
        closing = self.stopped_job(manual=False)
        self.assertEqual((closing.status, closing.error), ("paused", CLOSING_PAUSE_MESSAGE))


if __name__ == "__main__":
    unittest.main()
