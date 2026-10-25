"""1.3.0 UI contracts: queue row totals, queue clearing, advanced settings,
the upload/forward page and account backup/restore.  Never contacts Telegram."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.app import PAGE_NAMES, BackendBridge, DownloadJob, DownloadPage, MainWindow, QueueRow, _format_duration, _last_error_line
from desktop.backend import EVENT_PREFIX, Runner
from desktop.transfer_page import TransferPage

BUILD = Path(__file__).resolve().parents[2] / ".build"


class _ScriptRunner(Runner):
    """Run a Python script per core command; ``scripts`` maps a command name."""

    def __init__(self, store, scripts):  # type: ignore[no-untyped-def]
        super().__init__(store, executable=sys.executable)
        self.scripts = scripts
        self.built: list[list[str]] = []

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        args = super()._build_args(request, runtime)
        self.built.append(args)
        command = next((name for name in self.scripts if name in args), "")
        encoded = base64.b64encode(self.scripts.get(command, "").encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


def _events(*events: dict, exit_code: int = 0) -> str:
    lines = ["import json, sys", f"prefix = {EVENT_PREFIX!r}"]
    lines += [f"print(prefix + json.dumps({event!r}), flush=True)" for event in events]
    lines.append(f"sys.exit({exit_code})")
    return "\n".join(lines)


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

    def close(self, window: MainWindow) -> None:
        window.close()
        self.pump(lambda: window.isHidden() and not window._background_work_running())
        window.deleteLater()
        self.app.processEvents()


class QueueRowTests(_QtCase):
    def _job(self, **extra) -> DownloadJob:  # type: ignore[no-untyped-def]
        return DownloadJob(id="row", url="https://t.me/telegram/1", title="測試", output_dir="C:/Downloads",
                           status="下載中", links=["https://t.me/telegram/1"], **extra)

    def test_one_event_touches_only_its_own_file(self) -> None:
        job = self._job()
        job.file_progress = {
            str(index): {"filename": f"{index}.mp4", "state": "queued", "current": 0, "total": 100}
            for index in range(300)
        }
        row = QueueRow(job)
        job.file_progress["7"].update(state="downloading", current=40, percent=40)
        with patch.object(row, "_apply_file", wraps=row._apply_file) as applied:
            row.update_file(job, "7")
        self.assertEqual(applied.call_count, 1)
        self.assertEqual(row._file_rows["7"][2].text(), "40%")
        self.assertIn("檔案 0/300 · 進行中 1", row.percent.text())
        self.assertEqual(row._bytes_done, 40)

        job.file_progress["7"].update(state="completed", current=100, percent=100)
        row.update_file(job, "7")
        job.file_progress["8"].update(state="failed")
        row.update_file(job, "8")
        self.assertIn("檔案 1/300 · 進行中 0 · 失敗 1", row.percent.text())
        self.assertEqual(row._bytes_done, 100)
        row.deleteLater()

    def test_scanned_job_shows_whole_job_progress_and_remaining_time(self) -> None:
        job = self._job(expected_files=10, expected_bytes=1000)
        job.file_progress = {"a": {"filename": "a.mp4", "state": "downloading", "current": 100, "total": 400, "percent": 25}}
        row = QueueRow(job)
        row.show()
        self.app.processEvents()
        self.assertEqual(row.overall_percent(), 10)
        self.assertTrue(row.progress.isVisible())
        self.assertEqual(row.progress.value(), 10)
        # The scan's file count stands in until the core has found them all.
        self.assertIn("檔案 0/10", row.percent.text())
        self.assertIn("100.0 B / 1000.0 B", row.stats.text())

        clock = [1000.0]
        with patch("desktop.app.time.monotonic", side_effect=lambda: clock[0]):
            row._speed_sample = None
            row.update_file(job, "a")
            clock[0] += 2.0
            job.file_progress["a"].update(current=300, percent=75)
            row.update_file(job, "a")
        self.assertIn("100.0 B/s", row.stats.text())
        self.assertIn("剩餘約 7 秒", row.stats.text())

        # A paused job must not keep advertising a speed.
        job.status = "已暫停"
        row.set_job(job)
        self.assertNotIn("/s", row.stats.text())
        row.deleteLater()

    def test_link_job_without_known_size_keeps_the_whole_job_bar_hidden(self) -> None:
        job = self._job()
        job.file_progress = {"a": {"filename": "a.mp4", "state": "downloading", "current": 1, "total": 4, "percent": 25}}
        row = QueueRow(job)
        row.show()
        self.app.processEvents()
        self.assertIsNone(row.overall_percent())
        self.assertFalse(row.progress.isVisible())
        row.deleteLater()

    def test_manifest_job_names_the_destination_instead_of_an_internal_path(self) -> None:
        job = DownloadJob(id="m", url="C:/AppData/exports/uuid.json", title="頻道", output_dir="D:/影片",
                          files=["C:/AppData/exports/uuid.json"])
        row = QueueRow(job)
        self.assertEqual(row.url_label.text(), "下載到 D:/影片")
        row.deleteLater()

    def test_rows_stay_at_the_top_and_can_be_removed(self) -> None:
        page = DownloadPage()
        jobs = [DownloadJob(id=f"j{index}", url="u", title="t", output_dir="C:/") for index in range(3)]
        page.add_jobs(jobs)
        self.assertEqual(page.queue_list.indexOf(page.rows["j0"]), 1)  # after the hidden empty label
        self.assertIsNotNone(page.queue_list.itemAt(page.queue_list.count() - 1).spacerItem())
        page.remove_jobs(["j0", "j1", "j2"])
        self.assertEqual(page.rows, {})
        self.assertFalse(page.empty_queue.isHidden())
        page.deleteLater()

    def test_helpers(self) -> None:
        self.assertEqual(_format_duration(45), "45 秒")
        self.assertEqual(_format_duration(600), "10 分鐘")
        self.assertEqual(_format_duration(3 * 3600 + 5 * 60), "3 小時 5 分")
        self.assertEqual(_format_duration(50 * 3600), "2 天 2 小時")
        self.assertEqual(_last_error_line("Traceback…\n  File x\ndesktop.app.OperationFailed: 備份檔無法還原。\n"), "備份檔無法還原。")
        self.assertEqual(_last_error_line(""), "")


class MainWindowTests(_QtCase):
    def _window(self, directory: str, scripts=None):  # type: ignore[no-untyped-def]
        bridge = BackendBridge()
        account = bridge.store.add_account("介面測試")
        bridge.store.update_account(account.id, authenticated=True)
        bridge.store.set_active_account(account.id)
        if scripts is not None:
            bridge.runner = _ScriptRunner(bridge.store, scripts)
        window = MainWindow(bridge, resume_downloads=False)
        window.show()
        self.pump(lambda: window._accounts_loaded and window._queue_loaded)
        return bridge, account, window

    def test_pages_match_the_sidebar(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, account, window = self._window(directory)
            self.assertEqual(tuple(button.text() for button in window.nav_buttons), PAGE_NAMES)
            self.assertEqual(window.stack.count(), len(PAGE_NAMES))
            window._show_widget(window.transfer_page)
            self.assertEqual(window.stack.currentIndex(), PAGE_NAMES.index("上傳與轉傳"))
            self.assertEqual(window.transfer_page.active_account_id(), account.id)
            self.close(window)

    def test_clear_finished_removes_rows_and_queue_records_but_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            account = bridge.store.add_account("清除測試")
            failed = bridge.store.create_download_job(account.id, ["https://t.me/telegram/1"])
            bridge.store.finish_job(failed.id, "failed", error="x")
            paused = bridge.store.create_download_job(account.id, ["https://t.me/telegram/2"])
            bridge.store.finish_job(paused.id, "paused")
            window = MainWindow(bridge, resume_downloads=False)
            window.show()
            self.pump(lambda: window._queue_loaded and len(window.jobs) == 2)

            window.home_page.clear_button.click()
            self.pump(lambda: len(window.home_page.rows) == 1)
            self.assertEqual([job.backend_job_id for job in window.jobs.values()], [paused.id])
            self.assertEqual([job.id for job in bridge.store.list_queue()], [paused.id])
            self.assertEqual(len(bridge.store.list_history()), 2)
            self.close(window)

    def test_advanced_settings_are_validated_then_saved(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, _account, window = self._window(directory)
            page = window.settings_page
            page.include_edit.setText("mp4")
            page.exclude_edit.setText("jpg")
            page._apply_advanced()
            self.assertIn("擇一", page.advanced_status.text())
            self.assertEqual(bridge.store.get_preferences().include_ext, "")

            page.exclude_edit.clear()
            page.template_combo.setCurrentIndex(2)
            page.order_combo.setCurrentIndex(1)
            page.threads_spin.setValue(8)
            page.takeout_check.setChecked(True)
            page._apply_advanced()
            # Settings save on every change; wait for the last one to land.
            self.pump(lambda: bridge.store.get_preferences().threads == 8 and not window._preference_saving)
            self.assertTrue(page.advanced_status.text().startswith("已儲存"))
            saved = bridge.store.get_preferences()
            self.assertEqual(saved.include_ext, "mp4")
            self.assertEqual(saved.template, "{{ .MessageID }}_{{ filenamify .FileName }}")
            self.assertTrue(saved.desc and saved.takeout)
            self.assertEqual(saved.threads, 8)

            # A saved custom template comes back as the custom entry.
            bridge.store.update_preferences(template="{{ .FileName }}_{{ .MessageID }}")
            reopened = type(page)(bridge)
            self.assertIsNone(reopened.template_combo.currentData())
            self.assertEqual(reopened.template_edit.text(), "{{ .FileName }}_{{ .MessageID }}")
            reopened.deleteLater()
            self.close(window)

    def test_channel_scan_totals_reach_the_stored_job(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, account, window = self._window(directory, {"dl": _events()})
            manifest = Path(directory) / "scan.json"
            manifest.write_text(json.dumps({"id": 1, "messages": [{"id": 5}]}), encoding="utf-8")
            with patch.object(window, "_launch_download"):
                window._enqueue_channel_downloads([], {
                    "account_id": account.id, "output_dir": directory, "files": [str(manifest)],
                    "title": "頻道 · 全部時間", "expected_files": 12, "expected_bytes": 3456,
                })
                self.pump(lambda: bool(bridge.store.list_queue()))
            saved = bridge.store.list_queue()[0]
            self.assertEqual((saved.expected_files, saved.expected_bytes), (12, 3456))
            job = next(iter(window.jobs.values()))
            self.assertEqual(window.home_page.rows[job.id]._expected_bytes, 3456)
            self.close(window)

    def test_backup_then_restore_adds_a_confirmed_account(self) -> None:
        scripts = {
            "backup": "",
            "recover": "",
            "whoami": _events({"event": "authenticated", "id": "77", "username": "restored", "name": "還原帳號"}),
        }
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, account, window = self._window(directory, scripts)
            destination = str(Path(directory) / "account.backup.tdl")
            self.assertEqual(bridge.backup_account(account.id, destination), destination)
            self.assertEqual(bridge.runner.built[-1][-3:], ["backup", "--dst", str(Path(destination).resolve())])

            Path(destination).write_bytes(b"backup")
            with patch("desktop.app.QFileDialog.getOpenFileName", return_value=(destination, "")), \
                    patch("desktop.app.QInputDialog.getText", return_value=("第二台電腦", True)):
                window._restore_account()
            self.pump(lambda: len(window.accounts) == 2 and window.account_page.restore_button.isEnabled())
            restored = next(item for item in bridge.store.list_accounts() if item.name == "第二台電腦")
            self.assertTrue(restored.authenticated)
            self.assertEqual(restored.username, "restored")
            self.assertIn("已還原", window.account_page.operation_status.text())
            self.close(window)

    def test_restore_that_telegram_rejects_leaves_no_account_behind(self) -> None:
        scripts = {"recover": "", "whoami": _events({"event": "error", "message": "AUTH_KEY_UNREGISTERED"}, exit_code=1)}
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, _account, window = self._window(directory, scripts)
            source = Path(directory) / "old.tdl"
            source.write_bytes(b"backup")
            with patch("desktop.app.QFileDialog.getOpenFileName", return_value=(str(source), "")), \
                    patch("desktop.app.QInputDialog.getText", return_value=("過期備份", True)):
                window._restore_account()
            self.pump(lambda: "還原失敗" in window.account_page.operation_status.text())
            self.assertIn("重新登入", window.account_page.operation_status.text())
            self.assertEqual([item.name for item in bridge.store.list_accounts()], ["介面測試"])
            self.close(window)


class TransferPageTests(_QtCase):
    def _page(self, directory: str, scripts):  # type: ignore[no-untyped-def]
        bridge = BackendBridge()
        account = bridge.store.add_account("傳輸")
        bridge.store.update_account(account.id, authenticated=True)
        bridge.runner = _ScriptRunner(bridge.store, scripts)
        bridge.store.save_chat_cache(account.id, [
            {"id": "1", "ref": "channel:1", "type": "channel", "title": "我的頻道", "username": "mine"},
        ])
        page = TransferPage()
        page.set_runner(bridge.runner)
        page.set_accounts(bridge.accounts(), account.id)
        return bridge, page

    def test_destination_list_comes_from_the_cache_and_accepts_typed_text(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            _bridge, page = self._page(directory, {})
            combo = page.upload_destination
            self.assertEqual(combo.count(), 2)
            self.assertEqual(combo.destination(), "")  # Saved Messages by default
            combo.setCurrentIndex(1)
            self.assertEqual(combo.destination(), "channel:1")
            combo.setEditText("@someone")
            self.assertEqual(combo.destination(), "@someone")
            self.assertIn("1 個對話", page.destination_hint.text())
            page.deleteLater()

    def test_upload_runs_with_the_chosen_options_and_reports_each_file(self) -> None:
        scripts = {"up": _events(
            {"event": "status", "count": 2, "message": "共 2 個檔案等待上傳"},
            {"event": "progress", "file_id": "C:/a.mp4", "filename": "a.mp4", "state": "uploading", "percent": 40, "total": 2048, "speed": "1.0 MiB/s"},
            {"event": "progress", "file_id": "C:/a.mp4", "filename": "a.mp4", "state": "completed", "percent": 100},
            {"event": "progress", "file_id": "C:/b.mp4", "filename": "b.mp4", "state": "completed", "percent": 100},
        )}
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, page = self._page(directory, scripts)
            media = Path(directory) / "clip.mp4"
            media.write_bytes(b"x")
            page.add_upload_paths([str(media), str(media)])
            self.assertEqual(page.upload_list.count(), 1)
            page.upload_destination.setCurrentIndex(1)
            page.caption_combo.setCurrentIndex(1)
            page.photo_check.setChecked(True)
            page.upload_button.click()
            self.assertTrue(page.is_busy())
            self.assertFalse(page.upload_button.isEnabled())
            self.pump(lambda: not page.is_busy())

            args = bridge.runner.built[-1]
            self.assertEqual(args[args.index("--chat") + 1], "channel:1")
            self.assertEqual(args[args.index("--caption") + 1], "FileName + FileExt")
            self.assertIn("--photo", args)
            self.assertNotIn("--rm", args)
            self.assertIn("上傳完成：已完成 2/2", page.upload_status.text())
            self.assertEqual(page.upload_progress.count(), 2)
            self.assertIn("已完成", page.upload_progress.item(0).text())
            self.assertEqual(page.upload_list.count(), 0)
            self.assertTrue(page.upload_button.isEnabled())
            page.deleteLater()

    def test_delete_after_upload_needs_an_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, page = self._page(directory, {"up": _events()})
            media = Path(directory) / "keep.mp4"
            media.write_bytes(b"x")
            page.add_upload_paths([str(media)])
            page.remove_check.setChecked(True)
            with patch("desktop.transfer_page._confirm", return_value=False) as confirm:
                page.upload_button.click()
            confirm.assert_called_once()
            self.assertFalse(page.is_busy())
            self.assertEqual(bridge.runner.built, [])
            page.deleteLater()

    def test_dry_run_and_failed_forward_are_reported_separately(self) -> None:
        scripts = {"forward": _events(
            {"event": "status", "count": 1},
            {"event": "progress", "file_id": "1:5:9", "filename": "來源 #5 → 目的地", "state": "completed", "percent": 100},
        )}
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge, page = self._page(directory, scripts)
            page.forward_links.setPlainText("t.me/telegram/193\nhttps://t.me/telegram/193")
            self.assertEqual(page.forward_sources(), ["https://t.me/telegram/193"])
            page.forward_mode.setCurrentIndex(1)
            page.dry_run_button.click()
            self.pump(lambda: not page.is_busy())
            args = bridge.runner.built[-1]
            self.assertIn("--dry-run", args)
            self.assertEqual(args[args.index("--mode") + 1], "clone")
            self.assertNotIn("--to", args)
            self.assertIn("沒有送出任何訊息", page.forward_status.text())

            bridge.runner.scripts["forward"] = _events(
                {"event": "status", "count": 1},
                {"event": "error", "file_id": "1:5:9", "state": "failed", "message": "CHAT_FORWARDS_RESTRICTED"},
                exit_code=1,
            )
            page.forward_button.click()
            self.pump(lambda: not page.is_busy())
            self.assertNotIn("--dry-run", bridge.runner.built[-1])
            self.assertIn("轉傳未全部完成", page.forward_status.text())
            self.assertIn("失敗 1", page.forward_status.text())
            self.assertIn("複製", page.forward_status.text())
            page.deleteLater()

    def test_unauthenticated_account_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory(dir=BUILD) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            bridge.store.add_account("未登入")
            bridge.runner = _ScriptRunner(bridge.store, {})
            page = TransferPage()
            page.set_runner(bridge.runner)
            page.set_accounts(bridge.accounts())
            page.forward_links.setPlainText("https://t.me/telegram/193")
            page.forward_button.click()
            self.assertFalse(page.is_busy())
            self.assertIn("尚未登入", page.forward_status.text())
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
