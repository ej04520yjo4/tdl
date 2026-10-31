"""Offline regression tests for the group batch-forward page."""

from __future__ import annotations

import os
import queue
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.batch_forward_page import BatchForwardPage


class _Result:
    def __init__(self, data=None, *, success=True, cancelled=False, error=None):
        self.data = data
        self.success = success
        self.cancelled = cancelled
        self.error = error


class _Handle:
    def __init__(self, data, events=()):
        self.done = threading.Event()
        self.result = _Result(data)
        self._events: queue.Queue[dict] = queue.Queue()
        for event in events:
            self._events.put(dict(event))
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        time.sleep(0.01)
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
        self.result.cancelled = True
        self.done.set()


class _Runner:
    chats = [
        {"id": "1", "ref": "chat:1001", "type": "group", "title": "來源群組"},
        {"id": "2", "ref": "chat:1002", "type": "group", "title": "我的群組"},
        {
            "id": "3",
            "ref": "chat:1003",
            "type": "group",
            "title": "我的論壇",
            "is_forum": True,
        },
    ]

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def start_list_topics(self, account_id, chat_ref, *, on_event=None, **kwargs):
        self.calls.append(("topics", (account_id, chat_ref), kwargs))
        return _Handle({"topics": [{"id": "7", "title": "影片分享"}]})

    def start_export(self, account_id, chat_ref, start, end, media_types, *, on_event=None, **kwargs):
        self.calls.append(("export", (account_id, chat_ref, start, end, tuple(media_types)), kwargs))
        return _Handle(
            {
                "path": "C:/fixture/manifest.json",
                "count": 3,
                "total_bytes": 1024,
                "sha256": "a" * 64,
            }
        )

    def start_batch_forward(self, account_id, path, *, on_event=None, **kwargs):
        self.calls.append(("batch", (account_id, path), kwargs))
        expected = int(kwargs.get("expected_count", 0))
        if kwargs.get("dry_run"):
            return _Handle({"dry_run": True, "count": expected})
        return _Handle({"sent_count": expected, "failed_count": 0})


def _wait(app: QApplication, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


class BatchForwardPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.runner = _Runner()
        self.page = BatchForwardPage()
        self.page.set_runner(self.runner)
        self.page.set_accounts([{"id": "acct", "label": "測試帳號", "authenticated": True}], "acct")
        self.page._chats = list(_Runner.chats)
        self.page._apply_filter()
        self.page.chat_list.setCurrentRow(0)
        target_index = next(
            i for i in range(self.page.target_group_combo.count())
            if getattr(self.page.target_group_combo.itemData(i), "get", lambda *_: None)("ref") == "chat:1002"
        )
        self.page.target_group_combo.setCurrentIndex(target_index)
        self.assertTrue(self.page.scan_button.isEnabled())

    def tearDown(self):
        self.page.shutdown()
        self.page.deleteLater()
        self.app.processEvents()

    def _scan(self):
        self.page._scan_clicked()
        self.assertTrue(self._wait(lambda: self.page.download_button.isEnabled()))

    def _wait(self, predicate, timeout=2.0):
        return _wait(self.app, predicate, timeout)

    def test_destination_is_never_auto_selected_and_source_is_blocked(self):
        page = BatchForwardPage()
        page.set_accounts([{"id": "acct", "authenticated": True}], "acct")
        page._chats = list(_Runner.chats)
        page._apply_filter()
        self.assertEqual(page.target_group_combo.currentIndex(), 0)
        page.chat_list.setCurrentRow(0)
        source_index = next(
            i for i in range(page.target_group_combo.count())
            if getattr(page.target_group_combo.itemData(i), "get", lambda *_: None)("ref") == "chat:1001"
        )
        page.target_group_combo.setCurrentIndex(source_index)
        self.assertFalse(page.scan_button.isEnabled())
        self.assertIn("不可與來源相同", page.target_hint.text())
        page.shutdown()
        page.deleteLater()

    def test_scan_requires_destination_and_preserves_manifest_binding(self):
        self._scan()
        self.assertEqual(self.page._batch_manifest_sha256, "a" * 64)
        self.assertIn("我的群組", self.page.scan_summary_label.text())
        self.assertTrue(self.page.download_button.isEnabled())
        # Changing the destination invalidates the old source export too.
        forum_index = next(
            i for i in range(self.page.target_group_combo.count())
            if getattr(self.page.target_group_combo.itemData(i), "get", lambda *_: None)("ref") == "chat:1003"
        )
        self.page.target_group_combo.setCurrentIndex(forum_index)
        self.assertFalse(self.page.download_button.isEnabled())
        self.assertEqual(self.page._batch_manifest_sha256, "")

    def test_forum_destination_loads_topics_without_auto_topic(self):
        forum_index = next(
            i for i in range(self.page.target_group_combo.count())
            if getattr(self.page.target_group_combo.itemData(i), "get", lambda *_: None)("ref") == "chat:1003"
        )
        self.page.target_group_combo.setCurrentIndex(forum_index)
        self.assertTrue(self._wait(lambda: self.page._target_topics_loaded))
        self.assertEqual(self.page.target_topic_combo.currentIndex(), 0)
        self.assertIn("含一般", self.page.target_topic_combo.currentText())
        self.assertFalse(self.page.scan_button.isEnabled())
        self.page.target_topic_combo.setCurrentIndex(1)
        self.assertTrue(self.page.scan_button.isEnabled())

    def test_dry_run_then_confirmed_forward_uses_manifest_contract(self):
        self._scan()
        self.page.dry_run_button.click()
        self.assertTrue(self._wait(lambda: not self.page.is_busy()))
        batch_calls = [call for call in self.runner.calls if call[0] == "batch"]
        self.assertEqual(len(batch_calls), 1)
        self.assertTrue(batch_calls[0][2]["dry_run"])
        self.assertEqual(batch_calls[0][2]["source_chat"], "chat:1001")
        self.assertEqual(batch_calls[0][2]["to"], "chat:1002")
        with patch("desktop.batch_forward_page._confirm_batch", return_value=True):
            self.page.download_button.click()
        self.assertTrue(self._wait(lambda: not self.page.is_busy()))
        batch_calls = [call for call in self.runner.calls if call[0] == "batch"]
        self.assertEqual(len(batch_calls), 2)
        self.assertFalse(batch_calls[-1][2]["dry_run"])
        self.assertFalse(self.page.download_button.isEnabled())
        self.assertIn("已送出", self.page.status_label.text())

    def test_confirmation_rechecks_snapshot_before_sending(self):
        self._scan()

        def mutate_and_accept(_page, _snapshot):
            self.page.target_group_combo.setCurrentIndex(0)
            return True

        with patch("desktop.batch_forward_page._confirm_batch", side_effect=mutate_and_accept):
            self.page.download_button.click()
        self.assertFalse(any(call[0] == "batch" for call in self.runner.calls))
        self.assertIn("已變更", self.page.status_label.text())

    def test_numeric_only_cached_peers_are_not_sent(self):
        self.page._chats = [
            {"id": "11", "type": "group", "title": "舊來源"},
            {"id": "12", "type": "group", "title": "舊目的"},
        ]
        self.page._apply_filter()
        self.page._selected_chat = self.page._chats[0]
        self.page._target_chat = self.page._chats[1]
        self.page._scan_clicked()
        self.assertFalse(any(call[0] == "export" for call in self.runner.calls))
        self.assertIn("類型識別", self.page.status_label.text())

    def test_switching_account_drops_old_destination_cache(self):
        self.assertGreater(self.page.target_group_combo.count(), 1)
        self.page.set_accounts([{"id": "other", "label": "另一帳號", "authenticated": True}], "other")
        self.assertEqual(self.page.target_group_combo.count(), 1)
        self.assertIsNone(self.page.target_group_combo.itemData(0))

    def test_authoritative_empty_chat_refresh_clears_destinations(self):
        self.assertGreater(self.page.target_group_combo.count(), 1)
        self.page._chats = []
        self.page._apply_filter()
        self.assertEqual(self.page.target_group_combo.count(), 1)
        self.assertIsNone(self.page.target_group_combo.itemData(0))


if __name__ == "__main__":
    unittest.main()
