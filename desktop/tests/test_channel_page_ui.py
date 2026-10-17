"""Offline render/workflow fixtures for the channel download page."""

from __future__ import annotations

import os
import queue
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from desktop.app import AccountInfo, BackendBridge, MainWindow
from desktop.channel_page import ChannelPage


class _Result:
    success = True
    cancelled = False
    error = None

    def __init__(self, data):
        self.data = data


class _Handle:
    def __init__(self, data):
        self.done = threading.Event()
        self.result = _Result(data)
        self.events: list[dict] = []
        self.cancelled = False

        def finish():
            time.sleep(0.01)
            self.events.append({"event": "status", "message": "fixture"})
            self.done.set()

        threading.Thread(target=finish, daemon=True).start()

    def get_event(self, timeout=None):
        deadline = time.monotonic() + float(timeout or 0)
        while not self.events and not self.done.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        return self.events.pop(0) if self.events else None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.result

    def cancel(self):
        self.cancelled = True
        self.done.set()


class _FixtureRunner:
    fixture_chats = [
        {"id": "-1001", "ref": "channel:1001", "type": "channel", "title": "台灣攝影頻道", "username": "taiwan_photo"},
        {"id": "-1002", "ref": "chat:1002", "type": "supergroup", "title": "器材交流群組", "username": "camera_group"},
    ]

    def start_list_chats(self, account_id, on_event=None):
        return _Handle({"chats": self.fixture_chats})

    def start_export(self, account_id, chat_id, start_date, end_date, media_types, on_event=None):
        self.last_chat_id = chat_id
        return _Handle({"path": "C:/fixture/channel-export.json", "count": 3})


class _BulkHandle:
    """Thread-safe fixture with a dense event stream and controllable cancel."""

    def __init__(self, chats, *, status_events=0, delay=0.0):
        self.done = threading.Event()
        self.cancelled = False
        self.result = _Result({"chats": list(chats)})
        self._events: queue.Queue[dict] = queue.Queue()
        for index in range(status_events):
            self._events.put({"event": "status", "message": f"fixture status {index}"})
        if delay:
            def finish_later():
                time.sleep(delay)
                self.done.set()

            threading.Thread(target=finish_later, daemon=True).start()
        else:
            self.done.set()

    def get_event(self, timeout=None):
        try:
            return self._events.get(timeout=float(timeout or 0))
        except queue.Empty:
            return None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.result

    def cancel(self):
        self.cancelled = True
        self.result.cancelled = True
        self.done.set()


class _BulkRunner:
    def __init__(self, chats, *, status_events=0, first_delay=0.0):
        self.chats = list(chats)
        self.status_events = status_events
        self.first_delay = first_delay
        self.calls = 0
        self.handles = []

    def start_list_chats(self, account_id, on_event=None):
        self.calls += 1
        handle = _BulkHandle(
            self.chats,
            status_events=self.status_events,
            delay=self.first_delay if self.calls == 1 else 0.0,
        )
        self.handles.append(handle)
        return handle


class _ThreadRecordingPage(ChannelPage):
    """Record the thread used by real UI mutations made by operation slots."""

    def __init__(self):
        self.gui_thread_id = threading.get_ident()
        self.operation_status_threads = []
        self.record_operation_status = False
        super().__init__()

    def _set_status(self, text):
        if self.record_operation_status:
            self.operation_status_threads.append(
                (
                    threading.get_ident(),
                    QThread.currentThread() == self.thread(),
                    str(text),
                )
            )
        return super()._set_status(text)


@unittest.skipUnless(QApplication, "PySide6 is required")
class ChannelPageFixtureTests(unittest.TestCase):
    def test_channel_scan_then_file_only_download_request(self):
        app = QApplication.instance() or QApplication([])
        page = ChannelPage()
        runner = _FixtureRunner()
        page.set_runner(runner)
        page.set_accounts([{"id": "acct-1", "label": "測試帳號", "authenticated": True}], "acct-1")
        page.set_download_folder("C:/Downloads")
        requests = []
        page.download_requested.connect(lambda urls, options: requests.append((urls, options)))
        page.refresh_channels()

        deadline = time.monotonic() + 2
        while page.chat_list.count() < 2 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertEqual(page.chat_list.count(), 2)
        page.chat_list.setCurrentRow(0)
        page._scan_clicked()
        deadline = time.monotonic() + 2
        while not page.download_button.isEnabled() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertTrue(page.download_button.isEnabled())
        self.assertEqual(runner.last_chat_id, "channel:1001")
        export_path = page._export_path
        page.set_accounts([{"id": "acct-1", "label": "測試帳號（更新）", "authenticated": True}], "acct-1")
        self.assertEqual(page._export_path, export_path)
        page._download_clicked()
        self.assertEqual(len(requests), 1)
        urls, options = requests[0]
        self.assertEqual(urls, [])
        self.assertEqual(options["files"], ["C:/fixture/channel-export.json"])
        self.assertFalse(options["group_album"])
        self.assertIn("台灣攝影頻道", options["title"])
        page.shutdown()
        page.deleteLater()

    def test_replacing_query_waits_for_old_thread_and_account_switch_invalidates(self):
        app = QApplication.instance() or QApplication([])
        page = ChannelPage()
        page.set_runner(_FixtureRunner())
        page.set_accounts([{"id": "acct-1", "label": "帳號一", "authenticated": True}], "acct-1")
        page.refresh_channels()
        page.refresh_channels()  # refresh button is disabled visually, but this is a stale-call guard
        page.set_accounts([{"id": "acct-2", "label": "帳號二", "authenticated": True}], "acct-2")
        deadline = time.monotonic() + 2
        while page.is_busy() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertFalse(page.is_busy())
        self.assertEqual(page.active_account_id(), "acct-2")
        self.assertEqual(page.chat_list.count(), 0)
        page.shutdown()
        page.deleteLater()

    def test_filter_hiding_selected_chat_invalidates_manifest(self):
        app = QApplication.instance() or QApplication([])
        page = ChannelPage()
        page.set_accounts([{"id": "acct-1", "label": "帳號一", "authenticated": True}], "acct-1")
        page._chats = list(_FixtureRunner.fixture_chats)
        page._apply_filter()
        page.chat_list.setCurrentRow(0)
        page._export_path = "C:/fixture/visible.json"
        page._export_count = 2
        page.download_button.setEnabled(True)

        page.search_edit.setText("台灣攝影")
        self.assertIsNotNone(page._selected_chat)
        self.assertEqual(page._export_path, "C:/fixture/visible.json")
        self.assertTrue(page.download_button.isEnabled())

        page.search_edit.setText("不存在的頻道")
        self.assertIsNone(page._selected_chat)
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())
        self.assertFalse(page.scan_button.isEnabled())
        page.deleteLater()

    def test_simplified_search_matches_channel_without_changing_visible_title(self):
        app = QApplication.instance() or QApplication([])
        page = ChannelPage()
        page.set_accounts([{"id": "acct-1", "label": "帳號一", "authenticated": True}], "acct-1")
        page._chats = list(_FixtureRunner.fixture_chats)
        page._apply_filter()

        page.search_edit.setText("台湾摄影")

        self.assertEqual(page.chat_list.count(), 1)
        self.assertEqual(page.chat_list.item(0).text(), "台灣攝影頻道  @taiwan_photo")
        self.assertEqual(page.chat_list.item(0).data(256)["title"], "台灣攝影頻道")
        page.search_edit.setText("@TAIWAN_PHOTO")
        self.assertEqual(page.chat_list.count(), 1)
        self.assertEqual(page.chat_list.item(0).data(256)["title"], "台灣攝影頻道")
        page.deleteLater()

    def test_empty_partial_chat_result_keeps_incomplete_warning(self):
        app = QApplication.instance() or QApplication([])
        page = ChannelPage()
        page._operation_succeeded(
            page._generation,
            "list",
            {"chats": [], "partial": True, "skipped": 2, "message": "部分對話無法讀取"},
        )
        self.assertIn("不完整", page.status_label.text())
        self.assertIn("略過 2", page.status_label.text())
        self.assertIn("不完整", page.chat_hint.text())
        page.deleteLater()

    def test_large_chat_stream_updates_only_from_gui_thread(self):
        app = QApplication.instance() or QApplication([])
        chats = [
            {
                "id": str(index),
                "ref": f"channel:{index}",
                "type": "channel",
                "title": f"頻道 {index}",
            }
            for index in range(618)
        ]
        page = _ThreadRecordingPage()
        page.set_runner(_BulkRunner(chats, status_events=618, first_delay=0.1))
        page.set_accounts([{"id": "acct-bulk", "label": "大量清單", "authenticated": True}], "acct-bulk")
        page.record_operation_status = True
        page.refresh_channels()

        deadline = time.monotonic() + 5
        while (
            (page.is_busy() or page.chat_list.count() < len(chats) or len(page.operation_status_threads) < 618)
            and time.monotonic() < deadline
        ):
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        self.assertEqual(page.chat_list.count(), 618)
        self.assertGreaterEqual(len(page.operation_status_threads), 618)
        self.assertTrue(
            all(thread_id == page.gui_thread_id and is_gui for thread_id, is_gui, _text in page.operation_status_threads),
            page.operation_status_threads[:3],
        )
        self.assertFalse(page.is_busy())
        self.assertEqual(page._thread_workers, {})
        page.shutdown()
        page.deleteLater()

    def test_cancel_then_refresh_replaces_operation_and_cleans_old_thread(self):
        app = QApplication.instance() or QApplication([])
        chats = [
            {"id": "1", "ref": "channel:1", "type": "channel", "title": "最新清單"}
        ]
        runner = _BulkRunner(chats, first_delay=0.6)
        page = ChannelPage()
        page.set_runner(runner)
        page.set_accounts([{"id": "acct-refresh", "label": "快速操作", "authenticated": True}], "acct-refresh")

        page.refresh_channels()
        deadline = time.monotonic() + 2
        while page._worker is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        self.assertIsNotNone(page._worker)

        # A second refresh queues only the newest request while the first
        # handle receives a real cancel signal.  The old thread must not own
        # the page references or reset controls after the replacement starts.
        page.refresh_channels()
        deadline = time.monotonic() + 5
        while (page.is_busy() or page.chat_list.count() != 1) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        self.assertGreaterEqual(runner.calls, 2)
        self.assertTrue(runner.handles[0].cancelled)
        self.assertEqual(page.chat_list.count(), 1)
        self.assertEqual(page.chat_list.item(0).text(), "最新清單")
        self.assertFalse(page.is_busy())
        self.assertIsNone(page._thread)
        self.assertIsNone(page._worker)
        self.assertEqual(page._thread_workers, {})
        page.shutdown()
        page.deleteLater()

    def test_explicit_cancel_waits_for_worker_cleanup(self):
        app = QApplication.instance() or QApplication([])
        runner = _BulkRunner(
            [{"id": "1", "ref": "channel:1", "type": "channel", "title": "可取消清單"}],
            first_delay=1.0,
        )
        page = ChannelPage()
        page.set_runner(runner)
        page.set_accounts([{"id": "acct-cancel", "label": "取消測試", "authenticated": True}], "acct-cancel")
        page.refresh_channels()
        deadline = time.monotonic() + 2
        while page._worker is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        self.assertIsNotNone(page._worker)

        page.cancel_operations()
        deadline = time.monotonic() + 3
        while page.is_busy() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()

        self.assertTrue(runner.handles[0].cancelled)
        self.assertFalse(page.is_busy())
        self.assertIsNone(page._thread)
        self.assertIsNone(page._worker)
        self.assertEqual(page._thread_workers, {})
        page.shutdown()
        page.deleteLater()

    def test_channel_download_signal_shows_queue_only_after_job_is_added(self):
        app = QApplication.instance() or QApplication([])
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repo_root / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                account = bridge.store.add_account("頻道下載測試")
                bridge.store.update_account(account.id, authenticated=True)
                window = MainWindow(bridge)
                # ``add_account`` creates an unauthenticated record; the
                # signal handler should be exercised with the same connected
                # account state that the accounts page supplies after a
                # successful login.
                info = AccountInfo(id=account.id, label="頻道下載測試", authenticated=True)
                window.accounts = [info]
                window.home_page.set_accounts(window.accounts, account.id)
                window.channel_page.set_accounts(window.accounts, account.id)
                window._show_widget(window.channel_page)

                options = {
                    "account_id": account.id,
                    "output_dir": directory,
                    "files": [str(Path(directory) / "channel-export.json")],
                    "title": "測試頻道 · 2026-01-01～2026-01-02",
                    "group_album": False,
                }
                with patch.object(window, "_start_job") as start_job:
                    window.channel_page.download_requested.emit([], options)
                    app.processEvents()
                    current = window.stack.currentWidget().widget()
                    self.assertIs(current, window.home_page)
                    self.assertEqual(start_job.call_count, 1)

                window._show_widget(window.channel_page)
                before = set(window.jobs)
                with patch("desktop.app.show_error"):
                    window.channel_page.download_requested.emit(
                        [], {"account_id": "", "output_dir": directory, "files": ["export.json"]}
                    )
                    app.processEvents()
                current = window.stack.currentWidget().widget()
                self.assertIs(current, window.channel_page)
                self.assertEqual(set(window.jobs), before)

                # MainWindow starts local Store reads on construction.  Drain
                # those short-lived QThreads before the temporary data root is
                # removed, otherwise Windows can still hold accounts.json.
                deadline = time.monotonic() + 5
                while window._background_work_running() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                window.close()
                deadline = time.monotonic() + 5
                while not window.isHidden() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(window.isHidden())


if __name__ == "__main__":
    unittest.main()
