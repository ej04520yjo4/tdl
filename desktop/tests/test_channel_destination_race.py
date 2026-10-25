"""Offline regressions for destination-prompt nested event-loop races."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.channel_page import ChannelPage


class ChannelDestinationRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _page(self) -> ChannelPage:
        page = ChannelPage()
        page.set_accounts(
            [
                {"id": "acct-a", "label": "帳號 A", "authenticated": True},
                {"id": "acct-b", "label": "帳號 B", "authenticated": True},
            ],
            "acct-a",
        )
        page.set_download_folder("C:/old-downloads")
        page._chats = [
            {"id": "-1001", "ref": "channel:a", "type": "channel", "title": "頻道 A"},
            {"id": "-1002", "ref": "channel:b", "type": "channel", "title": "頻道 B"},
        ]
        page._apply_filter()
        page.chat_list.setCurrentRow(0)
        self.app.processEvents()

        # Install a complete, valid-looking scan result without invoking a
        # backend.  The test is about the picker event loop, not Telegram.
        page._export_path = "C:/manifests/channel-a.json"
        page._export_count = 3
        page._export_snapshot = page._current_export_snapshot()
        page.set_download_preferences(ask_download_folder=True)
        page._update_download_button()
        self.addCleanup(page.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_source_change_while_picker_open_invalidates_manifest(self) -> None:
        page = self._page()
        requests: list[dict] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))

        def picker_returns_after_source_change(*_args: object) -> str:
            # QFileDialog has a nested Qt event loop in the real application;
            # model the queued selection change before it returns.
            page.chat_list.setCurrentRow(1)
            self.app.processEvents()
            return "D:/new-downloads"

        with patch(
            "desktop.channel_page.QFileDialog.getExistingDirectory",
            side_effect=picker_returns_after_source_change,
        ):
            page._download_clicked()

        self.assertEqual(requests, [])
        self.assertEqual(page._selected_chat["ref"], "channel:b")
        self.assertEqual(page._export_path, "")
        self.assertFalse(page._export_request_pending)
        self.assertFalse(page.download_button.isEnabled())
        self.assertIn("重新掃描", page.status_label.text())

    def test_account_change_while_picker_open_invalidates_manifest(self) -> None:
        page = self._page()
        requests: list[dict] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))

        def picker_returns_after_account_change(*_args: object) -> str:
            page.set_accounts(
                [
                    {"id": "acct-a", "label": "帳號 A", "authenticated": True},
                    {"id": "acct-b", "label": "帳號 B", "authenticated": True},
                ],
                "acct-b",
            )
            self.app.processEvents()
            return "D:/new-downloads"

        with patch(
            "desktop.channel_page.QFileDialog.getExistingDirectory",
            side_effect=picker_returns_after_account_change,
        ):
            page._download_clicked()

        self.assertEqual(requests, [])
        self.assertEqual(page.active_account_id(), "acct-b")
        self.assertIsNone(page._selected_chat)
        self.assertEqual(page._export_path, "")
        self.assertFalse(page._export_request_pending)
        self.assertFalse(page.download_button.isEnabled())
        self.assertIn("重新掃描", page.status_label.text())


if __name__ == "__main__":
    unittest.main()
