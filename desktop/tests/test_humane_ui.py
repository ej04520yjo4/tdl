"""1.5.0 friendlier UI: one account switcher, messages instead of piles of
dialogs, notifications, folders that point at the files, settings that save
themselves, history actions, drag and drop, and remembered window state.
Never contacts Telegram."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from desktop.app import (
    PAGE_NAMES,
    STALL_SECONDS,
    UI_STATE_FILE,
    BackendBridge,
    DownloadJob,
    ErrorDialog,
    MainWindow,
    QueueRow,
    _LinkEdit,
    _TaskbarProgress,
    _nav_icon,
    extract_message_links,
)
from desktop.backend import EVENT_PREFIX, Runner
from desktop.transfer_page import _DropList

BUILD = Path(__file__).resolve().parents[2] / ".build"


class _QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, predicate, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for the UI")

    def window(self, directory: str, *, accounts=(("介面測試", True),)):  # type: ignore[no-untyped-def]
        bridge = BackendBridge()
        created = []
        for name, authenticated in accounts:
            account = bridge.store.add_account(name)
            bridge.store.update_account(account.id, authenticated=authenticated)
            created.append(account)
        if created:
            bridge.store.set_active_account(created[0].id)
        window = MainWindow(bridge, resume_downloads=False)
        window.show()
        self.pump(lambda: window._accounts_loaded and window._queue_loaded)
        self._open_windows.append(window)
        return bridge, created, window

    def setUp(self) -> None:
        self._open_windows: list[MainWindow] = []

    def tearDown(self) -> None:
        self.close_all()

    def close_all(self) -> None:
        while self._open_windows:
            self.close(self._open_windows.pop())

    def close(self, window: MainWindow) -> None:
        if window in getattr(self, "_open_windows", []):
            self._open_windows.remove(window)
        if window.isVisible():
            window.close()
            self.pump(lambda: window.isHidden() and not window._background_work_running())
        window.deleteLater()
        self.app.processEvents()

    def running_job(self, window: MainWindow, **extra) -> DownloadJob:  # type: ignore[no-untyped-def]
        job = DownloadJob(
            id="job-1", url="https://t.me/telegram/1", title="旅行影片", output_dir=str(BUILD),
            status="下載中", links=["https://t.me/telegram/1"], **extra,
        )
        window.jobs[job.id] = job
        window.home_page.add_jobs([job])
        return job


class AccountTests(_QtCase):
    def test_login_without_a_name_uses_the_telegram_name_not_the_phone(self) -> None:
        script = (
            "import json, sys\n"
            f"print({EVENT_PREFIX!r} + json.dumps({{'event': 'prompt', 'kind': 'phone'}}), flush=True)\n"
            "sys.stdin.readline()\n"
            f"print({EVENT_PREFIX!r} + json.dumps({{'event': 'authenticated', 'id': '5', 'username': 'xm', 'name': '王小明'}}), flush=True)\n"
        )

        class FixtureRunner(Runner):
            def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
                encoded = base64.b64encode(script.encode()).decode()
                return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]

        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            bridge.runner = FixtureRunner(bridge.store)
            bridge.login({"label": "", "method": "code", "phone": "+886912345678"}, threading.Event(), lambda _e: None)
            account = bridge.store.list_accounts()[0]
            self.assertEqual(account.name, "王小明")
            self.assertNotIn("912345678", (Path(directory) / "accounts.json").read_text(encoding="utf-8"))

    def test_top_bar_menu_switches_accounts_for_every_page(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, (first, second), window = self.window(directory, accounts=(("甲帳號", True), ("乙帳號", True)))
            labels = [action.text() for action in window.account_menu.actions() if action.text()]
            self.assertIn("甲帳號（已連線）", labels)
            self.assertIn("＋ 新增帳號…", labels)
            switch = next(action for action in window.account_menu.actions() if action.text().startswith("乙帳號"))
            switch.trigger()
            self.app.processEvents()
            for combo in (window.home_page.account_combo, window.channel_page.account_combo, window.transfer_page.account_combo):
                self.assertEqual(combo.currentData(), second.id)
                self.assertFalse(combo.isVisible())
            self.assertIn("乙帳號", window.top_account.text())
            self.close_all()

    def test_banner_offers_login_or_relogin(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory, accounts=())
            self.assertTrue(window.account_banner.isVisible())
            self.assertEqual(window.account_banner.action_button.text(), "立即登入")
            self.close_all()
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, (stale,), window = self.window(directory, accounts=(("過期帳號", False),))
            self.assertEqual(window.account_banner.action_button.text(), "重新登入")
            with patch.object(window, "_relogin_account") as relogin:
                window.account_banner.action_button.click()
            relogin.assert_called_once_with(stale.id)
            self.close_all()
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            self.assertFalse(window.account_banner.isVisible())
            self.close_all()

    def test_first_launch_opens_login_only_when_asked(self) -> None:
        calls: list[int] = []

        def fake_add_account(_window: MainWindow) -> None:
            calls.append(1)

        # A plain function, not a MagicMock: Qt connects this slot in
        # _build_ui, and PySide cannot connect a signal to a mock object.
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            with patch.object(MainWindow, "_add_account", fake_add_account):
                window = MainWindow(BackendBridge(), resume_downloads=False)
                window.prompt_login_when_empty = True
                window.show()
                self.pump(lambda: window._accounts_loaded)
                self.pump(lambda: len(calls) == 1, timeout=3)
                quiet = MainWindow(BackendBridge(), resume_downloads=False)
                quiet.show()
                self.pump(lambda: quiet._accounts_loaded)
                time.sleep(0.4)
                self.app.processEvents()
                self.assertEqual(len(calls), 1)
                self.close(window)
                self.close(quiet)

class QueueTests(_QtCase):
    def test_queue_sits_above_the_link_editor(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            page = window.home_page
            self.assertLess(page.queue_scroll.mapTo(page, QPoint(0, 0)).y(), page.url_edit.mapTo(page, QPoint(0, 0)).y())
            for hidden in (page.account_combo, page.group_check, page.skip_check, page.concurrency_spin):
                self.assertFalse(hidden.isVisible())
            self.close_all()

    def test_failures_become_one_message_instead_of_dialogs(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            first = self.running_job(window)
            second = DownloadJob(id="job-2", url="u", title="第二個", output_dir=directory, status="下載中")
            window.jobs[second.id] = second
            window.home_page.add_jobs([second])
            shown: list[object] = []
            with patch("desktop.app.show_error") as modal, patch.object(ErrorDialog, "exec", lambda dialog: shown.append(dialog) or 0):
                window._job_failed(first.id, "下載資料夾所在的磁碟空間不足。", "trace…")
                window._job_failed(second.id, "網路連線逾時，請檢查網路後重試。", "trace2")
                modal.assert_not_called()
                self.assertEqual(shown, [])
                self.assertTrue(window.notice_bar.isVisible())
                self.assertIn("2 個下載失敗", window.notice_bar.label.text())
                row = window.home_page.rows[first.id]
                self.assertFalse(row.detail_button.isHidden())
                # The reason and the next step sit on the full-width line under
                # the title; the narrow right-hand label only says it failed.
                self.assertIn("磁碟空間不足", row.url_label.text())
                self.assertIn("建議", row.url_label.text())
                self.assertEqual(row.percent.text(), "下載未完成")
                window.notice_bar.action_button.click()
                self.assertEqual(len(shown), 1)
            self.close_all()

    def test_unseen_failure_turns_the_taskbar_red_until_the_user_returns(self) -> None:
        calls: list[tuple[int, int, int]] = []

        class FakeTaskbar:
            available = True

            def update(self, _hwnd: int, state: int, done: int = 0, total: int = 0) -> None:
                calls.append((state, done, total))

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            window.taskbar = FakeTaskbar()  # type: ignore[assignment]
            job = self.running_job(window)
            with patch.object(window, "_user_is_looking", return_value=False):
                window._job_failed(job.id, "網路連線逾時，請檢查網路後重試。", "")
            self.assertEqual(calls[-1], (_TaskbarProgress.ERROR, 1, 1))
            window._tick_activity()
            self.assertEqual(calls[-1][0], _TaskbarProgress.ERROR, "stays red while the user is away")
            window._application_state_changed(Qt.ApplicationActive)
            self.assertEqual(calls[-1][0], _TaskbarProgress.NOPROGRESS)
            # A failure while the user is looking does not need the red button.
            second = DownloadJob(id="job-2", url="u", title="第二個", output_dir=directory, status="下載中")
            window.jobs[second.id] = second
            window.home_page.add_jobs([second])
            with patch.object(window, "_user_is_looking", return_value=True):
                window._job_failed(second.id, "網路連線逾時，請檢查網路後重試。", "")
            window._tick_activity()
            self.assertEqual(calls[-1][0], _TaskbarProgress.NOPROGRESS)
            self.close_all()

    def test_completion_offers_the_real_folder(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            chat_folder = Path(directory) / "旅行影片收藏"
            chat_folder.mkdir()
            job = self.running_job(window, file_progress={
                "1": {"filename": "a.mp4", "state": "completed", "current": 1, "total": 1, "path": str(chat_folder)},
            })
            row = window.home_page.rows[job.id]
            self.assertEqual(row.open_target(), str(chat_folder))
            self.assertFalse(row.open_button.isHidden(), "the folder can be opened while downloading")
            window._job_succeeded(job.id, {"success": True})
            self.assertEqual(window.notice_bar.action_button.text(), "開啟資料夾")
            with patch("desktop.app.QDesktopServices.openUrl") as open_url:
                window.notice_bar.action_button.click()
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), chat_folder)
            self.close_all()

    def test_open_output_falls_back_to_an_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            with patch("desktop.app.QDesktopServices.openUrl") as open_url:
                window.open_output(str(Path(directory) / "not-created-yet"))
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), Path(directory))
            self.close_all()

    def test_stalled_job_is_explained_and_cleared_by_progress(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            job = self.running_job(window, expected_files=1, expected_bytes=100, file_progress={
                "1": {"filename": "a.mp4", "state": "downloading", "current": 25, "total": 100},
            })
            window._last_activity[job.id] = time.monotonic() - STALL_SECONDS - 5
            window._tick_activity()
            row = window.home_page.rows[job.id]
            self.assertIn("沒有新進度", row.stats.text())
            self.assertEqual(window.windowTitle(), "（25%）tdl 繁中下載器")
            window._job_progress(job.id, {"event": "progress", "file_id": "1", "state": "downloading", "current": 50, "total": 100, "percent": 50})
            self.assertNotIn("沒有新進度", row.stats.text())
            self.close_all()

    def test_link_box_turns_dropped_json_into_an_import(self) -> None:
        edit = _LinkEdit()
        emitted = []
        edit.files_dropped.connect(emitted.append)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("C:/exports/result.json"), QUrl.fromLocalFile("C:/a.txt")])
        self.assertTrue(edit.canInsertFromMimeData(mime))
        edit.insertFromMimeData(mime)
        self.assertEqual([Path(path) for path in emitted[0]], [Path("C:/exports/result.json")])
        text = QMimeData()
        text.setHtml("<b>https://t.me/telegram/1</b>")
        text.setText("https://t.me/telegram/1")
        edit.insertFromMimeData(text)
        self.assertEqual(edit.toPlainText(), "https://t.me/telegram/1")


class ClipboardTests(_QtCase):
    def test_message_links_are_found_and_others_ignored(self) -> None:
        text = (
            "看這個 https://t.me/telegram/193 還有 t.me/c/1234567890/45，"
            "頻道首頁 https://t.me/telegram 與邀請 https://t.me/+AbCdEf 不算。"
        )
        self.assertEqual(
            extract_message_links(text),
            ["https://t.me/telegram/193", "https://t.me/c/1234567890/45"],
        )

    def test_clipboard_links_are_offered_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            QGuiApplication.clipboard().setText("https://t.me/telegram/193\nhttps://t.me/telegram/194")
            window._offer_clipboard_links()
            self.assertIn("2 個 Telegram 訊息連結", window.notice_bar.label.text())
            window.notice_bar.action_button.click()
            self.assertEqual(window.home_page.parse_urls(), ["https://t.me/telegram/193", "https://t.me/telegram/194"])
            window.notice_bar.dismiss()
            window._offer_clipboard_links()
            self.assertFalse(window.notice_bar.isVisible(), "the same clipboard is not offered twice")
            self.close_all()

class SettingsTests(_QtCase):
    def test_invalid_proxy_is_not_saved_and_explained(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, _created, window = self.window(directory)
            page = window.settings_page
            page.proxy_edit.setText("127.0.0.1:7890")
            page._proxy_edited()
            self.assertIn("尚未儲存", page.proxy_status.text())
            self.app.processEvents()
            self.assertEqual(bridge.store.get_preferences().proxy, "")
            page.proxy_edit.setText("socks5://127.0.0.1:1080")
            page._proxy_edited()
            self.pump(lambda: bridge.store.get_preferences().proxy == "socks5://127.0.0.1:1080")
            self.assertEqual(window.transfer_page._proxy, "socks5://127.0.0.1:1080")
            self.close_all()

    def test_half_typed_proxy_does_not_block_other_settings(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, _created, window = self.window(directory)
            page = window.settings_page
            page.proxy_edit.setText("socks5://")
            page._proxy_edited()
            page.concurrency.setValue(5)
            self.pump(lambda: bridge.store.get_preferences().concurrency == 5 and not window._preference_saving)
            self.assertEqual(bridge.store.get_preferences().proxy, "")
            self.assertIn("尚未儲存", page.proxy_status.text())
            self.close_all()

    def test_album_and_same_file_options_live_in_settings(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, _created, window = self.window(directory)
            window.settings_page.album_check.setChecked(False)
            window.settings_page.skip_check.setChecked(False)
            self.pump(lambda: not bridge.store.get_preferences().group and not bridge.store.get_preferences().skip_same)
            self.assertFalse(window.home_page.group_check.isChecked())
            self.assertFalse(window.home_page.skip_check.isChecked())
            self.close_all()

class HistoryTests(_QtCase):
    def test_history_actions(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, (account,), window = self.window(directory)
            page = window.history_page
            window._show_widget(page)
            self.pump(lambda: True, timeout=0.2)
            self.assertFalse(page.empty_label.isHidden())
            self.assertIn("還沒有下載紀錄", page.empty_label.text())

            done = bridge.store.create_download_job(account.id, ["https://t.me/telegram/1"], output_dir=directory)
            bridge.store.update_job_progress(done.id, folders=[str(Path(directory) / "群組")])
            bridge.store.finish_job(done.id, "completed")
            window.refresh_history()
            self.pump(lambda: page.table.rowCount() == 1)
            opened = []
            page.open_requested.connect(opened.append)
            page.table.cellDoubleClicked.emit(0, 1)
            self.assertEqual(opened, [str(Path(directory) / "群組")])

            with patch("desktop.app.ask_confirm", return_value=True):
                page.clear_button.click()
            self.pump(lambda: bridge.store.list_history() == [] and page.table.rowCount() == 0)
            self.close_all()

class TransferDropTests(_QtCase):
    def test_upload_list_accepts_local_paths_only(self) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("C:/videos/a.mp4"), QUrl("https://example.com/b.mp4")])

        class _Event:
            def mimeData(self):  # type: ignore[no-untyped-def]
                return mime

        self.assertEqual([Path(path) for path in _DropList._local_paths(_Event())], [Path("C:/videos/a.mp4")])


class LookTests(_QtCase):
    def test_sidebar_icons_are_distinct(self) -> None:
        images = [
            _nav_icon(kind).pixmap(20, 20).toImage()
            for kind in ("channel", "download", "transfer", "account", "history", "settings")
        ]
        for index, image in enumerate(images):
            for other in images[index + 1:]:
                self.assertNotEqual(image, other)

    def test_channel_page_hides_internal_ids_and_says_things_once(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, _created, window = self.window(directory)
            page = window.channel_page
            page._operation_succeeded(page._generation, "list", {"chats": [
                {"id": "10001", "ref": "channel:10001", "type": "channel", "title": "很長很長的頻道名稱" * 6},
            ]})
            page.chat_list.setCurrentRow(0)
            self.app.processEvents()
            self.assertFalse(page.current_source_identity_label.isVisible())
            self.assertIn("channel:10001", page.current_source_label.toolTip())
            self.assertNotIn("channel:10001", page.current_source_label.text())
            self.assertNotIn("已選", page.chat_hint.text())
            self.assertEqual(page.chat_list.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(page.scan_button.text(), "① 查看符合的檔案")
            self.assertEqual(page.download_button.text(), "② 加入下載")
            page.video_check.setChecked(False)
            page.photo_check.setChecked(True)
            self.assertNotEqual(page.status_label.text(), page.scan_summary_label.text())
            self.close_all()

    def test_error_dialog_suggests_and_copies(self) -> None:
        dialog = ErrorDialog("下載失敗", "旅行影片：下載資料夾所在的磁碟空間不足。", "trace")
        self.assertIn("清出空間", dialog.suggestion_label.text())
        dialog.copy_button.click()
        copied = QGuiApplication.clipboard().text()
        self.assertIn("磁碟空間不足", copied)
        self.assertIn("trace", copied)
        dialog.deleteLater()


class WindowStateTests(_QtCase):
    def test_size_position_and_page_are_remembered(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            first = MainWindow(bridge, resume_downloads=False)
            first.show()
            self.pump(lambda: first._accounts_loaded and first._queue_loaded)
            first.resize(1000, 700)
            first._show_widget(first.settings_page)
            self.close(first)
            state = json.loads((Path(directory) / UI_STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["page"], PAGE_NAMES.index("設定與說明"))

            second = MainWindow(BackendBridge(), resume_downloads=False)
            self.addCleanup(self.close, second)
            second.show()
            self.pump(lambda: second._accounts_loaded)
            self.assertEqual(second.stack.currentIndex(), PAGE_NAMES.index("設定與說明"))
            # The offscreen test screen is smaller than a desktop, so Qt may
            # clamp the restored size; the saved geometry is what matters.
            self.assertTrue(state["geometry"])
            self.assertEqual(second.height(), 700)

if __name__ == "__main__":
    unittest.main()
