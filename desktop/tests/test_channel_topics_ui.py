"""Forum topic selection workflow tests for the channel download page."""

from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.channel_page import ChannelPage


class _Result:
    cancelled = False

    def __init__(self, data=None, *, success=True, error=""):
        self.success = success
        self.data = data
        self.error = error


class _Handle:
    def __init__(self, data=None, *, success=True, error=""):
        self.done = threading.Event()
        self.result = _Result(data, success=success, error=error)
        self.cancelled = False
        self.done.set()

    def get_event(self, timeout=None):
        return None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.result

    def cancel(self):
        self.cancelled = True
        self.result.cancelled = True
        self.done.set()


class _TopicRunner:
    chats = [
        {
            "id": "-10020",
            "ref": "channel:20",
            "type": "group",
            "title": "開發論壇",
            "is_forum": True,
        },
        {
            "id": "-10021",
            "ref": "channel:21",
            "type": "channel",
            "title": "一般頻道",
        },
    ]
    topics = [
        {"id": "101", "title": "公告"},
        {"id": "202", "title": "問題討論"},
    ]

    def __init__(self, *, topic_error: str = ""):
        self.topic_error = topic_error
        self.topic_calls: list[tuple[str, str]] = []
        self.export_calls: list[dict] = []

    def start_list_chats(self, account_id, on_event=None):
        return _Handle({"chats": list(self.chats)})

    def start_list_topics(self, account_id, chat_ref, on_event=None):
        self.topic_calls.append((account_id, chat_ref))
        if self.topic_error:
            return _Handle(success=False, error=self.topic_error)
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
        self.export_calls.append(
            {
                "account_id": account_id,
                "chat_id": chat_id,
                "topic_id": topic_id,
                "start_date": start_date,
                "end_date": end_date,
                "media_types": list(media_types),
            }
        )
        return _Handle({"path": "C:/fixture/forum.json", "count": 2})


def _wait(app: QApplication, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


@unittest.skipUnless(QApplication, "PySide6 is required")
class ForumTopicUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _page(self, runner):
        page = ChannelPage()
        page.set_runner(runner)
        page.set_accounts(
            [{"id": "acct-forum", "label": "論壇帳號", "authenticated": True}],
            "acct-forum",
        )
        page.set_download_folder("C:/Downloads")
        page.refresh_channels()
        _wait(self.app, lambda: page.chat_list.count() == 2)
        page.chat_list.setCurrentRow(0)
        _wait(self.app, lambda: page._topics_loaded or page.topic_state.text() == "讀取失敗")
        return page

    def test_forum_requires_explicit_topic_and_passes_selected_topic(self):
        runner = _TopicRunner()
        page = self._page(runner)
        self.assertEqual(runner.topic_calls, [("acct-forum", "channel:20")])
        self.assertEqual(page.topic_list.count(), 3)  # all + two exact topics
        self.assertIsNone(page._selected_topic)
        self.assertFalse(page.scan_button.isEnabled())

        page._scan_clicked()
        self.assertIn("選擇一個論壇主題", page.status_label.text())
        self.assertEqual(runner.export_calls, [])

        page.topic_list.setCurrentRow(1)
        self.assertEqual(page._selected_topic["id"], "101")
        self.assertTrue(page.scan_button.isEnabled())
        page._scan_clicked()
        _wait(self.app, lambda: bool(runner.export_calls))
        self.assertEqual(runner.export_calls[0]["chat_id"], "channel:20")
        self.assertEqual(runner.export_calls[0]["topic_id"], "101")
        page.shutdown()
        page.deleteLater()

    def test_all_topics_is_explicit_and_search_cannot_retain_hidden_topic(self):
        runner = _TopicRunner()
        page = self._page(runner)
        page.topic_list.setCurrentRow(2)
        self.assertEqual(page._selected_topic["id"], "202")
        page.topic_search.setText("不存在")
        self.assertIsNone(page._selected_topic)
        self.assertFalse(page.scan_button.isEnabled())
        page.topic_search.clear()
        page.topic_list.setCurrentRow(0)
        self.assertTrue(page._selected_topic["all"])
        page._scan_clicked()
        _wait(self.app, lambda: bool(runner.export_calls))
        self.assertIsNone(runner.export_calls[0]["topic_id"])
        page.shutdown()
        page.deleteLater()

    def test_topic_error_is_visible_and_never_falls_back_to_whole_group(self):
        runner = _TopicRunner(topic_error="主題查詢失敗")
        page = self._page(runner)
        self.assertEqual(page.topic_state.text(), "讀取失敗")
        self.assertIn("主題查詢失敗", page.topic_hint.text())
        self.assertIsNone(page._selected_topic)
        self.assertFalse(page.scan_button.isEnabled())
        page._scan_clicked()
        self.assertIn("尚未載入完成", page.status_label.text())
        self.assertEqual(runner.export_calls, [])
        page.shutdown()
        page.deleteLater()

    def test_regular_channel_keeps_existing_export_contract_without_topic(self):
        runner = _TopicRunner()
        page = self._page(runner)
        page.chat_list.setCurrentRow(1)
        self.assertFalse(page.topic_panel.isVisible())
        self.assertIsNone(page._selected_topic)
        self.assertTrue(page.scan_button.isEnabled())
        page._scan_clicked()
        _wait(self.app, lambda: bool(runner.export_calls))
        self.assertEqual(runner.export_calls[0]["chat_id"], "channel:21")
        self.assertIsNone(runner.export_calls[0]["topic_id"])
        page.shutdown()
        page.deleteLater()

    def test_topic_operation_ignores_interleaved_chats_event(self):
        """A parent chat stream must not repaint the page during topic load."""

        runner = _TopicRunner()
        page = self._page(runner)
        original_chats = list(page._chats)
        original_topic_ids = [topic["id"] for topic in page._topics]
        original_selected = page._selected_chat["ref"]

        page._operation_kind = "topics"
        page._operation_event(
            page._generation,
            {
                "event": "chats",
                "chats": [{"ref": "channel:stale", "title": "不應取代目前清單"}],
            },
        )
        self.assertEqual(page._chats, original_chats)
        self.assertEqual(page._selected_chat["ref"], original_selected)
        self.assertEqual(page.chat_list.count(), len(original_chats))

        # A delayed topic event must likewise be ignored once a chat refresh
        # owns the page state.
        page._operation_kind = "list"
        page._operation_event(
            page._generation,
            {"event": "topics", "topics": [{"id": "stale", "title": "不應顯示"}]},
        )
        self.assertEqual([topic["id"] for topic in page._topics], original_topic_ids)
        page.shutdown()
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
