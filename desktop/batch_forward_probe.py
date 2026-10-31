"""Offline Qt batch-forward fixture; never creates a real Telegram client."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from desktop.workflow_probe import _FixtureHandle, _FixtureResult, _FixtureRunner, _pump_until


class _BatchFixtureRunner(_FixtureRunner):
    chats = [
        {"id": "1000001", "ref": "channel:1000001", "type": "channel", "title": "離線來源頻道", "forum": False},
        {"id": "1000002", "ref": "channel:1000002", "type": "group", "title": "離線來源群組", "forum": True},
        {"id": "1000003", "ref": "channel:1000003", "type": "group", "title": "我的離線收藏群組", "forum": True, "creator": True},
    ]

    def __init__(self, store: Any, state: Path) -> None:
        super().__init__(store, state)
        self.batch_calls: list[dict[str, Any]] = []
        self.fail_next = False
        self.hold_next = False

    def start_export(self, account_id: str, chat_id: str, start_date: Any, end_date: Any, media_types: Any,
                     output_path: Any = None, *, topic_id: Any = None, **kwargs: Any) -> _FixtureHandle:
        self.export_calls.append({"account_id": account_id, "chat_id": chat_id, "start_date": str(start_date),
                                  "end_date": str(end_date), "media_types": list(media_types), "topic_id": topic_id})
        directory = self.store.account_root(account_id) / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"batch-fixture-{len(self.export_calls)}.json"
        payload = {"id": int(chat_id.split(":")[-1]), "peer_type": "channel", "messages": [
            {"id": 11, "type": "message", "file": "離線影片一.mp4", "date": 1700000000, "media_type": "video", "size": 1024},
            {"id": 12, "type": "message", "file": "離線影片二.mp4", "date": 1700000010, "media_type": "video", "size": 2048},
        ]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self._handle({"path": str(path), "count": 2, "total_bytes": 3072,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
                            {"event": "status", "message": "離線預覽完成"})

    def start_batch_forward(self, account_id: str, manifest_path: str, **kwargs: Any) -> _FixtureHandle:
        digest = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
        if digest != kwargs.get("manifest_sha256") or kwargs.get("expected_count") != 2:
            raise ValueError("fixture manifest does not match preview")
        if kwargs.get("source_chat") != "channel:1000002" or kwargs.get("to") != "channel:1000003":
            raise ValueError("fixture source or destination mismatch")
        self.batch_calls.append({"account_id": account_id, **{key: value for key, value in kwargs.items() if key != "on_event"}})
        failing, holding = self.fail_next, self.hold_next
        self.fail_next = self.hold_next = False
        events = [{"event": "status", "kind": "forward", "count": 2, "message": "離線模擬轉傳"},
                  {"event": "progress", "file_id": "fixture-11", "filename": "離線影片一.mp4", "state": "completed", "percent": 100},
                  {"event": "progress", "file_id": "fixture-12", "filename": "離線影片二.mp4", "state": "failed" if failing else "completed", "percent": 0 if failing else 100}]
        return _FixtureHandle(_FixtureResult(success=not failing, error="離線模擬部分失敗" if failing else None),
                              events, delay=1.0 if holding else 0.08)


def run_probe(report_path: str | os.PathLike[str] | None = None) -> int:
    from PySide6.QtCore import QDate, Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QScrollArea
    from desktop import batch_forward_page as batch_module
    from desktop.app import BackendBridge, MainWindow, STYLE

    report = Path(report_path or "batch-forward-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    for name in ("msjh.ttc", "msjhbd.ttc"):
        font = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    metrics: dict[str, Any] = {"ok": False, "offline_fixture": True, "screenshot_files": []}
    original_confirm = batch_module._confirm_batch
    old_root = os.environ.get("TDL_DESKTOP_DATA_DIR")
    started = time.monotonic()
    window = None

    def require(predicate: Any, code: str, timeout: float = 8.0) -> None:
        if not _pump_until(app, predicate, timeout):
            raise RuntimeError(code)

    def save(name: str) -> None:
        path = report.parent / f"batch-forward-{name}.png"
        if not window.grab().save(str(path)):
            raise RuntimeError("screenshot_failed")
        metrics["screenshot_files"].append(path.name)

    try:
        with tempfile.TemporaryDirectory(prefix="batch-forward-fixture-", dir=report.parent) as directory:
            state = Path(directory)
            os.environ["TDL_DESKTOP_DATA_DIR"] = str(state)
            bridge = BackendBridge()
            account = bridge.store.add_account("離線批次轉傳帳號")
            bridge.store.update_account(account.id, authenticated=True)
            bridge.store.set_active_account(account.id)
            bridge.store.save_chat_cache(account.id, _BatchFixtureRunner.chats)
            runner = _BatchFixtureRunner(bridge.store, state)
            bridge.runner = runner
            window = MainWindow(bridge, resume_downloads=False)
            try:
                window.show()
                require(lambda: window._accounts_loaded and window._queue_loaded, "startup_timeout")
                window._show_widget(window.transfer_page)
                page = window.transfer_page.batch_page
                metrics["batch_entry_default"] = window.transfer_page.stack.currentWidget() is page
                page.refresh_button.click()
                require(lambda: runner.list_calls == 1 and page.chat_list.count() == 3 and not page.is_busy(), "refresh_timeout")
                source_row = next(i for i in range(page.chat_list.count()) if
                                  page.chat_list.item(i).data(Qt.UserRole).get("ref") == "channel:1000002")
                page.chat_list.setCurrentRow(source_row)
                require(lambda: page._topics_loaded and not page.is_busy(), "source_topics_timeout")
                topic_row = next(i for i in range(page.topic_list.count()) if
                                 str(page.topic_list.item(i).data(Qt.UserRole).get("id")) == "2")
                page.topic_list.setCurrentRow(topic_row)
                target_index = next(i for i in range(page.target_group_combo.count()) if
                                    isinstance(page.target_group_combo.itemData(i), dict) and
                                    page.target_group_combo.itemData(i).get("ref") == "channel:1000003")
                page.target_group_combo.setCurrentIndex(target_index)
                require(lambda: page._target_topics_loaded and not page.is_busy(), "target_topics_timeout")
                target_topic = next(i for i in range(page.target_topic_combo.count()) if
                                    isinstance(page.target_topic_combo.itemData(i), dict) and
                                    str(page.target_topic_combo.itemData(i).get("id")) == "2")
                page.target_topic_combo.setCurrentIndex(target_topic)
                page.start_date.setDate(QDate(2026, 10, 1))
                page.end_date.setDate(QDate(2026, 10, 31))
                page.scan_button.click()
                require(lambda: page._export_count == 2 and not page.is_busy(), "scan_timeout")
                scan = runner.export_calls[-1]
                metrics["source_topic_date_media"] = (str(scan["topic_id"]) == "2" and
                    scan["start_date"] == "2026-10-01" and scan["end_date"] == "2026-10-31" and scan["media_types"] == ["video"])
                metrics["preview_does_not_send"] = not runner.batch_calls
                metrics["geometry"] = []
                for width, height in ((1180, 800), (960, 680)):
                    window.resize(width, height)
                    app.processEvents()
                    container = window.stack.currentWidget()
                    if not isinstance(container, QScrollArea):
                        raise RuntimeError("missing_scroll_container")
                    container.verticalScrollBar().setValue(0)
                    app.processEvents()
                    save(f"preview-{width}x{height}")
                    container.ensureWidgetVisible(page.download_button, 0, 16)
                    app.processEvents()
                    metrics["geometry"].append({"size": [width, height],
                        "no_horizontal_scroll": container.horizontalScrollBar().maximum() == 0,
                        "action_visible": container.viewport().rect().intersects(page.download_button.rect().translated(
                            page.download_button.mapTo(container.viewport(), page.download_button.rect().topLeft())))})
                    save(f"actions-{width}x{height}")
                page.dry_run_button.click()
                require(lambda: len(runner.batch_calls) == 1 and not page.is_busy(), "dry_run_timeout")
                metrics["dry_run_preserves_preview"] = bool(runner.batch_calls[0]["dry_run"] and page.download_button.isEnabled())
                batch_module._confirm_batch = lambda *_args: False
                page.download_button.click()
                metrics["confirmation_cancel_no_send"] = len(runner.batch_calls) == 1 and page.download_button.isEnabled()
                batch_module._confirm_batch = lambda *_args: True
                page.download_button.click()
                metrics["legacy_locked_during_batch"] = not window.transfer_page.upload_button.isEnabled()
                require(lambda: len(runner.batch_calls) == 2 and not page.is_busy(), "forward_timeout")
                metrics["destination_topic_forwarded"] = str(runner.batch_calls[-1].get("topic_id")) == "2"
                metrics["actual_after_confirmation"] = not runner.batch_calls[-1]["dry_run"]
                page.download_button.click()
                metrics["duplicate_submit_blocked"] = len(runner.batch_calls) == 2 and not page.download_button.isEnabled()
                save("completed")
                page.end_date.setDate(QDate(2026, 10, 30))
                metrics["changed_conditions_invalidate"] = not page.download_button.isEnabled() and not page._export_path
                page.scan_button.click()
                require(lambda: page.download_button.isEnabled() and not page.is_busy(), "rescan_timeout")
                runner.hold_next = True
                page.download_button.click()
                require(lambda: len(runner.batch_calls) == 3, "cancel_start_timeout")
                page.cancel_button.click()
                require(lambda: not page.is_busy(), "cancel_timeout")
                metrics["cancel_blocks_resubmit"] = not page.download_button.isEnabled()
                metrics["progress_rows"] = page.batch_progress.count()
                save("cancelled")
                page.scan_button.click()
                require(lambda: page.download_button.isEnabled() and not page.is_busy(), "failure_rescan_timeout")
                runner.fail_next = True
                page.download_button.click()
                require(lambda: len(runner.batch_calls) == 4 and not page.is_busy(), "partial_failure_timeout")
                metrics["partial_failure_blocks_resubmit"] = not page.download_button.isEnabled()
                metrics["partial_failure_visible"] = "失敗" in page.status_label.text() or "未完成" in page.status_label.text()
                save("partial-failure")
            finally:
                window.close()
                require(lambda: not window.isVisible() and not window._background_work_running(), "cleanup_timeout")
                metrics["clean_shutdown"] = True
                window.deleteLater()
                app.processEvents()
                window = None
            checks = ("batch_entry_default", "source_topic_date_media", "preview_does_not_send", "dry_run_preserves_preview",
                      "confirmation_cancel_no_send", "legacy_locked_during_batch", "destination_topic_forwarded",
                      "actual_after_confirmation", "duplicate_submit_blocked", "changed_conditions_invalidate",
                      "cancel_blocks_resubmit", "partial_failure_blocks_resubmit", "partial_failure_visible", "clean_shutdown")
            metrics["ok"] = all(metrics.get(key) for key in checks) and all(
                row["no_horizontal_scroll"] and row["action_visible"] for row in metrics["geometry"])
    except Exception as exc:
        import traceback
        metrics["failure"] = f"{type(exc).__name__}: {exc}"
        metrics["traceback"] = traceback.format_exc()
        metrics["ok"] = False
    finally:
        batch_module._confirm_batch = original_confirm
        if old_root is None:
            os.environ.pop("TDL_DESKTOP_DATA_DIR", None)
        else:
            os.environ["TDL_DESKTOP_DATA_DIR"] = old_root
    metrics["elapsed"] = round(time.monotonic() - started, 3)
    report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if metrics["ok"] else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    raise SystemExit(run_probe(parser.parse_args().report))
