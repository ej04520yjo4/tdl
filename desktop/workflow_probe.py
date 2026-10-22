"""Offline native Qt acceptance check for the channel download workflow.

The fixture deliberately uses the real ``MainWindow`` and ``ChannelPage``.
Only the backend runner is replaced with an in-memory implementation, so this
check can exercise the Qt worker threads, queued slots, durable queue handoff,
page navigation, and forum topic selection without an account or network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


_STARTUP_TIMEOUT = 15.0
_OPERATION_TIMEOUT = 15.0
_CLOSE_TIMEOUT = 15.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_metrics() -> dict[str, Any]:
    return {
        "ok": False,
        "offline_fixture": True,
        "chat_count": 0,
        "scan_count": 0,
        "refreshes": 0,
        "queued_on_channel_page": False,
        "queue_view_checked": False,
        "returned_to_channel": False,
        "switch_invalidated_scan": False,
        "topic_selection_checked": False,
        "topic_scan_forwarded": False,
        "callback_events": 0,
        "callback_successes": 0,
        "thread_violations": 0,
        "heartbeat": 0,
        "geometry_checks": 0,
        "button_scroll_checks": 0,
        "geometry_ok": False,
        "geometry_states": {},
        "screenshot_960x680": False,
        "screenshot_files": [],
        "cleanup_clean": False,
        "elapsed": 0.0,
    }


class _FixtureResult:
    def __init__(
        self,
        *,
        success: bool,
        data: Optional[Mapping[str, Any]] = None,
        job_id: Optional[str] = None,
        cancelled: bool = False,
        error: Optional[str] = None,
    ) -> None:
        self.success = bool(success)
        self.data = dict(data or {})
        self.job_id = job_id
        self.cancelled = bool(cancelled)
        self.error = error


class _FixtureHandle:
    """Small RunHandle-compatible object consumed by the real Qt workers."""

    def __init__(
        self,
        result: _FixtureResult,
        events: list[Mapping[str, Any]],
        *,
        on_complete: Optional[Callable[[], None]] = None,
        delay: float = 0.025,
    ) -> None:
        self.done = threading.Event()
        self._events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._result = result
        self._on_complete = on_complete
        self._cancelled = threading.Event()
        self._delay = max(0.0, float(delay))
        for event in events:
            self._events.put(dict(event))
        self._thread = threading.Thread(target=self._finish, daemon=True)
        self._thread.start()

    def _finish(self) -> None:
        if self._delay:
            time.sleep(self._delay)
        if self._cancelled.is_set():
            return
        if self._on_complete is not None:
            try:
                self._on_complete()
            except Exception:
                # A fixture persistence callback must never strand a Qt worker.
                self._result = _FixtureResult(
                    success=False,
                    cancelled=False,
                    error="fixture persistence failed",
                    job_id=self._result.job_id,
                )
        self.done.set()

    def get_event(self, timeout: float | None = None) -> Optional[dict[str, Any]]:
        try:
            return self._events.get(timeout=timeout or 0)
        except queue.Empty:
            return None

    def wait(self, timeout: float | None = None) -> Optional[_FixtureResult]:
        if timeout is None:
            self.done.wait()
        else:
            self.done.wait(max(0.0, float(timeout)))
        return self._result if self.done.is_set() else None

    def cancel(self) -> bool:
        if self.done.is_set():
            return False
        self._cancelled.set()
        self._result = _FixtureResult(
            success=False,
            cancelled=True,
            job_id=self._result.job_id,
            error="fixture cancelled",
        )
        self.done.set()
        return True

    def pause(self) -> bool:
        return self.cancel()


class _FixtureRunner:
    """Offline backend surface used by ChannelPage and DownloadWorker."""

    chats = [
        {
            "id": "-1000001",
            "ref": "channel:1000001",
            "type": "channel",
            "title": "離線頻道 A",
            "username": "fixture_a",
            "forum": False,
        },
        {
            "id": "-1000002",
            "ref": "channel:1000002",
            "type": "group",
            "title": "離線論壇 B",
            "username": "fixture_b",
            "forum": True,
        },
    ]

    def __init__(self, store: Any, state: Path) -> None:
        self.store = store
        self.state = state
        self.list_calls = 0
        self.topic_calls = 0
        self.export_calls: list[dict[str, Any]] = []
        self.download_calls = 0

    @staticmethod
    def _handle(
        data: Mapping[str, Any],
        *events: Mapping[str, Any],
        on_complete: Optional[Callable[[], None]] = None,
    ) -> _FixtureHandle:
        return _FixtureHandle(
            _FixtureResult(success=True, data=data),
            [dict(event) for event in events],
            on_complete=on_complete,
        )

    def start_list_chats(self, _account_id: str, *, on_event: Any = None, **_kwargs: Any) -> _FixtureHandle:
        self.list_calls += 1
        return self._handle(
            {"chats": [dict(item) for item in self.chats]},
            {"event": "status", "message": "離線清單已準備"},
            {"event": "chats", "chats": [dict(item) for item in self.chats]},
        )

    def start_list_topics(
        self,
        _account_id: str,
        chat_id: str,
        *,
        on_event: Any = None,
        **_kwargs: Any,
    ) -> _FixtureHandle:
        self.topic_calls += 1
        topics = [
            {"id": "1", "title": "一般"},
            {"id": "2", "title": "離線測試主題"},
        ]
        return self._handle(
            {"topics": topics, "chat_ref": str(chat_id)},
            {"event": "status", "message": "離線主題清單已準備"},
            {"event": "topics", "chat_ref": str(chat_id), "topics": topics},
        )

    def start_export(
        self,
        account_id: str,
        chat_id: str,
        start_date: Any,
        end_date: Any,
        media_types: Any,
        output_path: Any = None,
        *,
        topic_id: Any = None,
        on_event: Any = None,
        **_kwargs: Any,
    ) -> _FixtureHandle:
        self.export_calls.append(
            {
                "account_id": str(account_id),
                "chat_id": str(chat_id),
                "start_date": str(start_date),
                "end_date": str(end_date),
                "media_types": list(media_types or []),
                "topic_id": None if topic_id in (None, "") else str(topic_id),
            }
        )
        index = len(self.export_calls)
        export_dir = self.state / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"fixture-export-{index}.json"
        message_count = 2 if str(chat_id) == "channel:1000001" else 1
        messages = [
            {
                "id": item,
                "type": "message",
                "file": f"fixture-{index}-{item}.mp4",
                "date": 1_700_000_000 + item,
                "media_type": "video",
                "size": 1024 * item,
            }
            for item in range(1, message_count + 1)
        ]
        export_path.write_text(
            json.dumps(
                {
                    "id": 1000001 if str(chat_id) == "channel:1000001" else 1000002,
                    "peer_type": "channel",
                    "messages": messages,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        total_bytes = sum(int(item["size"]) for item in messages)
        return self._handle(
            {
                "path": str(export_path),
                "count": message_count,
                "total_bytes": total_bytes,
            },
            {"event": "status", "message": "離線掃描已完成"},
            {"event": "exported", "path": str(export_path), "count": message_count},
        )

    def start(self, request: Any, *, on_event: Any = None, **_kwargs: Any) -> _FixtureHandle:
        """Complete a queued import through the normal DownloadWorker path."""

        self.download_calls += 1
        job_id = str(getattr(request, "job_id", "") or "")
        if job_id:
            try:
                self.store.mark_job_running(job_id)
            except Exception:
                pass

        def complete() -> None:
            if job_id:
                self.store.update_job_progress(
                    job_id,
                    percent=100,
                    current=2048,
                    total=2048,
                    filename="fixture-video.mp4",
                    file_progress={
                        "fixture-video": {
                            "state": "completed",
                            "percent": 100,
                            "current": 2048,
                            "total": 2048,
                            "filename": "fixture-video.mp4",
                        }
                    },
                )
                self.store.finish_job(job_id, "completed")

        handle = _FixtureHandle(
            _FixtureResult(success=True, data={}, job_id=job_id),
            [
                {
                    "event": "progress",
                    "file_id": "fixture-video",
                    "filename": "fixture-video.mp4",
                    "state": "downloading",
                    "current": 1024,
                    "total": 2048,
                    "percent": 50,
                },
                {
                    "event": "progress",
                    "file_id": "fixture-video",
                    "filename": "fixture-video.mp4",
                    "state": "completed",
                    "current": 2048,
                    "total": 2048,
                    "percent": 100,
                },
            ],
            on_complete=complete,
        )
        return handle

    def start_download(self, *_args: Any, **kwargs: Any) -> _FixtureHandle:
        # This fallback keeps the fixture compatible with callers that have
        # not yet assigned a durable job id.
        return _FixtureHandle(
            _FixtureResult(success=True, data={}),
            [{"event": "progress", "percent": 100, "state": "completed"}],
        )

    def cancel_all(self) -> None:
        return None


def _pump_until(app: Any, predicate: Callable[[], bool], timeout: float) -> bool:
    from PySide6.QtCore import QEventLoop

    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents(QEventLoop.AllEvents, 50)
    return bool(predicate())


def _item_data(item: Any, qt: Any) -> Any:
    if item is None:
        return None
    try:
        return item.data(qt.UserRole)
    except Exception:
        return None


def _chat_ref(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("ref", value.get("chat_ref", "")) or "").strip()


def _save_screenshot(window: Any, path: Path) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = window.grab()
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError("screenshot_failed")
    return int(pixmap.width()), int(pixmap.height())


def _check_channel_geometry(
    window: Any,
    page: Any,
    metrics: dict[str, Any],
    *,
    state: str,
) -> dict[str, Any]:
    """Check the real page scroll container at the two supported sizes.

    The channel page is mounted inside MainWindow's QScrollArea.  Checking
    the mapped widget rectangles is more useful than a sizeHint assertion:
    it catches a real horizontal overflow and proves that the two action
    buttons remain reachable after the page is scrolled.
    """

    from PySide6.QtCore import QPoint, QRect, QEventLoop, Qt
    from PySide6.QtWidgets import QScrollArea

    container = None
    for index in range(window.stack.count()):
        candidate = window.stack.widget(index)
        if isinstance(candidate, QScrollArea) and candidate.widget() is page:
            container = candidate
            break
    if container is None:
        metrics["_failure_code"] = f"{state}_scroll_container_missing"
        raise RuntimeError("scroll_container_missing")

    # QApplication.processEvents is intentionally obtained from the widget;
    # the probe already has a live QApplication and this avoids a second app.
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance()
    if qapp is None:
        metrics["_failure_code"] = f"{state}_application_missing"
        raise RuntimeError("application_missing")

    results: dict[str, Any] = {}
    # Keep partial measurements in the receipt when a geometry assertion
    # fails, so a release report identifies the first failing size.
    metrics.setdefault("geometry_states", {})[state] = results
    for width, height in ((1180, 800), (960, 680)):
        window.resize(width, height)
        qapp.processEvents(QEventLoop.AllEvents, 100)
        container.verticalScrollBar().setValue(0)
        qapp.processEvents(QEventLoop.AllEvents, 100)

        viewport = container.viewport()
        viewport_rect = viewport.rect()
        page_top_left = page.mapTo(viewport, QPoint(0, 0))
        page_rect = QRect(page_top_left, page.size())
        horizontal_ok = bool(
            page_rect.left() >= viewport_rect.left()
            and page_rect.right() <= viewport_rect.right()
            and not container.horizontalScrollBar().isVisible()
            and container.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        )
        key = f"{width}x{height}"
        results[key] = {
            "window_size": [int(window.width()), int(window.height())],
            "viewport_size": [int(viewport.width()), int(viewport.height())],
            "page_size": [int(page.width()), int(page.height())],
            "page_left": int(page_rect.left()),
            "page_right": int(page_rect.right()),
            "viewport_right": int(viewport_rect.right()),
            "horizontal_bar_visible": bool(container.horizontalScrollBar().isVisible()),
            "horizontal_ok": horizontal_ok,
            "buttons_visible_after_scroll": {},
        }
        if not horizontal_ok:
            metrics["_failure_code"] = f"{state}_horizontal_clip_{width}x{height}"
            raise RuntimeError("horizontal_clip")

        buttons: dict[str, bool] = {}
        for name, button in (
            ("scan", page.scan_button),
            ("download", page.download_button),
        ):
            container.ensureWidgetVisible(button, 0, 24)
            qapp.processEvents(QEventLoop.AllEvents, 100)
            button_top_left = button.mapTo(viewport, QPoint(0, 0))
            button_rect = QRect(button_top_left, button.size())
            visible = bool(viewport_rect.contains(button_rect))
            buttons[name] = visible
            results[key]["buttons_visible_after_scroll"] = dict(buttons)
            metrics["button_scroll_checks"] += 1
            if not visible:
                metrics["_failure_code"] = f"{state}_{name}_unreachable_{width}x{height}"
                raise RuntimeError("action_button_unreachable")

        metrics["geometry_checks"] += 1

    return results


def _probe_page_type(base_type: type[Any], app: Any, metrics: dict[str, Any], callbacks: dict[str, int]) -> type[Any]:
    """Observe production slots without bypassing Qt queued delivery."""

    from PySide6.QtCore import QThread, Slot

    class ProbeChannelPage(base_type):
        @Slot(int, object, name="_operation_event")
        def _probe_operation_event(self: Any, token: int, event: Any) -> None:
            callbacks["events"] += 1
            if QThread.currentThread() != app.thread():
                metrics["thread_violations"] += 1
            super()._operation_event(token, event)

        _operation_event = _probe_operation_event

        @Slot(int, str, object, name="_operation_succeeded")
        def _probe_operation_succeeded(self: Any, token: int, kind: str, data: Any) -> None:
            callbacks["successes"] += 1
            if QThread.currentThread() != app.thread():
                metrics["thread_violations"] += 1
            super()._operation_succeeded(token, kind, data)

        _operation_succeeded = _probe_operation_succeeded

        def download_queued(self: Any, title: str, count: int) -> bool:
            callbacks["download_ack"] += 1
            if QThread.currentThread() != app.thread():
                metrics["thread_violations"] += 1
            return super().download_queued(title, count)

    return ProbeChannelPage


def _run_native_ui(state: Path, report: Path, metrics: dict[str, Any]) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("TDLDesktop")
    app.setQuitOnLastWindowClosed(False)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass

    from desktop import app as app_module
    from desktop.app import BackendBridge, MainWindow, STYLE

    app.setStyleSheet(STYLE)
    for font_path in (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"):
        if Path(font_path).exists():
            QFontDatabase.addApplicationFont(font_path)
    app.setFont(QFont("Microsoft JhengHei UI", 10))

    bridge = BackendBridge()
    if not bridge.available:
        raise RuntimeError("backend_unavailable")
    account = bridge.store.add_account("離線流程帳號")
    bridge.store.update_account(account.id, authenticated=True)
    bridge.store.set_active_account(account.id)
    fixture = _FixtureRunner(bridge.store, state)
    bridge.runner = fixture

    callbacks = {"events": 0, "successes": 0, "download_ack": 0}
    base_page_type = app_module.ChannelPage
    app_module.ChannelPage = _probe_page_type(base_page_type, app, metrics, callbacks)
    window = None
    page = None
    heartbeat_timer = None
    workflow_ok = False
    close_verified = False
    screenshot_names: list[str] = []
    try:
        try:
            window = MainWindow(bridge)
        finally:
            app_module.ChannelPage = base_page_type
        page = window.channel_page
        page.set_download_folder(str(state / "downloads"))
        heartbeat_timer = QTimer(window)
        heartbeat_timer.setInterval(100)
        heartbeat_timer.timeout.connect(lambda: metrics.__setitem__("heartbeat", metrics["heartbeat"] + 1))
        heartbeat_timer.start()

        window.show()
        window.move(-window.width() - 50, -window.height() - 50)
        window.repaint()
        app.processEvents(QEventLoop.AllEvents, 100)

        def startup_ready() -> bool:
            return bool(
                window._accounts_loaded
                and window._queue_loaded
                and page.active_account_id() == account.id
                and page._current_account_is_authenticated()
                and page.refresh_button.isEnabled()
                and not window._background_work_running()
            )

        if not _pump_until(app, startup_ready, _STARTUP_TIMEOUT):
            raise RuntimeError("startup_timeout")

        page.refresh_button.click()
        metrics["refreshes"] += 1
        if not _pump_until(
            app,
            lambda: (
                not page.is_busy()
                and page.chat_list.count() == 2
                and not getattr(page, "_thread_workers", {})
            ),
            _OPERATION_TIMEOUT,
        ):
            raise RuntimeError("channel_list_timeout")
        metrics["chat_count"] = page.chat_list.count()

        from PySide6.QtCore import Qt

        def find_chat(ref: str) -> Any:
            for index in range(page.chat_list.count()):
                item = page.chat_list.item(index)
                if _chat_ref(_item_data(item, Qt)) == ref:
                    return item
            return None

        item_a = find_chat("channel:1000001")
        item_b = find_chat("channel:1000002")
        if item_a is None or item_b is None:
            raise RuntimeError("fixture_chats_not_rendered")
        page.chat_list.setCurrentItem(item_a)
        if not _pump_until(
            app,
            lambda: page._selected_chat is not None
            and _chat_ref(page._selected_chat) == "channel:1000001"
            and page.scan_button.isEnabled(),
            5.0,
        ):
            raise RuntimeError("channel_a_selection_failed")

        page.scan_button.click()
        if not _pump_until(
            app,
            lambda: (
                not page.is_busy()
                and page._export_count == 2
                and page.download_button.isEnabled()
                and len(fixture.export_calls) >= 1
            ),
            _OPERATION_TIMEOUT,
        ):
            raise RuntimeError("channel_a_scan_failed")
        metrics["scan_count"] = page._export_count
        scan_image = report.with_name(f"{report.stem}-channel-a-scan.png")
        _save_screenshot(window, scan_image)
        screenshot_names.append(scan_image.name)
        _check_channel_geometry(window, page, metrics, state="channel_a")
        window.resize(1180, 800)
        app.processEvents(QEventLoop.AllEvents, 100)

        page.download_button.click()

        def queued() -> bool:
            current = window.stack.currentWidget()
            current_page = current.widget() if hasattr(current, "widget") else current
            return bool(
                callbacks["download_ack"] >= 1
                and current_page is page
                and page._export_consumed
                and not page._export_request_pending
                and len(window.jobs) == 1
            )

        if not _pump_until(app, queued, _OPERATION_TIMEOUT):
            raise RuntimeError("queue_ack_failed")
        metrics["queued_on_channel_page"] = True
        queue_image = report.with_name(f"{report.stem}-channel-a-queued.png")
        _save_screenshot(window, queue_image)
        screenshot_names.append(queue_image.name)

        page.view_queue_button.click()

        def queue_visible() -> bool:
            current = window.stack.currentWidget()
            current_page = current.widget() if hasattr(current, "widget") else current
            return bool(current_page is window.home_page and window.home_page.rows)

        if not _pump_until(app, queue_visible, 5.0):
            raise RuntimeError("queue_view_failed")
        metrics["queue_view_checked"] = True
        queue_view_image = report.with_name(f"{report.stem}-queue.png")
        _save_screenshot(window, queue_view_image)
        screenshot_names.append(queue_view_image.name)

        if not hasattr(window.home_page, "channel_button"):
            raise RuntimeError("channel_return_button_missing")
        window.home_page.channel_button.click()

        def channel_visible() -> bool:
            current = window.stack.currentWidget()
            current_page = current.widget() if hasattr(current, "widget") else current
            return current_page is page

        if not _pump_until(app, channel_visible, 5.0):
            raise RuntimeError("return_to_channel_failed")
        metrics["returned_to_channel"] = True

        page.chat_list.setCurrentItem(item_b)
        if not _pump_until(
            app,
            lambda: (
                page._selected_chat is not None
                and _chat_ref(page._selected_chat) == "channel:1000002"
                and page._export_path == ""
                and page._export_count == 0
                and page._export_snapshot is None
            ),
            5.0,
        ):
            raise RuntimeError("channel_b_invalidation_failed")
        metrics["switch_invalidated_scan"] = True

        if not _pump_until(
            app,
            lambda: (
                page._topics_loaded
                and not page._topics_loading
                and not page.is_busy()
                and page.topic_list.count() >= 2
                and not getattr(page, "_thread_workers", {})
            ),
            _OPERATION_TIMEOUT,
        ):
            raise RuntimeError("topic_list_timeout")

        topic_item = None
        for index in range(page.topic_list.count()):
            candidate = page.topic_list.item(index)
            value = _item_data(candidate, Qt)
            if isinstance(value, Mapping) and str(value.get("id", "")) == "2":
                topic_item = candidate
                break
        if topic_item is None:
            raise RuntimeError("topic_item_missing")
        page.topic_list.setCurrentItem(topic_item)
        if not _pump_until(
            app,
            lambda: (
                isinstance(page._selected_topic, Mapping)
                and str(page._selected_topic.get("id", "")) == "2"
                and page.scan_button.isEnabled()
            ),
            5.0,
        ):
            raise RuntimeError("topic_selection_failed")
        metrics["topic_selection_checked"] = True
        # The selected topic is passed through the real scan request.  This
        # makes the fixture catch a UI regression that silently scans all
        # forum topics after a topic is selected.
        page.scan_button.click()
        if not _pump_until(
            app,
            lambda: not page.is_busy() and len(fixture.export_calls) >= 2,
            _OPERATION_TIMEOUT,
        ):
            raise RuntimeError("topic_scan_failed")
        metrics["topic_scan_forwarded"] = fixture.export_calls[-1]["topic_id"] == "2"
        if not metrics["topic_scan_forwarded"]:
            raise RuntimeError("topic_id_not_forwarded")
        topic_image = report.with_name(f"{report.stem}-topic-selected.png")
        _save_screenshot(window, topic_image)
        screenshot_names.append(topic_image.name)
        _check_channel_geometry(window, page, metrics, state="forum_topic")
        window.resize(960, 680)
        app.processEvents(QEventLoop.AllEvents, 100)
        topic_small_image = report.with_name(f"{report.stem}-topic-selected-960x680.png")
        small_size = _save_screenshot(window, topic_small_image)
        screenshot_names.append(topic_small_image.name)
        metrics["screenshot_960x680"] = bool(small_size[0] > 0 and small_size[1] > 0)
        metrics["geometry_ok"] = bool(
            metrics["geometry_checks"] == 4
            and len(metrics.get("geometry_states", {})) == 2
            and metrics["button_scroll_checks"] == 8
        )
        window.resize(1180, 800)
        app.processEvents(QEventLoop.AllEvents, 100)

        workflow_ok = bool(
            metrics["refreshes"] == 1
            and metrics["chat_count"] == 2
            and metrics["scan_count"] == 2
            and metrics["queued_on_channel_page"]
            and metrics["queue_view_checked"]
            and metrics["returned_to_channel"]
            and metrics["switch_invalidated_scan"]
            and metrics["topic_selection_checked"]
            and metrics["topic_scan_forwarded"]
            and metrics["geometry_ok"]
            and metrics["screenshot_960x680"]
            and callbacks["events"] > 0
            and callbacks["successes"] >= 3
            and metrics["thread_violations"] == 0
        )
        if not workflow_ok:
            raise RuntimeError("workflow_assertions_failed")
    finally:
        metrics["callback_events"] = callbacks["events"]
        metrics["callback_successes"] = callbacks["successes"]
        if heartbeat_timer is not None:
            heartbeat_timer.stop()
        if window is not None:
            try:
                window.shutdown_workers(timeout_ms=10000)
                if workflow_ok:
                    window.close()

                    def closed_cleanly() -> bool:
                        collections_empty = all(
                            not getattr(window, name, ())
                            for name in (
                                "_threads",
                                "_workers",
                                "_store_threads",
                                "_store_workers",
                                "_action_threads",
                                "_action_workers",
                                "_action_relays",
                            )
                        )
                        return bool(
                            not window.isVisible()
                            and not window._background_work_running()
                            and not page.is_busy()
                            and not getattr(page, "_thread_workers", {})
                            and collections_empty
                        )

                    close_verified = _pump_until(app, closed_cleanly, _CLOSE_TIMEOUT)
                else:
                    # Error cleanup is bounded and cannot turn a failed
                    # workflow into a passing receipt.
                    window._allow_close = True
                    window.close()
                    app.processEvents(QEventLoop.AllEvents, 100)
                    if window.isVisible():
                        window.hide()
            finally:
                if workflow_ok and not close_verified:
                    window._allow_close = True
                    window.close()
                    app.processEvents(QEventLoop.AllEvents, 100)
                    if window.isVisible():
                        window.hide()
                window.deleteLater()
                app.processEvents(QEventLoop.AllEvents, 100)
    metrics["cleanup_clean"] = bool(close_verified)
    metrics["screenshot_files"] = screenshot_names
    metrics["ok"] = bool(workflow_ok and close_verified)


def run_probe(report_path: str | os.PathLike[str] | None = None) -> int:
    report = Path(report_path or "workflow-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    metrics = _empty_metrics()
    started = time.monotonic()
    previous_root = os.environ.get("TDL_DESKTOP_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-fixture-", dir=report.parent) as directory:
            state = Path(directory).resolve()
            os.environ["TDL_DESKTOP_DATA_DIR"] = str(state)
            try:
                _run_native_ui(state, report, metrics)
            except Exception as exc:
                if not metrics.get("_failure_code"):
                    metrics["_failure_code"] = type(exc).__name__.lower()
                metrics["ok"] = False
            finally:
                if previous_root is None:
                    os.environ.pop("TDL_DESKTOP_DATA_DIR", None)
                else:
                    os.environ["TDL_DESKTOP_DATA_DIR"] = previous_root
    except Exception as exc:
        metrics["ok"] = False
        metrics["_failure_code"] = type(exc).__name__.lower()
    metrics["elapsed"] = round(time.monotonic() - started, 3)
    failure_code = metrics.pop("_failure_code", "")
    report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    if not metrics["ok"] and sys.stderr is not None:
        suffix = f": {failure_code}" if failure_code else ""
        print(f"workflow verification failed{suffix}", file=sys.stderr)
    return 0 if metrics["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="離線驗證頻道下載視窗流程")
    parser.add_argument("--report", "--smoke-report", dest="report", default="workflow-verification.json")
    raise SystemExit(run_probe(parser.parse_args().report))
