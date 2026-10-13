"""Live native Qt verification for the channel list page.

The probe is deliberately opt in.  It copies one remembered encrypted account
envelope into a temporary desktop store, creates the real ``MainWindow`` and
drives ``ChannelPage.refresh_button`` through the Qt event loop.  The source
account files remain outside the temporary store and the report contains only
aggregate health information.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from desktop.backend import _atomic_write_json, _restrict_permissions


_STARTUP_TIMEOUT = 45.0
_REFRESH_TIMEOUT = 180.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_metrics() -> dict[str, Any]:
    return {
        "ok": False,
        "count": 0,
        "nonforum_selection_checked": False,
        "topic_count": 0,
        "topic_selection_checked": False,
        "refreshes": 0,
        "heartbeat": 0,
        "thread_violations": 0,
        "original_metadata_hashes_unchanged": False,
        "elapsed": 0.0,
        "core_sha256": "",
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("desktop metadata is not an object")
    return dict(value)


def _select_account(source: Path) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any]]:
    accounts_path = source / "accounts.json"
    preferences_path = source / "preferences.json"
    accounts_doc = _load_json(accounts_path)
    preferences_doc = _load_json(preferences_path)
    raw_accounts = accounts_doc.get("accounts")
    if not isinstance(raw_accounts, list):
        raise ValueError("desktop accounts metadata is invalid")
    eligible = [
        dict(item)
        for item in raw_accounts
        if isinstance(item, Mapping)
        and item.get("authenticated")
        and str(item.get("id") or "").strip()
    ]
    if not eligible:
        raise ValueError("no authenticated desktop account")
    values = preferences_doc.get("values")
    if not isinstance(values, Mapping):
        values = {}
    active_id = str(values.get("active_account_id") or "")
    account = next((item for item in eligible if str(item["id"]) == active_id), eligible[0])
    account_id = str(account["id"])
    envelope = source / "accounts" / account_id / "session.dpapi"
    if not envelope.is_file():
        raise ValueError("remembered account envelope is missing")
    return account, envelope, accounts_path, preferences_path, preferences_doc


def _pump_until(app: Any, predicate: Callable[[], bool], timeout: float) -> bool:
    """Keep the native Qt event loop moving while a worker is running."""

    from PySide6.QtCore import QEventLoop

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents(QEventLoop.AllEvents, 50)
    return bool(predicate())


def _mapping_value(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def _chat_key(value: Any) -> str:
    """Return an in-memory identity for a rendered chat item.

    The probe uses this only to find the same item after applying a search
    filter.  It is deliberately never copied into the aggregate report.
    """

    raw = _mapping_value(value, "ref", "chat_ref", "chatRef", "id", "chat_id", "chatId")
    return str(raw or "").strip()


def _chat_title(value: Any) -> str:
    raw = _mapping_value(value, "title", "name", "display_name", "displayName")
    return str(raw or "").strip()


def _chat_is_forum(value: Any) -> bool:
    """Match the bridge's forum markers without requiring private UI helpers."""

    explicit = _mapping_value(
        value,
        "forum",
        "is_forum",
        "isForum",
        "has_topics",
        "hasTopics",
        "forum_enabled",
        "topic_enabled",
    )
    if explicit is not None:
        return bool(explicit)
    raw_type = str(_mapping_value(value, "type", "kind", "chat_type") or "").casefold()
    return raw_type in {"forum", "forum_group", "supergroup_forum"}


def _item_chat(item: Any, qt: Any) -> Any:
    if item is None:
        return None
    try:
        return item.data(qt.UserRole)
    except Exception:
        return None


def _topics_ready_for_probe(page: Any) -> bool:
    """Wait for the complete topic operation, not only its result payload."""

    topic_list = getattr(page, "topic_list", None)
    if topic_list is None:
        return False
    is_enabled = getattr(topic_list, "isEnabled", None)
    return bool(
        getattr(page, "_topics_loaded", False)
        and not getattr(page, "_topics_loading", False)
        and not page.is_busy()
        and getattr(page, "_thread", None) is None
        and callable(is_enabled)
        and is_enabled()
    )


def _probe_channel_page_type(
    base_type: type[Any],
    app: Any,
    metrics: dict[str, Any],
    callbacks: dict[str, int],
) -> type[Any]:
    """Build a probe subclass whose slot names stay in Qt's metaobject.

    A decorated function assigned to an existing class attribute keeps its
    Python name (for example ``checked_event``), so PySide can fall back to a
    direct callback even when the connection requests ``QueuedConnection``.
    Defining the methods with their production names on a real QObject
    subclass preserves the queued-slot lookup and lets the probe observe
    affinity without changing the shipped ``ChannelPage`` class.
    """

    from PySide6.QtCore import QThread, Slot

    class ProbeChannelPage(base_type):
        @Slot(int, object, name="_operation_event")
        def _probe_operation_event(self: Any, token: int, event: Any) -> None:
            callbacks["events"] += 1
            if QThread.currentThread() != app.thread():
                metrics["thread_violations"] += 1
                return
            super()._operation_event(token, event)

        # Keep the attribute used by ChannelPage._launch_operation while
        # giving PySide the exact production slot names in its metaobject.
        _operation_event = _probe_operation_event

        @Slot(int, str, object, name="_operation_succeeded")
        def _probe_operation_succeeded(self: Any, token: int, kind: str, data: Any) -> None:
            callbacks["succeeded"] += 1
            if QThread.currentThread() != app.thread():
                metrics["thread_violations"] += 1
                return
            super()._operation_succeeded(token, kind, data)

        _operation_succeeded = _probe_operation_succeeded

    return ProbeChannelPage


def _run_native_ui(state: Path, account_id: str, metrics: dict[str, Any]) -> None:
    """Run the actual MainWindow/ChannelPage workflow in an isolated store."""

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    # The app entry point calls this probe before creating QApplication.  Keep
    # the fallback for direct tests that already own one.
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("TDLDesktop")
    app.setQuitOnLastWindowClosed(False)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass

    from desktop import app as app_module
    from desktop.app import BackendBridge, MainWindow, STYLE

    bridge = BackendBridge()
    if not bridge.available:
        metrics["_failure_code"] = "backend_unavailable"
        raise RuntimeError("desktop backend unavailable")
    try:
        metrics["core_sha256"] = _sha256(Path(bridge.runner.resolve_executable()))
    except Exception:
        metrics["core_sha256"] = ""

    app.setStyleSheet(STYLE)
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    callbacks = {"events": 0, "succeeded": 0}
    base_page_type = app_module.ChannelPage
    probe_page_type = _probe_channel_page_type(base_page_type, app, metrics, callbacks)
    app_module.ChannelPage = probe_page_type
    try:
        window = MainWindow(bridge)
    finally:
        app_module.ChannelPage = base_page_type
    page = window.channel_page
    heartbeat_timer = QTimer(window)
    workflow_ok = False
    close_verified = False

    def heartbeat() -> None:
        metrics["heartbeat"] += 1

    heartbeat_timer.setInterval(250)
    heartbeat_timer.timeout.connect(heartbeat)
    heartbeat_timer.start()
    window.show()
    # Keep native creation, layout and paint active while avoiding a real
    # account list being displayed to the user during the release check.
    window.move(-window.width() - 50, -window.height() - 50)
    window.repaint()
    app.processEvents(QEventLoop.AllEvents, 100)

    try:
        def startup_ready() -> bool:
            store_threads = getattr(window, "_store_threads", ())
            return (
                page.active_account_id() == account_id
                and page._current_account_is_authenticated()
                and page.refresh_button.isEnabled()
                and all(not thread.isRunning() for thread in store_threads)
            )

        if not _pump_until(app, startup_ready, _STARTUP_TIMEOUT):
            metrics["_failure_code"] = "startup_timeout"
            raise RuntimeError("account page did not become ready")

        def refresh_once() -> int:
            if not page.refresh_button.isEnabled():
                metrics["_failure_code"] = "refresh_button_disabled"
                raise RuntimeError("refresh button is disabled")
            page.refresh_button.click()
            metrics["refreshes"] += 1
            saw_thread = False
            deadline = time.monotonic() + _REFRESH_TIMEOUT
            while time.monotonic() < deadline:
                app.processEvents(QEventLoop.AllEvents, 50)
                thread = getattr(page, "_thread", None)
                if thread is not None or page.is_busy():
                    saw_thread = True
                if (
                    saw_thread
                    and not page.is_busy()
                    and getattr(page, "_thread", None) is None
                    and page.chat_list.count() > 0
                ):
                    return page.chat_list.count()
                time.sleep(0.01)
            metrics["_failure_code"] = f"refresh_{metrics['refreshes']}_timeout"
            raise RuntimeError("channel list refresh timed out")

        try:
            first_count = refresh_once()
        except Exception:
            metrics.setdefault("_failure_code", "refresh_1_failed")
            raise
        try:
            second_count = refresh_once()
        except Exception:
            metrics.setdefault("_failure_code", "refresh_2_failed")
            raise
        metrics["count"] = max(first_count, second_count)

        # First exercise search and selection with a regular channel/group.
        # Selecting row zero is not sufficient: Telegram may return a forum
        # first, and a forum must go through the topic workflow below.
        from PySide6.QtCore import Qt

        regular_item = None
        forum_item = None
        for index in range(page.chat_list.count()):
            candidate = page.chat_list.item(index)
            chat = _item_chat(candidate, Qt)
            if not _chat_is_forum(chat) and regular_item is None:
                regular_item = candidate
            if _chat_is_forum(chat) and forum_item is None:
                forum_item = candidate
            if regular_item is not None and forum_item is not None:
                break

        if regular_item is None:
            metrics["_failure_code"] = "no_regular_chat"
            raise RuntimeError("channel list has no regular channel or group")

        regular_chat = _item_chat(regular_item, Qt)
        # QListWidget.clear() below destroys the C++ items.  Keep the forum's
        # value, rather than the item pointer, before the search rebuilds the
        # list; dereferencing a stale QListWidgetItem can terminate a frozen
        # probe process on Windows.
        forum_chat = _item_chat(forum_item, Qt) if forum_item is not None else None
        query = str(regular_item.text() or "").strip().split("  @", 1)[0].strip()
        if not query:
            query = str(regular_item.text() or "").strip()
        page.search_edit.setText(query)

        def regular_search_result() -> bool:
            for index in range(page.chat_list.count()):
                candidate = page.chat_list.item(index)
                if _chat_key(_item_chat(candidate, Qt)) == _chat_key(regular_chat):
                    return True
            return False

        if not _pump_until(app, regular_search_result, 5.0):
            metrics["_failure_code"] = "regular_search_empty"
            raise RuntimeError("regular channel could not be found by search")

        for index in range(page.chat_list.count()):
            candidate = page.chat_list.item(index)
            if _chat_key(_item_chat(candidate, Qt)) == _chat_key(regular_chat):
                page.chat_list.setCurrentItem(candidate)
                break
        if not _pump_until(
            app,
            lambda: page._selected_chat is not None and page.scan_button.isEnabled(),
            5.0,
        ):
            metrics["_failure_code"] = "regular_selection_not_applied"
            raise RuntimeError("regular channel could not be selected")
        metrics["nonforum_selection_checked"] = True

        # If the account exposes a forum, exercise the real topic operation.
        # The probe never starts an export; it only verifies that a concrete
        # topic enables scanning and clearing it disables scanning again.
        has_forum = forum_item is not None
        if has_forum:
            page.search_edit.clear()

            def forum_visible() -> bool:
                for index in range(page.chat_list.count()):
                    candidate = page.chat_list.item(index)
                    if _chat_key(_item_chat(candidate, Qt)) == _chat_key(forum_chat):
                        return True
                return False

            if not _pump_until(app, forum_visible, 5.0):
                metrics["_failure_code"] = "forum_not_visible"
                raise RuntimeError("forum group could not be rendered")
            for index in range(page.chat_list.count()):
                candidate = page.chat_list.item(index)
                if _chat_key(_item_chat(candidate, Qt)) == _chat_key(forum_chat):
                    page.chat_list.setCurrentItem(candidate)
                    break

            def topics_loaded() -> bool:
                # The success signal can be delivered before the worker's
                # finished signal.  Wait until the operation has released the
                # controls so a later _operation_succeeded callback cannot
                # clear a topic selected by this probe.
                return _topics_ready_for_probe(page)

            if not _pump_until(app, topics_loaded, _REFRESH_TIMEOUT):
                metrics["_failure_code"] = "forum_topics_timeout"
                raise RuntimeError("forum topics did not load")

            topics = list(getattr(page, "_topics", ()) or ())
            metrics["topic_count"] = len(topics)
            if not topics:
                metrics["_failure_code"] = "forum_topics_empty"
                raise RuntimeError("forum has no selectable topic")

            topic_item = None
            for index in range(page.topic_list.count()):
                candidate = page.topic_list.item(index)
                value = _item_chat(candidate, Qt)
                if isinstance(value, Mapping) and not value.get("all"):
                    topic_item = candidate
                    break
            if topic_item is None:
                metrics["_failure_code"] = "forum_topic_not_rendered"
                raise RuntimeError("forum topic was not rendered")
            page.topic_list.setCurrentItem(topic_item)
            if not _pump_until(
                app,
                lambda: page._selected_topic is not None and page.scan_button.isEnabled(),
                5.0,
            ):
                metrics["_failure_code"] = "forum_topic_selection_failed"
                raise RuntimeError("forum topic selection did not enable scanning")

            # Clear both current and selected item explicitly.  Qt can retain
            # a current item after clearSelection(), so use both operations and
            # observe the production state rather than only visual selection.
            page.topic_list.clearSelection()
            page.topic_list.setCurrentItem(None)
            page.topic_list.setCurrentRow(-1)
            if not _pump_until(
                app,
                lambda: page._selected_topic is None and not page.scan_button.isEnabled(),
                5.0,
            ):
                metrics["_failure_code"] = "forum_topic_clear_failed"
                raise RuntimeError("clearing forum topic did not disable scanning")
            metrics["topic_selection_checked"] = True

        expected_successes = 2 + (1 if has_forum else 0)
        workflow_ok = (
            metrics["refreshes"] == 2
            and metrics["count"] > 0
            and metrics["nonforum_selection_checked"]
            and (not has_forum or metrics["topic_selection_checked"])
            and metrics["thread_violations"] == 0
            and callbacks["events"] > 0
            and callbacks["succeeded"] >= expected_successes
        )
        if not workflow_ok:
            metrics["_failure_code"] = "callback_or_affinity_check"
        if not workflow_ok:
            metrics["_failure_code"] = "callback_or_affinity_check"
    finally:
        heartbeat_timer.stop()
        try:
            window.shutdown_workers(timeout_ms=10000)
            if workflow_ok:
                # Let the production closeEvent prove that all worker
                # collections are drained.  A forced close is reserved for a
                # failed workflow so an apparently successful probe cannot
                # hide a QThread shutdown regression.
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
                    return (
                        not window.isVisible()
                        and not window._background_work_running()
                        and not page.is_busy()
                        and not getattr(page, "_thread_workers", {})
                        and collections_empty
                    )

                close_verified = _pump_until(app, closed_cleanly, 15.0)
                if not close_verified:
                    metrics["_failure_code"] = "close_not_clean"
            else:
                # Bounded error cleanup may use the application's defensive
                # close path, but never turns a failed workflow into success.
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
    metrics["ok"] = bool(workflow_ok and close_verified)


def _verify(report: Path) -> dict[str, Any]:
    metrics = _empty_metrics()
    local_app_data = os.environ.get("LOCALAPPDATA")
    source = Path(local_app_data) / "TDLDesktop" if local_app_data else Path.home() / "AppData" / "Local" / "TDLDesktop"
    originals: dict[Path, str] = {}
    source_paths: tuple[Path, ...] = ()
    try:
        account, envelope, accounts_path, preferences_path, preferences_doc = _select_account(source)
        account_id = str(account["id"])
        source_paths = (envelope, accounts_path, preferences_path)
        originals = {path: _sha256(path) for path in source_paths}

        with tempfile.TemporaryDirectory(prefix="channel-ui-acceptance-", dir=report.parent) as temporary:
            state = Path(temporary).resolve()
            _restrict_permissions(state, strict=True)
            isolated_account = state / "accounts" / account_id
            isolated_account.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(isolated_account, strict=True)
            shutil.copyfile(envelope, isolated_account / "session.dpapi")
            _restrict_permissions(isolated_account / "session.dpapi", strict=True)

            accounts_version = _load_json(accounts_path).get("version", 1)
            _atomic_write_json(
                state / "accounts.json",
                {"version": accounts_version, "accounts": [account]},
            )
            isolated_preferences = dict(preferences_doc)
            values = isolated_preferences.get("values")
            values = dict(values) if isinstance(values, Mapping) else {}
            values.update(
                active_account_id=account_id,
                download_dir=str(state / "downloads"),
                concurrency=1,
                group=False,
            )
            isolated_preferences["values"] = values
            _atomic_write_json(state / "preferences.json", isolated_preferences)
            _restrict_permissions(state / "accounts.json", strict=True)
            _restrict_permissions(state / "preferences.json", strict=True)

            old_data_root = os.environ.get("TDL_DESKTOP_DATA_DIR")
            os.environ["TDL_DESKTOP_DATA_DIR"] = str(state)
            try:
                _run_native_ui(state, account_id, metrics)
            finally:
                if old_data_root is None:
                    os.environ.pop("TDL_DESKTOP_DATA_DIR", None)
                else:
                    os.environ["TDL_DESKTOP_DATA_DIR"] = old_data_root
    except Exception:
        # Keep the report aggregate-only.  The frozen wrapper retains stdout
        # and stderr logs for a maintainer who needs to diagnose a failed run.
        metrics.setdefault("_failure_code", "probe_failed")
        metrics["ok"] = False
    finally:
        if originals:
            try:
                metrics["original_metadata_hashes_unchanged"] = all(
                    path.is_file() and _sha256(path) == digest
                    for path, digest in originals.items()
                )
            except Exception:
                metrics["original_metadata_hashes_unchanged"] = False
        if not metrics["original_metadata_hashes_unchanged"]:
            metrics["ok"] = False
    return metrics


def run_probe(report_path: str | os.PathLike[str] | None) -> int:
    report = Path(report_path or "channel-ui-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    payload = _verify(report)
    payload["elapsed"] = round(time.monotonic() - started, 3)
    failure_code = payload.pop("_failure_code", "")
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Do not print account or channel diagnostics; the release wrapper keeps
    # bounded process logs if a maintainer needs to inspect a failed run.
    if not payload["ok"]:
        suffix = f": {failure_code}" if failure_code else ""
        print(f"channel UI verification failed{suffix}", file=sys.stderr)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="隔離驗證原生頻道清單視窗流程")
    parser.add_argument("--report", "--smoke-report", dest="report", default="channel-ui-verification.json")
    raise SystemExit(run_probe(parser.parse_args().report))
