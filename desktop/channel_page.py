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

import shutil
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

try:
    from .search import normalize_search_text, search_matches
except ImportError:  # pragma: no cover - direct module execution
    from search import normalize_search_text, search_matches


_MEDIA_LABELS = {
    "video": "影片",
    "photo": "照片",
    "audio": "音訊",
    "document": "文件",
}

# Telegram opened in August 2013; no message is older than this.
_TELEGRAM_EPOCH = QDate(2013, 8, 1)


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


def _format_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def _free_bytes(folder: str) -> Optional[int]:
    """Free space of the drive that will hold ``folder``, if it can be read.

    The download folder is created on demand, so walk up to the nearest
    directory that already exists.
    """

    if not folder:
        return None
    try:
        path = Path(folder).expanduser()
        for candidate in (path, *path.parents):
            if candidate.is_dir():
                return int(shutil.disk_usage(candidate).free)
    except (OSError, ValueError):
        pass
    return None


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
    # The main window owns the durable queue.  The page deliberately waits
    # for its acknowledgement before claiming that a scan was accepted.
    view_queue_requested = Signal()

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
        self._cancel_status = ""
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunnerWorker] = None
        # Keep stale workers reachable until their QThread has emitted
        # finished.  A refresh may queue a replacement while the previous
        # RunHandle is still unwinding.
        self._thread_workers: dict[QThread, _RunnerWorker] = {}
        self._pending_operation: Optional[tuple[int, str, str, tuple[Any, ...], dict[str, Any]]] = None
        self._export_path = ""
        self._export_count = 0
        self._export_bytes = 0
        # A manifest is valid only for the exact account/source/topic/date/
        # media selection that produced it.  Keep the snapshot separate from
        # the transient status text so a late worker result cannot resurrect a
        # result after the user changed the form.
        self._export_snapshot: Optional[dict[str, Any]] = None
        self._operation_snapshot: Optional[dict[str, Any]] = None
        self._export_request_pending = False
        self._export_consumed = False
        self._download_folder = ""
        self._proxy = ""
        self._concurrency = 2
        self._skip_existing = True
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 22)
        outer.setSpacing(12)

        kicker = QLabel("頻道與群組")
        kicker.setObjectName("Kicker")
        # The sidebar already identifies this page.  Keeping the title and
        # one-line intro together gives the scan/download controls room at
        # the default 1180x800 window size.
        title = QLabel("選頻道，先看數量再下載。")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        intro = QLabel("選擇帳號、頻道與日期，掃描後再加入下載佇列。")
        intro.setObjectName("HeroDescription")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        account_card = _Card()
        account_layout = QHBoxLayout(account_card)
        account_layout.setContentsMargins(14, 10, 14, 10)
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
        channel_layout.setContentsMargins(12, 12, 12, 12)
        channel_layout.setSpacing(10)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜尋頻道、群組名稱或 username")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_edit, 1)
        channel_layout.addLayout(search_row)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部", "all")
        self.type_combo.addItem("頻道", "channel")
        self.type_combo.addItem("群組", "group")
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        self.type_combo.setMinimumWidth(86)
        filter_row.addWidget(self.type_combo, 1)
        self.clear_search_button = QPushButton("清除")
        self.clear_search_button.setObjectName("SecondaryButton")
        self.clear_search_button.setFixedWidth(60)
        self.clear_search_button.setToolTip("清除搜尋與目前頻道選取，保留日期、媒體類型和資料夾")
        self.clear_search_button.clicked.connect(self._clear_source_selection)
        filter_row.addWidget(self.clear_search_button)
        channel_layout.addLayout(filter_row)
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
        self.chat_hint.setTextFormat(Qt.TextFormat.PlainText)
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
        self.topic_search.setClearButtonEnabled(True)
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
        self.topic_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.topic_hint.setWordWrap(True)
        topic_layout.addWidget(self.topic_hint)
        self.topic_panel.setVisible(False)
        channel_layout.addWidget(self.topic_panel)

        filters = _Card()
        filters_layout = QVBoxLayout(filters)
        filters_layout.setContentsMargins(14, 10, 14, 10)
        filters_layout.setSpacing(8)
        filter_title = QLabel("下載條件")
        filter_title.setObjectName("SectionTitle")
        filters_layout.addWidget(filter_title)

        dates = QGridLayout()
        dates.setHorizontalSpacing(10)
        dates.setVerticalSpacing(4)
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
            editor.dateChanged.connect(self._selection_inputs_changed)
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
        self.ninety_days_button = QPushButton("近 90 日")
        self.all_time_button = QPushButton("全部時間")
        self.all_time_button.setToolTip("從頻道的第一則訊息到今天；大型頻道的掃描需要較久")
        self._date_buttons = (
            self.today_button, self.seven_days_button, self.thirty_days_button,
            self.ninety_days_button, self.all_time_button,
        )
        for button in self._date_buttons:
            button.setObjectName("LinkButton")
            shortcut_row.addWidget(button)
        shortcut_row.addStretch(1)
        self.today_button.clicked.connect(lambda: self._set_date_range(0))
        self.seven_days_button.clicked.connect(lambda: self._set_date_range(6))
        self.thirty_days_button.clicked.connect(lambda: self._set_date_range(29))
        self.ninety_days_button.clicked.connect(lambda: self._set_date_range(89))
        self.all_time_button.clicked.connect(self._set_all_time)
        dates.addLayout(shortcut_row, 2, 0, 1, 2)
        filters_layout.addLayout(dates)

        media_label = QLabel("媒體類型")
        media_label.setObjectName("FieldLabel")
        filters_layout.addWidget(media_label)
        media_row = QHBoxLayout()
        media_row.setSpacing(12)
        self.video_check = QCheckBox("影片")
        self.photo_check = QCheckBox("照片")
        self.audio_check = QCheckBox("音訊")
        self.document_check = QCheckBox("文件")
        self.video_check.setChecked(True)
        for checkbox in (self.video_check, self.photo_check, self.audio_check, self.document_check):
            checkbox.stateChanged.connect(self._selection_inputs_changed)
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
        folder_hint = QLabel("檔案會按群組／頻道名稱自動分資料夾。")
        folder_hint.setObjectName("MutedText")
        folder_hint.setWordWrap(True)
        filters_layout.addWidget(folder_hint)

        action_card = _Card()
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(14, 10, 14, 10)
        action_layout.setSpacing(6)
        self.status_label = QLabel("請選擇帳號並按「更新清單」。")
        self.status_label.setObjectName("MutedText")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
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
        context_card = _Card()
        context_layout = QVBoxLayout(context_card)
        context_layout.setContentsMargins(10, 6, 10, 6)
        context_layout.setSpacing(2)
        self.current_source_label = QLabel("尚未選擇頻道或群組。")
        self.current_source_label.setObjectName("CurrentSource")
        self.current_source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_source_label.setStyleSheet("font-weight: 600; color: #3157b7;")
        self.current_source_label.setWordWrap(True)
        context_layout.addWidget(self.current_source_label)
        self.current_source_identity_label = QLabel("識別：尚未選擇")
        self.current_source_identity_label.setObjectName("MutedText")
        self.current_source_identity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.current_source_identity_label.setWordWrap(True)
        context_layout.addWidget(self.current_source_identity_label)
        self.scan_summary_label = QLabel("尚未掃描；選好來源、日期與媒體類型後開始掃描。")
        self.scan_summary_label.setObjectName("ScanSummary")
        self.scan_summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_summary_label.setWordWrap(True)
        context_layout.addWidget(self.scan_summary_label)
        self.queue_status_label = QLabel("下載佇列：尚無項目")
        self.queue_status_label.setObjectName("MutedText")
        self.queue_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_status_label.setWordWrap(True)
        context_layout.addWidget(self.queue_status_label)
        context_actions = QHBoxLayout()
        context_actions.setSpacing(8)
        self.change_source_button = QPushButton("換另一個頻道")
        self.change_source_button.setObjectName("SecondaryButton")
        self.change_source_button.setToolTip("清除目前選取，保留日期、媒體類型和下載資料夾")
        self.change_source_button.clicked.connect(self._clear_source_selection)
        context_actions.addWidget(self.change_source_button)
        self.view_queue_button = QPushButton("查看下載佇列")
        self.view_queue_button.setObjectName("SecondaryButton")
        self.view_queue_button.clicked.connect(self.view_queue_requested.emit)
        context_actions.addWidget(self.view_queue_button)
        context_actions.addStretch(1)
        context_layout.addLayout(context_actions)
        right_column.addWidget(context_card)
        right_column.addWidget(filters)
        right_column.addWidget(action_card)
        right_column.addStretch(1)
        workflow.addLayout(right_column, 6)
        outer.addLayout(workflow, 1)

        self._set_controls_enabled(True)
        self._update_current_source_label()

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
        saved_export_bytes = self._export_bytes
        saved_export_snapshot = dict(self._export_snapshot) if self._export_snapshot else None
        saved_scan_summary = self.scan_summary_label.text()
        saved_request_pending = self._export_request_pending
        saved_consumed = self._export_consumed
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
            # the selected chat and an unconsumed manifest stay valid for the
            # same account.  A queued manifest is intentionally never
            # resurrected by this refresh path.
            self._apply_filter()
            # Rebuilding QListWidget rows can emit currentItemChanged and
            # clear the preview as a side effect.  Restore only when the
            # source snapshot still matches the visible selection.
            if (
                not saved_consumed
                and self._selected_chat is not None
                and saved_export_snapshot is not None
                and self._current_export_snapshot() is not None
                and self._snapshots_match(saved_export_snapshot, self._current_export_snapshot() or {})
            ):
                self._export_path = saved_export_path
                self._export_count = saved_export_count
                self._export_bytes = saved_export_bytes
                self._export_snapshot = saved_export_snapshot
                self._export_request_pending = saved_request_pending
                self._export_consumed = False
                self.scan_summary_label.setText(saved_scan_summary)
            self._update_download_button()
        self._update_current_source_label()
        if not self.account_combo.count():
            self.account_combo.addItem("尚未登入 Telegram")
            self._set_status("尚未有可用帳號。請先到「帳號管理」完成登入。")
        elif not self._current_account_is_authenticated():
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
        elif changed and not self._load_cached_chats():
            self._set_status("請按「更新清單」載入目前帳號可存取的頻道與群組。")

    def _load_cached_chats(self) -> bool:
        """Show the list saved by the last refresh so the page is usable at once.

        Listing every dialog takes several seconds on a large account; the
        cache is only a starting point and 「更新清單」 still replaces it.
        """

        loader = getattr(getattr(self._runner, "store", None), "load_chat_cache", None)
        account_id = self.active_account_id()
        if not callable(loader) or not account_id or not self._current_account_is_authenticated():
            return False
        try:
            chats, saved_at = loader(account_id)
            chats = self._normalise_chats({"chats": list(chats)})
        except Exception:
            return False
        if not chats:
            return False
        self._chats = chats
        self._apply_filter()
        day = _text(saved_at)[:10] or "先前"
        self.chat_hint.setText(f"顯示 {day} 更新的清單，共 {len(chats)} 個對話。")
        self._set_status("已載入上次的清單，可直接選擇；有新加入的頻道或群組時請按「更新清單」。")
        return True

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
        was_busy = self.is_busy()
        if not was_busy:
            # The worker may have stopped while its queued success signal is
            # still waiting in the GUI event queue.  Invalidate that result
            # before returning from an explicit cancel.
            self._generation += 1
            if self._operation_snapshot is not None:
                self._operation_snapshot = None
                self._clear_scan(keep_status=True)
                self._set_scan_summary("操作已取消；請確認來源與條件後重試。")
            self._pending_operation = None
            self._cancel_status = ""
            self._finish_idle_state()
            return
        operation_kind = self._operation_kind
        self._generation += 1
        self._pending_operation = None
        self._operation_snapshot = None
        self._cancel_status = "操作已取消。"
        if operation_kind == "export":
            self._clear_scan(keep_status=True)
            self._set_scan_summary("掃描已取消；請確認來源與條件後重試。")
        worker = self._worker
        if worker is not None:
            worker.cancel()
        if was_busy:
            self._set_status("正在取消目前操作…")

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
            if not self._load_cached_chats():
                self._set_status("帳號已切換，請按「更新清單」。")
        elif account_id:
            self._set_status("目前帳號尚未登入，請先完成 Telegram 登入。")
        else:
            self._set_status("請先選擇一個 Telegram 帳號。")

    def _set_date_range(self, days_before_today: int) -> None:
        today = _taipei_qdate()
        self.start_date.blockSignals(True)
        self.end_date.blockSignals(True)
        self.end_date.setDate(today)
        self.start_date.setDate(today.addDays(-int(days_before_today)))
        self.start_date.blockSignals(False)
        self.end_date.blockSignals(False)
        self._selection_inputs_changed()

    def _set_all_time(self) -> None:
        self.start_date.blockSignals(True)
        self.end_date.blockSignals(True)
        self.end_date.setDate(_taipei_qdate())
        self.start_date.setDate(_TELEGRAM_EPOCH)
        self.start_date.blockSignals(False)
        self.end_date.blockSignals(False)
        self._selection_inputs_changed()

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

    def _selection_inputs_changed(self, *_args: Any) -> None:
        """Invalidate a scan when its source/date/media inputs change.

        The output folder is intentionally excluded: the exported manifest is
        reusable when the user decides to save the resulting files elsewhere.
        Account/source/topic changes use the same helper below.
        """

        self._invalidate_scan("下載條件已變更，請重新掃描。")

    def _invalidate_scan(self, reason: str = "", *, cancel_operation: bool = True) -> None:
        self._generation += 1
        self._pending_operation = None
        self._operation_snapshot = None
        self._cancel_status = ""
        if cancel_operation and self._worker is not None:
            self._worker.cancel()
        self._clear_scan(keep_status=True)
        self._update_current_source_label()
        if reason:
            self._set_scan_summary(reason)
            self._set_status(reason)

    def _clear_source_selection(self) -> None:
        """Reset source/search/topic state while retaining download settings."""

        self._invalidate_scan("已清除目前來源，請選擇另一個頻道或群組。")
        self._selected_chat = None
        self._selected_topic = None
        self._topic_chat_ref = ""
        self._topics = []
        self._topics_loaded = False
        self._topics_loading = False
        self.topic_panel.setVisible(False)
        self.topic_state.setText("選擇論壇群組後載入")
        self.topic_hint.setText("論壇群組需要先選擇主題，或明確選擇「全部主題」。")
        self.topic_search.blockSignals(True)
        self.topic_search.clear()
        self.topic_search.blockSignals(False)
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.type_combo.blockSignals(True)
        self.type_combo.setCurrentIndex(0)
        self.type_combo.blockSignals(False)
        self.chat_list.blockSignals(True)
        self.chat_list.clearSelection()
        self.chat_list.setCurrentRow(-1)
        self.chat_list.blockSignals(False)
        self._apply_filter()
        self.chat_list.blockSignals(True)
        self.chat_list.clearSelection()
        self.chat_list.setCurrentRow(-1)
        self.chat_list.blockSignals(False)
        self._update_current_source_label()
        self._update_scan_button()
        self._set_scan_summary("已清除來源；日期、媒體類型和資料夾仍保留。")
        self._set_status("已清除目前來源，請從清單選擇另一個頻道或群組。")
        self.search_edit.setFocus()

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
        self._update_current_source_label()
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
        self._update_current_source_label()
        self._update_scan_button(False)

    def _apply_topic_filter(self, *_args: Any) -> None:
        if not self._topics_loaded:
            return
        query = normalize_search_text(self.topic_search.text())
        selected_id = _text((self._selected_topic or {}).get("id", ""))
        selected_item: Optional[QListWidgetItem] = None
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        all_item = QListWidgetItem("全部主題（掃描整個論壇群組）")
        all_topic = {"id": "", "title": "全部主題", "all": True}
        all_item.setData(Qt.UserRole, all_topic)
        if not query or search_matches(query, "全部主題（掃描整個論壇群組）"):
            self.topic_list.addItem(all_item)
            if not selected_id and self._selected_topic and self._selected_topic.get("all"):
                selected_item = all_item
        for topic in self._topics:
            if query and not search_matches(query, topic["title"], topic["id"]):
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
            self._invalidate_scan("搜尋已隱藏目前話題，請重新選擇並掃描。")
            self.topic_hint.setText("目前搜尋結果沒有原本選取的主題，請重新選擇。")
        self._update_current_source_label()
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
            self._update_current_source_label()
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
        self._invalidate_scan("已更換論壇話題，請重新掃描。")
        value = current.data(Qt.UserRole) if current is not None else None
        self._selected_topic = dict(value) if isinstance(value, Mapping) else None
        if self._selected_topic is None:
            self.topic_hint.setText("請選擇一個主題；也可以明確選擇「全部主題」。")
        elif self._selected_topic.get("all"):
            self.topic_hint.setText("已明確選擇整個論壇群組，掃描會包含所有主題。")
        else:
            self.topic_hint.setText(f"已選主題：{self._selected_topic.get('title', '未命名主題')}")
        self._update_current_source_label()
        self._update_scan_button()

    def _apply_filter(self, *_args: Any) -> None:
        query = normalize_search_text(self.search_edit.text())
        kind = _text(self.type_combo.currentData() or "all")
        current_id = _chat_ref(self._selected_chat) if self._selected_chat is not None else ""
        saved_export_path = self._export_path
        saved_export_count = self._export_count
        saved_export_bytes = self._export_bytes
        saved_export_snapshot = dict(self._export_snapshot) if self._export_snapshot else None
        saved_scan_summary = self.scan_summary_label.text() if hasattr(self, "scan_summary_label") else ""
        saved_request_pending = self._export_request_pending
        saved_consumed = self._export_consumed
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        selected_item: Optional[QListWidgetItem] = None
        for chat in self._chats:
            chat_kind = _chat_kind(chat)
            username = _chat_username(chat)
            if query and not search_matches(query, _chat_title(chat), username, f"@{username}" if username else "", _chat_ref(chat), _chat_id(chat)):
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
            if (
                not saved_consumed
                and saved_export_snapshot is not None
                and self._current_export_snapshot() is not None
                and self._snapshots_match(saved_export_snapshot, self._current_export_snapshot() or {})
            ):
                self._export_path = saved_export_path
                self._export_count = saved_export_count
                self._export_bytes = saved_export_bytes
                self._export_snapshot = saved_export_snapshot
                self._export_request_pending = saved_request_pending
                self._export_consumed = False
                self.scan_summary_label.setText(saved_scan_summary)
            self._update_download_button()
            if hasattr(self, "scan_button") and not self.is_busy():
                self._update_scan_button()
        elif current_id:
            # A hidden selection must never remain the source of a download.
            # Clear both the selected chat and the previously scanned export.
            self._selected_chat = None
            self._prepare_topics_for_chat()
            self._invalidate_scan("搜尋或篩選已隱藏目前來源，請重新選擇並掃描。")
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
        self._update_current_source_label()
        self._update_scan_button()

    def _chat_selected(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
        # A topic result belongs to the chat that started it.  In normal use
        # the list is disabled while loading, but invalidating here also
        # protects programmatic selection and rapid filter changes.
        self._invalidate_scan("已更換來源，請重新掃描。")
        self._selected_chat = current.data(Qt.UserRole) if current is not None else None
        self._prepare_topics_for_chat()
        self._update_current_source_label()
        if not self.is_busy():
            self._update_scan_button()
        if self._selected_chat is not None:
            self.chat_hint.setText(
                f"已選：{_chat_title(self._selected_chat)}（{_kind_label(_chat_kind(self._selected_chat))}）；"
                "請先掃描符合媒體數。"
            )

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

    def _source_descriptor(
        self,
        chat: Any = None,
        topic: Optional[Mapping[str, Any]] = None,
    ) -> str:
        chat = self._selected_chat if chat is None else chat
        if chat is None:
            return "尚未選擇頻道或群組"
        title = _chat_title(chat)
        identity = _chat_ref(chat) or _chat_id(chat) or "無識別碼"
        username = _chat_username(chat)
        identity_text = f"{identity} · @{username}" if username else identity
        value = f"{title}（{_kind_label(_chat_kind(chat))}） · {identity_text}"
        if _chat_is_forum(chat):
            if topic is None:
                value += " · 話題：尚未選擇"
            elif topic.get("all"):
                value += " · 話題：全部主題"
            else:
                value += f" · 話題：{_text(topic.get('title'), '未命名主題')} · ID {_text(topic.get('id'))}"
        return value

    def _current_export_snapshot(self) -> Optional[dict[str, Any]]:
        """Return the inputs that make an export manifest specific."""

        if self._selected_chat is None:
            return None
        topic = self._selected_topic
        return {
            "account_id": self.active_account_id(),
            "chat_ref": _chat_ref(self._selected_chat),
            "chat_id": _chat_id(self._selected_chat),
            "chat_title": _chat_title(self._selected_chat),
            "source_label": self._source_descriptor(self._selected_chat, topic),
            "is_forum": _chat_is_forum(self._selected_chat),
            "topic_id": _text(topic.get("id")) if topic is not None else "",
            "topic_title": _text(topic.get("title")) if topic is not None else "",
            "topic_all": bool(topic and topic.get("all")),
            "start_date": _date_text(self.start_date.date()),
            "end_date": _date_text(self.end_date.date()),
            "media_types": tuple(self._media_types()),
        }

    def _update_current_source_label(self) -> None:
        if not hasattr(self, "current_source_label"):
            return
        if self._selected_chat is None:
            self.current_source_label.setText("尚未選擇頻道或群組。")
            if hasattr(self, "current_source_identity_label"):
                self.current_source_identity_label.setText("識別：尚未選擇")
            return
        title = _chat_title(self._selected_chat)
        kind = _kind_label(_chat_kind(self._selected_chat))
        username = _chat_username(self._selected_chat)
        prominent = f"目前來源：{title}（{kind}）"
        if username:
            prominent += f" · @{username}"
        self.current_source_label.setText(prominent)
        if hasattr(self, "current_source_identity_label"):
            identity = _chat_ref(self._selected_chat) or _chat_id(self._selected_chat) or "無識別碼"
            detail = f"識別：{identity}"
            if _chat_is_forum(self._selected_chat):
                topic = self._selected_topic
                if topic is None:
                    detail += " · 話題：尚未選擇"
                elif topic.get("all"):
                    detail += " · 話題：全部主題"
                else:
                    detail += f" · 話題：{_text(topic.get('title'), '未命名主題')} · ID {_text(topic.get('id'))}"
            self.current_source_identity_label.setText(detail)

    def _set_scan_summary(self, text: str) -> None:
        if hasattr(self, "scan_summary_label"):
            self.scan_summary_label.setText(_text(text))

    def _snapshot_display_source(self, snapshot: Mapping[str, Any]) -> str:
        """Return a compact, human-readable source for the scan summary.

        The context card already displays the full ref/username.  Repeating it
        in the result line makes long channel identities push the action area
        below the first viewport, so summaries use the title and explicit
        forum topic only.
        """

        title = _text(snapshot.get("chat_title")).strip() or "頻道或群組"
        if snapshot.get("is_forum"):
            topic = (
                "全部主題"
                if snapshot.get("topic_all")
                else _text(snapshot.get("topic_title")).strip() or "指定主題"
            )
            return f"{title} · 話題：{topic}"
        return title

    def _format_scan_summary(self, count: int, total_bytes: int = 0) -> str:
        snapshot = self._export_snapshot or self._operation_snapshot or self._current_export_snapshot()
        if snapshot is None:
            return f"掃描結果：{count} 個媒體。"
        media = "、".join(_MEDIA_LABELS.get(item, item) for item in snapshot.get("media_types", ())) or "未選類型"
        size = f"，約 {_format_bytes(total_bytes)}" if total_bytes > 0 else ""
        source = self._snapshot_display_source(snapshot)
        return (
            f"掃描結果：{source} · {snapshot.get('start_date', '')}～"
            f"{snapshot.get('end_date', '')} · {media} · 符合 {count} 個媒體{size}。"
        )

    def _manifest_matches_current(self) -> bool:
        snapshot = self._export_snapshot
        current = self._current_export_snapshot()
        if snapshot is None or current is None:
            return False
        return bool(
            self._export_path
            and self._export_count > 0
            and self._snapshots_match(snapshot, current)
            and not self._export_request_pending
            and not self._export_consumed
        )

    @staticmethod
    def _snapshots_match(expected: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        """Compare only stable inputs, allowing refreshed display metadata."""

        keys = (
            "account_id", "chat_ref", "chat_id", "is_forum", "topic_id", "topic_all",
            "start_date", "end_date", "media_types",
        )
        return all(expected.get(key) == current.get(key) for key in keys)

    def _update_download_button(self) -> None:
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(self._manifest_matches_current() and not self.is_busy())

    def download_queued(self, title: str, count: int) -> bool:
        """Acknowledge a job accepted by MainWindow's durable queue.

        The page emits ``download_requested`` first but never reports success
        from that emission alone.  A late acknowledgement after the source
        was cleared is ignored by the pending-request guard.
        """

        if not self._export_request_pending:
            return False
        self._export_request_pending = False
        self._export_consumed = True
        self._export_path = ""
        self._export_count = 0
        self._export_bytes = 0
        self._export_snapshot = None
        self.download_button.setEnabled(False)
        self.download_button.setText("已加入佇列")
        clean_title = _text(title).strip() or "頻道下載"
        try:
            safe_count = max(0, int(count or 0))
        except (TypeError, ValueError):
            safe_count = 0
        self._set_scan_summary(f"已加入下載佇列：{clean_title} · 共 {safe_count} 個媒體。")
        self._set_status(f"已加入下載佇列：{safe_count} 個媒體。")
        return True

    def download_rejected(self, message: str = "") -> bool:
        """Release a pending manifest when MainWindow rejected queueing it."""

        if not self._export_request_pending:
            return False
        self._export_request_pending = False
        self._export_consumed = False
        self._set_status(f"加入下載佇列失敗：{_safe_message(message) or '請稍後重試。'}")
        self.download_button.setText("開始下載")
        self._set_scan_summary("掃描結果仍可重試加入下載佇列。")
        self._update_download_button()
        return True

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
        snapshot = self._current_export_snapshot()
        if snapshot is None:
            self._set_status("請先從清單選擇一個頻道或群組。")
            return
        self._clear_scan()
        self._operation_snapshot = snapshot
        self._set_scan_summary(
            f"正在掃描：{self._snapshot_display_source(snapshot)} · "
            f"{snapshot['start_date']}～{snapshot['end_date']} · "
            f"{'、'.join(_MEDIA_LABELS.get(item, item) for item in snapshot['media_types'])}"
        )
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
        if self._export_request_pending:
            self._set_status("正在加入下載佇列，請稍候。")
            return
        if self._export_consumed:
            self._set_status("這份掃描結果已加入佇列；如要下載其他條件，請重新掃描。")
            return
        if not self._export_path or self._export_count <= 0:
            self._set_status("請先掃描，確認有符合條件的媒體。")
            return
        if not self._manifest_matches_current():
            self._set_scan_summary("下載條件已變更或掃描結果已失效，請重新掃描。")
            self._set_status("下載條件已變更，請先重新掃描。")
            self._update_download_button()
            return
        snapshot = dict(self._export_snapshot or {})
        folder = self.folder_edit.text().strip()
        if not folder:
            self._set_status("請先選擇下載資料夾。")
            return
        chat_title = _text(snapshot.get("chat_title"), _chat_title(self._selected_chat))
        topic_title = _text(snapshot.get("topic_title"))
        topic_suffix = ""
        if bool(snapshot.get("is_forum")):
            topic_suffix = f" · {topic_title or '全部主題'}"
        title = f"{chat_title}{topic_suffix} · {snapshot.get('start_date', '')}～{snapshot.get('end_date', '')}"
        options = {
            "account_id": snapshot.get("account_id", self.active_account_id()),
            "output_dir": folder,
            "files": [self._export_path],
            "title": title,
            "group_album": False,
            "group_by_chat": True,
            "skip_existing": self._skip_existing,
            "continue_download": True,
            "concurrency": self._concurrency,
            "proxy": self._proxy,
            # Lets the queue show whole-job progress and remaining time.
            "expected_files": self._export_count,
            "expected_bytes": self._export_bytes,
            "media_types": list(snapshot.get("media_types", ())),
        }
        # The receiver must explicitly acknowledge acceptance.  This keeps a
        # failed validation from looking like a successful enqueue and also
        # prevents a second click from submitting the same manifest twice.
        self._export_request_pending = True
        self._export_consumed = True
        self.download_button.setEnabled(False)
        self._set_status("正在加入下載佇列…")
        self.download_requested.emit([], options)

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
        if self._cancel_status:
            self._set_status(self._cancel_status)
            self._cancel_status = ""

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
            snapshot = self._operation_snapshot
            current_snapshot = self._current_export_snapshot()
            if (
                snapshot is None
                or current_snapshot is None
                or not self._snapshots_match(snapshot, current_snapshot)
            ):
                # A programmatic form change can happen before a queued Qt
                # result is delivered.  Never let that result restore a
                # manifest for a different source or date range.
                self._operation_snapshot = None
                self._clear_scan(keep_status=True)
                self._set_scan_summary("掃描結果已失效，請依目前條件重新掃描。")
                self._set_status("掃描結果已失效，請依目前條件重新掃描。")
                return
            path, count = self._normalise_export(data)
            total_bytes = self._export_total_bytes(data)
            self._operation_snapshot = None
            self._export_snapshot = dict(snapshot)
            self._export_consumed = False
            self._export_request_pending = False
            if count <= 0:
                self._export_path = ""
                self._export_count = 0
                self._export_bytes = 0
                self._update_download_button()
                self._set_scan_summary(self._format_scan_summary(0, total_bytes))
                self._set_status("掃描完成：符合條件的媒體為 0 個，請調整日期或類型。")
            elif not path:
                self._export_path = ""
                self._export_count = 0
                self._export_bytes = 0
                self._update_download_button()
                self._set_scan_summary("掃描完成但找不到匯出檔案，請重新掃描。")
                self._set_status("掃描完成但找不到匯出檔案，請重試。")
            else:
                self._export_path = path
                self._export_count = count
                self._export_bytes = total_bytes
                self._update_download_button()
                size = f"，約 {_format_bytes(total_bytes)}" if total_bytes > 0 else ""
                status = f"掃描完成：共 {count} 個媒體{size}，可以開始下載。"
                free = _free_bytes(self.folder_edit.text().strip())
                if total_bytes > 0 and free is not None and free < total_bytes:
                    status += f"\n注意：下載資料夾所在磁碟只剩 {_format_bytes(free)}，空間可能不足。"
                self._set_scan_summary(self._format_scan_summary(count, total_bytes))
                self._set_status(status)

    @Slot(int, str)
    def _operation_failed(self, token: int, message: str) -> None:
        if token != self._generation:
            return
        if self._operation_kind == "topics":
            self._set_topic_error(message)
        self._operation_snapshot = None
        self._set_status(f"操作失敗：{_safe_message(message) or '請稍後重試。'}")
        self._clear_scan(keep_status=True)
        self._set_scan_summary("掃描或主題讀取未完成，請依目前條件重試。")

    @Slot(int)
    def _operation_canceled(self, token: int) -> None:
        if token != self._generation:
            return
        if self._operation_kind == "topics":
            self._set_topic_error("主題讀取已取消。")
        self._operation_snapshot = None
        self._clear_scan(keep_status=True)
        self._set_scan_summary("操作已取消；請確認來源與條件後重試。")
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

    @staticmethod
    def _export_total_bytes(data: Any) -> int:
        if not isinstance(data, Mapping):
            return 0
        try:
            return max(0, int(data.get("total_bytes", 0) or 0))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Small visual/state helpers
    # ------------------------------------------------------------------
    def _set_controls_enabled(self, enabled: bool) -> None:
        self.refresh_button.setEnabled(enabled)
        self.account_combo.setEnabled(enabled)
        self.search_edit.setEnabled(enabled)
        self.type_combo.setEnabled(enabled)
        self.clear_search_button.setEnabled(enabled)
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
        for button in self._date_buttons:
            button.setEnabled(enabled)
        self.change_source_button.setEnabled(enabled)
        self.view_queue_button.setEnabled(True)
        if enabled:
            self._update_download_button()
        else:
            self.download_button.setEnabled(False)

    def _set_status(self, text: str) -> None:
        if hasattr(self, "status_label"):
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
        self._operation_snapshot = None
        self._update_current_source_label()
        self._set_scan_summary("尚未掃描；選好來源、日期與媒體類型後開始掃描。")
        if hasattr(self, "scan_button"):
            self.scan_button.setEnabled(False)

    def _clear_scan(self, *_args: Any, keep_status: bool = False) -> None:
        self._export_path = ""
        self._export_count = 0
        self._export_bytes = 0
        self._export_snapshot = None
        self._export_request_pending = False
        self._export_consumed = False
        if hasattr(self, "download_button"):
            self.download_button.setEnabled(False)
            self.download_button.setText("開始下載")
        if not keep_status and hasattr(self, "status_label"):
            # Avoid replacing useful account/chats status while the page is
            # being populated; callers can set a more specific message.
            pass

    def _finish_idle_state(self) -> None:
        self.cancel_button.setVisible(False)
        self._set_controls_enabled(True)


__all__ = ["ChannelPage"]
