"""Focused offline contracts for the channel scan and queue workflow.

These tests keep Telegram out of the loop.  The fake runner only models the
small RunHandle surface consumed by ``ChannelPage`` so the tests can exercise
selection invalidation, late results, and the queue acknowledgement contract
deterministically.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.channel_page import ChannelPage


class _Result:
    def __init__(self, data=None, *, success: bool = True, error: str = "") -> None:
        self.data = data
        self.success = success
        self.error = error
        self.cancelled = False


class _Handle:
    def __init__(self, data=None, *, delay: float = 0.0) -> None:
        self.done = threading.Event()
        self.result = _Result(data)
        self.cancelled = False
        self._delay = float(delay)
        if self._delay <= 0:
            self.done.set()
        else:
            threading.Thread(target=self._finish_later, daemon=True).start()

    def _finish_later(self) -> None:
        time.sleep(self._delay)
        self.done.set()

    def get_event(self, timeout=None):
        if timeout:
            self.done.wait(float(timeout))
        return None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.result

    def cancel(self) -> None:
        self.cancelled = True
        self.result.cancelled = True
        self.done.set()


class _StoppedThread:
    """A completed QThread stand-in whose queued GUI result is still pending."""

    def isRunning(self) -> bool:
        return False


class _StoppedWorker:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _Runner:
    chats = [
        {
            "id": "-1001",
            "ref": "channel:source-a",
            "type": "channel",
            "title": "來源 A",
            "username": "source_a",
        },
        {
            "id": "-1002",
            "ref": "channel:source-b",
            "type": "group",
            "title": "來源 B",
            "username": "source_b",
        },
    ]
    forum = {
        "id": "-1003",
        "ref": "channel:forum",
        "type": "supergroup",
        "title": "論壇來源",
        "is_forum": True,
    }
    topics = [
        {"id": "101", "title": "公告"},
        {"id": "202", "title": "問題討論"},
    ]

    def __init__(self, *, export_delay: float = 0.0) -> None:
        self.export_delay = export_delay
        self.export_calls: list[dict] = []
        self.export_handles: list[_Handle] = []

    def start_list_chats(self, account_id, on_event=None):
        return _Handle({"chats": list(self.chats)})

    def start_list_topics(self, account_id, chat_ref, on_event=None):
        return _Handle({"topics": list(self.topics)})

    def start_export(
        self,
        account_id,
        chat_id,
        start_date,
        end_date,
        media_types,
        *,
        topic_id=None,
        on_event=None,
        **_kwargs,
    ):
        call = {
            "account_id": account_id,
            "chat_id": chat_id,
            "start_date": start_date,
            "end_date": end_date,
            "media_types": list(media_types),
            "topic_id": topic_id,
        }
        self.export_calls.append(call)
        handle = _Handle(
            {
                "path": f"C:/fixture/{len(self.export_calls)}.json",
                "count": 2,
                "total_bytes": 2048,
            },
            delay=self.export_delay,
        )
        self.export_handles.append(handle)
        return handle


def _wait_until(app: QApplication, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


class ChannelWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _page(self, runner: _Runner | None = None) -> ChannelPage:
        page = ChannelPage()
        page.set_runner(runner or _Runner())
        page.set_accounts(
            [{"id": "acct", "label": "測試帳號", "authenticated": True}],
            "acct",
        )
        page.set_download_folder("C:/Downloads/Telegram")
        page._chats = list((runner or page._runner).chats)
        page._apply_filter()
        self.addCleanup(page.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def _select(self, page: ChannelPage, row: int) -> None:
        page.chat_list.setCurrentRow(row)
        self.app.processEvents()
        _wait_until(self.app, lambda: not page.is_busy())

    def _scan(self, page: ChannelPage) -> None:
        page._scan_clicked()
        _wait_until(self.app, lambda: bool(page._export_path) and not page.is_busy())
        self.assertTrue(page._export_path, page.status_label.text())
        self.assertEqual(page._export_count, 2)
        self.assertTrue(page.download_button.isEnabled())

    def test_switching_source_clears_snapshot_summary_and_download(self) -> None:
        page = self._page()
        self._select(page, 0)
        self._scan(page)
        old_summary = page.scan_summary_label.text()
        self.assertIn("掃描結果", old_summary)

        self._select(page, 1)

        self.assertEqual(page._selected_chat["ref"], "channel:source-b")
        self.assertEqual(page._export_path, "")
        self.assertEqual(page._export_count, 0)
        self.assertFalse(page.download_button.isEnabled())
        self.assertNotEqual(page.scan_summary_label.text(), old_summary)
        self.assertNotIn("可以開始下載", page.status_label.text())

    def test_date_media_and_hidden_filter_invalidate_scan(self) -> None:
        page = self._page()
        self._select(page, 0)
        self._scan(page)

        page.start_date.setDate(page.start_date.date().addDays(-1))
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

        self._scan(page)
        page.video_check.setChecked(False)
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

        page.video_check.setChecked(True)
        self._scan(page)
        page.search_edit.setText("來源 B")
        self.assertIsNone(page._selected_chat)
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

    def test_same_account_metadata_and_visible_filter_keep_valid_snapshot(self) -> None:
        page = self._page()
        self._select(page, 0)
        self._scan(page)
        path = page._export_path

        page.search_edit.setText("來源 A")
        page.type_combo.setCurrentIndex(1)  # 頻道；來源 A 仍可見
        page.set_accounts(
            [{"id": "acct", "label": "測試帳號（更新）", "authenticated": True}],
            "acct",
        )

        self.assertEqual(page._selected_chat["ref"], "channel:source-a")
        self.assertEqual(page._export_path, path)
        self.assertTrue(page.download_button.isEnabled())

    def test_late_scan_result_is_ignored_after_input_change_and_cancel(self) -> None:
        runner = _Runner(export_delay=1.0)
        page = self._page(runner)
        self._select(page, 0)
        page._scan_clicked()
        _wait_until(self.app, lambda: bool(runner.export_handles))
        old_token = page._generation

        page.start_date.setDate(page.start_date.date().addDays(-1))
        page._operation_succeeded(
            old_token,
            "export",
            {"path": "C:/fixture/stale-date.json", "count": 9},
        )
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

        # A cancel must invalidate the same token even when a late result is
        # delivered synchronously by a fast fixture.
        page.video_check.setChecked(True)
        page._selected_chat = page._chats[0]
        page._scan_clicked()
        _wait_until(self.app, lambda: len(runner.export_handles) >= 2)
        old_token = page._generation
        page.cancel_operations()
        page._operation_succeeded(
            old_token,
            "export",
            {"path": "C:/fixture/stale-cancel.json", "count": 9},
        )
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

    def test_cancel_invalidates_late_list_result_after_worker_stops(self) -> None:
        page = self._page()
        page._chats = []
        page.chat_list.clear()
        page._generation = 41
        page._operation_kind = "list"
        page._thread = _StoppedThread()
        page._worker = _StoppedWorker()
        old_token = page._generation

        page.cancel_operations()
        page._operation_succeeded(
            old_token,
            "list",
            {"chats": [{"id": "late", "ref": "channel:late", "title": "晚到清單", "type": "channel"}]},
        )

        self.assertEqual(page._chats, [])
        self.assertEqual(page.chat_list.count(), 0)

    def test_cancel_invalidates_late_topic_result_after_worker_stops(self) -> None:
        runner = _Runner()
        page = self._page(runner)
        page._selected_chat = runner.forum
        page._topics = []
        page._topics_loaded = False
        page.topic_panel.setVisible(True)
        page._generation = 43
        page._operation_kind = "topics"
        page._thread = _StoppedThread()
        page._worker = _StoppedWorker()
        old_token = page._generation

        page.cancel_operations()
        page._operation_succeeded(
            old_token,
            "topics",
            {"chat_ref": "channel:forum", "topics": [{"id": "101", "title": "晚到主題"}]},
        )

        self.assertEqual(page._topics, [])
        self.assertFalse(page._topics_loaded)
        self.assertEqual(page.topic_list.count(), 0)

    def test_cancel_keeps_export_result_invalid_after_worker_stops(self) -> None:
        page = self._page()
        page._selected_chat = page._chats[0]
        page._operation_snapshot = page._current_export_snapshot()
        page._generation = 47
        page._operation_kind = "export"
        page._thread = _StoppedThread()
        page._worker = _StoppedWorker()
        old_token = page._generation

        page.cancel_operations()
        page._operation_succeeded(
            old_token,
            "export",
            {"path": "C:/fixture/late-export.json", "count": 9},
        )

        self.assertEqual(page._export_path, "")
        self.assertEqual(page._export_count, 0)
        self.assertFalse(page.download_button.isEnabled())

    def test_forum_topic_selection_keeps_exports_distinct(self) -> None:
        runner = _Runner()
        page = self._page(runner)
        page._chats = [runner.forum]
        page._apply_filter()
        self._select(page, 0)
        _wait_until(self.app, lambda: page._topics_loaded and not page.is_busy())
        self.assertEqual(page.topic_list.count(), 3)  # 全部 + 2 個主題

        page.topic_list.setCurrentRow(1)
        page._scan_clicked()
        _wait_until(self.app, lambda: len(runner.export_calls) == 1 and not page.is_busy())
        page.topic_list.setCurrentRow(2)
        page._scan_clicked()
        _wait_until(self.app, lambda: len(runner.export_calls) == 2 and not page.is_busy())

        self.assertEqual([call["topic_id"] for call in runner.export_calls], ["101", "202"])
        self.assertEqual({call["chat_id"] for call in runner.export_calls}, {"channel:forum"})

    def test_queue_ack_blocks_duplicate_after_filter_and_metadata_refresh(self) -> None:
        page = self._page()
        self._select(page, 0)
        self._scan(page)
        requests: list[dict] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))

        page._download_clicked()
        self.assertEqual(len(requests), 1)
        self.assertTrue(page._export_request_pending)
        self.assertNotIn("已加入下載佇列", page.status_label.text())

        self.assertTrue(page.download_queued("來源 A", 2))
        self.assertEqual(page._export_path, "")
        self.assertFalse(page.download_button.isEnabled())

        page.search_edit.setText("來源 A")
        page.set_accounts(
            [{"id": "acct", "label": "測試帳號（更新）", "authenticated": True}],
            "acct",
        )
        page._download_clicked()
        self.assertEqual(len(requests), 1)

        viewed: list[bool] = []
        page.view_queue_requested.connect(lambda: viewed.append(True))
        page.view_queue_button.click()
        self.assertEqual(viewed, [True])

    def test_rejected_enqueue_keeps_retryable_scan_without_success_message(self) -> None:
        page = self._page()
        self._select(page, 0)
        self._scan(page)
        requests: list[dict] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))

        page._download_clicked()
        self.assertTrue(page._export_request_pending)
        self.assertTrue(page.download_rejected("佇列暫時不可用"))
        self.assertEqual(page._export_count, 2)
        self.assertTrue(page.download_button.isEnabled())
        self.assertIn("失敗", page.status_label.text())
        self.assertNotIn("已加入下載佇列", page.status_label.text())

        page._download_clicked()
        self.assertEqual(len(requests), 2)

    def test_switch_source_action_clears_search_type_and_selection_only(self) -> None:
        page = self._page()
        start_date = page.start_date.date()
        end_date = page.end_date.date()
        media = page._media_types()
        folder = page.folder_edit.text()
        self._select(page, 0)
        page.search_edit.setText("來源 A")
        page.type_combo.setCurrentIndex(1)

        page.change_source_button.click()

        self.assertEqual(page.search_edit.text(), "")
        self.assertEqual(page.topic_search.text(), "")
        self.assertEqual(page.type_combo.currentData(), "all")
        self.assertIsNone(page._selected_chat)
        self.assertIsNone(page.chat_list.currentItem())
        self.assertEqual(page.start_date.date(), start_date)
        self.assertEqual(page.end_date.date(), end_date)
        self.assertEqual(page._media_types(), media)
        self.assertEqual(page.folder_edit.text(), folder)


if __name__ == "__main__":
    unittest.main()
