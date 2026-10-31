"""群組／頻道批次轉傳頁。

This page deliberately builds on :class:`ChannelPage` instead of duplicating
the Telegram source selector.  The source is scanned first and the resulting
manifest is then sent to an explicitly selected destination.  A manifest is
bound to the account, source, topic, date range, media types and destination;
changing any of those inputs invalidates it.  This is important because a
forward is an outbound action and cannot be silently repeated after a late
worker result or a nested confirmation dialog.

The backend contract is intentionally small::

    runner.start_batch_forward(
        account_id, manifest_path, *, source_chat, to,
        manifest_sha256, expected_count, topic_id=None,
        mode="direct", silent=False, dry_run=False, proxy=None,
        on_event=None,
    ) -> RunHandle

The page does not import ``transfer_page`` or the main window.  The parent
window can therefore embed it in the transfer tab without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Optional

from PySide6.QtCore import QThread, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    from .channel_page import (
        ChannelPage,
        _Card,
        _MEDIA_LABELS,
        _account_label,
        _chat_id,
        _chat_is_forum,
        _chat_kind,
        _chat_ref,
        _chat_title,
        _date_text,
        _kind_label,
        _safe_message,
        _text,
        _value,
        _topic_display_title,
        _RunnerWorker,
    )
    from .search import normalize_search_text, search_matches
except ImportError:  # pragma: no cover - direct ``python desktop/app.py``
    from channel_page import (  # type: ignore[no-redef]
        ChannelPage,
        _Card,
        _MEDIA_LABELS,
        _account_label,
        _chat_id,
        _chat_is_forum,
        _chat_kind,
        _chat_ref,
        _chat_title,
        _date_text,
        _kind_label,
        _safe_message,
        _text,
        _value,
        _topic_display_title,
        _RunnerWorker,
    )
    from search import normalize_search_text, search_matches  # type: ignore[no-redef]


def _confirm_batch(parent: QWidget, snapshot: Mapping[str, Any]) -> bool:
    """Ask for an explicit final confirmation with Chinese button text."""

    source = _text(snapshot.get("source_title"), "來源").strip()
    target = _text(snapshot.get("target_title"), "目的地").strip()
    topic = _text(snapshot.get("source_topic_title")).strip()
    target_topic = _text(snapshot.get("target_topic_title")).strip()
    account = _text(snapshot.get("account_label"), "目前帳號").strip()
    start_date = _text(snapshot.get("start_date"), "").strip()
    end_date = _text(snapshot.get("end_date"), "").strip()
    media = "、".join(_MEDIA_LABELS.get(item, item) for item in snapshot.get("media_types", ())) or "未選類型"
    count = int(snapshot.get("expected_count", 0) or 0)
    source_text = f"{source} · 話題：{topic}" if topic else source
    target_text = f"{target} · 話題：{target_topic}" if target_topic else target
    message = (
        f"將把 {source_text} 的 {count} 則符合條件的媒體訊息，\n"
        f"批次轉傳到「{target_text}」。\n"
        f"帳號：{account}\n日期：{start_date}～{end_date}\n媒體：{media}\n\n"
        "訊息送出後不會自動收回；若目的地或日期不對，請先取消。"
    )
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("確認批次轉傳")
    box.setText(message)
    accept = box.addButton("確認批次轉傳", QMessageBox.AcceptRole)
    cancel = box.addButton("取消", QMessageBox.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is accept


class _CompactDateEdit(QDateEdit):
    """Native date editor with a compact size hint for the transfer tab."""

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        base = super().minimumSizeHint()
        return QSize(min(base.width(), 140), base.height())

    def sizeHint(self) -> QSize:  # type: ignore[override]
        base = super().sizeHint()
        return QSize(min(base.width(), 140), base.height())


class BatchForwardPage(ChannelPage):
    """Source scan + explicit destination + batch-forward workflow."""

    busy_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        # ``ChannelPage.__init__`` calls ``self._build_ui``.  Initialize every
        # subclass field used by the polymorphic setup methods first.
        self._batch_busy_last = False
        self._all_target_chats: list[Any] = []
        self._target_chat: Any = None
        self._target_topics: list[dict[str, str]] = []
        self._target_topic: Optional[dict[str, str]] = None
        self._target_topics_loaded = False
        self._target_topics_loading = False
        self._target_operation_token = 0
        self._batch_scan_snapshot: Optional[dict[str, Any]] = None
        self._batch_manifest_sha256 = ""
        self._batch_actual_started = False
        self._batch_dry_run = False
        self._batch_terminal = False
        self._batch_progress_items: dict[str, QListWidgetItem] = {}
        self._batch_progress_sequence = 0
        self._proxy = ""
        super().__init__(parent)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        super()._build_ui()

        # A batch forward writes to Telegram and does not use the local
        # download destination.  Hide (rather than delete) the inherited
        # folder controls so ChannelPage's source layout and stylesheet stay
        # intact.
        for widget in (getattr(self, "folder_edit", None), getattr(self, "folder_button", None)):
            if widget is not None:
                widget.hide()
        for label in self.findChildren(QLabel):
            if label.text() in {"儲存位置", "檔案會按群組／頻道名稱自動分資料夾。"}:
                label.hide()

        self.scan_button.setText("① 掃描符合訊息")
        self.scan_button.setToolTip("先查詢符合來源、日期、話題與媒體類型的訊息；這一步不會送出")
        self.download_button.setText("② 批次轉傳")
        self.download_button.setToolTip("確認目的地後送出整份掃描結果；送出後不會自動收回")
        for label in self.findChildren(QLabel):
            if label.objectName() == "PageTitle":
                label.setText("選來源，先預覽，再確認轉傳。")
            elif label.objectName() == "HeroDescription":
                label.setText("選來源、日期與媒體，再選目的群組；先掃描與試跑，最後明確送出。")
            elif label.objectName() == "SectionTitle" and label.text() == "下載條件":
                label.setText("來源條件")
        # A batch forward is not a local download job.  These queue controls
        # belong to ChannelPage's download workflow and would otherwise make
        # the destination page look as if it accepted a download manifest.
        self.queue_status_label.hide()
        self.view_queue_button.hide()
        # The inherited date editors size themselves for the native calendar
        # button and make the embedded 960 px transfer page unnecessarily
        # wide.  A fixed, still-readable width keeps the entire workflow
        # inside the scroll area's viewport without a horizontal scrollbar.
        for date_editor in (self.start_date, self.end_date):
            date_editor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            date_editor.setFixedWidth(140)

        self._target_card = _Card(self)
        target_layout = QVBoxLayout(self._target_card)
        target_layout.setContentsMargins(12, 10, 12, 10)
        target_layout.setSpacing(6)

        target_title = QLabel("轉傳目的地（請明確選擇）")
        target_title.setObjectName("SectionTitle")
        target_layout.addWidget(target_title)

        self.target_group_search = QLineEdit()
        self.target_group_search.setPlaceholderText("搜尋自己的群組、頻道或 username")
        self.target_group_search.setClearButtonEnabled(True)
        self.target_group_search.setToolTip("只在已載入的對話清單中搜尋，不會自動選擇目的地")
        self.target_group_search.textChanged.connect(self._target_search_changed)
        target_layout.addWidget(self.target_group_search)

        group_row = QHBoxLayout()
        group_label = QLabel("目標群組／頻道")
        group_label.setObjectName("FieldLabel")
        group_row.addWidget(group_label)
        self.target_group_combo = QComboBox()
        self.target_group_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.target_group_combo.setMinimumWidth(0)
        self.target_group_combo.currentIndexChanged.connect(self._target_group_changed)
        group_row.addWidget(self.target_group_combo, 1)
        target_layout.addLayout(group_row)

        # Descriptive aliases keep the embedding/probe contract readable and
        # avoid coupling callers to the internal ``target_*`` naming.
        self.destination_combo = self.target_group_combo

        self.target_topic_row = QWidget()
        target_topic_layout = QHBoxLayout(self.target_topic_row)
        target_topic_layout.setContentsMargins(0, 0, 0, 0)
        target_topic_layout.setSpacing(6)
        topic_label = QLabel("目的話題")
        topic_label.setObjectName("FieldLabel")
        target_topic_layout.addWidget(topic_label)
        self.target_topic_combo = QComboBox()
        self.target_topic_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.target_topic_combo.setMinimumWidth(0)
        self.target_topic_combo.currentIndexChanged.connect(self._target_topic_changed)
        target_topic_layout.addWidget(self.target_topic_combo, 1)
        self.destination_topic_combo = self.target_topic_combo
        self.target_topic_row.hide()
        target_layout.addWidget(self.target_topic_row)

        options_row = QHBoxLayout()
        mode_label = QLabel("轉傳方式")
        mode_label.setObjectName("FieldLabel")
        options_row.addWidget(mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("直接轉傳（保留來源標記）", "direct")
        self.mode_combo.addItem("複製訊息（建立目的地副本）", "clone")
        self.mode_combo.setFixedWidth(180)
        self.mode_combo.setToolTip("由核心依方式處理；媒體篩選仍以掃描結果為準")
        options_row.addWidget(self.mode_combo, 1)
        self.silent_check = QCheckBox("不發送通知")
        self.silent_check.setToolTip("若 Telegram 與目的地支援，轉傳時以靜默方式送出")
        options_row.addWidget(self.silent_check)
        target_layout.addLayout(options_row)

        self.target_hint = QLabel("請選擇目的地；不會預設來源或目前帳號的任何群組。")
        self.target_hint.setObjectName("MutedText")
        self.target_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.target_hint.setWordWrap(True)
        target_layout.addWidget(self.target_hint)

        self.batch_progress = QListWidget()
        self.batch_progress.setObjectName("BatchForwardProgress")
        self.batch_progress.setMinimumHeight(70)
        self.batch_progress.setMaximumHeight(136)
        self.batch_progress.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.batch_progress.setTextElideMode(Qt.ElideRight)
        self.batch_progress.setWordWrap(False)
        self.batch_progress.setToolTip("轉傳進度；可捲動查看已回報的項目")
        self.batch_progress.hide()
        target_layout.addWidget(self.batch_progress)

        dry_row = QHBoxLayout()
        self.dry_run_button = QPushButton("先試跑（不會送出）")
        self.dry_run_button.setObjectName("SecondaryButton")
        self.dry_run_button.setToolTip("驗證目的地與清單流程，不會送出任何訊息")
        self.dry_run_button.clicked.connect(lambda: self._batch_clicked(dry_run=True))
        dry_row.addWidget(self.dry_run_button)
        dry_row.addStretch(1)
        target_layout.addLayout(dry_row)

        self.preview_label = self.scan_summary_label
        self.forward_button = self.download_button

        # Find the right column created by ChannelPage and place this card
        # above the date/media card.  This keeps the source list and all batch
        # actions in one familiar workflow while remaining responsive inside
        # the main window's scroll area.
        outer = self.layout()
        workflow = outer.itemAt(outer.count() - 1).layout() if outer.count() else None
        right_column = workflow.itemAt(1).layout() if workflow is not None and workflow.count() > 1 else None
        if right_column is not None:
            right_column.insertWidget(1, self._target_card)
            self._replace_date_editor(right_column, "start_date")
            self._replace_date_editor(right_column, "end_date")
            self._compact_date_filter_layout(right_column)
        else:  # pragma: no cover - defensive for future ChannelPage layout
            outer.addWidget(self._target_card)

        self._populate_target_combo()
        self._update_target_controls()
        self.setMinimumWidth(0)

    def _replace_date_editor(self, right_column: Any, attribute: str) -> None:
        """Replace ChannelPage's wide native editor without losing its value."""

        old = getattr(self, attribute, None)
        if not isinstance(old, QDateEdit) or right_column is None or right_column.count() < 3:
            return
        filters_card = right_column.itemAt(2).widget()
        if filters_card is None or filters_card.layout() is None:
            return
        date_item = filters_card.layout().itemAt(1)
        grid = date_item.layout() if date_item is not None else None
        if grid is None:
            return
        position: Optional[tuple[int, int, int, int]] = None
        for index in range(grid.count()):
            item = grid.itemAt(index)
            if item.widget() is old:
                position = grid.getItemPosition(index)
                grid.takeAt(index)
                break
        if position is None:
            return
        compact = _CompactDateEdit(self)
        compact.setCalendarPopup(old.calendarPopup())
        compact.setDisplayFormat(old.displayFormat())
        compact.setDate(old.date())
        compact.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        compact.setFixedWidth(140)
        compact.dateChanged.connect(self._selection_inputs_changed)
        setattr(self, attribute, compact)
        row, column, row_span, column_span = position
        grid.addWidget(compact, row, column, row_span, column_span)
        old.hide()
        old.setParent(None)
        old.deleteLater()

    def _compact_date_filter_layout(self, right_column: Any) -> None:
        """Stack date editors at narrow widths to avoid horizontal scrolling.

        Qt's native ``QDateEdit`` minimum hint includes a wide calendar
        button.  Keeping two of them side-by-side makes the *whole* transfer
        page wider than a 960 px window even though the fields themselves are
        short.  A compact vertical date pair costs only a few pixels of
        height, and the surrounding scroll area already handles vertical
        content naturally.
        """

        if right_column is None or right_column.count() < 3:
            return
        filters_card = right_column.itemAt(2).widget()
        if filters_card is None or filters_card.layout() is None:
            return
        filters_layout = filters_card.layout()
        date_item = filters_layout.itemAt(1)
        grid = date_item.layout() if date_item is not None else None
        if grid is None:
            return
        widgets: list[QWidget] = []
        shortcut_layout: Any = None
        while grid.count():
            item = grid.takeAt(0)
            if item.widget() is not None:
                widgets.append(item.widget())
            elif item.layout() is not None:
                shortcut_layout = item.layout()
        start_label = next((widget for widget in widgets if widget.text().startswith("起始")), None)
        end_label = next((widget for widget in widgets if widget.text().startswith("結束")), None)
        editors = [widget for widget in widgets if widget in {self.start_date, self.end_date}]
        if start_label is None or end_label is None or len(editors) != 2:
            return
        grid.addWidget(start_label, 0, 0, 1, 2)
        grid.addWidget(self.start_date, 1, 0, 1, 2)
        grid.addWidget(end_label, 2, 0, 1, 2)
        grid.addWidget(self.end_date, 3, 0, 1, 2)
        if shortcut_layout is not None:
            # Three buttons per row keep the card narrow while retaining all
            # of the useful Taiwan-date shortcuts.
            shortcut_grid = QGridLayout()
            shortcut_grid.setHorizontalSpacing(6)
            shortcut_grid.setVerticalSpacing(4)
            buttons: list[QPushButton] = []
            while shortcut_layout.count():
                item = shortcut_layout.takeAt(0)
                if item.widget() is not None:
                    buttons.append(item.widget())
            for index, button in enumerate(buttons):
                shortcut_grid.addWidget(button, index // 3, index % 3)
            grid.addLayout(shortcut_grid, 4, 0, 2, 2)

    # ------------------------------------------------------------------
    # Public integration surface
    # ------------------------------------------------------------------
    def set_runner(self, runner: Any) -> None:
        super().set_runner(runner)

    def set_proxy(self, proxy: str) -> None:
        self._proxy = _text(proxy).strip()

    def set_accounts(self, accounts: Sequence[Any], active_id: str = "") -> None:
        old_account = self.active_account_id() if hasattr(self, "account_combo") else ""
        super().set_accounts(accounts, active_id)
        new_account = self.active_account_id()
        if old_account != new_account:
            # ``_chats`` is cleared by ChannelPage on an account switch.  Do
            # not let the destination cache from the previous account become
            # authoritative while the new account list is still empty.
            self._all_target_chats = []
            self._clear_target_selection(clear_search=False)
        self._populate_target_combo()
        self._update_scan_button()
        self._update_download_button()

    def is_busy(self) -> bool:
        return super().is_busy()

    def shutdown(self, timeout_ms: int = 7000) -> None:
        super().shutdown(timeout_ms=timeout_ms)
        self._emit_busy_changed()

    # ------------------------------------------------------------------
    # Destination selection
    # ------------------------------------------------------------------
    def _apply_filter(self, *_args: Any) -> None:
        super()._apply_filter(*_args)
        if hasattr(self, "target_group_combo"):
            self._populate_target_combo()

    def _populate_target_combo(self) -> None:
        if not hasattr(self, "target_group_combo"):
            return
        current_ref = _chat_ref(self._target_chat) if self._target_chat is not None else ""
        query = normalize_search_text(self.target_group_search.text()) if hasattr(self, "target_group_search") else ""
        chats: list[Any] = []
        seen: set[str] = set()
        # ``_chats`` is authoritative.  An explicit empty response from the
        # backend must clear destinations rather than resurrecting another
        # account's cached rows from ``_all_target_chats``.
        all_chats = list(self._chats)
        for chat in all_chats:
            ref = _chat_ref(chat)
            if not self._has_typed_chat_ref(chat) or ref in seen:
                continue
            if query and not search_matches(query, _chat_title(chat), _chat_username(chat), ref, _chat_id(chat)):
                continue
            seen.add(ref)
            chats.append(chat)
        self._all_target_chats = all_chats
        self.target_group_combo.blockSignals(True)
        self.target_group_combo.clear()
        self.target_group_combo.addItem("請選擇目的地（不會自動選擇）", None)
        for chat in chats:
            label = f"{_chat_title(chat)}（{_kind_label(_chat_kind(chat))}）"
            username = _text(_value(chat, "username", "user_name", default="")).strip().lstrip("@")
            if username:
                label += f"  @{username}"
            item = self.target_group_combo.count()
            self.target_group_combo.addItem(label, chat)
            self.target_group_combo.setItemData(item, chat, Qt.UserRole)
        restored = self.target_group_combo.findData(self._target_chat, Qt.UserRole)
        if current_ref:
            for index in range(1, self.target_group_combo.count()):
                if _chat_ref(self.target_group_combo.itemData(index, Qt.UserRole)) == current_ref:
                    restored = index
                    break
        if restored < 0:
            restored = 0
        self.target_group_combo.setCurrentIndex(restored)
        self.target_group_combo.blockSignals(False)
        # A filter may hide the previously selected destination.  Do not keep
        # sending to an invisible item; invalidate its manifest and require a
        # deliberate re-selection.
        if current_ref and restored == 0:
            self._clear_target_selection(clear_search=False)
        self._update_target_controls()

    def _target_search_changed(self, _text_value: str) -> None:
        self._populate_target_combo()

    def _clear_target_selection(self, *, clear_search: bool = False) -> None:
        self._target_chat = None
        self._target_topic = None
        self._target_topics = []
        self._target_topics_loaded = False
        self._target_topics_loading = False
        self._target_operation_token = 0
        if hasattr(self, "target_group_combo"):
            self.target_group_combo.blockSignals(True)
            if self.target_group_combo.count():
                self.target_group_combo.setCurrentIndex(0)
            self.target_group_combo.blockSignals(False)
        if hasattr(self, "target_topic_combo"):
            self.target_topic_combo.blockSignals(True)
            self.target_topic_combo.clear()
            self.target_topic_combo.addItem("請先選擇目的地", None)
            self.target_topic_combo.blockSignals(False)
        if hasattr(self, "target_topic_row"):
            self.target_topic_row.hide()
        if clear_search and hasattr(self, "target_group_search"):
            self.target_group_search.clear()
        self._invalidate_batch_scan("目的地已清除，請重新選擇目的地。", cancel_operation=False)
        self._update_target_controls()

    def _target_group_changed(self, index: int) -> None:
        if not hasattr(self, "target_group_combo"):
            return
        target = self.target_group_combo.itemData(index, Qt.UserRole)
        self._target_chat = target if isinstance(target, Mapping) else None
        self._target_topic = None
        self._target_topics = []
        self._target_topics_loaded = False
        self._target_topics_loading = False
        self._invalidate_batch_scan("目的地已變更，請重新掃描。", cancel_operation=False)
        if self._target_chat is None:
            self.target_topic_row.hide()
            self.target_hint.setText("請選擇目的地；不會預設來源或目前帳號的任何群組。")
            self._update_target_controls()
            return
        if self._same_chat(self._selected_chat, self._target_chat):
            self.target_hint.setText("目的地不可與來源相同，請改選自己的另一個群組或頻道。")
            self.target_topic_row.hide()
            self._update_target_controls()
            return
        if _chat_is_forum(self._target_chat):
            self.target_topic_row.show()
            self.target_topic_combo.blockSignals(True)
            self.target_topic_combo.clear()
            self.target_topic_combo.addItem("正在載入目的話題…", None)
            self.target_topic_combo.blockSignals(False)
            self._target_topics_loading = True
            self.target_hint.setText("目的地是論壇群組，正在讀取可選話題…")
            account_id = self.active_account_id()
            method = getattr(self._runner, "start_list_topics", None)
            if not account_id or not callable(method):
                self._target_topics_loading = False
                self._target_topics_loaded = False
                self.target_hint.setText("無法讀取目的話題；請更新清單或改選一般群組。")
                self._update_target_controls()
                return
            kwargs = {"proxy": self._proxy} if self._proxy else {}
            self._start_operation("target_topics", "start_list_topics", (account_id, _chat_ref(self._target_chat)), kwargs)
        else:
            self._target_topics_loaded = True
            self.target_hint.setText("已選擇目的地；掃描來源後可先試跑，再確認送出。")
            self.target_topic_row.hide()
            self._update_target_controls()

    def _target_topic_changed(self, index: int) -> None:
        if not hasattr(self, "target_topic_combo"):
            return
        value = self.target_topic_combo.itemData(index, Qt.UserRole)
        self._target_topic = dict(value) if isinstance(value, Mapping) else None
        self._invalidate_batch_scan("目的話題已變更，請重新掃描。", cancel_operation=False)
        if self._target_chat is not None and _chat_is_forum(self._target_chat):
            if self._target_topic is None:
                self.target_hint.setText("請明確選擇目的論壇話題（包含「一般」）。")
                self._set_status("請明確選擇目的論壇話題（包含「一般」）。")
            else:
                title = _text(self._target_topic.get("title"), "未命名話題").strip()
                self.target_hint.setText(f"目的話題：{title}")
                self._set_status(f"目的話題：{title}。")
        self._update_target_controls()

    @staticmethod
    def _same_chat(source: Any, target: Any) -> bool:
        if source is None or target is None:
            return False
        source_ref = _chat_ref(source)
        target_ref = _chat_ref(target)
        if source_ref and target_ref:
            return source_ref == target_ref
        source_id = _chat_id(source)
        target_id = _chat_id(target)
        return bool(source_id and target_id and source_id == target_id)

    @staticmethod
    def _has_typed_chat_ref(chat: Any) -> bool:
        """Require the backend's typed peer identity, never a bare ID."""

        ref = _text(_value(chat, "ref", "chat_ref", "reference", default="")).strip()
        return bool(re.fullmatch(r"(?:channel|chat|group):[1-9][0-9]*", ref, flags=re.IGNORECASE))

    def _target_topic_id(self) -> Optional[str]:
        if self._target_topic is None:
            return None
        value = _text(self._target_topic.get("id", "")).strip()
        return value or None

    # ------------------------------------------------------------------
    # Snapshot / validation
    # ------------------------------------------------------------------
    def _current_batch_snapshot(self) -> Optional[dict[str, Any]]:
        source = self._current_export_snapshot()
        if source is None or self._target_chat is None or self._same_chat(self._selected_chat, self._target_chat):
            return None
        topic_id = self._target_topic_id()
        target_title = _chat_title(self._target_chat)
        account_label = self.active_account_id()
        for account in self._accounts:
            if _text(_value(account, "id", "account_id", "key", default="")) == account_label:
                account_label = _account_label(account)
                break
        return {
            **source,
            "account_label": account_label,
            "source_ref": _chat_ref(self._selected_chat),
            "source_id": _chat_id(self._selected_chat),
            "source_title": _chat_title(self._selected_chat),
            "source_topic_id": source.get("topic_id", "") or None,
            "source_topic_title": source.get("topic_title", "") or ("全部主題" if source.get("topic_all") else ""),
            "target_ref": _chat_ref(self._target_chat),
            "target_id": _chat_id(self._target_chat),
            "target_title": target_title,
            "target_is_forum": _chat_is_forum(self._target_chat),
            "target_topic_id": topic_id,
            "target_topic_title": _text(self._target_topic.get("title")) if self._target_topic else "",
            "expected_count": int(self._export_count or 0),
        }

    @staticmethod
    def _batch_snapshots_match(expected: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        keys = (
            "account_id", "chat_ref", "chat_id", "is_forum", "topic_id", "topic_all",
            "start_date", "end_date", "media_types", "target_ref", "target_id",
            "target_is_forum", "target_topic_id",
        )
        return all(expected.get(key) == current.get(key) for key in keys)

    def _batch_manifest_matches_current(self) -> bool:
        current = self._current_batch_snapshot()
        snapshot = self._batch_scan_snapshot
        return bool(
            current is not None
            and snapshot is not None
            and self._export_path
            and self._export_count > 0
            and self._batch_manifest_sha256
            and self._batch_snapshots_match(snapshot, current)
            and self._export_snapshot is not None
            and not self._export_request_pending
            and not self._batch_terminal
        )

    def _invalidate_batch_scan(self, reason: str = "", *, cancel_operation: bool = True) -> None:
        # Keep an export worker from restoring an old manifest after a target
        # or source input changes.  ``_generation`` also invalidates queued Qt
        # success signals that arrive after a fast fake/real handle finishes.
        self._generation += 1
        self._pending_operation = None
        self._operation_snapshot = None
        if cancel_operation and self._worker is not None:
            self._worker.cancel()
        super()._clear_scan(keep_status=True)
        self._batch_scan_snapshot = None
        self._batch_manifest_sha256 = ""
        self._batch_actual_started = False
        self._batch_dry_run = False
        self._batch_terminal = False
        self._batch_progress_items.clear()
        if hasattr(self, "batch_progress"):
            self.batch_progress.clear()
            self.batch_progress.hide()
        if reason and hasattr(self, "scan_summary_label"):
            self._set_scan_summary(reason)
        self._update_download_button()

    def _invalidate_scan(self, reason: str = "", *, cancel_operation: bool = True) -> None:
        super()._invalidate_scan(reason, cancel_operation=cancel_operation)
        self._batch_scan_snapshot = None
        self._batch_manifest_sha256 = ""
        self._batch_actual_started = False
        self._batch_dry_run = False
        self._batch_terminal = False
        if hasattr(self, "batch_progress"):
            self.batch_progress.clear()
            self.batch_progress.hide()
            self._batch_progress_items.clear()

    def _clear_scan(self, *_args: Any, keep_status: bool = False) -> None:
        super()._clear_scan(keep_status=keep_status)
        if hasattr(self, "download_button"):
            self.download_button.setText("② 批次轉傳")
            self.download_button.setToolTip("確認目的地後送出整份掃描結果；送出後不會自動收回")
        self._batch_scan_snapshot = None
        self._batch_manifest_sha256 = ""
        self._batch_actual_started = False
        self._batch_dry_run = False
        self._batch_terminal = False
        if hasattr(self, "batch_progress"):
            self.batch_progress.clear()
            self.batch_progress.hide()
            self._batch_progress_items.clear()

    # ------------------------------------------------------------------
    # Source scan / outbound action
    # ------------------------------------------------------------------
    def _update_scan_button(self, enabled: Optional[bool] = None) -> None:
        if not hasattr(self, "scan_button"):
            return super()._update_scan_button(enabled)
        if enabled is None:
            enabled = True
        source_ready = self._selected_chat is not None and not self._topic_selection_required()
        target_ready = (
            self._target_chat is not None
            and not self._same_chat(self._selected_chat, self._target_chat)
            and (
                not _chat_is_forum(self._target_chat)
                or (self._target_topics_loaded and self._target_topic is not None)
            )
            and not self._target_topics_loading
        )
        self.scan_button.setEnabled(bool(enabled and source_ready and target_ready and not self.is_busy()))

    def _scan_clicked(self) -> None:
        account_id = self.active_account_id()
        if not account_id:
            self._set_status("請先選擇一個 Telegram 帳號。")
            return
        if not self._current_account_is_authenticated():
            self._set_status("目前帳號需要重新登入：請按視窗上方的「重新登入」。")
            return
        if self._selected_chat is None:
            self._set_status("請先從左側清單選擇一個來源頻道或群組。")
            return
        if self._target_chat is None:
            self._set_status("請先選擇明確的轉傳目的地。")
            return
        if not self._has_typed_chat_ref(self._selected_chat) or not self._has_typed_chat_ref(self._target_chat):
            self._set_status("目前清單缺少 Telegram 類型識別，請先按「更新清單」後再掃描。")
            return
        if self._same_chat(self._selected_chat, self._target_chat):
            self._set_status("目的地不可與來源相同，請改選另一個群組或頻道。")
            return
        if _chat_is_forum(self._selected_chat) and (not self._topics_loaded or self._selected_topic is None):
            self._set_status("請先選擇來源論壇主題，或明確選擇「全部主題」。")
            return
        if _chat_is_forum(self._target_chat) and (
            not self._target_topics_loaded or self._target_topics_loading or self._target_topic is None
        ):
            self._set_status("請先選擇目的論壇話題（包含「一般」），不可省略。")
            return
        if self.start_date.date() > self.end_date.date():
            self._set_status("起始日期不可晚於結束日期。")
            return
        media_types = self._media_types()
        if not media_types:
            self._set_status("請至少選擇一種媒體類型。")
            return
        if not callable(getattr(self._runner, "start_export", None)):
            self._set_status("目前後端尚未提供頻道掃描功能。")
            return
        source_snapshot = self._current_export_snapshot()
        batch_snapshot = self._current_batch_snapshot()
        if source_snapshot is None or batch_snapshot is None:
            self._set_status("目前來源或目的地已失效，請重新選擇。")
            return
        self._clear_scan(keep_status=True)
        self._operation_snapshot = source_snapshot
        self._batch_scan_snapshot = batch_snapshot
        self._batch_scan_snapshot["expected_count"] = 0
        self._set_scan_summary(
            f"正在掃描：{self._snapshot_display_source(source_snapshot)} · "
            f"{source_snapshot['start_date']}～{source_snapshot['end_date']} · "
            f"{ '、'.join(_MEDIA_LABELS.get(item, item) for item in source_snapshot['media_types']) } · "
            f"目的地：{_chat_title(self._target_chat)}"
        )
        self._set_status("正在掃描符合條件的訊息…")
        kwargs: dict[str, Any] = {}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        if _chat_is_forum(self._selected_chat):
            kwargs["topic_id"] = None if self._selected_topic and self._selected_topic.get("all") else self._selected_topic.get("id")
        args = (
            account_id,
            _chat_ref(self._selected_chat),
            _date_text(self.start_date.date()),
            _date_text(self.end_date.date()),
            media_types,
        )
        self._start_operation("export", "start_export", args, kwargs)

    def _update_download_button(self) -> None:
        if not hasattr(self, "download_button"):
            return
        self.download_button.setEnabled(bool(self._batch_manifest_matches_current() and not self.is_busy()))
        if hasattr(self, "dry_run_button"):
            self.dry_run_button.setEnabled(bool(self._batch_manifest_matches_current() and not self.is_busy()))

    def _download_clicked(self) -> None:
        self._batch_clicked(dry_run=False)

    def _batch_clicked(self, *, dry_run: bool) -> None:
        if self.is_busy():
            self._set_status("目前批次操作仍在進行中，請先取消後再開始另一個操作。")
            return
        if not callable(getattr(self._runner, "start_batch_forward", None)):
            self._set_status("目前後端尚未提供群組批次轉傳功能。")
            return
        if not self._batch_manifest_matches_current():
            self._set_scan_summary("來源、目的地或條件已變更，請重新掃描後再轉傳。")
            self._set_status("請先完成一次有效掃描。")
            self._update_download_button()
            return
        snapshot = dict(self._batch_scan_snapshot or {})
        snapshot["expected_count"] = self._export_count
        if not dry_run and not self._confirm_send(snapshot):
            self._set_status("已取消批次轉傳；掃描結果仍保留。")
            return
        # QMessageBox.exec() owns a nested event loop.  Re-check every bound
        # input after it closes; a queued account/source change must never send
        # an old manifest to a new destination.
        current = self._current_batch_snapshot()
        if (
            current is None
            or not self._batch_snapshots_match(snapshot, current)
            or self.active_account_id() != _text(snapshot.get("account_id"))
        ):
            self._set_scan_summary("轉傳條件已變更，請重新掃描後再試。")
            self._set_status("轉傳已停止：帳號、來源或目的地在確認期間已變更。")
            self._update_download_button()
            return
        path = self._export_path
        self._batch_actual_started = not dry_run
        self._batch_dry_run = bool(dry_run)
        self._batch_terminal = False
        self._batch_progress_items.clear()
        self._batch_progress_sequence = 0
        self.batch_progress.clear()
        self.batch_progress.show()
        self._set_status("正在試跑轉傳流程…" if dry_run else "正在批次轉傳…")
        kwargs: dict[str, Any] = {
            "source_chat": snapshot.get("source_ref") or snapshot.get("chat_ref"),
            "to": snapshot.get("target_ref"),
            "manifest_sha256": self._batch_manifest_sha256,
            "expected_count": int(self._export_count),
            "topic_id": snapshot.get("target_topic_id") or None,
            "mode": self.mode_combo.currentData() or "direct",
            "silent": bool(self.silent_check.isChecked()),
            "dry_run": bool(dry_run),
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._start_operation("batch_forward", "start_batch_forward", (snapshot["account_id"], path), kwargs)

    def _confirm_send(self, snapshot: Mapping[str, Any]) -> bool:
        """Confirmation hook used by the native dialog and offline probes."""

        return _confirm_batch(self, snapshot)

    # ------------------------------------------------------------------
    # Runner lifecycle and queued GUI slots
    # ------------------------------------------------------------------
    def _start_operation(self, kind: str, method: str, args: tuple[Any, ...], kwargs: Optional[Mapping[str, Any]] = None) -> None:
        if self.is_busy():
            self._set_status("目前操作仍在進行中，請先取消後再開始另一個操作。")
            return
        super()._start_operation(kind, method, args, kwargs)

    def _launch_operation(self, token: int, kind: str, method: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
        # Do not reuse ChannelPage's slot-name connections here.  PySide6
        # resolves an overridden @Slot with the base class' meta-object entry
        # in some frozen builds, which silently drops the queued result for
        # this subclass.  Give the worker unambiguous QObject receivers and
        # route them to the normal operation methods on the GUI thread.
        thread = QThread(self)
        worker = _RunnerWorker(self._runner, method, args, kwargs, token=token, kind=kind)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.event.connect(self._batch_worker_event, Qt.QueuedConnection)
        worker.succeeded.connect(self._batch_worker_succeeded, Qt.QueuedConnection)
        worker.failed.connect(self._batch_worker_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._batch_worker_canceled, Qt.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._operation_thread_finished, Qt.QueuedConnection)
        thread.finished.connect(thread.deleteLater)
        self._thread_workers[thread] = worker
        self._thread = thread
        self._worker = worker
        self._operation_kind = kind
        if kind == "target_topics":
            self._target_operation_token = token
        self._set_controls_enabled(False)
        self.cancel_button.setVisible(True)
        thread.start()
        # Emit after ``start`` so the embedding TransferPage's synchronous
        # ``is_busy()`` check observes a running QThread and keeps its legacy
        # upload/forward controls locked.
        self._emit_busy_changed()

    @Slot(int, object)
    def _batch_worker_event(self, token: int, event: Any) -> None:
        self._operation_event(token, event)

    @Slot(int, str, object)
    def _batch_worker_succeeded(self, token: int, kind: str, data: Any) -> None:
        self._operation_succeeded(token, kind, data)

    @Slot(int, str)
    def _batch_worker_failed(self, token: int, message: str) -> None:
        self._operation_failed(token, message)

    @Slot(int)
    def _batch_worker_canceled(self, token: int) -> None:
        self._operation_canceled(token)

    def _operation_finished(self, thread: Any, worker: Any) -> None:
        super()._operation_finished(thread, worker)
        self._emit_busy_changed()

    def _emit_busy_changed(self) -> None:
        busy = bool(self.is_busy())
        if busy != self._batch_busy_last:
            self._batch_busy_last = busy
            self.busy_changed.emit(busy)

    @Slot(int, object)
    def _operation_event(self, token: int, event: Any) -> None:
        if token != self._generation or not isinstance(event, Mapping):
            return
        if self._operation_kind == "target_topics":
            name = _text(event.get("event", event.get("type", ""))).lower()
            if name in {"topics", "topic_list"}:
                raw = event.get("topics", event.get("items", event.get("data", [])))
                if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                    self._populate_target_topics(raw)
            elif name in {"status", "log", "progress"}:
                message = _safe_message(event.get("message", event.get("text", event.get("status", ""))))
                if message:
                    self.target_hint.setText(message)
            return
        if self._operation_kind == "batch_forward":
            self._record_batch_event(event)
            return
        super()._operation_event(token, event)

    def _populate_target_topics(self, values: Sequence[Any]) -> None:
        topics: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in values:
            topic_id = _text(_value(value, "id", "topic_id", "thread_id", default="")).strip()
            if not topic_id or topic_id in seen:
                continue
            title = _topic_display_title(topic_id, _text(_value(value, "title", "name", "topic_title", default="")))
            topics.append({"id": topic_id, "title": title})
            seen.add(topic_id)
        self._target_topics = topics
        self._target_topics_loaded = True
        self._target_topics_loading = False
        self.target_topic_combo.blockSignals(True)
        self.target_topic_combo.clear()
        self.target_topic_combo.addItem("請選擇目的話題（含一般）", None)
        for topic in topics:
            self.target_topic_combo.addItem(topic["title"], topic)
        self.target_topic_combo.setCurrentIndex(0)
        self.target_topic_combo.blockSignals(False)
        self._target_topic = None
        self.target_hint.setText(f"目的話題已載入 {len(topics)} 個；請明確選擇一個（包含「一般」）。")
        self._update_target_controls()

    def _record_batch_event(self, event: Mapping[str, Any]) -> None:
        name = _text(event.get("event", event.get("type", ""))).lower()
        message = _text(event.get("message", event.get("text", event.get("status", "")))).strip()
        filename = _text(event.get("filename", event.get("file", ""))).strip()
        current = event.get("current", event.get("sent", event.get("completed")))
        total = event.get("total", event.get("expected", self._export_count))
        percent = event.get("percent")
        speed = _text(event.get("speed", "")).strip()
        state_raw = _text(event.get("state", "")).strip().casefold()
        state_labels = {
            "completed": "完成",
            "complete": "完成",
            "done": "完成",
            "failed": "失敗",
            "error": "失敗",
            "canceled": "取消",
            "cancelled": "取消",
            "running": "處理中",
            "processing": "處理中",
            "uploading": "處理中",
            "queued": "等待中",
            "pending": "等待中",
        }
        state_text = state_labels.get(state_raw) or (_safe_message(state_raw) if state_raw else "")
        if filename or current is not None or state_raw or name in {"progress", "forwarded", "sent", "message"}:
            parts: list[str] = []
            if filename:
                parts.append(filename)
            if current is not None and total is not None:
                parts.append(f"{current}/{total}")
            if percent is not None:
                parts.append(f"{percent}%")
            if speed:
                parts.append(speed)
            if state_text:
                parts.append(f"狀態：{state_text}")
            if message:
                parts.append(message)
            text = " · ".join(part for part in parts if part) or "處理中"
            key = _text(event.get("file_id", event.get("message_id", event.get("id", "")))).strip()
            if not key:
                key = filename or f"event-{self._batch_progress_sequence}"
            item = self._batch_progress_items.get(key)
            if item is None:
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, key)
                self._batch_progress_items[key] = item
                self.batch_progress.addItem(item)
                self._batch_progress_sequence += 1
            else:
                item.setText(text)
            item.setToolTip(text)
        elif message:
            self._set_status(_safe_message(message))

    @Slot(int, str, object)
    def _operation_succeeded(self, token: int, kind: str, data: Any) -> None:
        if token != self._generation:
            return
        if kind == "target_topics":
            values: Any = data
            if isinstance(data, Mapping):
                values = data.get("topics", data.get("items", data.get("data", [])))
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
                self._populate_target_topics(values)
                self._set_status(f"目的話題清單已載入，共 {len(self._target_topics)} 個。")
            else:
                self._target_topics_loading = False
                self._target_topics_loaded = False
                self.target_hint.setText("目的話題格式無法辨識，請改選一般群組或重新更新清單。")
                self._set_status("無法讀取目的話題。")
                self._update_target_controls()
            return
        if kind == "export":
            self._batch_export_succeeded(data)
            return
        if kind == "batch_forward":
            self._batch_forward_succeeded(data)
            return
        super()._operation_succeeded(token, kind, data)
        if kind == "list":
            self._populate_target_combo()

    def _batch_export_succeeded(self, data: Any) -> None:
        source_snapshot = self._operation_snapshot
        current = self._current_batch_snapshot()
        expected = self._batch_scan_snapshot
        if source_snapshot is None or current is None or expected is None or not self._batch_snapshots_match(expected, current):
            self._operation_snapshot = None
            super()._clear_scan(keep_status=True)
            self._batch_scan_snapshot = None
            self._batch_manifest_sha256 = ""
            self._set_scan_summary("掃描結果已失效，請依目前來源、條件與目的地重新掃描。")
            self._set_status("掃描結果已失效，沒有建立可轉傳清單。")
            self._update_download_button()
            return
        path, count = self._normalise_export(data)
        total_bytes = self._export_total_bytes(data)
        sha = ""
        if isinstance(data, Mapping):
            sha = _text(data.get("sha256", data.get("manifest_sha256", data.get("checksum", "")))).strip().lower()
        self._operation_snapshot = None
        self._export_snapshot = dict(source_snapshot)
        self._export_request_pending = False
        self._export_consumed = False
        if count <= 0:
            self._export_path = ""
            self._export_count = 0
            self._export_bytes = 0
            self._batch_scan_snapshot = None
            self._batch_manifest_sha256 = ""
            self._set_scan_summary("掃描結果：沒有符合目前日期、話題與媒體類型的訊息。")
            self._set_status("沒有符合的訊息；可以放寬日期或多勾幾種媒體類型。")
        elif not path or not sha:
            self._export_path = ""
            self._export_count = 0
            self._export_bytes = 0
            self._batch_scan_snapshot = None
            self._batch_manifest_sha256 = ""
            self._set_scan_summary("掃描完成但缺少安全清單資訊，請重新掃描。")
            self._set_status("掃描結果缺少清單檔或校驗碼，沒有啟用轉傳。")
        else:
            self._export_path = path
            self._export_count = count
            self._export_bytes = total_bytes
            self._batch_manifest_sha256 = sha
            self._batch_scan_snapshot = dict(current)
            self._batch_scan_snapshot["expected_count"] = count
            self._batch_terminal = False
            self._set_scan_summary(self._batch_summary(count, total_bytes))
            self._set_status("掃描完成；可先試跑，確認目的地後按「② 批次轉傳」。")
        self._update_download_button()

    def _batch_summary(self, count: int, total_bytes: int = 0) -> str:
        snapshot = self._batch_scan_snapshot or self._current_batch_snapshot() or {}
        source = _text(snapshot.get("source_title"), "來源")
        topic = _text(snapshot.get("source_topic_title")).strip()
        if topic:
            source += f" · 話題：{topic}"
        media = "、".join(_MEDIA_LABELS.get(item, item) for item in snapshot.get("media_types", ())) or "未選類型"
        target = _text(snapshot.get("target_title"), "目的地")
        target_topic = _text(snapshot.get("target_topic_title")).strip()
        if target_topic:
            target += f" · 話題：{target_topic}"
        size = f"，約 {self._format_bytes(total_bytes)}" if total_bytes > 0 else ""
        return (
            f"掃描結果：{source} · {snapshot.get('start_date', '')}～{snapshot.get('end_date', '')} · "
            f"{media} · 符合 {count} 則 · 目的地：{target}{size}。"
        )

    @staticmethod
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

    @staticmethod
    def _result_number(data: Any, *keys: str) -> Optional[int]:
        if not isinstance(data, Mapping):
            return None
        for key in keys:
            if key not in data or data.get(key) is None:
                continue
            try:
                return max(0, int(data.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return None

    def _batch_forward_succeeded(self, data: Any) -> None:
        if self._batch_dry_run:
            self._batch_terminal = False
            self._set_status("試跑完成：沒有送出任何訊息；確認無誤後可按「② 批次轉傳」。")
            self._set_scan_summary("試跑完成；掃描結果仍可確認後送出。")
            self._update_download_button()
            return
        snapshot = self._batch_scan_snapshot or {}
        expected = int(snapshot.get("expected_count", self._export_count) or 0)
        sent = self._result_number(data, "sent_count", "forwarded", "completed", "count")
        failed = self._result_number(data, "failed_count", "failed") or 0
        partial = bool(data.get("partial", False)) if isinstance(data, Mapping) else False
        self._batch_terminal = True
        self._export_consumed = True
        self._update_download_button()
        if sent is None:
            sent_text = f"{expected} 則" if expected else "已回報項目"
        else:
            sent_text = f"{sent}/{expected} 則" if expected else f"{sent} 則"
        if partial or failed:
            self._set_status(f"批次轉傳部分完成：已送出 {sent_text}，失敗 {failed} 則。已送出的訊息不會自動收回；重新送出可能重複，請先檢查目的群組。")
        else:
            self._set_status(f"批次轉傳完成：已送出 {sent_text}。訊息不會自動收回；重新送出可能重複，請先檢查目的群組。")
        self._set_scan_summary("批次轉傳已結束；為避免重複送出，這份掃描結果已鎖定。")

    @Slot(int, str)
    def _operation_failed(self, token: int, message: str) -> None:
        if token != self._generation:
            return
        kind = self._operation_kind
        if kind == "target_topics":
            self._target_topics_loading = False
            self._target_topics_loaded = False
            self.target_hint.setText("無法讀取目的話題；請重新更新清單或改選一般群組。")
            self._set_status(f"目的話題讀取失敗：{_safe_message(message) or '請稍後重試。'}")
            self._update_target_controls()
            return
        if kind == "batch_forward":
            if self._batch_actual_started:
                self._batch_terminal = True
                self._export_consumed = True
                self._update_download_button()
                self._set_status(f"批次轉傳未能完成：{_safe_message(message) or '請查看進度後再試。'} 請檢查目的群組；重新送出可能重複。")
                self._set_scan_summary("批次轉傳未能完成；請檢查目的群組，重新送出可能重複。")
            else:
                self._set_status(f"試跑失敗：{_safe_message(message) or '請修正目的地後重試。'}")
                self._update_download_button()
            return
        if kind == "export":
            self._operation_snapshot = None
            self._clear_scan(keep_status=True)
            self._set_scan_summary("掃描未完成，請依目前來源與條件重試。")
            self._set_status(f"掃描失敗：{_safe_message(message) or '請稍後重試。'}")
            return
        super()._operation_failed(token, message)

    @Slot(int)
    def _operation_canceled(self, token: int) -> None:
        if token != self._generation:
            return
        kind = self._operation_kind
        if kind == "target_topics":
            self._target_topics_loading = False
            self._target_topics_loaded = False
            self.target_hint.setText("目的話題讀取已取消；請重新選擇目的地或更新清單。")
            self._update_target_controls()
            return
        if kind == "batch_forward":
            if self._batch_actual_started:
                self._batch_terminal = True
                self._export_consumed = True
                self._set_status("批次轉傳已取消；操作可能已有部分送出，請檢查目的群組。重新送出可能重複。")
                self._set_scan_summary("批次轉傳未能完成；請檢查目的群組，重新送出可能重複。")
            else:
                self._set_status("試跑已取消；掃描結果仍可再次試跑或確認。")
            self._update_download_button()
            return
        if kind == "export":
            self._operation_snapshot = None
            self._clear_scan(keep_status=True)
            self._set_scan_summary("掃描已取消；請確認來源、目的地與條件後重試。")
            self._set_status("掃描已取消。")
            return
        super()._operation_canceled(token)

    def cancel_operations(self) -> None:
        """Cancel without allowing a real outbound manifest to re-submit.

        ``ChannelPage.cancel_operations`` increments the generation before the
        worker can emit its canceled signal.  That is intentional for stale
        scan results, but it means this page must persist the outbound terminal
        state itself rather than waiting for ``_operation_canceled``.
        """

        kind = self._operation_kind
        actual = bool(self._batch_actual_started)
        dry_run = bool(self._batch_dry_run)
        was_busy = self.is_busy()
        super().cancel_operations()
        if kind == "batch_forward":
            if actual:
                self._batch_terminal = True
                self._export_consumed = True
                self._cancel_status = "批次轉傳已取消；操作可能已有部分送出，請檢查目的群組。重新送出可能重複。"
                if not was_busy:
                    self._set_status(self._cancel_status)
                else:
                    self._set_status("正在取消批次轉傳…")
            elif dry_run:
                self._cancel_status = "試跑已取消；掃描結果仍可再次試跑或確認。"
                if not was_busy:
                    self._set_status(self._cancel_status)
                else:
                    self._set_status("正在取消試跑…")
            self._update_download_button()
        elif kind == "target_topics":
            self._target_topics_loading = False
            self._target_topics_loaded = False
            self.target_hint.setText("目的話題讀取已取消；請重新選擇目的地或更新清單。")
            self._update_target_controls()

    # ------------------------------------------------------------------
    # Controls / shutdown
    # ------------------------------------------------------------------
    def _update_target_controls(self) -> None:
        if not hasattr(self, "target_group_search"):
            return
        busy = self.is_busy()
        enabled = not busy
        self.target_group_search.setEnabled(enabled)
        self.target_group_combo.setEnabled(enabled)
        topic_enabled = enabled and self._target_topics_loaded and not self._target_topics_loading
        self.target_topic_combo.setEnabled(topic_enabled)
        self.mode_combo.setEnabled(enabled)
        self.silent_check.setEnabled(enabled)
        self.target_group_search.setToolTip(
            "只在已載入的對話清單中搜尋，不會自動選擇目的地。"
            if not busy else "目前操作進行中；完成或取消後才能變更目的地。"
        )
        self._update_scan_button(enabled)

    def _set_controls_enabled(self, enabled: bool) -> None:
        super()._set_controls_enabled(enabled)
        if not hasattr(self, "target_group_search"):
            return
        self._update_target_controls()
        valid = self._batch_manifest_matches_current() and bool(enabled) and not self.is_busy()
        self.dry_run_button.setEnabled(valid)
        if not enabled:
            self.download_button.setEnabled(False)
            self.dry_run_button.setEnabled(False)


__all__ = ["BatchForwardPage"]
