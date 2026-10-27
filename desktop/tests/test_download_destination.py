"""Destination-picker preferences and cross-page workflow contracts.

The tests use only local Qt fixtures and the backend Store.  No Telegram
account or network operation is started.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.app import AccountInfo, BackendBridge, DownloadPage, MainWindow
from desktop.backend import Store, Preferences
from desktop.channel_page import ChannelPage


class _Result:
    success = True
    cancelled = False
    error = ""

    def __init__(self, data):
        self.data = data


class _Handle:
    def __init__(self, data):
        self.done = threading.Event()
        self.result = _Result(data)
        self.done.set()

    def get_event(self, timeout=None):
        return None

    def wait(self, timeout=None):
        return self.result

    def cancel(self):
        self.result.cancelled = True
        self.done.set()


class _ChannelRunner:
    chats = [
        {
            "id": "-1001",
            "ref": "channel:destination-test",
            "type": "channel",
            "title": "目的地測試頻道",
        }
    ]

    def start_export(self, account_id, chat_id, start_date, end_date, media_types, on_event=None, **kwargs):
        return _Handle({"path": "C:/fixture/destination.json", "count": 2})


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-destination-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class PreferenceDestinationTests(_StoreCase):
    def test_old_preferences_default_to_explicit_picker(self) -> None:
        path = self.root / "preferences.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "values": {"download_dir": "D:/legacy-downloads", "concurrency": 4},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        reopened = Store(self.root)
        preferences = reopened.get_preferences()

        self.assertTrue(preferences.ask_download_folder)
        self.assertTrue(Preferences.from_dict({"ask_download_folder": "false"}, "D:/fallback").ask_download_folder)
        self.assertIn("ask_download_folder", preferences.to_dict())

    def test_disabled_picker_survives_restart(self) -> None:
        saved = self.store.update_preferences(ask_download_folder=False)
        self.assertFalse(saved.ask_download_folder)

        reopened = Store(self.root)
        self.assertFalse(reopened.get_preferences().ask_download_folder)
        raw = json.loads((self.root / "preferences.json").read_text(encoding="utf-8"))
        self.assertIs(raw["values"]["ask_download_folder"], False)

    def test_saving_another_preference_keeps_new_destination(self) -> None:
        self.store.update_preferences(download_dir="D:/chosen", ask_download_folder=False)
        self.store.update_preferences(concurrency=7)

        preferences = Store(self.root).get_preferences()
        self.assertEqual(preferences.download_dir, "D:/chosen")
        self.assertFalse(preferences.ask_download_folder)
        self.assertEqual(preferences.concurrency, 7)

    def test_existing_queued_job_is_unchanged_by_destination_preference(self) -> None:
        account = self.store.add_account("佇列測試")
        job = self.store.create_download_job(
            account.id,
            ["https://t.me/telegram/193"],
            output_dir=self.root / "old-output",
            continue_download=False,
            skip_same=False,
            group=False,
            title="既有工作",
        )
        before = self.store.get_job(job.id).to_dict()

        self.store.update_preferences(ask_download_folder=False, download_dir="D:/new-default")
        after = Store(self.root).get_job(job.id).to_dict()

        self.assertEqual(after, before)


class _QtDestinationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, predicate, timeout: float = 6.0) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for the UI")

    def close_window(self, window: MainWindow) -> None:
        window.close()
        self.pump(lambda: window.isHidden() and not window._background_work_running())
        window.deleteLater()
        self.app.processEvents()

    def make_channel_page(self) -> ChannelPage:
        page = ChannelPage()
        runner = _ChannelRunner()
        page.set_runner(runner)
        page.set_accounts([{"id": "acct", "label": "測試帳號", "authenticated": True}], "acct")
        page.set_download_folder("D:/default")
        page._chats = list(runner.chats)
        page._apply_filter()
        page.chat_list.setCurrentRow(0)
        self.app.processEvents()
        page._scan_clicked()
        self.pump(lambda: bool(page._export_path) and not page.is_busy())
        self.addCleanup(page.shutdown)
        self.addCleanup(page.deleteLater)
        return page

    def test_channel_picker_uses_returned_folder_and_cancel_keeps_scan(self) -> None:
        page = self.make_channel_page()
        requests: list[dict] = []
        selected: list[str] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))
        page.download_folder_selected.connect(selected.append)
        original_path = page._export_path

        with patch("desktop.channel_page.QFileDialog.getExistingDirectory", return_value="D:/picked"):
            page.set_download_preferences(ask_download_folder=True)
            page._download_clicked()

        self.assertEqual(requests[0]["output_dir"], "D:/picked")
        self.assertEqual(selected, ["D:/picked"])
        self.assertEqual(page.folder_edit.text(), "D:/picked")
        page.download_rejected("fixture retry")

        with patch("desktop.channel_page.QFileDialog.getExistingDirectory", return_value=""):
            page._download_clicked()

        self.assertEqual(len(requests), 1)
        self.assertEqual(page._export_path, original_path)
        self.assertTrue(page.download_button.isEnabled())
        self.assertIn("取消", page.status_label.text())

    def test_standalone_download_page_picker_cancel_creates_no_job(self) -> None:
        page = DownloadPage()
        self.addCleanup(page.deleteLater)
        self.assertFalse(page.ask_download_folder)
        page.set_accounts(
            [AccountInfo(id="acct", label="測試帳號", authenticated=True)],
            "acct",
        )
        page.ask_download_folder = True
        page.url_edit.setPlainText("https://t.me/telegram/193")
        requests: list[tuple[list[str], dict]] = []
        page.download_requested.connect(lambda urls, options: requests.append((urls, options)))

        with patch("desktop.app.QFileDialog.getExistingDirectory", return_value="D:/picked"):
            self.assertTrue(page.choose_folder())
        self.assertEqual(page.folder_edit.text(), "D:/picked")

        page.folder_edit.clear()
        with patch("desktop.app.QFileDialog.getExistingDirectory", return_value=""):
            page._request_download()

        self.assertEqual(requests, [])
        self.assertEqual(page.folder_edit.text(), "")

    def test_download_page_without_account_does_not_open_picker_or_emit(self) -> None:
        page = DownloadPage()
        self.addCleanup(page.deleteLater)
        page.ask_download_folder = True
        page.url_edit.setPlainText("https://t.me/telegram/193")
        requests: list[tuple[list[str], dict]] = []
        errors: list[tuple[object, ...]] = []
        page.download_requested.connect(lambda urls, options: requests.append((urls, options)))

        with (
            patch("desktop.app.QFileDialog.getExistingDirectory") as picker,
            patch("desktop.app.show_error", side_effect=lambda *args, **_kwargs: errors.append(args)),
        ):
            page._request_download()

        picker.assert_not_called()
        self.assertEqual(requests, [])
        self.assertTrue(errors)

    def test_download_page_unauthenticated_account_does_not_open_picker_or_emit(self) -> None:
        page = DownloadPage()
        self.addCleanup(page.deleteLater)
        page.set_accounts(
            [AccountInfo(id="acct", label="需要重新登入", authenticated=False)],
            "acct",
        )
        page.ask_download_folder = True
        page.url_edit.setPlainText("https://t.me/telegram/193")
        requests: list[tuple[list[str], dict]] = []
        errors: list[tuple[object, ...]] = []
        page.download_requested.connect(lambda urls, options: requests.append((urls, options)))

        with (
            patch("desktop.app.QFileDialog.getExistingDirectory") as picker,
            patch("desktop.app.show_error", side_effect=lambda *args, **_kwargs: errors.append(args)),
        ):
            page._request_download()

        picker.assert_not_called()
        self.assertEqual(requests, [])
        self.assertTrue(errors)

    def test_channel_picker_opens_when_prompt_disabled_but_folder_is_empty(self) -> None:
        page = self.make_channel_page()
        page.set_download_folder("")
        page.set_download_preferences(ask_download_folder=False)
        requests: list[dict] = []
        page.download_requested.connect(lambda _urls, options: requests.append(options))

        with patch("desktop.channel_page.QFileDialog.getExistingDirectory", return_value="D:/fallback") as picker:
            page._download_clicked()

        picker.assert_called_once()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["output_dir"], "D:/fallback")

    def test_main_window_syncs_destination_between_pages_and_persists_picker_setting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tdl-destination-window-", dir=Path(__file__).parent) as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                window = MainWindow(bridge, resume_downloads=False)
                window.show()
                self.pump(lambda: window._accounts_loaded and window._queue_loaded)

                chosen = "D:/from-channel"
                with patch("desktop.channel_page.QFileDialog.getExistingDirectory", return_value=chosen):
                    self.assertTrue(window.channel_page._choose_folder())
                self.assertEqual(window.channel_page.folder_edit.text(), chosen)
                self.assertEqual(window.home_page.folder_edit.text(), chosen)
                self.assertEqual(window.settings_page.folder_edit.text(), chosen)

                window.settings_page.ask_folder_check.setChecked(False)
                self.pump(lambda: not window._preference_saving and bridge.store.get_preferences().ask_download_folder is False)
                self.assertFalse(window.home_page.ask_download_folder)
                self.assertFalse(window.channel_page._ask_download_folder)

                window.settings_page.concurrency.setValue(5)
                self.pump(lambda: not window._preference_saving and bridge.store.get_preferences().concurrency == 5)
                self.assertEqual(bridge.store.get_preferences().download_dir, chosen)
                self.close_window(window)

                reopened = Store(directory).get_preferences()
                self.assertEqual(reopened.download_dir, chosen)
                self.assertFalse(reopened.ask_download_folder)


if __name__ == "__main__":
    unittest.main()
