"""頻道與群組下載頁。

This module intentionally owns only the Qt page and its runner bridge.  It does
not know about the main window's queue implementation: after an export has
been scanned it emits a file-only download request, which the main window can
route through the same durable queue used by link downloads.

The runner methods used here are the small desktop contract supplied by the
Python backend::

    runner.start_list_chats(account_id, on_event=None)
    runner.start_list_topics(account_id, chat_ref, on_event=None)  # optional
    runner.start_export(account_id, chat_id, start_date, end_date,
                        media_types, topic_id=None, on_event=None)

Both methods return a RunHandle.  All polling happens in a QThread so the page
never blocks the GUI event loop while Telegram is queried or an export is
written.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QDate, QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


_MEDIA_LABELS = {
    "video": "影片",
    "photo": "照片",
    "audio": "音訊",
    "document": "文件",
}


def _value(item: Any, *names: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
    for name in names:
        try:
            result = getattr(item, name)
        except Exception:
            continue
        if result is not None:
            return result
    return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _safe_message(value: Any) -> str:
    """Keep operation errors/status useful without exposing credentials."""

    import re

    text = _text(value).strip()
    text = re.sub(r"(?i)(socks(?:4|5)?|https?)://([^\s:@/]+):([^\s@/]+)@", r"\1://***:***@", text)
    text = re.sub(r"(?i)(password|passcode|token|secret|authorization)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    return text[:400]


def _account_id(account: Any) -> str:
    return _text(_value(account, "id", "account_id", "key", default=""))


def _account_label(account: Any) -> str:
    return _text(_value(account, "label", "name", "title", default=_account_id(account))) or _account_id(account)


def _account_authenticated(account: Any) -> bool:
    return bool(_value(account, "authenticated", "is_authenticated", "logged_in", default=False))


def _chat_id(chat: Any) -> str:
    return _text(_value(chat, "id", "chat_id", "peer_id", default=""))


def _chat_ref(chat: Any) -> str:
    """Prefer Telegram's typed peer reference over a bare numeric id."""

    return _text(_value(chat, "ref", "chat_ref", "reference", default="")) or _chat_id(chat)


def _chat_title(chat: Any) -> str:
    title = _text(_value(chat, "title", "name", "visible_name", default=""))
    return title or "未命名對話"


def _chat_username(chat: Any) -> str:
    username = _text(_value(chat, "username", "user_name", default="")).strip()
    return username.lstrip("@")


def _chat_kind(chat: Any) -> str:
    """Return a Traditional Chinese filter key for backend chat variants."""

    raw = _text(_value(chat, "type", "kind", "chat_type", default="")).lower()
    if bool(_value(chat, "is_channel", default=False)) or "channel" in raw:
        return "channel"
    if bool(_value(chat, "is_group", default=False)) or raw in {"chat", "group", "supergroup"} or any(token in raw for token in ("group", "supergroup")):
        return "group"
    # Telegram's chat list can contain a user dialog too.  It remains visible
    # under 全部 but never appears when the user asks specifically for channels
    # or groups.
    return "other"


def _chat_topics(chat: Any) -> list[dict[str, str]]:
    """Return the safe, displayable forum topics attached to one chat."""

    raw = _value(chat, "topics", "forum_topics", default=[])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        topic_id = _text(_value(item, "id", "topic_id", "thread_id", default="")).strip()
        if not topic_id or topic_id in seen:
            continue
        title = _text(_value(item, "title", "name", "topic_title", default="")).strip()
        result.append({"id": topic_id, "title": _topic_display_title(topic_id, title), "index": str(index)})
        seen.add(topic_id)
    return result


def _topic_display_title(topic_id: str, title: str) -> str:
    """Use a familiar Traditional Chinese label for Telegram's General topic."""

    clean_title = title.strip()
    if topic_id == "1" and clean_title.casefold() == "general":
        return "一般（General）"
    return clean_title or f"主題 {topic_id}"


def _chat_is_forum(chat: Any) -> bool:
    """Detect a forum without treating every ordinary group as one."""

    explicit = _value(
        chat,
        "is_forum",
        "isForum",
        "forum",
        "has_topics",
        "hasTopics",
        "forum_enabled",
        "topic_enabled",
        default=None,
    )
    if explicit is not None:
        return bool(explicit)
    raw_type = _text(_value(chat, "type", "kind", "chat_type", default="")).lower()
    if raw_type in {"forum", "forum_group", "supergroup_forum"}:
        return True
    # A non-empty embedded topic list is authoritative even when an older
    # bridge did not include its explicit forum flag.
    return bool(_chat_topics(chat))


def _kind_label(kind: str) -> str:
    return {"channel": "頻道", "group": "群組", "other": "其他"}.get(kind, "其他")


def _date_text(value: QDate) -> str:
    return value.toString("yyyy-MM-dd")


def _taipei_qdate() -> QDate:
    """Return today's date in the app's explicit UTC+8 user timezone."""

    # Use a fixed offset instead of ZoneInfo: the frozen Windows runtime does
    # not ship a tzdata database, and Taiwan has no daylight-saving transition.
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    return QDate(today.year, today.month, today.day)


def _thread_running(thread: Any) -> bool:
    try:
        return bool(thread is not None and thread.isRunning())
    except (RuntimeError, AttributeError):
        return False


class _Card(QFrame):
    """Use the main app's global stylesheet without importing app.py."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.setObjectName("Card")


class _RunnerWorker(QObject):
    """Poll one backend RunHandle from a dedicated Qt worker thread."""

    # Include the operation identity in every signal.  ChannelPage receives
    # these through real QObject slots below with an explicit queued
    # connection, so no worker-thread callback can touch a widget directly.
    event = Signal(int, object)
    succeeded = Signal(int, str, object)
    failed = Signal(int, str)
    canceled = Signal(int)
    finished = Signal()

    def __init__(
        self,
        runner: Any,
        method: str,
        args: tuple[Any, ...],
        kwargs: Optional[Mapping[str, Any]] = None,
        *,
        token: int = 0,
        kind: str = "",
    ) -> None:
        super().__init__()
        self.runner = runner
        self.method = method
        self.args = args
        self.kwargs = dict(kwargs or {})
        self.token = int(token)
        self.kind = str(kind)
        self.cancel_event = threading.Event()
        self.handle: Any = None

    @Slot()
    def run(self) -> None:
        try:
            method = getattr(self.runner, self.method, None)
            if not callable(method):
                raise RuntimeError("目前後端尚未提供頻道下載功能。")
            # The explicit callback is intentionally omitted.  Reading the
            # handle's queue here gives one deterministic stream and prevents
            # a callback running in the backend's Python thread from touching
            # Qt objects.
            self.handle = method(*self.args, **self.kwargs, on_event=None)
            if self.handle is None:
                raise RuntimeError("後端沒有回傳有效的工作控制項。")
            while not bool(getattr(self.handle, "done", threading.Event()).is_set()):
                if self.cancel_event.is_set():
                    self._cancel_handle()
                getter = getattr(self.handle, "get_event", None)
                raw = getter(timeout=0.15) if callable(getter) else None
                if raw is not None:
                    self.event.emit(self.token, raw)
            # Drain events queued immediately before the done flag.  This is
            # useful for the final exported/count event on fast fixtures.
            getter = getattr(self.handle, "get_event", None)
            if callable(getter):
                while True:
                    raw = getter(timeout=0)
                    if raw is None:
                        break
                    self.event.emit(self.token, raw)
            result = self.handle.wait(0) if callable(getattr(self.handle, "wait", None)) else getattr(self.handle, "result", None)
            if self.cancel_event.is_set() or bool(getattr(result, "cancelled", False)):
                self.canceled.emit(self.token)
                return
            if result is None:
                raise RuntimeError("後端工作沒有回傳結果。")
            if not bool(getattr(result, "success", False)):
                raise RuntimeError(_text(getattr(result, "error", "頻道操作未完成。"), "頻道操作未完成。"))
            data = getattr(result, "data", None)
            if data is None and isinstance(result, Mapping):
                data = result.get("data", result)
            self.succeeded.emit(self.token, self.kind, data)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.canceled.emit(self.token)
            else:
                self.failed.emit(self.token, _text(exc, "頻道操作未完成。"))
        finally:
            self.finished.emit()

    def _cancel_handle(self) -> None:
        handle = self.handle
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                # The worker still waits for done; backend watchdogs own the
                # final process termination.  Never turn a cancellation into
                # a second visible error.
                pass

    def cancel(self) -> None:
        self.cancel_event.set()
        self._cancel_handle()


class ChannelPage(QWidget):
    """Friendly channel/group selector and date/media export workflow."""

    account_changed = Signal(str)
    download_requested = Signal(list, dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._runner: Any = None
        self._accounts: list[Any] = []
        self._active_account_id = ""
        self._chats: list[Any] = []
        self._selected_chat: Any = None
        self._selected_topic: Optional[dict[str, str]] = None
        self._topic_chat_ref = ""
        self._topics: list[dict[str, str]] = []
        self._topics_loaded = False
        self._topics_loading = False
        self._generation = 0
        self._operation_kind = ""
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunnerWorker] = None
        # Keep stale workers reachable until their QThread has emitted
        # finished.  A refresh may queue a replacement while the previous
        # RunHandle is still unwinding.
        self._thread_workers: dict[QThread, _RunnerWorker] = {}
        self._pending_operation: Optional[tuple[int, str, str, tuple[Any, ...], dict[str, Any]]] = None
        self._export_path = ""
        self._export_count = 0
        self._download_folder = ""
        self._proxy = ""
        self._concurrency = 2
        self._skip_existing = True
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 25, 30, 35)
        outer.setSpacing(12)

        kicker = QLabel("頻道與群組")
        kicker.setObjectName("Kicker")
        outer.addWidget(kicker)
        title = QLabel("選頻道，先看數量再下載。")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        intro = QLabel("選擇帳號、頻道與日期，掃描後再加入下載佇列。")
        intro.setObjectName("HeroDescription")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        account_card = _Card()
        account_layout = QHBoxLayout(account_card)
        account_layout.setContentsMargins(18, 15, 18, 15)
        account_layout.setSpacing(12)
        account_label = QLabel("下載帳號")
        account_label.setObjectName("FieldLabel")
        account_layout.addWidget(account_label)
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(280)
        self.account_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        account_layout.addWidget(self.account_combo, 1)
        self.refresh_button = QPushButton("更新清單")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.clicked.connect(self.refresh_channels)
        account_layout.addWidget(self.refresh_button)
        outer.addWidget(account_card)

        channel_card = _Card()
        channel_layout = QVBoxLayout(channel_card)
        channel_layout.setContentsMargins(18, 16, 18, 16)
        channel_layout.setSpacing(10)
        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜尋頻道、群組名稱或 username")
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_edit, 1)
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部", "all")
        self.type_combo.addItem("頻道", "channel")
        self.type_combo.addItem("群組", "group")
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        self.type_combo.setMinimumWidth(110)
        search_row.addWidget(self.type_combo)
        channel_layout.addLayout(search_row)
        self.chat_list = QListWidget()
        self.chat_list.setObjectName("ChannelList")
        self.chat_list.setMinimumHeight(180)
        self.chat_list.setMaximumHeight(250)
        self.chat_list.setSpacing(4)
        self.chat_list.setStyleSheet(
            "QListWidget::item { padding: 8px 10px; border-radius: 6px; }"
            "QListWidget::item:selected { background: #4f46e5; color: #ffffff; }"
        )
        self.chat_list.setAlternatingRowColors(True)
        self.chat_list.currentItemChanged.connect(self._chat_selected)
        channel_layout.addWidget(self.chat_list)
        self.chat_hint = QLabel("請按「更新清單」載入目前帳號可存取的頻道與群組。")
        self.chat_hint.setObjectName("MutedText")
        self.chat_hint.setWordWrap(True)
        channel_layout.addWidget(self.chat_hint)

        # Forum groups have a second selection step.  The list starts with no
        # current item; 全部主題 is an explicit opt-in row rather than an
        # implicit fallback to the whole group.
        self.topic_panel = QFrame()
        self.topic_panel.setObjectName("TopicPanel")
        topic_layout = QVBoxLayout(self.topic_panel)
        topic_layout.setContentsMargins(0, 8, 0, 0)
        topic_layout.setSpacing(7)
        topic_header = QHBoxLayout()
        topic_title = QLabel("論壇主題／話題")
        topic_title.setObjectName("FieldLabel")
        topic_header.addWidget(topic_title)
        self.topic_state = QLabel("選擇論壇群組後載入")
        self.topic_state.setObjectName("MutedText")
        topic_header.addWidget(self.topic_state, 1, Qt.AlignRight)
        topic_layout.addLayout(topic_header)
        self.topic_search = QLineEdit()
        self.topic_search.setPlaceholderText("搜尋主題名稱或 ID")
        self.topic_search.textChanged.connect(self._apply_topic_filter)
        topic_layout.addWidget(self.topic_search)
        self.topic_list = QListWidget()
        self.topic_list.setObjectName("TopicList")
        self.topic_list.setMinimumHeight(86)
        self.topic_list.setMaximumHeight(150)
        self.topic_list.setSpacing(3)
        self.topic_list.setStyleSheet(
            "QListWidget::item { padding: 7px 9px; border-radius: 6px; }"
            "QListWidget::item:selected { background: #4f46e5; color: #ffffff; }"
        )
        self.topic_list.currentItemChanged.connect(self._topic_selected)
        topic_layout.addWidget(self.topic_list)
        self.topic_hint = QLabel("論壇群組需要先選擇主題，或明確選擇「全部主題」。")
        self.topic_hint.setObjectName("MutedText")
        self.topic_hint.setWordWrap(True)
        topic_layout.addWidget(self.topic_hint)
        self.topic_panel.setVisible(False)
        channel_layout.addWidget(self.topic_panel)

        filters = _Card()
        filters_layout = QVBoxLayout(filters)
        filters_layout.setContentsMargins(18, 16, 18, 16)
        filters_layout.setSpacing(13)
        filter_title = QLabel("下載條件")
        filter_title.setObjectName("SectionTitle")
        filters_layout.addWidget(filter_title)

        dates = QGridLayout()
        dates.setHorizontalSpacing(10)
        dates.setVerticalSpacing(8)
        start_label = QLabel("起始日期（台灣時間）")
        start_label.setObjectName("FieldLabel")
        end_label = QLabel("結束日期（台灣時間）")
        end_label.setObjectName("FieldLabel")
        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for editor in (self.start_date, self.end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy/MM/dd")
            editor.setMinimumWidth(150)
            editor.dateChanged.connect(self._clear_scan)
        today = _taipei_qdate()
        self.end_date.setDate(today)
        self.start_date.setDate(today.addDays(-6))
        dates.addWidget(start_label, 0, 0)
        dates.addWidget(end_label, 0, 1)
        dates.addWidget(self.start_date, 1, 0)
        dates.addWidget(self.end_date, 1, 1)
        shortcut_row = QHBoxLayout()
        shortcut_row.setSpacing(7)
        self.today_button = QPushButton("今天")
        self.seven_days_button = QPushButton("近 7 日")
        self.thirty_days_button = QPushButton("近 30 日")
        for button in (self.today_button, self.seven_days_button, self.thirty_days_button):
            button.setObjectName("LinkButton")
            shortcut_row.addWidget(button)
        shortcut_row.addStretch(1)
        self.today_button.clicked.connect(lambda: self._set_date_range(0))
        self.seven_days_button.clicked.connect(lambda: self._set_date_range(6))
        self.thirty_days_button.clicked.connect(lambda: self._set_date_range(29))
        dates.addLayout(shortcut_row, 2, 0, 1, 2)
        filters_layout.addLayout(dates)

        media_label = QLabel("媒體類型")
        media_label.setObjectName("FieldLabel")
        filters_layout.addWidget(media_label)
        media_row = QHBoxLayout()
        media_row.setSpacing(18)
        self.video_check = QCheckBox("影片")
        self.photo_check = QCheckBox("照片")
        self.audio_check = QCheckBox("音訊")
        self.document_check = QCheckBox("文件")
        self.video_check.setChecked(True)
        for checkbox in (self.video_check, self.photo_check, self.audio_check, self.document_check):
            checkbox.stateChanged.connect(self._clear_scan)
            media_row.addWidget(checkbox)
        media_row.addStretch(1)
        filters_layout.addLayout(media_row)

        folder_label = QLabel("下載資料夾")
        folder_label.setObjectName("FieldLabel")
        filters_layout.addWidget(folder_label)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setObjectName("FolderEdit")
        self.folder_edit.setPlaceholderText("請選擇檔案要保存的位置")
        self.folder_edit.textChanged.connect(self._folder_changed)
        folder_row.addWidget(self.folder_edit, 1)
        self.folder_button = QPushButton("選擇資料夾")
        self.folder_button.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_button)
        filters_layout.addLayout(folder_row)

        action_card = _Card()
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(18, 16, 18, 16)
        action_layout.setSpacing(10)
        self.status_label = QLabel("請選擇帳號並按「更新清單」。")
        self.status_label.setObjectName("MutedText")
        self.status_label.setWordWrap(True)
        action_layout.addWidget(self.status_label)
        button_row = QHBoxLayout()
        self.cancel_button = QPushButton("取消目前操作")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.cancel_operations)
        self.cancel_button.setVisible(False)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch(1)
        self.scan_button = QPushButton("先掃描符合媒體數")
        self.scan_button.setObjectName("PrimaryButton")
        self.scan_button.clicked.connect(self._scan_clicked)
        button_row.addWidget(self.scan_button)
        self.download_button = QPushButton("開始下載")
        self.download_button.setObjectName("PrimaryButton")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._download_clicked)
        button_row.addWidget(self.download_button)
        action_layout.addLayout(button_row)
        workflow = QHBoxLayout()
        workflow.setSpacing(16)
        workflow.addWidget(channel_card, 4)
        right_column = QVBoxLayout()
        right_column.setSpacing(12)
        right_column.addWidget(filters)
        right_column.addWidget(action_card)
        right_column.addStretch(1)
        workflow.addLayout(right_column, 6)
        outer.addLayout(workflow, 1)

        self._set_controls_enabled(True)

    # ------------------------------------------------------------------
    # Public integration surface
    # ------------------------------------------------------------------
    def set_runner(self, runner: Any) -> None:
        self._runner = runner

    def set_accounts(self, accounts: Sequence[Any], active_id: str = "") -> None:
        values = list(accounts or [])
        previous = _text(self.account_combo.currentData() or self._active_account_id)
        old_selected = previous
        saved_export_path = self._export_path
        saved_export_count = self._export_count
        self._accounts = values
        self._active_account_id = _text(active_id or previous)
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        selected_index = -1
        for index, account in enumerate(values):
            account_id = _account_id(account)
            if not account_id:
                continue
            label = _account_label(account)
            suffix = "已連線" if _account_authenticated(account) else "未登入"
            self.account_combo.addItem(f"{label} · {suffix}", account_id)
            if account_id == self._active_account_id:
                selected_index = self.account_combo.count() - 1
        if selected_index < 0 and self.account_combo.count():
            selected_index = 0
            self._active_account_id = _text(self.account_combo.itemData(0))
        if selected_index >= 0:
            self.account_combo.setCurrentIndex(selected_index)
        self.account_combo.blockSignals(False)
        if not self.account_combo.count():
            self._active_account_id = ""
        selected_id = _text(self.account_combo.currentData() or self._active_account_id)
        changed = old_selected != selected_id
        if changed:
            self._generation += 1
            self._pending_operation = None
            worker = self._worker
            if worker is not None:
                worker.cancel()
            self._clear_chat_selection()
        elif self._chats:
            # Rebuild only the visible rows after account metadata refresh;
            # the selected chat and an already-scanned manifest stay valid for
            # the same account.
            self._apply_filter()
            # Rebuilding QListWidget rows can emit currentItemChanged and
            # clear the preview as a side effect.  The account did not change,
            # so the already scanned manifest remains valid and is restored.
            self._export_path = saved_export_path
            self._export_count = saved_export_count
            if hasattr(self, "download_button") and not self.is_busy():
                self.download_button.setEnabled(bool(saved_export_path and saved_export_count > 0))
        if not self.account_combo.count():
            self.account_combo.addItem("尚未登入 Telegram")
            self._set_status("尚未有可用帳號。請先到「帳號管理」完成登入。")
        elif not self._current_account_is_authenticated():
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
        elif changed:
            self._set_status("請按「更新清單」載入目前帳號可存取的頻道與群組。")

    def set_download_folder(self, path: str) -> None:
        self._download_folder = _text(path).strip()
        if not self.folder_edit.hasFocus() or not self.folder_edit.text().strip():
            self.folder_edit.setText(self._download_folder)

    def set_download_preferences(self, *, concurrency: int = 2, proxy: str = "", skip_existing: bool = True) -> None:
        self._concurrency = max(1, min(64, int(concurrency or 2)))
        self._proxy = _text(proxy).strip()
        self._skip_existing = bool(skip_existing)

    def is_busy(self) -> bool:
        return _thread_running(self._thread)

    def shutdown(self, timeout_ms: int = 7000) -> None:
        self.cancel_operations()
        thread = self._thread
        if _thread_running(thread):
            thread.quit()
            thread.wait(max(0, int(timeout_ms)))

    def refresh_channels(self) -> None:
        account_id = self.active_account_id()
        if not account_id:
            self._set_status("請先選擇一個 Telegram 帳號。")
            return
        if not self._current_account_is_authenticated():
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
            return
        if self._runner is None or not callable(getattr(self._runner, "start_list_chats", None)):
            self._set_status("目前後端尚未提供頻道清單功能。")
            return
        self._clear_chat_selection()
        self._set_status("正在讀取頻道與群組…")
        kwargs = {"proxy": self._proxy} if self._proxy else {}
        self._start_operation("list", "start_list_chats", (account_id,), kwargs)

    def cancel_operations(self) -> None:
        self._generation += 1
        self._pending_operation = None
        worker = self._worker
        if worker is not None:
            worker.cancel()
        if self.is_busy():
            self._set_status("正在取消目前操作…")
        else:
            self._finish_idle_state()

    def active_account_id(self) -> str:
        return _text(self.account_combo.currentData() or self._active_account_id)

    # ------------------------------------------------------------------
    # UI interactions
    # ------------------------------------------------------------------
    def _account_changed(self, _index: int) -> None:
        account_id = self.active_account_id()
        self._active_account_id = account_id
        self._generation += 1
        self._pending_operation = None
        self._clear_chat_selection()
        worker = self._worker
        if worker is not None:
            worker.cancel()
        self.account_changed.emit(account_id)
        if account_id and self._current_account_is_authenticated():
            self._set_status("帳號已切換，請按「更新清單」。")
        elif account_id:
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
        else:
            self._set_status("請先選擇一個 Telegram 帳號。")

    def _set_date_range(self, days_before_today: int) -> None:
        today = _taipei_qdate()
        self.end_date.setDate(today)
        self.start_date.setDate(today.addDays(-int(days_before_today)))
        self._clear_scan()

    def _choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "選擇頻道下載資料夾",
            self.folder_edit.text().strip() or str(Path.home() / "Downloads"),
        )
        if selected:
            self.folder_edit.setText(selected)

    def _folder_changed(self, value: str) -> None:
        self._download_folder = value.strip()
        self._clear_scan()

    def _topic_selection_required(self) -> bool:
        return bool(
            self._selected_chat is not None
            and _chat_is_forum(self._selected_chat)
            and (not self._topics_loaded or self._selected_topic is None)
        )

    def _update_scan_button(self, enabled: Optional[bool] = None) -> None:
        if not hasattr(self, "scan_button"):
            return
        if enabled is None:
            enabled = True
        enabled = bool(
            enabled
            and self._selected_chat is not None
            and not self.is_busy()
            and not self._topic_selection_required()
        )
        self.scan_button.setEnabled(enabled)

    def _set_topic_error(self, message: str) -> None:
        self._topics = []
        self._topics_loaded = False
        self._topics_loading = False
        self._selected_topic = None
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        self.topic_list.blockSignals(False)
        self.topic_panel.setVisible(True)
        self.topic_state.setText("讀取失敗")
        self.topic_hint.setText(_safe_message(message) or "無法讀取論壇主題，請更新清單後重試。")
        self._update_scan_button(False)

    def _populate_topics(self, values: Sequence[Any], *, source: str = "後端") -> None:
        topics: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in values:
            topic_id = _text(_value(value, "id", "topic_id", "thread_id", default="")).strip()
            if not topic_id or topic_id in seen:
                continue
            title = _text(_value(value, "title", "name", "topic_title", default="")).strip()
            topics.append({"id": topic_id, "title": _topic_display_title(topic_id, title)})
            seen.add(topic_id)
        self._topics = topics
        self._topics_loaded = True
        self._topics_loading = False
        self._selected_topic = None
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        all_item = QListWidgetItem("全部主題（掃描整個論壇群組）")
        all_item.setData(Qt.UserRole, {"id": "", "title": "全部主題", "all": True})
        all_item.setToolTip("明確選擇後，才會掃描整個論壇群組")
        self.topic_list.addItem(all_item)
        for topic in topics:
            item = QListWidgetItem(topic["title"])
            item.setData(Qt.UserRole, topic)
            item.setToolTip(f"主題 ID：{topic['id']}")
            self.topic_list.addItem(item)
        self.topic_list.setCurrentRow(-1)
        self.topic_list.clearSelection()
        self.topic_list.blockSignals(False)
        self.topic_state.setText(f"可選 {len(topics)} 個主題")
        if topics:
            self.topic_hint.setText("請選擇一個主題；也可以明確選擇「全部主題」。")
        else:
            self.topic_hint.setText("目前沒有可用主題；若要掃描整個群組，請明確選擇「全部主題」。")
        self._update_scan_button(False)

    def _apply_topic_filter(self, *_args: Any) -> None:
        if not self._topics_loaded:
            return
        query = self.topic_search.text().strip().casefold()
        selected_id = _text((self._selected_topic or {}).get("id", ""))
        selected_item: Optional[QListWidgetItem] = None
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        all_item = QListWidgetItem("全部主題（掃描整個論壇群組）")
        all_topic = {"id": "", "title": "全部主題", "all": True}
        all_item.setData(Qt.UserRole, all_topic)
        if not query or "全部主題".casefold() in query:
            self.topic_list.addItem(all_item)
            if not selected_id and self._selected_topic and self._selected_topic.get("all"):
                selected_item = all_item
        for topic in self._topics:
            if query and query not in topic["title"].casefold() and query not in topic["id"].casefold():
                continue
            item = QListWidgetItem(topic["title"])
            item.setData(Qt.UserRole, topic)
            item.setToolTip(f"主題 ID：{topic['id']}")
            self.topic_list.addItem(item)
            if selected_id and topic["id"] == selected_id:
                selected_item = item
        self.topic_list.setCurrentItem(selected_item)
        self.topic_list.blockSignals(False)
        if selected_item is None and self._selected_topic is not None:
            self._selected_topic = None
            self._clear_scan()
            self.topic_hint.setText("目前搜尋結果沒有原本選取的主題，請重新選擇。")
        self._update_scan_button()

    def _prepare_topics_for_chat(self) -> None:
        chat = self._selected_chat
        self._selected_topic = None
        self._topics = []
        self._topics_loaded = False
        self._topics_loading = False
        self._topic_chat_ref = _chat_ref(chat) if chat is not None else ""
        self.topic_search.clear()
        self.topic_list.clear()
        if chat is None or not _chat_is_forum(chat):
            self.topic_panel.setVisible(False)
            self._update_scan_button()
            return
        self.topic_panel.setVisible(True)
        self.topic_state.setText("正在讀取主題…")
        self.topic_hint.setText("請稍候，正在讀取這個論壇群組的主題。")
        method = getattr(self._runner, "start_list_topics", None) if self._runner is not None else None
        if callable(method):
            self._topics_loading = True
            kwargs = {"proxy": self._proxy} if self._proxy else {}
            self._start_operation(
                "topics",
                "start_list_topics",
                (self.active_account_id(), self._topic_chat_ref),
                kwargs,
            )
            return
        embedded = _chat_topics(chat)
        if embedded:
            self._populate_topics(embedded, source="清單")
            return
        self._set_topic_error("目前後端尚未提供論壇主題清單。")

    def _topic_selected(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        value = current.data(Qt.UserRole) if current is not None else None
        self._selected_topic = dict(value) if isinstance(value, Mapping) else None
        self._clear_scan()
        if self._selected_topic is None:
            self.topic_hint.setText("請選擇一個主題；也可以明確選擇「全部主題」。")
        elif self._selected_topic.get("all"):
            self.topic_hint.setText("已明確選擇整個論壇群組，掃描會包含所有主題。")
        else:
            self.topic_hint.setText(f"已選主題：{self._selected_topic.get('title', '未命名主題')}")
        self._update_scan_button()

    def _apply_filter(self, *_args: Any) -> None:
        query = self.search_edit.text().strip().casefold()
        kind = _text(self.type_combo.currentData() or "all")
        current_id = _chat_ref(self._selected_chat) if self._selected_chat is not None else ""
        saved_export_path = self._export_path
        saved_export_count = self._export_count
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        selected_item: Optional[QListWidgetItem] = None
        for chat in self._chats:
            chat_kind = _chat_kind(chat)
            username = _chat_username(chat)
            haystack = f"{_chat_title(chat)} {username} {_chat_ref(chat)} {_chat_id(chat)}".casefold()
            if query and query not in haystack:
                continue
            if kind != "all" and chat_kind != kind:
                continue
            display = _chat_title(chat)
            if username:
                display += f"  @{username}"
            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, chat)
            item.setToolTip(f"{_kind_label(chat_kind)} · {_chat_ref(chat)}")
            self.chat_list.addItem(item)
            if current_id and _chat_ref(chat) == current_id:
                selected_item = item
        if selected_item is not None:
            self.chat_list.setCurrentItem(selected_item)
            # Keep a completed scan when the same chat remains visible.  The
            # item is set while QListWidget signals are blocked, so changing
            # the search text cannot accidentally clear a valid manifest.
            self._selected_chat = selected_item.data(Qt.UserRole)
            self._export_path = saved_export_path
            self._export_count = saved_export_count
            if hasattr(self, "download_button") and not self.is_busy():
                self.download_button.setEnabled(bool(saved_export_path and saved_export_count > 0))
            if hasattr(self, "scan_button") and not self.is_busy():
                self._update_scan_button()
        elif current_id:
            # A hidden selection must never remain the source of a download.
            # Clear both the selected chat and the previously scanned export.
            self._selected_chat = None
            self._prepare_topics_for_chat()
            self._clear_scan()
            if hasattr(self, "scan_button") and not self.is_busy():
                self._update_scan_button(False)
            self.chat_hint.setText(
                "找不到原本選取的頻道，請重新選擇。"
                if self.chat_list.count()
                else "找不到符合搜尋或類型條件的頻道。"
            )
        elif self.chat_list.count() == 0 and self._chats:
            self.chat_hint.setText("找不到符合搜尋或類型條件的頻道。")
        elif self._chats:
            self.chat_hint.setText(f"共找到 {self.chat_list.count()} 個可選對話。")
        self.chat_list.blockSignals(False)

    def _chat_selected(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        # A topic result belongs to the chat that started it.  In normal use
        # the list is disabled while loading, but invalidating here also
        # protects programmatic selection and rapid filter changes.
        self._generation += 1
        self._pending_operation = None
        worker = self._worker
        if worker is not None:
            worker.cancel()
        self._selected_chat = current.data(Qt.UserRole) if current is not None else None
        self._clear_scan()
        self._prepare_topics_for_chat()
        if not self.is_busy():
            self._update_scan_button()
        if self._selected_chat is not None:
            self.chat_hint.setText(f"已選：{_chat_title(self._selected_chat)}（{_kind_label(_chat_kind(self._selected_chat))}）")

    def _current_account(self) -> Any:
        account_id = self.active_account_id()
        return next((item for item in self._accounts if _account_id(item) == account_id), None)

    def _current_account_is_authenticated(self) -> bool:
        account = self._current_account()
        return bool(account is not None and _account_authenticated(account))

    def _media_types(self) -> list[str]:
        return [
            kind
            for kind, checkbox in (
                ("video", self.video_check),
                ("photo", self.photo_check),
                ("audio", self.audio_check),
                ("document", self.document_check),
            )
            if checkbox.isChecked()
        ]

    def _scan_clicked(self) -> None:
        account_id = self.active_account_id()
        if not account_id:
            self._set_status("請先選擇一個 Telegram 帳號。")
            return
        if not self._current_account_is_authenticated():
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
            return
        if self._selected_chat is None:
            self._set_status("請先從清單選擇一個頻道或群組。")
            return
        forum = _chat_is_forum(self._selected_chat)
        if forum and not self._topics_loaded:
            self._set_status("論壇主題尚未載入完成，請稍候或重試更新清單。")
            return
        if forum and self._selected_topic is None:
            self._set_status("請先選擇一個論壇主題，或明確選擇「全部主題」。")
            self._update_scan_button(False)
            return
        if self.start_date.date() > self.end_date.date():
            self._set_status("起始日期不可晚於結束日期。")
            return
        media_types = self._media_types()
        if not media_types:
            self._set_status("請至少選擇一種媒體類型。")
            return
        if not self.folder_edit.text().strip():
            self._set_status("請先選擇下載資料夾。")
            return
        if not callable(getattr(self._runner, "start_export", None)):
            self._set_status("目前後端尚未提供頻道掃描功能。")
            return
        self._clear_scan()
        self._set_status("正在掃描符合條件的媒體…")
        kwargs = {"proxy": self._proxy} if self._proxy else {}
        args = (
            account_id,
            _chat_ref(self._selected_chat),
            _date_text(self.start_date.date()),
            _date_text(self.end_date.date()),
            media_types,
        )
        if forum:
            # A forum export must carry the explicit choice.  None is reserved
            # for the deliberate 全部主題 row; it is never inferred from a
            # missing selection.
            kwargs["topic_id"] = (
                None if self._selected_topic.get("all") else self._selected_topic.get("id")
            )
        self._start_operation("export", "start_export", args, kwargs)

    def _download_clicked(self) -> None:
        if not self._export_path or self._export_count <= 0:
            self._set_status("請先掃描，確認有符合條件的媒體。")
            return
        folder = self.folder_edit.text().strip()
        if not folder:
            self._set_status("請先選擇下載資料夾。")
            return
        chat_title = _chat_title(self._selected_chat)
        topic = self._selected_topic
        topic_suffix = ""
        if _chat_is_forum(self._selected_chat) and topic is not None:
            topic_suffix = f" · {topic.get('title', '全部主題')}"
        title = f"{chat_title}{topic_suffix} · {_date_text(self.start_date.date())}～{_date_text(self.end_date.date())}"
        options = {
            "account_id": self.active_account_id(),
            "output_dir": folder,
            "files": [self._export_path],
            "title": title,
            "group_album": False,
            "skip_existing": self._skip_existing,
            "continue_download": True,
            "concurrency": self._concurrency,
            "proxy": self._proxy,
        }
        self.download_requested.emit([], options)
        self._set_status(f"已加入下載佇列：{self._export_count} 個媒體。")
        self.download_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Backend operation lifecycle
    # ------------------------------------------------------------------
    def _start_operation(
        self,
        kind: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._generation += 1
        token = self._generation
        operation = (token, kind, method, args, dict(kwargs or {}))
        if self.is_busy():
            # Never overwrite the sole thread reference while its worker is
            # still polling a RunHandle.  Queue a single newest request and
            # start it only after the previous handle has reached done=True.
            self._pending_operation = operation
            self._cancel_worker_only()
            self._set_status("正在取消上一個操作，準備開始新的查詢…")
            return
        self._launch_operation(*operation)

    def _launch_operation(
        self,
        token: int,
        kind: str,
        method: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> None:
        thread = QThread(self)
        worker = _RunnerWorker(self._runner, method, args, kwargs, token=token, kind=kind)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # A lambda or functools.partial is not a QObject receiver in PySide6;
        # those callbacks can therefore run in the worker thread.  The page
        # methods are real @Slot receivers and are explicitly queued onto the
        # GUI thread before they inspect or update any widget state.
        worker.event.connect(self._operation_event, Qt.QueuedConnection)
        worker.succeeded.connect(self._operation_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._operation_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._operation_canceled, Qt.QueuedConnection)
        # QThread.quit is thread-safe and is called directly from the worker's
        # finally block, so a busy GUI queue cannot keep the polling thread
        # alive after the RunHandle has completed.
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._operation_thread_finished, Qt.QueuedConnection)
        thread.finished.connect(thread.deleteLater)
        self._thread_workers[thread] = worker
        self._thread = thread
        self._worker = worker
        self._operation_kind = kind
        self._set_controls_enabled(False)
        self.cancel_button.setVisible(True)
        thread.start()

    def _cancel_worker_only(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()

    @Slot()
    def _operation_thread_finished(self) -> None:
        """Handle one QThread's completion on the GUI thread."""

        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        worker = self._thread_workers.pop(thread, None)
        if worker is not None:
            self._operation_finished(thread, worker)

    def _operation_finished(self, thread: QThread, worker: _RunnerWorker) -> None:
        # A stale thread can only arrive here after a queued replacement.  It
        # must not hide the cancel button or re-enable controls belonging to
        # the newer operation.
        if self._thread is not thread or self._worker is not worker:
            return
        self._thread = None
        self._worker = None
        pending = self._pending_operation
        self._pending_operation = None
        if pending is not None and not self._closing_parent_window():
            self._launch_operation(*pending)
            return
        self.cancel_button.setVisible(False)
        self._set_controls_enabled(True)

    def _closing_parent_window(self) -> bool:
        parent = self.window()
        return bool(getattr(parent, "_closing", False))

    @Slot(int, object)
    def _operation_event(self, token: int, event: Any) -> None:
        if token != self._generation or not isinstance(event, Mapping):
            return
        name = _text(event.get("event", event.get("type", ""))).lower()
        if name in {"status", "log", "progress"}:
            message = _text(event.get("message", event.get("text", event.get("status", ""))))
            if message:
                self._set_status(_safe_message(message))
        elif name == "chats":
            # ``chat ls --topics`` may still stream the parent dialog list
            # before its topic result.  That event belongs to the backend
            # operation, but must never rebuild the user's current chat view
            # while a topic request is active.
            if self._operation_kind != "list":
                return
            raw = event.get("chats", [])
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                self._chats = self._normalise_chats({"chats": raw})
                self._apply_filter()
            if event.get("partial") or int(event.get("skipped", 0) or 0) > 0:
                skipped = int(event.get("skipped", 0) or 0)
                detail = _safe_message(event.get("message", ""))
                suffix = f"（略過 {skipped} 個對話）" if skipped else "（部分對話未能讀取）"
                self._set_status(f"清單可能不完整{suffix}{': ' + detail if detail else '。'}")
        elif name in {"topics", "topic_list"}:
            # Conversely, a delayed topic event from an old request must not
            # populate a topic list while the page is refreshing chats.
            if self._operation_kind != "topics":
                return
            raw = event.get("topics", event.get("items", []))
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                self._populate_topics(raw, source="串流")
            if event.get("partial") or int(event.get("skipped", 0) or 0) > 0:
                self.topic_hint.setText("主題清單可能不完整，請確認後再選擇。")
        elif name in {"exported", "export_complete", "exported_file"}:
            count = event.get("count")
            if count is not None:
                self._set_status(f"已找到 {int(count or 0)} 個符合條件的媒體。")

    @Slot(int, str, object)
    def _operation_succeeded(self, token: int, kind: str, data: Any) -> None:
        if token != self._generation:
            return
        if kind == "list":
            self._chats = self._normalise_chats(data)
            self._apply_filter()
            count = self.chat_list.count()
            skipped = int(data.get("skipped", 0) or 0) if isinstance(data, Mapping) else 0
            partial = bool(data.get("partial", False)) if isinstance(data, Mapping) else False
            detail = _safe_message(data.get("message", "")) if isinstance(data, Mapping) else ""
            incomplete = partial or skipped > 0
            if count:
                self.chat_hint.setText(f"共找到 {count} 個可選對話。")
                if incomplete:
                    suffix = f"（略過 {skipped} 個對話）" if skipped else "（部分對話未能讀取）"
                    self._set_status(f"清單更新完成，但可能不完整{suffix}{': ' + detail if detail else '。'}")
                else:
                    self._set_status(f"清單更新完成，共 {count} 個可選對話。")
            else:
                if incomplete:
                    suffix = f"（略過 {skipped} 個對話）" if skipped else "（部分對話未能讀取）"
                    self.chat_hint.setText(f"目前沒有可選對話，清單可能不完整{suffix}。")
                    self._set_status(f"清單更新完成，但可能不完整{suffix}{': ' + detail if detail else '。'}")
                else:
                    self.chat_hint.setText("目前沒有符合的頻道或群組。")
                    self._set_status("清單更新完成，目前沒有可選對話。")
        elif kind == "topics":
            values: Any = data
            if isinstance(data, Mapping):
                values = data.get("topics", data.get("items", data.get("data", [])))
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
                if self._selected_chat is not None:
                    self._populate_topics(values)
                    self._set_status(
                        f"主題清單更新完成，共 {len(self._topics)} 個主題。"
                    )
            else:
                self._set_topic_error("後端回傳的論壇主題格式無法辨識。")
        else:
            path, count = self._normalise_export(data)
            if count <= 0:
                self._export_path = ""
                self._export_count = 0
                self.download_button.setEnabled(False)
                self._set_status("掃描完成：符合條件的媒體為 0 個，請調整日期或類型。")
            elif not path:
                self._set_status("掃描完成但找不到匯出檔案，請重試。")
            else:
                self._export_path = path
                self._export_count = count
                self.download_button.setEnabled(True)
                self._set_status(f"掃描完成：共 {count} 個媒體，可以開始下載。")

    @Slot(int, str)
    def _operation_failed(self, token: int, message: str) -> None:
        if token != self._generation:
            return
        if self._operation_kind == "topics":
            self._set_topic_error(message)
        self._set_status(f"操作失敗：{_safe_message(message) or '請稍後重試。'}")
        self._clear_scan(keep_status=True)

    @Slot(int)
    def _operation_canceled(self, token: int) -> None:
        if token != self._generation:
            return
        if self._operation_kind == "topics":
            self._set_topic_error("主題讀取已取消。")
        self._set_status("操作已取消。")

    @staticmethod
    def _normalise_chats(data: Any) -> list[Any]:
        def unique(items: Sequence[Any]) -> list[Any]:
            result: list[Any] = []
            seen: set[str] = set()
            for item in items:
                key = _chat_ref(item) if isinstance(item, Mapping) else ""
                if not key:
                    key = repr(item)
                if key in seen:
                    continue
                seen.add(key)
                result.append(item)
            return result

        if isinstance(data, Mapping):
            for key in ("chats", "items", "channels", "groups"):
                value = data.get(key)
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                    return unique(value)
            # A backend fixture may return an id -> chat mapping.  Ignore
            # obvious metadata keys, but preserve the chat objects themselves.
            values = [value for key, value in data.items() if key not in {"event", "skipped", "count"}]
            if values and all(isinstance(item, Mapping) for item in values):
                return unique(values)
            return []
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
            return unique(data)
        return []

    @staticmethod
    def _normalise_export(data: Any) -> tuple[str, int]:
        if not isinstance(data, Mapping):
            return "", 0
        path = _text(data.get("path", data.get("file", data.get("output", "")))).strip()
        try:
            count = max(0, int(data.get("count", data.get("media_count", 0)) or 0))
        except (TypeError, ValueError):
            count = 0
        return path, count

    # ------------------------------------------------------------------
    # Small visual/state helpers
    # ------------------------------------------------------------------
    def _set_controls_enabled(self, enabled: bool) -> None:
        self.refresh_button.setEnabled(enabled)
        self.account_combo.setEnabled(enabled)
        self.search_edit.setEnabled(enabled)
        self.type_combo.setEnabled(enabled)
        self.chat_list.setEnabled(enabled)
        self._update_scan_button(enabled)
        topic_enabled = bool(enabled and self.topic_panel.isVisible() and not self._topics_loading)
        self.topic_search.setEnabled(topic_enabled)
        self.topic_list.setEnabled(topic_enabled)
        self.folder_edit.setEnabled(enabled)
        self.folder_button.setEnabled(enabled)
        for checkbox in (self.video_check, self.photo_check, self.audio_check, self.document_check):
            checkbox.setEnabled(enabled)
        self.start_date.setEnabled(enabled)
        self.end_date.setEnabled(enabled)
        self.today_button.setEnabled(enabled)
        self.seven_days_button.setEnabled(enabled)
        self.thirty_days_button.setEnabled(enabled)
        if not enabled:
            self.download_button.setEnabled(False)
        else:
            self.download_button.setEnabled(bool(self._export_path and self._export_count > 0))

    def _set_status(self, text: str) -> None:
        self.status_label.setText(_text(text))

    def _clear_chat_selection(self) -> None:
        self._chats = []
        self._selected_chat = None
        self._selected_topic = None
        self._topic_chat_ref = ""
        self._topics = []
        self._topics_loaded = False
        self._topics_loading = False
        self.chat_list.clear()
        self.topic_search.clear()
        self.topic_list.clear()
        self.topic_panel.setVisible(False)
        self.topic_state.setText("選擇論壇群組後載入")
        self.topic_hint.setText("論壇群組需要先選擇主題，或明確選擇「全部主題」。")
        self.chat_hint.setText("請按「更新清單」載入目前帳號可存取的頻道與群組。")
        self._clear_scan()
        if hasattr(self, "scan_button"):
            self.scan_button.setEnabled(False)

    def _clear_scan(self, *_args: Any, keep_status: bool = False) -> None:
        self._export_path = ""
        self._export_count = 0
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(False)
        if not keep_status and hasattr(self, "status_label"):
            # Avoid replacing useful account/chats status while the page is
            # being populated; callers can set a more specific message.
            pass

    def _finish_idle_state(self) -> None:
        self.cancel_button.setVisible(False)
        self._set_controls_enabled(True)


__all__ = ["ChannelPage"]
