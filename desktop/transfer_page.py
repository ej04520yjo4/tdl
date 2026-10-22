"""上傳與轉傳頁。

Wraps the core's ``up`` and ``forward`` commands.  Like the channel page, this
module owns only the Qt widgets and one RunHandle at a time; it talks to the
backend through the small runner contract::

    runner.start_upload(account_id, paths, chat=..., ..., on_event=None)
    runner.start_forward(account_id, sources, to=..., ..., on_event=None)
    runner.store.load_chat_cache(account_id)

Both operations send messages from the user's account, so nothing here starts
on its own: every run is one explicit button press, and a forward can be
rehearsed first with the core's dry-run mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from desktop.channel_page import (
        _account_authenticated,
        _account_id,
        _account_label,
        _Card,
        _chat_kind,
        _chat_ref,
        _chat_title,
        _chat_username,
        _kind_label,
        _RunnerWorker,
        _safe_message,
        _text,
        _thread_running,
    )
except ImportError:  # pragma: no cover - direct ``python desktop/app.py``
    from channel_page import (  # type: ignore[no-redef]
        _account_authenticated,
        _account_id,
        _account_label,
        _Card,
        _chat_kind,
        _chat_ref,
        _chat_title,
        _chat_username,
        _kind_label,
        _RunnerWorker,
        _safe_message,
        _text,
        _thread_running,
    )


_SAVED_MESSAGES = "儲存的訊息（只有自己看得到）"
_STATE_LABELS = {
    "queued": "等待中",
    "uploading": "上傳中",
    "cloning": "複製中",
    "completed": "已完成",
    "failed": "失敗",
}


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


def _confirm(parent: QWidget, title: str, text: str, accept_label: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    accept = box.addButton(accept_label, QMessageBox.AcceptRole)
    cancel = box.addButton("取消", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is accept


class _DestinationCombo(QComboBox):
    """Pick a target chat from the cached list, or type an ID / @username."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_chats([])

    def set_chats(self, chats: Sequence[Any]) -> None:
        typed = "" if self.currentIndex() >= 0 and self.itemText(self.currentIndex()) == self.currentText() else self.currentText()
        selected = self.currentData() if not typed else None
        self.blockSignals(True)
        self.clear()
        self.addItem(_SAVED_MESSAGES, "")
        for chat in chats:
            ref = _chat_ref(chat)
            if not ref:
                continue
            username = _chat_username(chat)
            label = f"{_chat_title(chat)}（{_kind_label(_chat_kind(chat))}）" + (f"  @{username}" if username else "")
            self.addItem(label, ref)
        if typed:
            self.setEditText(typed)
        else:
            index = self.findData(selected) if selected else 0
            self.setCurrentIndex(index if index >= 0 else 0)
        self.blockSignals(False)

    def destination(self) -> str:
        """Return the chat reference, or the raw text the user typed."""

        index = self.currentIndex()
        if index >= 0 and self.itemText(index) == self.currentText():
            return _text(self.itemData(index))
        # ``findText`` covers a label picked through the completer, which
        # changes the text without always moving the current index.
        index = self.findText(self.currentText(), Qt.MatchFixedString)
        if index >= 0:
            return _text(self.itemData(index))
        return self.currentText().strip()

    def label(self) -> str:
        return self.currentText().strip() or _SAVED_MESSAGES


class _ProgressList(QListWidget):
    """One line per transferred item, updated in place by its stable id."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TransferProgress")
        self.setSelectionMode(QListWidget.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(120)
        self.setMaximumHeight(220)
        self._items: dict[str, QListWidgetItem] = {}
        self._names: dict[str, str] = {}
        self.counts = {"completed": 0, "failed": 0}
        self.total = 0

    def start_over(self) -> None:
        # Not named `reset`: that is a QAbstractItemView virtual which Qt
        # itself calls from `clear()`.
        self.clear()
        self._items.clear()
        self._names.clear()
        self.counts = {"completed": 0, "failed": 0}
        self.total = 0

    def apply(self, event: Mapping[str, Any]) -> None:
        file_id = _text(event.get("file_id")).strip()
        if not file_id:
            return
        item = self._items.get(file_id)
        if item is None:
            item = QListWidgetItem()
            self._items[file_id] = item
            self.addItem(item)
        name = _text(event.get("filename")).strip()
        if name:
            self._names[file_id] = name
        name = self._names.get(file_id) or Path(file_id).name or file_id
        state = "failed" if event.get("event") == "error" else _text(event.get("state")).lower()
        previous = item.data(Qt.UserRole)
        if state in self.counts and previous != state:
            self.counts[state] += 1
        item.setData(Qt.UserRole, state)
        detail = _STATE_LABELS.get(state, "處理中")
        if state in {"uploading", "cloning"}:
            try:
                detail = f"{detail} {int(round(float(event.get('percent', 0))))}%"
            except (TypeError, ValueError):
                pass
            size = _format_bytes(event.get("total")) if event.get("total") else ""
            speed = _text(event.get("speed"))
            detail = " · ".join(part for part in (detail, size, speed) if part)
        elif state == "failed":
            detail = f"失敗：{_safe_message(event.get('message')) or '請重試'}"
        item.setText(f"{name}　—　{detail}")
        item.setToolTip(item.text())

    def summary(self) -> str:
        done, failed = self.counts["completed"], self.counts["failed"]
        total = max(self.total, len(self._items))
        text = f"已完成 {done}/{total}"
        return text + (f"，失敗 {failed}" if failed else "")


class _DropList(QListWidget):
    """Upload list that accepts files and folders dragged from Explorer."""

    paths_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)

    @staticmethod
    def _local_paths(event: Any) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]

    def dragEnterEvent(self, event: Any) -> None:  # type: ignore[override]
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # type: ignore[override]
        if self._local_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:  # type: ignore[override]
        paths = self._local_paths(event)
        if paths:
            event.acceptProposedAction()
            self.paths_dropped.emit(paths)
        else:
            event.ignore()


class TransferPage(QWidget):
    """Upload local files and forward messages for the active account."""

    account_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._runner: Any = None
        self._accounts: list[Any] = []
        self._proxy = ""
        self._generation = 0
        self._operation_kind = ""
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunnerWorker] = None
        self._thread_workers: dict[QThread, _RunnerWorker] = {}
        self._forward_files: list[str] = []
        self._build_ui()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 25, 30, 35)
        outer.setSpacing(12)
        kicker = QLabel("上傳與轉傳")
        kicker.setObjectName("Kicker")
        outer.addWidget(kicker)
        title = QLabel("把檔案送上 Telegram，或把訊息轉到別處。")
        title.setObjectName("PageTitle")
        outer.addWidget(title)
        intro = QLabel("這兩項功能會用目前帳號實際送出訊息。不確定時，先送到「儲存的訊息」或使用轉傳的試跑。")
        intro.setObjectName("HeroDescription")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        account_card = _Card()
        account_layout = QHBoxLayout(account_card)
        account_layout.setContentsMargins(18, 15, 18, 15)
        account_layout.setSpacing(12)
        # The account follows the window's top-bar switcher; this combo is
        # hidden state, kept so an upload keeps the account it started with.
        self.account_combo = QComboBox(self)
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        self.account_combo.hide()
        mode_label = QLabel("要做什麼")
        mode_label.setObjectName("FieldLabel")
        account_layout.addWidget(mode_label)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.upload_tab = QPushButton("上傳檔案")
        self.forward_tab = QPushButton("轉傳訊息")
        for index, button in enumerate((self.upload_tab, self.forward_tab)):
            button.setCheckable(True)
            button.setProperty("segment", True)
            self.mode_group.addButton(button, index)
            account_layout.addWidget(button)
        account_layout.addStretch(1)
        self.upload_tab.setChecked(True)
        outer.addWidget(account_card)

        self.destination_hint = QLabel("")
        self.destination_hint.setObjectName("MutedText")
        self.destination_hint.setWordWrap(True)
        outer.addWidget(self.destination_hint)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_upload())
        self.stack.addWidget(self._build_forward())
        self.mode_group.idClicked.connect(self.stack.setCurrentIndex)
        outer.addWidget(self.stack)
        outer.addStretch(1)
        self._set_controls_enabled(True)

    def _build_upload(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        source = _Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(18, 16, 18, 16)
        source_layout.setSpacing(10)
        source_title = QLabel("要上傳的檔案或資料夾（可直接拖進來）")
        source_title.setObjectName("SectionTitle")
        source_layout.addWidget(source_title)
        self.upload_list = _DropList()
        self.upload_list.paths_dropped.connect(self.add_upload_paths)
        self.upload_list.setToolTip("可以直接把檔案或資料夾從檔案總管拖進來")
        self.upload_list.setObjectName("UploadList")
        self.upload_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.upload_list.setMinimumHeight(110)
        self.upload_list.setMaximumHeight(170)
        source_layout.addWidget(self.upload_list)
        buttons = QHBoxLayout()
        self.add_files_button = QPushButton("加入檔案…")
        self.add_files_button.clicked.connect(self._add_upload_files)
        self.add_folder_button = QPushButton("加入資料夾…")
        self.add_folder_button.setToolTip("會上傳資料夾內（含子資料夾）的所有檔案")
        self.add_folder_button.clicked.connect(self._add_upload_folder)
        self.remove_upload_button = QPushButton("移除選取")
        self.remove_upload_button.clicked.connect(self._remove_selected_uploads)
        self.clear_upload_button = QPushButton("清空")
        self.clear_upload_button.clicked.connect(self.upload_list.clear)
        for button in (self.add_files_button, self.add_folder_button, self.remove_upload_button, self.clear_upload_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        source_layout.addLayout(buttons)
        layout.addWidget(source)

        options = _Card()
        form = QFormLayout(options)
        form.setContentsMargins(18, 16, 18, 16)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(11)
        self.upload_destination = _DestinationCombo()
        form.addRow("送到", self.upload_destination)
        self.upload_topic = QLineEdit()
        self.upload_topic.setPlaceholderText("選填：論壇群組的主題編號，例如 12")
        self.upload_topic.setMaximumWidth(320)
        form.addRow("論壇主題", self.upload_topic)
        caption_wrap = QWidget()
        caption_layout = QHBoxLayout(caption_wrap)
        caption_layout.setContentsMargins(0, 0, 0, 0)
        self.caption_combo = QComboBox()
        self.caption_combo.addItem("不加說明", "none")
        self.caption_combo.addItem("使用檔名", "filename")
        self.caption_combo.addItem("自訂文字", "text")
        self.caption_edit = QLineEdit()
        self.caption_edit.setPlaceholderText("每個檔案都會附上這段文字")
        self.caption_edit.setMaxLength(1024)
        self.caption_combo.currentIndexChanged.connect(
            lambda _index: self.caption_edit.setVisible(self.caption_combo.currentData() == "text")
        )
        self.caption_edit.setVisible(False)
        caption_layout.addWidget(self.caption_combo)
        caption_layout.addWidget(self.caption_edit, 1)
        form.addRow("說明文字", caption_wrap)
        self.upload_exclude = QLineEdit()
        self.upload_exclude.setPlaceholderText("選填：不要上傳的副檔名，例如 tmp, part")
        form.addRow("排除副檔名", self.upload_exclude)
        self.photo_check = QCheckBox("圖片以「相片」送出（Telegram 會壓縮；不勾選則以原始檔案送出）")
        form.addRow("", self.photo_check)
        self.remove_check = QCheckBox("上傳成功後刪除本機檔案")
        self.remove_check.setToolTip("只有 Telegram 確實收下的檔案才會刪除；刪除後無法從資源回收筒復原。")
        form.addRow("", self.remove_check)
        layout.addWidget(options)

        action = _Card()
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(18, 16, 18, 16)
        action_layout.setSpacing(10)
        self.upload_status = QLabel("加入檔案後即可開始上傳。")
        self.upload_status.setObjectName("MutedText")
        self.upload_status.setWordWrap(True)
        action_layout.addWidget(self.upload_status)
        self.upload_progress = _ProgressList()
        self.upload_progress.setVisible(False)
        action_layout.addWidget(self.upload_progress)
        row = QHBoxLayout()
        self.upload_cancel = QPushButton("取消上傳")
        self.upload_cancel.clicked.connect(self.cancel_operations)
        self.upload_cancel.setVisible(False)
        row.addWidget(self.upload_cancel)
        row.addStretch(1)
        self.upload_button = QPushButton("開始上傳")
        self.upload_button.setObjectName("PrimaryButton")
        self.upload_button.clicked.connect(self._upload_clicked)
        row.addWidget(self.upload_button)
        action_layout.addLayout(row)
        layout.addWidget(action)
        # The stack is as tall as its tallest page; keep the slack at the
        # bottom rather than spread between the cards.
        layout.addStretch(1)
        return page

    def _build_forward(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        source = _Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(18, 16, 18, 16)
        source_layout.setSpacing(10)
        source_title = QLabel("要轉傳的訊息")
        source_title.setObjectName("SectionTitle")
        source_layout.addWidget(source_title)
        source_hint = QLabel("貼上訊息連結（每行一個），或加入 Telegram Desktop／tdl 匯出的 JSON 清單。")
        source_hint.setObjectName("MutedText")
        source_hint.setWordWrap(True)
        source_layout.addWidget(source_hint)
        self.forward_links = QTextEdit()
        self.forward_links.setObjectName("UrlEdit")
        self.forward_links.setPlaceholderText("https://t.me/channel/123\nhttps://t.me/c/1234567890/45")
        self.forward_links.setMinimumHeight(96)
        self.forward_links.setMaximumHeight(150)
        source_layout.addWidget(self.forward_links)
        file_row = QHBoxLayout()
        self.forward_files_label = QLabel("尚未加入 JSON 清單")
        self.forward_files_label.setObjectName("MutedText")
        file_row.addWidget(self.forward_files_label, 1)
        self.forward_add_file = QPushButton("加入 JSON…")
        self.forward_add_file.clicked.connect(self._add_forward_files)
        self.forward_clear_files = QPushButton("清除 JSON")
        self.forward_clear_files.clicked.connect(self._clear_forward_files)
        file_row.addWidget(self.forward_add_file)
        file_row.addWidget(self.forward_clear_files)
        source_layout.addLayout(file_row)
        layout.addWidget(source)

        options = _Card()
        form = QFormLayout(options)
        form.setContentsMargins(18, 16, 18, 16)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(11)
        self.forward_destination = _DestinationCombo()
        form.addRow("轉傳到", self.forward_destination)
        self.forward_mode = QComboBox()
        self.forward_mode.addItem("直接轉傳（保留「轉傳自」標示；被禁止時自動改用複製）", "direct")
        self.forward_mode.addItem("複製（重新上傳，不顯示來源，可用於禁止轉傳的對話）", "clone")
        form.addRow("方式", self.forward_mode)
        self.silent_check = QCheckBox("靜音送出（對方不會收到通知音）")
        form.addRow("", self.silent_check)
        self.single_check = QCheckBox("只轉傳指定的那一則，不自動帶出同組相簿")
        form.addRow("", self.single_check)
        self.forward_desc_check = QCheckBox("JSON 清單由新到舊轉傳")
        form.addRow("", self.forward_desc_check)
        layout.addWidget(options)

        action = _Card()
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(18, 16, 18, 16)
        action_layout.setSpacing(10)
        self.forward_status = QLabel("加入訊息後，可以先試跑確認，再正式轉傳。")
        self.forward_status.setObjectName("MutedText")
        self.forward_status.setWordWrap(True)
        action_layout.addWidget(self.forward_status)
        self.forward_progress = _ProgressList()
        self.forward_progress.setVisible(False)
        action_layout.addWidget(self.forward_progress)
        row = QHBoxLayout()
        self.forward_cancel = QPushButton("取消轉傳")
        self.forward_cancel.clicked.connect(self.cancel_operations)
        self.forward_cancel.setVisible(False)
        row.addWidget(self.forward_cancel)
        row.addStretch(1)
        self.dry_run_button = QPushButton("先試跑（不會送出）")
        self.dry_run_button.setToolTip("讀取來源並走完流程，但不會在目的地送出任何訊息")
        self.dry_run_button.clicked.connect(lambda: self._forward_clicked(dry_run=True))
        row.addWidget(self.dry_run_button)
        self.forward_button = QPushButton("開始轉傳")
        self.forward_button.setObjectName("PrimaryButton")
        self.forward_button.clicked.connect(lambda: self._forward_clicked(dry_run=False))
        row.addWidget(self.forward_button)
        action_layout.addLayout(row)
        layout.addWidget(action)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # Public integration surface
    # ------------------------------------------------------------------
    def set_runner(self, runner: Any) -> None:
        self._runner = runner

    def set_proxy(self, proxy: str) -> None:
        self._proxy = _text(proxy)

    def set_accounts(self, accounts: Sequence[Any], active_id: str = "") -> None:
        self._accounts = list(accounts)
        current = active_id or self.active_account_id()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in self._accounts:
            state = "已連線" if _account_authenticated(account) else "未完成登入"
            self.account_combo.addItem(f"{_account_label(account)} · {state}", _account_id(account))
        if not self._accounts:
            self.account_combo.addItem("尚未登入 Telegram")
        else:
            index = self.account_combo.findData(current)
            self.account_combo.setCurrentIndex(index if index >= 0 else 0)
        self.account_combo.blockSignals(False)
        self.reload_destinations()
        if not self.is_busy():
            self._set_controls_enabled(True)

    def active_account_id(self) -> str:
        return _text(self.account_combo.currentData())

    def is_busy(self) -> bool:
        return _thread_running(self._thread) or any(_thread_running(thread) for thread in self._thread_workers)

    def cancel_operations(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()

    def shutdown(self, timeout_ms: int = 7000) -> None:
        self.cancel_operations()
        for thread in list(self._thread_workers):
            if _thread_running(thread):
                thread.quit()
                thread.wait(timeout_ms)

    def reload_destinations(self) -> None:
        """Fill both target pickers from the active account's cached chats."""

        chats: list[Any] = []
        saved_at = ""
        store = getattr(self._runner, "store", None)
        loader = getattr(store, "load_chat_cache", None)
        account_id = self.active_account_id()
        if callable(loader) and account_id:
            try:
                chats, saved_at = loader(account_id)
            except Exception:
                chats, saved_at = [], ""
        self.upload_destination.set_chats(chats)
        self.forward_destination.set_chats(chats)
        if chats:
            self.destination_hint.setText(
                f"目的地清單共 {len(chats)} 個對話（{saved_at[:10] or '先前'} 更新）。也可以直接輸入 @username 或數字 ID。"
            )
        else:
            self.destination_hint.setText(
                "尚無對話清單：到「頻道下載」按一次「更新清單」後，這裡就能直接選擇目的地；也可以輸入 @username 或數字 ID。"
            )

    def showEvent(self, event: Any) -> None:  # type: ignore[override]
        super().showEvent(event)
        # The channel page may have refreshed the list since the last visit.
        if not self.is_busy():
            self.reload_destinations()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------
    def _account_changed(self, _index: int) -> None:
        self.reload_destinations()
        self._set_controls_enabled(not self.is_busy())
        self.account_changed.emit(self.active_account_id())

    def upload_paths(self) -> list[str]:
        return [self.upload_list.item(row).data(Qt.UserRole) for row in range(self.upload_list.count())]

    def add_upload_paths(self, paths: Sequence[str]) -> None:
        existing = set(self.upload_paths())
        for raw in paths:
            path = str(Path(raw))
            if not raw or path in existing:
                continue
            existing.add(path)
            item = QListWidgetItem(("📁 " if Path(path).is_dir() else "") + path)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.upload_list.addItem(item)

    def _add_upload_files(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(self, "選擇要上傳的檔案", str(Path.home()))
        self.add_upload_paths(paths)

    def _add_upload_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "選擇要上傳的資料夾", str(Path.home()))
        if path:
            self.add_upload_paths([path])

    def _remove_selected_uploads(self) -> None:
        for item in self.upload_list.selectedItems():
            self.upload_list.takeItem(self.upload_list.row(item))

    def _add_forward_files(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(self, "選擇匯出的 JSON 清單", str(Path.home()), "JSON 清單 (*.json)")
        for path in paths:
            if path and path not in self._forward_files:
                self._forward_files.append(path)
        self._update_forward_files_label()

    def _clear_forward_files(self) -> None:
        self._forward_files.clear()
        self._update_forward_files_label()

    def _update_forward_files_label(self) -> None:
        if not self._forward_files:
            self.forward_files_label.setText("尚未加入 JSON 清單")
            return
        names = "、".join(Path(path).name for path in self._forward_files[:3])
        more = f" 等 {len(self._forward_files)} 個" if len(self._forward_files) > 3 else ""
        self.forward_files_label.setText(f"JSON 清單：{names}{more}")

    def forward_sources(self) -> list[str]:
        links: list[str] = []
        for line in self.forward_links.toPlainText().replace(",", "\n").replace("，", "\n").splitlines():
            value = line.strip()
            if not value:
                continue
            if not value.lower().startswith("http"):
                value = "https://" + value
            if value not in links:
                links.append(value)
        return links + list(self._forward_files)

    def _ready_account(self, status: QLabel) -> str:
        account_id = self.active_account_id()
        account = next((item for item in self._accounts if _account_id(item) == account_id), None)
        if not account_id or account is None:
            status.setText("請先到「帳號管理」新增並登入一個 Telegram 帳號。")
            return ""
        if not _account_authenticated(account):
            status.setText("目前帳號尚未登入，請先完成 Telegram 登入。")
            return ""
        return account_id

    def _upload_clicked(self) -> None:
        account_id = self._ready_account(self.upload_status)
        if not account_id:
            return
        paths = self.upload_paths()
        if not paths:
            self.upload_status.setText("請先加入要上傳的檔案或資料夾。")
            return
        topic = self.upload_topic.text().strip()
        if topic and not topic.isdigit():
            self.upload_status.setText("論壇主題必須是數字編號。")
            return
        destination = self.upload_destination.destination()
        if topic and not destination:
            self.upload_status.setText("指定論壇主題時，請先選擇目的地群組。")
            return
        if self.remove_check.isChecked() and not _confirm(
            self,
            "上傳後刪除本機檔案",
            f"上傳成功的檔案會從這台電腦永久刪除（共 {len(paths)} 個項目，資料夾會包含其中所有檔案）。\n\n確定要繼續嗎？",
            "上傳並刪除",
        ):
            return
        kwargs: dict[str, Any] = {
            "chat": destination,
            "topic_id": topic or None,
            "as_photo": self.photo_check.isChecked(),
            "remove_after": self.remove_check.isChecked(),
            "caption_mode": self.caption_combo.currentData(),
            "caption_text": self.caption_edit.text(),
            "exclude": self.upload_exclude.text(),
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self.upload_progress.start_over()
        self.upload_progress.setVisible(True)
        self.upload_status.setText(f"正在準備上傳到「{self.upload_destination.label()}」…")
        self._start("upload", "start_upload", (account_id, paths), kwargs)

    def _forward_clicked(self, *, dry_run: bool) -> None:
        account_id = self._ready_account(self.forward_status)
        if not account_id:
            return
        sources = self.forward_sources()
        if not sources:
            self.forward_status.setText("請先貼上訊息連結，或加入 JSON 清單。")
            return
        kwargs: dict[str, Any] = {
            "to": self.forward_destination.destination(),
            "mode": self.forward_mode.currentData(),
            "silent": self.silent_check.isChecked(),
            "dry_run": dry_run,
            "single": self.single_check.isChecked(),
            "desc": self.forward_desc_check.isChecked(),
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self.forward_progress.start_over()
        self.forward_progress.setVisible(True)
        target = self.forward_destination.label()
        self.forward_status.setText(
            f"正在試跑：不會送出任何訊息（目的地「{target}」）…" if dry_run else f"正在轉傳到「{target}」…"
        )
        self._start("dry-run" if dry_run else "forward", "start_forward", (account_id, sources), kwargs)

    # ------------------------------------------------------------------
    # Backend operation lifecycle
    # ------------------------------------------------------------------
    def _start(self, kind: str, method: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
        if self.is_busy():
            return
        if not callable(getattr(self._runner, method, None)):
            self._status_label(kind).setText("目前後端尚未提供這項功能。")
            return
        self._generation += 1
        thread = QThread(self)
        worker = _RunnerWorker(self._runner, method, args, kwargs, token=self._generation, kind=kind)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Real @Slot receivers with queued connections: a lambda would run in
        # the worker thread and touch widgets from there.
        worker.event.connect(self._operation_event, Qt.QueuedConnection)
        worker.succeeded.connect(self._operation_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._operation_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._operation_canceled, Qt.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._operation_thread_finished, Qt.QueuedConnection)
        thread.finished.connect(thread.deleteLater)
        self._thread_workers[thread] = worker
        self._thread = thread
        self._worker = worker
        self._operation_kind = kind
        self._set_controls_enabled(False)
        thread.start()

    def _status_label(self, kind: str) -> QLabel:
        return self.upload_status if kind == "upload" else self.forward_status

    def _progress_list(self, kind: str) -> _ProgressList:
        return self.upload_progress if kind == "upload" else self.forward_progress

    @Slot()
    def _operation_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        worker = self._thread_workers.pop(thread, None)
        if self._thread is thread and self._worker is worker:
            self._thread = None
            self._worker = None
            self._set_controls_enabled(True)

    @Slot(int, object)
    def _operation_event(self, token: int, event: Any) -> None:
        if token != self._generation or not isinstance(event, Mapping):
            return
        kind = self._operation_kind
        name = _text(event.get("event")).lower()
        progress = self._progress_list(kind)
        if name in {"progress", "error"} and event.get("file_id"):
            progress.apply(event)
            self._status_label(kind).setText(self._running_text(kind, progress))
        elif name == "status":
            try:
                progress.total = max(progress.total, int(event.get("count", 0) or 0))
            except (TypeError, ValueError):
                pass
            message = _text(event.get("message"))
            if message:
                self._status_label(kind).setText(_safe_message(message))

    @staticmethod
    def _running_text(kind: str, progress: _ProgressList) -> str:
        verb = {"upload": "上傳中", "forward": "轉傳中", "dry-run": "試跑中"}.get(kind, "處理中")
        return f"{verb}：{progress.summary()}"

    @Slot(int, str, object)
    def _operation_succeeded(self, token: int, kind: str, _data: Any) -> None:
        if token != self._generation:
            return
        summary = self._progress_list(kind).summary()
        if kind == "upload":
            self.upload_status.setText(f"上傳完成：{summary}。")
            self.upload_list.clear()
        elif kind == "dry-run":
            self.forward_status.setText(f"試跑完成：{summary}，沒有送出任何訊息。確認無誤後可按「開始轉傳」。")
        else:
            self.forward_status.setText(f"轉傳完成：{summary}。")

    @Slot(int, str)
    def _operation_failed(self, token: int, message: str) -> None:
        if token != self._generation:
            return
        kind = self._operation_kind
        summary = self._progress_list(kind).summary()
        verb = {"upload": "上傳", "forward": "轉傳", "dry-run": "試跑"}.get(kind, "操作")
        self._status_label(kind).setText(
            f"{verb}未全部完成（{summary}）：{_safe_message(message) or '請稍後重試。'}"
        )

    @Slot(int)
    def _operation_canceled(self, token: int) -> None:
        if token != self._generation:
            return
        kind = self._operation_kind
        self._status_label(kind).setText(f"已取消（{self._progress_list(kind).summary()}）。已送出的項目不會收回。")

    def _set_controls_enabled(self, enabled: bool) -> None:
        busy = not enabled
        self.account_combo.setEnabled(enabled)
        for widget in (
            self.upload_list, self.add_files_button, self.add_folder_button, self.remove_upload_button,
            self.clear_upload_button, self.upload_destination, self.upload_topic, self.caption_combo,
            self.caption_edit, self.upload_exclude, self.photo_check, self.remove_check, self.upload_button,
            self.forward_links, self.forward_add_file, self.forward_clear_files, self.forward_destination,
            self.forward_mode, self.silent_check, self.single_check, self.forward_desc_check,
            self.dry_run_button, self.forward_button,
        ):
            widget.setEnabled(enabled)
        self.upload_cancel.setVisible(busy and self._operation_kind == "upload")
        self.forward_cancel.setVisible(busy and self._operation_kind in {"forward", "dry-run"})


__all__ = ["TransferPage"]
