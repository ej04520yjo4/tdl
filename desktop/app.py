"""tdl 繁中下載器的 PySide6 桌面介面。

這個檔案刻意把 Qt 畫面和 Telegram 核心分開。真正的帳號、登入和下載邏輯
由 ``desktop.backend``（或直接執行時的鄰接 ``backend``）提供，畫面透過
:class:`BackendBridge` 呼叫。後端未安裝時，程式只會顯示初始化問題，不會假裝
已登入，也不會產生假的下載進度。

支援：
    python -m desktop.app
    python -m desktop.app --smoke-test
    QT_QPA_PLATFORM=offscreen python -m desktop.app --screenshot out.png

桌面後端提供固定的 Store/Runner 方法：
    store.list_accounts()
    store.list_history()
    runner.start_download(account_id, links, ...)
    runner.start_login(account_id, mode=..., ...)

回呼中的 progress 只接受核心回報的實際數值，UI 不會自行遞增百分比。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QIODevice,
    QLibraryInfo,
    QLocale,
    QLockFile,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    QStandardPaths,
    QTranslator,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QStyle,
)

try:
    from desktop.channel_page import ChannelPage
    from desktop.search import search_matches
    from desktop.transfer_page import TransferPage
    from desktop.version import VERSION
except ImportError:  # pragma: no cover - direct ``python desktop/app.py``
    from channel_page import ChannelPage
    from search import search_matches
    from transfer_page import TransferPage
    from version import VERSION


APP_NAME = "tdl 繁中下載器"
APP_ORG = "tdl"
VERSION_TEXT = f"桌面版 {VERSION}"
PAGE_NAMES = ("頻道下載", "下載中心", "上傳與轉傳", "帳號管理", "下載紀錄", "設定與說明")

BG = "#f4f7fb"
CARD = "#ffffff"
TEXT = "#172033"
MUTED = "#65738b"
SUBTLE = "#8b98ad"
BORDER = "#e4eaf3"
PRIMARY = "#4f46e5"
PRIMARY_DARK = "#3730a3"
PRIMARY_LIGHT = "#eef2ff"
BLUE = "#2563eb"
SUCCESS = "#16845b"
SUCCESS_LIGHT = "#e7f8ef"
WARNING = "#b45309"
WARNING_LIGHT = "#fff5df"
DANGER = "#c83b4a"
DANGER_LIGHT = "#fff0f2"

URL_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/\S+$", re.I)
_TRANSLATOR: Optional[QTranslator] = None

try:
    # PySide deletes QThread's C++ object after ``deleteLater``.  A Python
    # reference can therefore remain in one of our bookkeeping lists while
    # its wrapper is no longer safe to call.  Use the binding validity check
    # before touching a worker during shutdown.
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:  # pragma: no cover - shiboken6 ships with PySide6
    def _qt_object_is_valid(_value: Any) -> bool:
        return True


def _thread_is_running(thread: Any) -> bool:
    try:
        return bool(_qt_object_is_valid(thread) and thread.isRunning())
    except (RuntimeError, AttributeError):
        return False


def _resource_path(*parts: str) -> Path:
    """Resolve packaged resources and source-tree resources consistently."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    root = Path(frozen_root) if frozen_root else Path(__file__).resolve().parent
    return root.joinpath(*parts)


def _safe_str(value: Any, default: str = "") -> str:
    """Convert backend values to display text without exposing secrets."""

    if value is None:
        return default
    return str(value)


def _normalise_message_url(value: Any) -> str:
    """Normalise the friendly URL forms accepted by the text box.

    The backend deliberately accepts only canonical HTTPS ``t.me`` links.  A
    pasted ``t.me/...`` or legacy ``telegram.me/...`` link is harmless to
    normalise here, while arbitrary text is left out of JSON imports so a
    local file path can never be mistaken for a Telegram URL.
    """

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    text = re.sub(r"^https?://(?:www\.)?telegram\.me/", "https://t.me/", text, flags=re.I)
    text = re.sub(r"^http://(?:www\.)?t\.me/", "https://t.me/", text, flags=re.I)
    if not re.match(r"^https://(?:www\.)?t\.me/\S+$", text, re.I):
        return ""
    return text


def _app_data_dir() -> Path:
    """Return a writable app data directory without requiring a backend."""

    location = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not location:
        location = str(Path(tempfile.gettempdir()) / "tdl-zh")
    result = Path(location)
    try:
        result.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A restricted test sandbox can expose a read-only roaming profile.
        # The real Windows executable normally uses AppData; this fallback
        # keeps diagnostics and a backend-unavailable screen usable there.
        result = Path(tempfile.gettempdir()) / "tdl-zh"
        result.mkdir(parents=True, exist_ok=True)
    return result


def _extract(mapping: Any, *names: str, default: Any = None) -> Any:
    if isinstance(mapping, Mapping):
        for name in names:
            if name in mapping:
                return mapping[name]
    for name in names:
        try:
            value = getattr(mapping, name)
        except Exception:
            continue
        if value is not None:
            return value
    return default


@dataclass
class AccountInfo:
    id: str
    label: str
    phone: str = ""
    display_name: str = ""
    authenticated: bool = False
    username: str = ""

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "AccountInfo":
        if isinstance(value, str):
            return cls(id=value, label=value, authenticated=False)
        account_id = _safe_str(_extract(value, "id", "account_id", "key", default=f"account-{index}"))
        label = _safe_str(_extract(value, "label", "name", "title", default=account_id))
        return cls(
            id=account_id,
            label=label or account_id,
            phone=_safe_str(_extract(value, "phone", "phone_number", default="")),
            display_name=_safe_str(_extract(value, "display_name", "first_name", default="")),
            authenticated=bool(_extract(value, "authenticated", "is_authenticated", "logged_in", default=False)),
            username=_safe_str(_extract(value, "username", default="")),
        )


@dataclass
class HistoryInfo:
    id: str
    title: str
    url: str = ""
    status: str = "已完成"
    created_at: str = ""
    path: str = ""
    detail: str = ""
    # "12 個檔案 · 3.4 GB" for a completed job; empty while unknown.
    summary: str = ""

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "HistoryInfo":
        created = _extract(value, "created_at", "date", "time", default="")
        if isinstance(created, str) and created:
            # The store keeps UTC ISO text; show the user's local wall time.
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone()
            except ValueError:
                pass
        if isinstance(created, datetime):
            created = created.strftime("%Y-%m-%d %H:%M")
        file_count = int(_extract(value, "file_count", default=0) or 0)
        total_bytes = int(_extract(value, "total_bytes", default=0) or 0)
        summary = f"{file_count} 個檔案" if file_count else ""
        if total_bytes:
            summary += (" · " if summary else "") + _format_bytes(total_bytes)
        raw_status = _safe_str(_extract(value, "status", default="已完成"))
        status_map = {
            "completed": "已完成",
            "complete": "已完成",
            "success": "已完成",
            "failed": "失敗",
            "cancelled": "已取消",
            "canceled": "已取消",
            "interrupted": "中斷",
            "paused": "已暫停",
            "running": "下載中",
            "queued": "等待中",
        }
        return cls(
            id=_safe_str(_extract(value, "id", "job_id", default=f"history-{index}")),
            title=_safe_str(_extract(value, "title", "name", "filename", "file_name", default="下載項目")),
            url=_safe_str(_extract(value, "url", "link", default="")),
            status=status_map.get(raw_status.lower(), raw_status or "已完成"),
            created_at=_safe_str(created),
            path=_safe_str(_extract(value, "path", "file_path", "directory", default="")),
            detail=_safe_str(_extract(value, "detail", "error", "message", default="")),
            summary=summary,
        )


@dataclass
class DownloadJob:
    id: str
    url: str
    title: str
    output_dir: str
    status: str = "等待中"
    progress: Optional[float] = None
    transferred: int = 0
    total: int = 0
    speed: str = ""
    error: str = ""
    result_path: str = ""
    account_id: str = ""
    links: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    backend_job_id: str = ""
    resume_on_start: bool = False
    closing_pause: bool = False
    file_progress: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    # Totals from a channel scan; zero when the job size is not known yet.
    expected_files: int = 0
    expected_bytes: int = 0
    options: dict[str, Any] = field(default_factory=dict, repr=False)
    worker: Any = field(default=None, repr=False)
    thread: Any = field(default=None, repr=False)


class BackendUnavailable(RuntimeError):
    """Raised when the UI is started before the Python backend is installed."""


class CancelledError(RuntimeError):
    pass


class OperationFailed(RuntimeError):
    """A core operation ended with a reason that is safe to show the user."""


def _load_backend_objects() -> tuple[Any, Any, Any]:
    """Create the concrete Store/Runner pair shipped with this desktop app."""

    try:
        module = importlib.import_module("desktop.backend")
    except (ImportError, ModuleNotFoundError):
        try:
            # Direct ``python desktop/app.py`` execution places the sibling
            # directory on sys.path, so this is the source-tree equivalent.
            module = importlib.import_module("backend")
        except (ImportError, ModuleNotFoundError):
            return None, None, None
    try:
        store = module.Store()
        runner = module.Runner(store)
    except Exception:
        return None, None, module
    return store, runner, module


class BackendBridge:
    """Stable UI-facing contract around the evolving Python backend."""

    def __init__(self) -> None:
        self.store, self.runner, self.module = _load_backend_objects()
        self.available = self.store is not None and self.runner is not None
        self.backend_error = "" if self.available else "Python 下載後端尚未安裝。"
        self._active_login_handle: Any = None
        self._active_login_lock = threading.RLock()
        self._account_handles: set[Any] = set()
        self._account_handles_lock = threading.Lock()
        self._account_operations_stopped = False

    def _require_backend(self) -> None:
        if not self.available:
            raise BackendUnavailable(self.backend_error or "Python 下載後端尚未安裝。")

    @property
    def data_root(self) -> Path:
        root = getattr(self.store, "root", None)
        return Path(root) if root else _app_data_dir()

    def accounts(self) -> list[AccountInfo]:
        self._require_backend()
        values = self.store.list_accounts()
        if values is None:
            return []
        if isinstance(values, Mapping):
            values = list(values.values())
        return [AccountInfo.from_value(item, i) for i, item in enumerate(values)]

    def history(self) -> list[HistoryInfo]:
        self._require_backend()
        values = self.store.list_history()
        if values is None:
            return []
        if isinstance(values, Mapping):
            values = list(values.values())
        return [HistoryInfo.from_value(item, i) for i, item in enumerate(values)]

    def queue(self) -> list[Any]:
        self._require_backend()
        return list(self.store.list_queue())

    def active_account_id(self) -> str:
        self._require_backend()
        account = self.store.get_active_account()
        return _safe_str(_extract(account, "id", "account_id", default="")) if account is not None else ""

    def set_active_account(self, account_id: str) -> None:
        self._require_backend()
        self.store.set_active_account(account_id or None)

    def delete_account(self, account_id: str) -> None:
        self._require_backend()
        self.store.remove_account(account_id)

    def prepare_download(self, job: DownloadJob, options: Mapping[str, Any]) -> Any:
        """Persist the inputs before a Qt download worker can be started."""
        self._require_backend()
        if job.backend_job_id:
            return self.store.get_job(job.backend_job_id)
        files = list(job.files or [])
        links = list(job.links or ([] if files else ([job.url] if job.url else [])))
        return self.store.create_download_job(
            _safe_str(options.get("account_id"), job.account_id), links,
            files=files, output_dir=job.output_dir,
            continue_download=options.get("continue_download", True),
            skip_same=options.get("skip_existing", options.get("skip_same", True)),
            group=options.get("group_album", options.get("group", True)),
            group_by_chat=options.get("group_by_chat", True),
            concurrency=int(options.get("concurrency", 2) or 2), title=job.title,
            # ``advanced`` is absent for ordinary jobs, which then inherit the
            # options currently saved on the settings page.
            advanced=options.get("advanced"),
            expected_files=job.expected_files, expected_bytes=job.expected_bytes,
        )

    def clear_finished_jobs(self) -> list[str]:
        self._require_backend()
        return list(self.store.clear_finished_jobs())

    def _run_account_operation(self, start: Callable[[], Any], fallback: str) -> Any:
        """Wait for one short account operation; raise its translated error."""

        with self._account_handles_lock:
            # A restore runs two operations; the window may close between them.
            if self._account_operations_stopped:
                raise OperationFailed("操作已取消。")
            handle = start()
            self._account_handles.add(handle)
        try:
            result = handle.wait()
        finally:
            with self._account_handles_lock:
                self._account_handles.discard(handle)
        if result is None or not result.success:
            raise OperationFailed(_safe_str(getattr(result, "error", ""), fallback) or fallback)
        return result

    def cancel_account_operations(self) -> None:
        """Stop a backup or restore that is still running when the app closes."""

        with self._account_handles_lock:
            self._account_operations_stopped = True
            handles = list(self._account_handles)
        for handle in handles:
            handle.cancel()

    def backup_account(self, account_id: str, destination: str) -> str:
        self._require_backend()
        self._run_account_operation(
            lambda: self.runner.start_backup(account_id, destination), "備份沒有完成。"
        )
        return destination

    def restore_account(self, label: str, source: str, proxy: str = "") -> str:
        """Create an account from a ``.tdl`` backup and confirm it with Telegram.

        The new account is removed again when either step fails, so a damaged
        or revoked backup never leaves a half-usable entry in the list.
        """

        self._require_backend()
        account = self.store.add_account(label)
        try:
            self._run_account_operation(
                lambda: self.runner.start_recover(account.id, source), "備份檔無法還原。"
            )
            self._run_account_operation(
                lambda: self.runner.start_whoami(account.id, proxy=proxy or None),
                "Telegram 沒有接受這份備份的登入資料。",
            )
        except Exception:
            try:
                self.store.remove_account(account.id)
            except Exception:
                pass
            raise
        return _safe_str(account.id)

    def download(
        self,
        job: DownloadJob,
        options: Mapping[str, Any],
        cancel_event: threading.Event,
        progress_callback: Callable[[Any], None],
        pause_event: Optional[threading.Event] = None,
    ) -> Any:
        self._require_backend()
        # The shipped backend starts a Runner handle and streams normalized
        # events while its own worker owns the Go process. This keeps the Qt
        # thread responsive and lets cancellation reach the actual process.
        account_id = _safe_str(options.get("account_id"))
        if not account_id:
            raise BackendUnavailable("請先選擇已連線的 Telegram 帳號。")
        if not job.backend_job_id:
            saved = self.prepare_download(job, options)
            job.backend_job_id = saved.id
            job.files = list(saved.files)
        files = list(job.files or [])
        # A files-only job uses ``url`` for a friendly queue label, but that
        # path must never be sent through Telegram's URL validator.
        links = list(job.links or ([] if files else ([job.url] if job.url else [])))
        if job.backend_job_id:
            request = self.module.DownloadRequest(
                account_id=account_id,
                links=links,
                files=files,
                output_dir=job.output_dir,
                continue_download=options.get("continue_download", True),
                skip_same=options.get("skip_existing", options.get("skip_same", True)),
                group=options.get("group_album", options.get("group", True)),
                group_by_chat=options.get("group_by_chat", True),
                concurrency=int(options.get("concurrency", 2) or 2),
                proxy=options.get("proxy") or None,
                job_id=job.backend_job_id,
                title=job.title,
            )
            handle = self.runner.start(request, on_event=None)
        else:
            handle = self.runner.start_download(
                account_id,
                links,
                files=files or None,
                output_dir=job.output_dir,
                continue_download=options.get("continue_download", True),
                skip_same=options.get("skip_existing", options.get("skip_same", True)),
                group=options.get("group_album", options.get("group", True)),
                group_by_chat=options.get("group_by_chat", True),
                concurrency=int(options.get("concurrency", 2) or 2),
                proxy=options.get("proxy") or None,
                title=job.title,
                on_event=None,
            )
        while not handle.done.is_set():
            if cancel_event.is_set():
                if pause_event is not None and pause_event.is_set():
                    handle.pause()
                else:
                    handle.cancel()
            event = handle.get_event(timeout=0.2)
            if event is not None:
                progress_callback(event)
        while True:
            event = handle.get_event(timeout=0)
            if event is None:
                break
            progress_callback(event)
        result = handle.wait(timeout=0)
        if result is not None and getattr(result, "job_id", None):
            job.backend_job_id = _safe_str(result.job_id)
        if cancel_event.is_set() or (result is not None and result.cancelled):
            raise CancelledError()
        if result is None or not result.success:
            message = _safe_str(getattr(result, "error", ""), "下載核心未完成工作。")
            raise RuntimeError(message)
        return {"success": True, "job_id": getattr(result, "job_id", "")}

    def login(
        self,
        payload: Mapping[str, Any],
        cancel_event: threading.Event,
        event_callback: Callable[[Any], None],
    ) -> Any:
        self._require_backend()
        label = _safe_str(payload.get("label"), "Telegram 帳號")
        account_id = _safe_str(payload.get("account_id"))
        created = False
        if not account_id:
            account = self.store.add_account(label)
            account_id = _safe_str(getattr(account, "id", _extract(account, "id", default="")))
            created = True
        if not account_id:
            raise BackendUnavailable("無法建立 Telegram 帳號識別碼。")

        # Runner.start_login starts its worker before returning the handle. The
        # queue is consumed below so the first phone prompt cannot race the
        # assignment of the active handle.
        try:
            handle = self.runner.start_login(
                account_id,
                mode=_safe_str(payload.get("method"), "qr"),
                proxy=payload.get("proxy") or None,
                on_event=None,
            )
        except Exception:
            if created:
                try:
                    self.store.remove_account(account_id)
                except Exception:
                    pass
            raise
        with self._active_login_lock:
            self._active_login_handle = handle
        try:
            phone_sent = False
            while not handle.done.is_set():
                if cancel_event.is_set():
                    handle.cancel()
                event = handle.get_event(timeout=0.2)
                if event is None:
                    continue
                if isinstance(event, Mapping) and event.get("event") == "prompt" and event.get("kind") == "phone" and not phone_sent:
                    phone = _safe_str(payload.get("phone"))
                    if phone:
                        handle.respond(phone)
                        phone_sent = True
                        continue
                event_callback(event)
            while True:
                event = handle.get_event(timeout=0)
                if event is None:
                    break
                event_callback(event)
            result = handle.wait(timeout=0)
        finally:
            with self._active_login_lock:
                self._active_login_handle = None
        if cancel_event.is_set() or (result is not None and result.cancelled):
            if created:
                try:
                    self.store.remove_account(account_id)
                except Exception:
                    pass
            raise CancelledError()
        if result is None or not result.success:
            if created:
                try:
                    self.store.remove_account(account_id)
                except Exception:
                    pass
            raise RuntimeError(_safe_str(getattr(result, "error", "登入核心未完成工作。")))
        return {"authenticated": True, "account_id": account_id}

    def respond_login(self, value: str) -> bool:
        """Send a code/password response to the active RunHandle only."""

        with self._active_login_lock:
            handle = self._active_login_handle
        if handle is None:
            return False
        return bool(handle.respond(value))

    def preferences(self) -> Any:
        self._require_backend()
        return self.store.get_preferences()

    def update_preferences(self, **values: Any) -> Any:
        self._require_backend()
        return self.store.update_preferences(**values)

    def export_jobs(self, path: str, jobs: Sequence[DownloadJob], options: Mapping[str, Any]) -> None:
        self._require_backend()
        # Store.export_downloads exports persisted history. For an input
        # queue, retain the local JSON shape instead of mixing UI-only
        # objects into protected account history.
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "options": dict(options),
            "urls": [job.url for job in jobs if job.links or job.url.startswith(("http://", "https://"))],
            "files": [path for job in jobs for path in job.files],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class DownloadWorker(QObject):
    progress = Signal(str, object)
    succeeded = Signal(str, object)
    failed = Signal(str, str, str)
    canceled = Signal(str)

    def __init__(self, bridge: BackendBridge, job: DownloadJob, options: Mapping[str, Any]) -> None:
        super().__init__()
        self.bridge = bridge
        self.account_id = ""
        self.job = job
        self.options = dict(options)
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = self.bridge.download(self.job, self.options, self.cancel_event, self._emit_progress, self.pause_event)
            if self.cancel_event.is_set():
                self.canceled.emit(self.job.id)
            else:
                self.succeeded.emit(self.job.id, result)
        except CancelledError:
            self.canceled.emit(self.job.id)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.canceled.emit(self.job.id)
            else:
                # BackendBridge raises the core's already translated,
                # redacted result.error.  Keep that useful reason in the row
                # and dialog instead of hiding every failure behind one generic
                # network message; the expandable diagnostic still retains the
                # sanitized traceback for troubleshooting.
                message = _safe_error_message(self.bridge, exc)
                self.failed.emit(self.job.id, message, _diagnostic(exc))

    def _emit_progress(self, value: Any) -> None:
        # The backend may provide a dictionary or a numeric percentage. Values
        # outside 0..100 are treated as unknown instead of manufacturing one.
        self.progress.emit(self.job.id, value)

    def cancel(self) -> None:
        self.cancel_event.set()

    def pause(self) -> None:
        # An explicit cancel must never turn back into a restartable pause.
        if not self.cancel_event.is_set():
            self.job.closing_pause = True
            self.pause_event.set()
            self.cancel_event.set()


class LoginWorker(QObject):
    event = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str, str)
    canceled = Signal()

    def __init__(self, bridge: BackendBridge, payload: Mapping[str, Any]) -> None:
        super().__init__()
        self.bridge = bridge
        self.payload = dict(payload)
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = self.bridge.login(self.payload, self.cancel_event, self._event)
            if self.cancel_event.is_set():
                self.canceled.emit()
            else:
                self.succeeded.emit(result)
        except CancelledError:
            self.canceled.emit()
        except Exception as exc:
            if self.cancel_event.is_set():
                self.canceled.emit()
            else:
                self.failed.emit("登入未完成。請重新嘗試，或確認 Telegram 驗證碼。", _diagnostic(exc))

    def _event(self, value: Any) -> None:
        self.event.emit(value)

    def cancel(self) -> None:
        self.cancel_event.set()


class StoreReadWorker(QObject):
    """Read local account/history storage away from Qt's GUI thread."""

    ready = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, bridge: BackendBridge, kind: str) -> None:
        super().__init__()
        self.bridge = bridge
        self.kind = kind

    @Slot()
    def run(self) -> None:
        try:
            if self.kind == "accounts":
                values = {"items": self.bridge.accounts(), "active_id": self.bridge.active_account_id()}
            elif self.kind == "history":
                values = self.bridge.history()
            else:
                values = self.bridge.queue()
            self.ready.emit(self.kind, values)
        except Exception as exc:
            self.failed.emit(self.kind, _diagnostic(exc))


class StoreActionWorker(QObject):
    """Execute a local Store mutation without pausing the Qt event loop."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, action: Callable[[], Any]) -> None:
        super().__init__()
        self.action = action

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.action())
        except Exception as exc:
            self.failed.emit(_diagnostic(exc))


class _StoreActionRelay(QObject):
    """Deliver Store callbacks on the GUI thread before ending the worker."""

    completed = Signal()

    def __init__(
        self,
        on_success: Optional[Callable[[Any], None]],
        on_failure: Optional[Callable[[str], None]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._on_success = on_success
        self._on_failure = on_failure

    @Slot(object)
    def success(self, value: Any) -> None:
        try:
            if self._on_success is not None:
                self._on_success(value)
        except Exception as exc:
            # Keep the old callback contract: an exception raised by the
            # success callback is surfaced through the failure callback.  It
            # is handled here, on the GUI thread, instead of escaping from a
            # worker-thread signal delivery.
            self._call_failure(_diagnostic(exc))
        finally:
            self.completed.emit()

    @Slot(str)
    def failure(self, detail: str) -> None:
        try:
            self._call_failure(detail)
        finally:
            self.completed.emit()

    def _call_failure(self, detail: str) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure(detail)
        except Exception as exc:
            # A UI callback must never keep the Store worker thread alive or
            # turn a handled Store error into an unhandled Qt callback error.
            # Frozen windowed builds may expose no stderr at all; when one is
            # available, write only the already-redacted diagnostic.
            stream = getattr(sys, "stderr", None)
            if stream is not None:
                try:
                    stream.write(f"Store callback failed: {_diagnostic(exc)}\n")
                    stream.flush()
                except Exception:
                    pass


def _diagnostic(exc: BaseException) -> str:
    """Return raw details for an explicit diagnostics expander only."""

    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
    # Diagnostics are user-visible only after expanding the details panel,
    # but they still must not expose proxy credentials or auth material.
    text = re.sub(r"(?i)(socks(?:4|5)?|https?)://([^\s:@/]+):([^\s@/]+)@", r"\1://***:***@", text)
    text = re.sub(r"(?i)(password|passcode|token|secret|authorization)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    return text


def _last_error_line(detail: str) -> str:
    """Pick the exception message out of a ``_diagnostic`` traceback.

    Store actions report failures as redacted traceback text.  Its last line
    is ``ExceptionType: message``; the message is the backend's own
    Traditional Chinese reason and reads better than a generic sentence.
    """

    lines = [line.strip() for line in _safe_str(detail).splitlines() if line.strip()]
    if not lines:
        return ""
    _kind, separator, message = lines[-1].partition(": ")
    return message.strip()[:400] if separator else ""


def _safe_error_message(bridge: BackendBridge, exc: BaseException) -> str:
    """Keep the backend's translated reason visible while redacting secrets.

    ``BackendBridge.download`` raises the translated ``result.error`` from the
    core.  Showing that value is considerably more useful than replacing every
    validation/auth/network failure with the same generic sentence.  The
    backend sanitizer is the source of truth; the small local fallback keeps
    this helper safe when a fixture supplies a minimal bridge module.
    """

    text = _safe_str(exc).strip()
    module = getattr(bridge, "module", None)
    sanitizer = getattr(module, "sanitize_text", None)
    if callable(sanitizer):
        try:
            text = _safe_str(sanitizer(text)).strip()
        except Exception:
            pass
    else:
        text = re.sub(r"(?i)(socks(?:4|5)?|https?)://([^\s:@/]+):([^\s@/]+)@", r"\1://***:***@", text)
        text = re.sub(r"(?i)(password|passcode|token|secret|authorization)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    return text[:400] or "下載未完成。"


def _preference_values(value: Any) -> dict[str, Any]:
    """Normalize Store.Preferences without leaking backend object types into Qt."""

    return {
        "download_folder": _safe_str(_extract(value, "download_dir", "download_folder", default="")),
        "proxy": _safe_str(_extract(value, "proxy", default="")),
        "concurrency": int(_extract(value, "concurrency", default=2) or 2),
        "template": _safe_str(_extract(value, "template", default="")),
        "include_ext": _safe_str(_extract(value, "include_ext", default="")),
        "exclude_ext": _safe_str(_extract(value, "exclude_ext", default="")),
        "desc": bool(_extract(value, "desc", default=False)),
        "takeout": bool(_extract(value, "takeout", default=False)),
        "rewrite_ext": bool(_extract(value, "rewrite_ext", default=False)),
        "threads": int(_extract(value, "threads", default=4) or 4),
        "delay_ms": int(_extract(value, "delay_ms", default=0) or 0),
    }


# Friendly names for ``tdl dl --template``.  An empty template is the core
# default; every preset keeps the message ID so two files never collide.
TEMPLATE_PRESETS: tuple[tuple[str, str], ...] = (
    ("預設：對話編號_訊息編號_原檔名", ""),
    ("日期開頭：20261013_訊息編號_原檔名", '{{ formatDate .MessageDate "20060102" }}_{{ .MessageID }}_{{ filenamify .FileName }}'),
    ("精簡：訊息編號_原檔名", "{{ .MessageID }}_{{ filenamify .FileName }}"),
)


class Card(QFrame):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.setObjectName("Card")


class Pill(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setProperty("pill", True)
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(25)
        self.setContentsMargins(10, 1, 10, 1)

    def set_tone(self, tone: str) -> None:
        # Called for every progress event; re-polishing is only needed when
        # the tone, and therefore the matching style rule, really changes.
        if self.property("tone") == tone:
            return
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


class SidebarButton(QPushButton):
    def __init__(self, icon: QIcon | str, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Standard Qt icons keep the navigation readable on clean Windows
        # installs where a symbol-font glyph such as ``⌂`` may render as a
        # square.  The string path remains useful for tests/custom callers.
        if isinstance(icon, QIcon):
            self.setIcon(icon)
            self.setIconSize(QSize(19, 19))
            self.setText(text)
        else:
            self.setText(f"  {icon}   {text}")
        self.setProperty("sidebarButton", True)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(46)
        self.setObjectName(f"sidebar_{text}")


class QueueRow(Card):
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, job: DownloadJob, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.job_id = job.id
        self.output_dir = job.output_dir
        # Keep the order in which the core first reports files.  Concurrent
        # downloads can update any file in any order; rebuilding from the
        # latest event would make the visible list jump around on every
        # callback.
        self._file_order: list[str] = []
        self._file_rows: dict[str, tuple[QProgressBar, QLabel, QLabel]] = {}
        self._has_file_progress = False
        # Per-file display state and byte counters, kept so one progress
        # event updates one row and the totals without rescanning every file.
        self._file_states: dict[str, str] = {}
        self._file_bytes: dict[str, tuple[int, int]] = {}
        self._state_counts: dict[str, int] = {}
        self._bytes_done = 0
        self._bytes_known = 0
        self._expected_files = 0
        self._expected_bytes = 0
        self._status = job.status
        self._speed = 0.0
        self._speed_sample: Optional[tuple[float, int]] = None
        self.title_label = QLabel(job.title)
        self.title_label.setObjectName("QueueTitle")
        self.title_label.setWordWrap(False)
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.title_label.setMinimumWidth(0)
        self.title_label.setToolTip(job.title)
        self.url_label = QLabel(job.url)
        self.url_label.setObjectName("QueueUrl")
        self.url_label.setWordWrap(False)
        self.url_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.url_label.setMinimumWidth(0)
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url_label.setToolTip(job.url)
        self.status = Pill("等待中")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(190)
        self.progress.setMaximumHeight(8)
        self.percent = QLabel("等待核心回報")
        self.percent.setObjectName("QueueProgressText")
        # A core progress event may switch to a long per-file name.  Keep the
        # right hand label bounded so its changing text cannot resize a row or
        # move the queue scrollbar while concurrent files report progress.
        self.percent.setWordWrap(False)
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.percent.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.percent.setMinimumWidth(92)
        self.percent.setMaximumWidth(210)
        self.percent.setToolTip("等待核心回報")
        self.cancel_button = QPushButton("取消")
        self.retry_button = QPushButton("重試")
        self.open_button = QPushButton("開啟資料夾")
        for button in (self.cancel_button, self.retry_button, self.open_button):
            button.setCursor(Qt.PointingHandCursor)
        self.retry_button.setVisible(False)
        self.open_button.setVisible(False)

        left = QVBoxLayout()
        left.setSpacing(4)
        left.addWidget(self.title_label)
        left.addWidget(self.url_label)
        progress_line = QHBoxLayout()
        progress_line.setSpacing(10)
        progress_line.addWidget(self.progress, 1)
        progress_line.addWidget(self.percent)
        left.addLayout(progress_line)

        self.file_progress_host = QWidget()
        self.file_progress_host.setObjectName("QueueFileProgress")
        self.file_progress_layout = QVBoxLayout(self.file_progress_host)
        self.file_progress_layout.setContentsMargins(0, 3, 0, 0)
        self.file_progress_layout.setSpacing(3)
        # Overall size, speed and remaining time.  It lives with the file
        # rows and keeps a fixed height so filling it in never moves them.
        self.stats = QLabel()
        self.stats.setObjectName("QueueStats")
        self.stats.setWordWrap(False)
        self.stats.setFixedHeight(20)
        self.stats.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.stats.setMinimumWidth(0)
        self.file_progress_layout.addWidget(self.stats)
        self.file_progress_host.setVisible(False)
        left.addWidget(self.file_progress_host)

        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(self.status, 0, Qt.AlignRight)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.open_button)
        right.addLayout(actions)
        # Keep the pill and buttons at the top once file rows make the row
        # taller, instead of stretching the pill over the whole height.
        right.addStretch(1)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(18)
        layout.addLayout(left, 1)
        layout.addLayout(right, 0)
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.job_id))
        self.retry_button.clicked.connect(lambda: self.retry_requested.emit(self.job_id))
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.output_dir))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Keep the three-line left column and two-line action column at a
        # predictable height even when the core changes the current filename.
        self.setMinimumHeight(96)
        self.set_job(job)

    def set_job(self, job: DownloadJob) -> None:
        """Update the row while keeping a stable per-file presentation.

        ``file_progress`` is deliberately read with ``getattr`` because the
        backend may add it to the in-memory DownloadJob after an older queue
        item has already been restored.  The row therefore remains compatible
        with persisted jobs and with the single-file progress contract.
        """

        self.output_dir = job.output_dir
        self.title_label.setText(job.title)
        self.title_label.setToolTip(job.title)
        # A link is worth showing; a channel or JSON job would only show the
        # path of an internal manifest, so name the destination instead.
        subtitle = job.url if job.links or not job.files else f"下載到 {job.output_dir}"
        self.url_label.setText(subtitle)
        self.url_label.setToolTip(subtitle)
        self._expected_files = int(getattr(job, "expected_files", 0) or 0)
        self._expected_bytes = int(getattr(job, "expected_bytes", 0) or 0)
        self._status = job.status
        values = getattr(job, "file_progress", None)
        self._set_file_progress(values)
        self.set_state(job.status, job.progress, job.speed or job.error)

    def update_file(self, job: DownloadJob, file_id: str) -> None:
        """Apply one file's progress event without touching the other rows."""

        self._status = job.status
        item = job.file_progress.get(file_id)
        if item is None:
            return
        self._apply_file(self._file_value(file_id, item))
        self._refresh_summary()
        self.set_state(job.status, job.progress, job.speed or job.error)

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number > 100:
            return None
        return number

    @classmethod
    def _file_value(cls, key: Any, value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            data = dict(value)
        else:
            data = {"percent": value}
        identifier = str(data.get("file_id", data.get("id", key)) or key)
        filename = str(
            data.get("filename", data.get("file", data.get("name", ""))) or ""
        )
        state = str(data.get("state", data.get("status", "")) or "")
        number = data.get("percent", data.get("percentage", data.get("progress")))
        return {
            "id": identifier,
            "filename": filename or identifier,
            "state": state,
            "percent": cls._number(number),
            "current": data.get("current", data.get("transferred")),
            "total": data.get("total", data.get("size")),
        }

    @classmethod
    def _file_values(cls, value: Any) -> list[dict[str, Any]]:
        if not value:
            return []
        if isinstance(value, Mapping):
            return [cls._file_value(key, item) for key, item in value.items()]
        if isinstance(value, (list, tuple)):
            return [cls._file_value(index, item) for index, item in enumerate(value)]
        return []

    @staticmethod
    def _file_state(value: str, percent: Optional[float]) -> str:
        lowered = value.strip().lower()
        if lowered in {"done", "complete", "completed", "success", "finished", "已完成"}:
            return "已完成"
        if lowered in {"failed", "failure", "error", "失敗"}:
            return "失敗"
        if lowered in {"cancelled", "canceled", "取消", "已取消"}:
            return "已取消"
        if lowered in {"queued", "pending", "waiting", "等待中"}:
            return "等待中"
        if lowered in {"running", "active", "downloading", "processing", "下載中", "處理中"}:
            return "下載中"
        if percent is not None and percent >= 100:
            return "已完成"
        if percent is not None or lowered:
            return "下載中"
        return "等待中"

    @staticmethod
    def _file_detail(item: Mapping[str, Any], state: str, percent: Optional[float]) -> str:
        if state == "已完成":
            return "已完成"
        if state in {"失敗", "已取消", "等待中"}:
            return state
        if percent is None:
            return "下載中"
        return f"{int(round(percent))}%"

    def _set_file_progress(self, value: Any) -> None:
        values = self._file_values(value)
        if not values:
            self._has_file_progress = False
            self.file_progress_host.setVisible(False)
            return
        # Keep existing rows, even if a late backend snapshot omits a file;
        # removing it would shift all rows while the user is reading them.
        for item in values:
            self._apply_file(item)
        self._refresh_summary()

    @staticmethod
    def _byte_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _apply_file(self, item: Mapping[str, Any]) -> None:
        """Create or update one file row and keep the running totals."""

        identifier = item["id"]
        row = self._file_rows.get(identifier)
        if row is None:
            # First-seen order is the display order.  Concurrent downloads
            # report in any order; sorting by the latest event would make the
            # visible list jump around on every callback.
            self._file_order.append(identifier)
            filename = QLabel(item["filename"])
            filename.setWordWrap(False)
            filename.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            filename.setMinimumWidth(0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            detail = QLabel()
            detail.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            detail.setFixedWidth(68)
            detail.setObjectName("QueueFileProgressText")
            line = QWidget(self.file_progress_host)
            line.setFixedHeight(22)
            line_layout = QHBoxLayout(line)
            line_layout.setContentsMargins(0, 0, 0, 0)
            line_layout.setSpacing(8)
            line_layout.addWidget(filename, 3)
            line_layout.addWidget(bar, 2)
            line_layout.addWidget(detail, 0)
            self.file_progress_layout.addWidget(line)
            row = (bar, filename, detail)
            self._file_rows[identifier] = row
        bar, filename, detail = row
        if filename.text() != item["filename"]:
            filename.setText(item["filename"])
            filename.setToolTip(item["filename"])
            detail.setToolTip(item["filename"])
        percent = item["percent"]
        state = self._file_state(item["state"], percent)
        if state == "已完成":
            bar.setValue(100)
        elif percent is not None:
            bar.setValue(max(0, min(100, int(round(percent)))))
        else:
            bar.setValue(0)
        detail.setText(self._file_detail(item, state, percent))

        previous = self._file_states.get(identifier)
        if previous != state:
            if previous is not None:
                self._state_counts[previous] -= 1
            self._state_counts[state] = self._state_counts.get(state, 0) + 1
            self._file_states[identifier] = state
        total = self._byte_count(item["total"])
        current = total if state == "已完成" and total else min(self._byte_count(item["current"]), total or sys.maxsize)
        old_current, old_total = self._file_bytes.get(identifier, (0, 0))
        self._file_bytes[identifier] = (current, total)
        self._bytes_done += current - old_current
        self._bytes_known += total - old_total

    def _refresh_summary(self) -> None:
        """Rebuild the count, size, speed and remaining-time texts."""

        completed = self._state_counts.get("已完成", 0)
        active = self._state_counts.get("下載中", 0)
        failed = self._state_counts.get("失敗", 0)
        total = max(len(self._file_order), self._expected_files)
        finished = self._status == "已完成"
        if finished:
            # The core does not report files it skips, so a finished job can
            # have fewer rows than the scan counted; "5/12" would look stuck.
            summary = f"已完成 {completed} 個檔案"
        else:
            summary = f"檔案 {completed}/{total} · 進行中 {active}"
        if failed:
            summary += f" · 失敗 {failed}"
        self._has_file_progress = True
        self.file_progress_host.setVisible(True)
        self.percent.setText(summary)
        self.percent.setToolTip(summary)

        # A scan knows the whole job size up front; a pasted link only learns
        # each file's size as the core reaches it.
        total_bytes = max(self._expected_bytes, self._bytes_known)
        downloading = self._status == "下載中"
        now = time.monotonic()
        if not downloading:
            self._speed, self._speed_sample = 0.0, None
        elif self._speed_sample is None:
            self._speed_sample = (now, self._bytes_done)
        elif now - self._speed_sample[0] >= 1.0:
            elapsed = now - self._speed_sample[0]
            rate = max(0.0, (self._bytes_done - self._speed_sample[1]) / elapsed)
            # Smooth the figure so one stalled second does not swing the
            # remaining time between minutes and hours.
            self._speed = rate if self._speed <= 0 else self._speed * 0.6 + rate * 0.4
            self._speed_sample = (now, self._bytes_done)

        parts: list[str] = []
        if finished:
            if self._bytes_done > 0:
                parts.append(_format_bytes(self._bytes_done))
            if total > completed + failed:
                parts.append(f"另有 {total - completed - failed} 個已存在或被篩選而略過")
        elif total_bytes > 0:
            parts.append(f"{_format_bytes(self._bytes_done)} / {_format_bytes(total_bytes)}")
        if downloading and self._speed > 0:
            parts.append(f"{_format_bytes(self._speed)}/s")
            remaining = total_bytes - self._bytes_done
            if self._expected_bytes > 0 and remaining > 0:
                parts.append(f"剩餘約 {_format_duration(remaining / self._speed)}")
        text = " · ".join(parts)
        if self.stats.text() != text:
            self.stats.setText(text)
            self.stats.setToolTip(text)

    def overall_percent(self) -> Optional[int]:
        """Whole-job percentage, known only when a scan supplied the size."""

        if self._expected_bytes <= 0:
            return None
        return max(0, min(100, int(self._bytes_done * 100 / self._expected_bytes)))

    def set_state(self, status: str, progress: Optional[float] = None, detail: str = "") -> None:
        self.status.setText(status)
        tone = "neutral"
        if status in ("下載中", "處理中"):
            tone = "blue"
        elif status in ("已完成", "已登入"):
            tone = "success"
        elif status in ("待重試",):
            tone = "warning"
        elif status in ("已暫停",):
            tone = "warning"
        elif status in ("失敗", "已取消"):
            tone = "danger" if status == "失敗" else "neutral"
        self.status.set_tone(tone)
        if self._has_file_progress:
            # Each file row owns its own bar.  A whole-job bar is only shown
            # when a scan supplied the total size; counting just the files
            # discovered so far would overstate the progress.
            overall = self.overall_percent()
            self.progress.setVisible(overall is not None)
            if overall is not None:
                self.progress.setValue(100 if status == "已完成" else overall)
            self.percent.setText(self.percent.text() or "等待核心回報")
        elif progress is None:
            self.progress.setVisible(True)
            self.percent.setText(detail or "等待核心回報")
        else:
            self.progress.setVisible(True)
            bounded = max(0, min(100, int(round(float(progress)))))
            self.progress.setValue(bounded)
            self.percent.setText(f"{bounded}%" + (f" · {detail}" if detail else ""))
        active = status in ("等待中", "下載中", "處理中")
        self.cancel_button.setVisible(active)
        resumable = status == "已暫停"
        self.retry_button.setText("繼續下載" if resumable else "重試")
        self.retry_button.setVisible(status in ("失敗", "已取消", "待重試", "已暫停"))
        self.open_button.setVisible(status == "已完成" and bool(self.output_dir))
        if self._has_file_progress:
            # The summary text is its own tooltip; see ``_refresh_summary``.
            if detail:
                self.percent.setToolTip(f"{self.percent.text()}\n{detail}")
        elif progress is None:
            self.percent.setToolTip(detail or "等待核心回報")
        elif detail:
            self.percent.setToolTip(detail)


class ErrorDialog(QDialog):
    def __init__(self, title: str, message: str, details: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setObjectName("DialogTitle")
        text_label = QLabel(message)
        text_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(text_label)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText(details)
        self.details.setVisible(False)
        self.details.setMinimumHeight(160)
        layout.addWidget(self.details)
        if details:
            show = QToolButton()
            show.setText("顯示診斷細節")
            show.setCheckable(True)
            show.toggled.connect(self.details.setVisible)
            layout.addWidget(show)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_button = buttons.button(QDialogButtonBox.Close)
        if close_button is not None:
            close_button.setText("關閉")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def show_error(parent: QWidget, title: str, message: str, details: str = "") -> None:
    dialog = ErrorDialog(title, message, details, parent)
    dialog.exec()


def show_info(parent: QWidget, title: str, message: str) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle(title)
    box.setText(message)
    box.setStandardButtons(QMessageBox.Ok)
    button = box.button(QMessageBox.Ok)
    if button is not None:
        button.setText("確定")
    box.exec()


def ask_remove_account(parent: QWidget, label: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("移除帳號")
    box.setText(f"確定要移除「{label}」的本機登入工作階段嗎？")
    box.setInformativeText("這不會刪除 Telegram 雲端資料。")
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    yes = box.button(QMessageBox.Yes)
    no = box.button(QMessageBox.No)
    if yes is not None:
        yes.setText("移除")
    if no is not None:
        no.setText("保留")
    return box.exec() == QMessageBox.Yes


def ask_confirm(parent: QWidget, title: str, message: str, detail: str, accept_label: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    box.setInformativeText(detail)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    yes = box.button(QMessageBox.Yes)
    no = box.button(QMessageBox.No)
    if yes is not None:
        yes.setText(accept_label)
    if no is not None:
        no.setText("取消")
    box.setDefaultButton(QMessageBox.No)
    return box.exec() == QMessageBox.Yes


class LoginDialog(QDialog):
    login_requested = Signal(dict)

    def __init__(
        self,
        bridge: BackendBridge,
        parent: Optional[QWidget] = None,
        account: Optional[AccountInfo] = None,
    ) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.existing_account_id = account.id if account is not None else ""
        self.account_id = ""
        self.thread: Optional[QThread] = None
        self.worker: Optional[LoginWorker] = None
        self.setWindowTitle("重新登入 Telegram 帳號" if account is not None else "新增 Telegram 帳號")
        self.setMinimumSize(570, 520)
        self.setModal(True)
        self._build_ui()
        if account is not None:
            self.label_edit.setText(account.label)
            self.phone_edit.setText(account.phone)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)
        heading = QLabel("連結你的 Telegram 帳號")
        heading.setObjectName("DialogTitle")
        intro = QLabel("登入資訊只會交給本機下載核心處理；密碼不會寫入設定檔或顯示在紀錄中。")
        intro.setWordWrap(True)
        intro.setObjectName("MutedText")
        root.addWidget(heading)
        root.addWidget(intro)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.form_page = QWidget()
        form = QVBoxLayout(self.form_page)
        form.setSpacing(14)
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("例如：工作帳號")
        self.phone_edit = QLineEdit()
        self.phone_edit.setPlaceholderText("例如：+886912345678")
        form_layout = QFormLayout()
        form_layout.setLabelAlignment(Qt.AlignRight)
        form_layout.addRow("帳號名稱", self.label_edit)
        self.phone_label = QLabel("手機號碼")
        form_layout.addRow(self.phone_label, self.phone_edit)
        form.addLayout(form_layout)
        method_title = QLabel("選擇登入方式")
        method_title.setObjectName("CardTitle")
        form.addWidget(method_title)
        self.qr_radio = QRadioButton("QR Code 登入（推薦）")
        self.code_radio = QRadioButton("手機驗證碼登入")
        self.qr_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.qr_radio)
        group.addButton(self.code_radio)
        form.addWidget(self.qr_radio)
        form.addWidget(self.code_radio)
        self.code_hint = QLabel("手機驗證碼方式會在下一步要求驗證碼；若帳號開啟兩步驟驗證，也會要求密碼。")
        self.code_hint.setWordWrap(True)
        self.code_hint.setObjectName("MutedText")
        self.code_hint.setVisible(False)
        form.addWidget(self.code_hint)
        self.code_radio.toggled.connect(self.code_hint.setVisible)
        self.code_radio.toggled.connect(self._toggle_phone_field)
        self._toggle_phone_field(False)
        form.addStretch(1)
        self.start_button = QPushButton("開始登入")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setMinimumHeight(44)
        self.start_button.clicked.connect(self._start_login)
        form.addWidget(self.start_button)
        self.stack.addWidget(self.form_page)

        self.auth_page = QWidget()
        auth_layout = QVBoxLayout(self.auth_page)
        auth_layout.setSpacing(12)
        self.auth_title = QLabel("正在準備登入…")
        self.auth_title.setObjectName("CardTitle")
        self.auth_title.setAlignment(Qt.AlignCenter)
        auth_layout.addWidget(self.auth_title)
        self.qr_image = QLabel()
        self.qr_image.setAlignment(Qt.AlignCenter)
        self.qr_image.setMinimumSize(260, 260)
        self.qr_image.setObjectName("QrImage")
        auth_layout.addWidget(self.qr_image, 1)
        self.step_label = QLabel("請依畫面提示操作。")
        self.step_label.setAlignment(Qt.AlignCenter)
        self.step_label.setWordWrap(True)
        self.step_label.setObjectName("MutedText")
        auth_layout.addWidget(self.step_label)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("輸入 Telegram 驗證碼")
        self.code_edit.setVisible(False)
        self.code_edit.setMaxLength(12)
        auth_layout.addWidget(self.code_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("兩步驟驗證密碼（若有設定）")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setVisible(False)
        auth_layout.addWidget(self.password_edit)
        self.submit_code_button = QPushButton("送出驗證")
        self.submit_code_button.setObjectName("PrimaryButton")
        self.submit_code_button.setVisible(False)
        self.submit_code_button.clicked.connect(self._submit_code)
        auth_layout.addWidget(self.submit_code_button)
        self.stack.addWidget(self.auth_page)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._cancel_login)
        root.addWidget(self.cancel_button, 0, Qt.AlignRight)

    def _start_login(self) -> None:
        label = self.label_edit.text().strip()
        phone = self.phone_edit.text().strip() if self.code_radio.isChecked() else ""
        if not label:
            label = phone or "Telegram 帳號"
        if self.code_radio.isChecked() and not phone:
            show_error(self, "需要手機號碼", "請輸入 Telegram 帳號的手機號碼。")
            self.phone_edit.setFocus()
            return
        self.start_button.setEnabled(False)
        self.stack.setCurrentWidget(self.auth_page)
        self.auth_title.setText("正在連線 Telegram…")
        self.step_label.setText("請稍候，登入核心會在需要操作時提示你。")
        payload = {
            "label": label,
            "phone": phone,
            "method": "qr" if self.qr_radio.isChecked() else "code",
        }
        if self.existing_account_id:
            # Re-login repairs the existing Store record.  BackendBridge only
            # creates/removes an account when this field is absent.
            payload["account_id"] = self.existing_account_id
        self._launch_worker(payload)

    def _toggle_phone_field(self, code_mode: bool) -> None:
        self.phone_label.setVisible(bool(code_mode))
        self.phone_edit.setVisible(bool(code_mode))
        self.phone_edit.setEnabled(bool(code_mode))

    def _launch_worker(self, payload: Mapping[str, Any]) -> None:
        self.thread = QThread(self)
        self.worker = LoginWorker(self.bridge, payload)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.event.connect(self._handle_event)
        self.worker.succeeded.connect(self._login_succeeded)
        self.worker.failed.connect(self._login_failed)
        self.worker.canceled.connect(self._login_canceled)
        self.worker.succeeded.connect(self.thread.quit)
        self.worker.failed.connect(lambda *_: self.thread.quit())
        self.worker.canceled.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _handle_event(self, event: Any) -> None:
        if not isinstance(event, Mapping):
            self.step_label.setText(_safe_str(event, "請依畫面提示操作。"))
            return
        state = _safe_str(event.get("state", event.get("status", ""))).lower()
        event_name = _safe_str(event.get("event", event.get("type", ""))).lower()
        prompt_kind = _safe_str(event.get("kind", event.get("prompt", ""))).lower()
        qr = event.get("qr", event.get("qr_base64", event.get("qr_code", event.get("png_base64"))))
        if qr:
            self._set_qr(qr)
            self.auth_title.setText("請用 Telegram 掃描 QR Code")
            self.step_label.setText("Telegram → 設定 → 裝置 → 連結桌面裝置。QR Code 會定期更新。")
        message = event.get("message", event.get("prompt", event.get("detail")))
        if message:
            self.step_label.setText(_safe_str(message))
        if event_name == "prompt" and prompt_kind == "code":
            state = "code"
        if event_name == "prompt" and prompt_kind == "password":
            state = "password"
        if state in ("code", "need_code", "verification_code") or event.get("need_code"):
            self.code_edit.setVisible(True)
            self.submit_code_button.setVisible(True)
            self.auth_title.setText("輸入 Telegram 驗證碼")
        if state in ("password", "need_password", "2fa") or event.get("need_password"):
            self.password_edit.setVisible(True)
            self.submit_code_button.setVisible(True)
            self.auth_title.setText("輸入兩步驟驗證密碼")

    def _set_qr(self, value: Any) -> None:
        text = _safe_str(value)
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            self.step_label.setText("收到的 QR Code 無法顯示，請重新開始登入。")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            self.step_label.setText("收到的 QR Code 無法顯示，請重新開始登入。")
            return
        self.qr_image.setPixmap(pixmap.scaled(260, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _submit_code(self) -> None:
        # RunHandle.respond writes one JSON value to the waiting Go prompt.
        # Values never enter persistent preferences or diagnostic output.
        value = self.code_edit.text().strip() if self.code_edit.isVisible() else self.password_edit.text()
        if not value:
            self.step_label.setText("請先輸入驗證碼或密碼。")
            return
        try:
            if not self.bridge.respond_login(value):
                self.step_label.setText("登入工作階段已結束，請重新開始登入。")
                return
            if self.code_edit.isVisible():
                self.code_edit.clear()
                self.code_edit.setVisible(False)
            if self.password_edit.isVisible():
                self.password_edit.clear()
                self.password_edit.setVisible(False)
            self.submit_code_button.setVisible(False)
            self.step_label.setText("已送出，等待 Telegram 回應…")
        except Exception as exc:
            show_error(self, "驗證失敗", "驗證碼或密碼未被接受，請確認後重試。", _diagnostic(exc))

    def _login_succeeded(self, result: Any) -> None:
        authenticated = True
        if isinstance(result, Mapping) and "authenticated" in result:
            authenticated = bool(result.get("authenticated"))
        if not authenticated:
            self._login_failed("登入未完成。請重新掃描 QR Code 或輸入驗證碼。", "backend returned authenticated=false")
            return
        if isinstance(result, Mapping):
            self.account_id = _safe_str(result.get("account_id"), self.existing_account_id)
        else:
            self.account_id = self.existing_account_id
        self.accept()

    def _login_failed(self, message: str, detail: str) -> None:
        self.start_button.setEnabled(True)
        show_error(self, "登入失敗", message, detail)
        self.stack.setCurrentWidget(self.form_page)

    def _login_canceled(self) -> None:
        self.reject()

    def _cancel_login(self) -> None:
        if self._stop_login_worker():
            self.reject()
        else:
            self.cancel_button.setEnabled(False)
            self.step_label.setText("正在安全取消登入，請稍候…")

    def _stop_login_worker(self, timeout_ms: int = 7000) -> bool:
        if self.worker is not None and _qt_object_is_valid(self.worker):
            self.worker.cancel()
        if self.thread is not None and _thread_is_running(self.thread):
            self.thread.quit()
            return self.thread.wait(timeout_ms)
        return True

    def reject(self) -> None:  # type: ignore[override]
        # Esc and the window close button both route through reject(); this is
        # the safe cancellation path for a Go prompt waiting on stdin.
        if not self._stop_login_worker():
            self.cancel_button.setEnabled(False)
            self.step_label.setText("正在安全取消登入，請稍候…")
            return
        super().reject()

    def closeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        if self._stop_login_worker():
            event.accept()
        else:
            event.ignore()
            self.cancel_button.setEnabled(False)
            self.step_label.setText("正在安全取消登入，請稍候…")


class DownloadPage(QWidget):
    download_requested = Signal(list, dict)
    account_requested = Signal()
    export_requested = Signal()
    import_requested = Signal()
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    open_requested = Signal(str)
    account_changed = Signal(str)
    clear_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.jobs: dict[str, DownloadJob] = {}
        self.rows: dict[str, QueueRow] = {}
        self.proxy_value = ""
        self._accounts: list[AccountInfo] = []
        # Official Telegram export JSON files are sent to the backend as
        # ``files``.  Keeping them separate from the URL editor prevents a
        # Windows path from ever being validated as a Telegram link.
        self.pending_files: list[str] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 30)
        outer.setSpacing(18)

        hero = Card()
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 24, 26, 24)
        hero_layout.setSpacing(18)
        text = QVBoxLayout()
        kicker = QLabel("tdl 繁中下載器")
        kicker.setObjectName("Kicker")
        title = QLabel("把 Telegram 檔案，安心收進電腦。")
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        desc = QLabel("貼上訊息連結，選好資料夾，剩下的交給下載核心。")
        desc.setObjectName("HeroDescription")
        desc.setWordWrap(True)
        text.addWidget(kicker)
        text.addWidget(title)
        text.addWidget(desc)
        hero_layout.addLayout(text, 1)
        badge = QFrame()
        badge.setObjectName("HeroBadge")
        badge_layout = QVBoxLayout(badge)
        badge_layout.setContentsMargins(18, 14, 18, 14)
        badge_title = QLabel("簡單三步")
        badge_title.setObjectName("CardTitle")
        badge_body = QLabel("貼連結  ·  選資料夾  ·  開始下載")
        badge_body.setObjectName("MutedText")
        badge_body.setWordWrap(True)
        badge_layout.addWidget(badge_title)
        badge_layout.addWidget(badge_body)
        hero_layout.addWidget(badge, 0, Qt.AlignVCenter)
        outer.addWidget(hero)

        account_card = Card()
        account_layout = QHBoxLayout(account_card)
        account_layout.setContentsMargins(20, 16, 20, 16)
        account_layout.setSpacing(14)
        self.account_icon = QLabel("●")
        self.account_icon.setObjectName("AccountDot")
        account_layout.addWidget(self.account_icon)
        account_text = QVBoxLayout()
        account_title = QLabel("使用帳號")
        account_title.setObjectName("CardTitle")
        self.account_state = QLabel("尚未選擇帳號")
        self.account_state.setObjectName("MutedText")
        account_text.addWidget(account_title)
        account_text.addWidget(self.account_state)
        account_layout.addLayout(account_text, 1)
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(210)
        self.account_combo.setObjectName("AccountCombo")
        self.account_combo.currentIndexChanged.connect(self._account_changed)
        account_layout.addWidget(self.account_combo)
        manage_button = QPushButton("管理帳號")
        manage_button.clicked.connect(self.account_requested.emit)
        account_layout.addWidget(manage_button)
        outer.addWidget(account_card)

        input_card = Card()
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(20, 20, 20, 20)
        input_layout.setSpacing(12)
        input_title = QLabel("貼上 Telegram 訊息連結")
        input_title.setObjectName("CardTitle")
        input_hint = QLabel("可貼上多行連結，每行一個；支援公開頻道、群組或你有權限的私有訊息。")
        input_hint.setObjectName("MutedText")
        self.url_edit = QTextEdit()
        self.url_edit.setObjectName("UrlEdit")
        self.url_edit.setPlaceholderText("https://t.me/channel/123\nhttps://t.me/channel/124")
        self.url_edit.setMinimumHeight(108)
        folder_line = QHBoxLayout()
        folder_label = QLabel("下載到")
        folder_label.setObjectName("FieldLabel")
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setObjectName("FolderEdit")
        folder_button = QPushButton("選擇資料夾")
        folder_button.clicked.connect(self.choose_folder)
        folder_line.addWidget(folder_label)
        folder_line.addWidget(self.folder_edit, 1)
        folder_line.addWidget(folder_button)
        action_line = QHBoxLayout()
        self.input_count = QLabel("尚未加入連結")
        self.input_count.setObjectName("MutedText")
        self.url_edit.textChanged.connect(self._update_count)
        action_line.addWidget(self.input_count)
        action_line.addStretch(1)
        self.export_button = QPushButton("匯出 JSON")
        self.export_button.setToolTip("把目前輸入的連結與選項儲存成 JSON")
        self.export_button.clicked.connect(self.export_requested.emit)
        self.import_button = QPushButton("匯入 JSON")
        self.import_button.setToolTip("從 JSON 清單加入 Telegram 連結")
        self.import_button.clicked.connect(self.import_requested.emit)
        action_line.addWidget(self.import_button)
        action_line.addWidget(self.export_button)
        self.download_button = QPushButton("開始下載")
        self.download_button.setObjectName("PrimaryButton")
        self.download_button.setMinimumSize(140, 42)
        self.download_button.clicked.connect(self._request_download)
        action_line.addWidget(self.download_button)
        input_layout.addWidget(input_title)
        input_layout.addWidget(input_hint)
        input_layout.addWidget(self.url_edit)
        input_layout.addLayout(folder_line)
        folder_hint = QLabel("新下載會自動放入「群組／頻道名稱」子資料夾。")
        folder_hint.setObjectName("MutedText")
        folder_hint.setWordWrap(True)
        input_layout.addWidget(folder_hint)
        input_layout.addLayout(action_line)
        outer.addWidget(input_card)

        advanced = Card()
        adv_layout = QGridLayout(advanced)
        adv_layout.setContentsMargins(20, 16, 20, 16)
        adv_layout.setHorizontalSpacing(24)
        adv_layout.setVerticalSpacing(8)
        adv_title = QLabel("進階選項")
        adv_title.setObjectName("CardTitle")
        adv_layout.addWidget(adv_title, 0, 0, 1, 2)
        self.group_check = QCheckBox("下載群組相簿的全部檔案")
        self.group_check.setChecked(True)
        self.skip_check = QCheckBox("略過已存在的同名檔案")
        self.skip_check.setChecked(True)
        adv_layout.addWidget(self.group_check, 1, 0)
        adv_layout.addWidget(self.skip_check, 1, 1)
        concurrency_label = QLabel("同時下載數")
        concurrency_label.setObjectName("FieldLabel")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 16)
        self.concurrency_spin.setValue(3)
        self.concurrency_spin.setSuffix(" 個")
        adv_layout.addWidget(concurrency_label, 2, 0)
        adv_layout.addWidget(self.concurrency_spin, 2, 1, Qt.AlignLeft)
        outer.addWidget(advanced)

        queue_card = Card()
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(20, 18, 20, 18)
        queue_layout.setSpacing(12)
        queue_header = QHBoxLayout()
        queue_title = QLabel("下載佇列")
        queue_title.setObjectName("SectionTitle")
        self.queue_summary = QLabel("尚無項目")
        self.queue_summary.setObjectName("MutedText")
        self.queue_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.queue_summary.setMinimumWidth(0)
        queue_header.addWidget(queue_title)
        queue_header.addWidget(self.queue_summary)
        queue_header.addStretch(1)
        self.clear_button = QPushButton("清除已結束")
        self.clear_button.setToolTip("移除已完成、失敗與已取消的項目；下載紀錄仍會保留")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        queue_header.addWidget(self.clear_button)
        queue_layout.addLayout(queue_header)

        # Keep the queue in its own bounded viewport.  The page itself is
        # already inside the main scroll area, but letting every row grow the
        # page made a long queue push the controls around and caused progress
        # updates to appear as jumps.  This nested viewport gives the queue a
        # stable geometry and leaves its scrollbar independent.
        self.queue_scroll = QScrollArea()
        self.queue_scroll.setObjectName("QueueScroll")
        self.queue_scroll.setWidgetResizable(True)
        self.queue_scroll.setFrameShape(QFrame.NoFrame)
        self.queue_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.queue_scroll.setFixedHeight(280)
        self.queue_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        queue_host = QWidget()
        queue_host.setObjectName("QueueHost")
        self.queue_list = QVBoxLayout(queue_host)
        self.queue_list.setContentsMargins(0, 0, 0, 0)
        self.queue_list.setSpacing(9)
        self.empty_queue = QLabel("貼上連結後，下載項目會顯示在這裡。")
        self.empty_queue.setObjectName("EmptyText")
        self.empty_queue.setAlignment(Qt.AlignCenter)
        self.queue_list.addWidget(self.empty_queue)
        # Rows are inserted above this stretch so a short queue stays at the
        # top of the viewport instead of floating in its middle.
        self.queue_list.addStretch(1)
        self.queue_scroll.setWidget(queue_host)
        queue_layout.addWidget(self.queue_scroll)
        outer.addWidget(queue_card)
        outer.addStretch(1)

    def _update_count(self) -> None:
        count = len(self.parse_urls())
        file_count = len(self.pending_files)
        if count == 0 and file_count == 0:
            self.input_count.setText("尚未加入連結")
        elif file_count:
            self.input_count.setText(f"已辨識 {count} 個連結、{file_count} 個 JSON 檔案")
        else:
            self.input_count.setText(f"已辨識 {count} 個連結")

    def _account_changed(self, _index: int = -1) -> None:
        account_id = self.account_combo.currentData() or ""
        selected = next((item for item in self._accounts if item.id == account_id), None) if hasattr(self, "_accounts") else None
        connected = bool(selected and selected.authenticated)
        if selected is None:
            self.account_state.setText("請先到「帳號管理」新增 Telegram 帳號")
        else:
            self.account_state.setText("已連線，可開始下載" if connected else "此帳號尚未完成登入")
        self.account_state.setProperty("connected", connected)
        self.account_state.style().unpolish(self.account_state)
        self.account_state.style().polish(self.account_state)
        self.account_icon.setProperty("connected", connected)
        self.account_icon.style().unpolish(self.account_icon)
        self.account_icon.style().polish(self.account_icon)
        self.account_changed.emit(account_id)

    def parse_urls(self) -> list[str]:
        raw = self.url_edit.toPlainText().replace(",", "\n").replace("，", "\n")
        urls = []
        for value in raw.splitlines():
            value = value.strip()
            if not value:
                continue
            normalised = _normalise_message_url(value)
            # Keep the editor forgiving while still normalising the URL that
            # is sent to the strict backend.
            if normalised and normalised not in urls:
                urls.append(normalised)
        return urls

    def choose_folder(self) -> None:
        start = self.folder_edit.text().strip() or str(Path.home() / "Downloads")
        path = QFileDialog.getExistingDirectory(self, "選擇下載資料夾", start)
        if path:
            self.folder_edit.setText(path)

    def _request_download(self) -> None:
        urls = self.parse_urls()
        files = list(self.pending_files)
        if not urls and not files:
            show_error(self, "還沒有下載項目", "請先貼上 Telegram 訊息連結，或匯入官方 Telegram JSON 檔案。")
            self.url_edit.setFocus()
            return
        if not self.folder_edit.text().strip():
            self.choose_folder()
        folder = self.folder_edit.text().strip()
        if not folder:
            return
        options = {
            "group_album": self.group_check.isChecked(),
            "skip_existing": self.skip_check.isChecked(),
            "concurrency": self.concurrency_spin.value(),
            "account_id": self.account_combo.currentData() or "",
            "proxy": self.proxy_value,
            "files": files,
        }
        self.download_requested.emit(urls, {"output_dir": folder, **options})

    def set_accounts(self, accounts: Sequence[AccountInfo], active_id: str = "") -> None:
        self._accounts = list(accounts)
        current = active_id or self.account_combo.currentData()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for account in accounts:
            state = "已連線" if account.authenticated else "未完成登入"
            self.account_combo.addItem(f"{account.label}  ·  {state}", account.id)
        if not accounts:
            # No userData keeps QComboBox.currentData() as None, which makes
            # the empty state unambiguous to callers and retry logic.
            self.account_combo.addItem("尚未登入 Telegram")
            self.account_state.setText("請先到「帳號管理」新增 Telegram 帳號")
            connected = False
        else:
            index = self.account_combo.findData(current)
            self.account_combo.setCurrentIndex(index if index >= 0 else 0)
            selected = accounts[self.account_combo.currentIndex()]
            self.account_state.setText("已連線，可開始下載" if selected.authenticated else "此帳號尚未完成登入")
            connected = bool(selected.authenticated)
        self.account_combo.blockSignals(False)
        self.account_state.setProperty("connected", connected)
        self.account_icon.setProperty("connected", connected)
        for widget in (self.account_state, self.account_icon):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _queue_scroll_position(self) -> int:
        return int(self.queue_scroll.verticalScrollBar().value())

    def _restore_queue_scroll_position(self, value: int) -> None:
        scrollbar = self.queue_scroll.verticalScrollBar()
        scrollbar.setValue(max(0, min(int(value), scrollbar.maximum())))

    def add_jobs(self, jobs: Sequence[DownloadJob]) -> None:
        scroll_value = self._queue_scroll_position()
        if self.empty_queue is not None:
            self.empty_queue.setVisible(False)
        for job in jobs:
            self.jobs[job.id] = job
            row = QueueRow(job)
            row.cancel_requested.connect(self.cancel_requested.emit)
            row.retry_requested.connect(self.retry_requested.emit)
            row.open_requested.connect(self.open_requested.emit)
            self.rows[job.id] = row
            self.queue_list.insertWidget(self.queue_list.count() - 1, row)
        self._update_queue_summary()
        # Adding a row must never auto-follow the bottom.  Restore the exact
        # prior value after the layout has accepted the new fixed-height row.
        self._restore_queue_scroll_position(scroll_value)

    def update_job(self, job: DownloadJob) -> None:
        row = self.rows.get(job.id)
        if row is None:
            return
        scroll_value = self._queue_scroll_position()
        row.set_job(job)
        self._update_queue_summary()
        self._restore_queue_scroll_position(scroll_value)

    def update_job_file(self, job: DownloadJob, file_id: str) -> None:
        """Refresh one file of a running job; the hot path of a download."""

        row = self.rows.get(job.id)
        if row is None:
            return
        scroll_value = self._queue_scroll_position()
        status_changed = row.status.text() != job.status
        row.update_file(job, file_id)
        if status_changed:
            self._update_queue_summary()
        self._restore_queue_scroll_position(scroll_value)

    def remove_jobs(self, job_ids: Sequence[str]) -> None:
        scroll_value = self._queue_scroll_position()
        for job_id in job_ids:
            self.jobs.pop(job_id, None)
            row = self.rows.pop(job_id, None)
            if row is not None:
                self.queue_list.removeWidget(row)
                row.deleteLater()
        if self.empty_queue is not None:
            self.empty_queue.setVisible(not self.rows)
        self._update_queue_summary()
        self._restore_queue_scroll_position(scroll_value)

    def _update_queue_summary(self) -> None:
        if not self.jobs:
            self.queue_summary.setText("尚無項目")
            return
        counts: dict[str, int] = {}
        for job in self.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        self.queue_summary.setText("  ·  ".join(f"{k} {v}" for k, v in counts.items()))


class AccountPage(QWidget):
    add_requested = Signal()
    account_deleted = Signal(str)
    account_relogin_requested = Signal(str)
    backup_requested = Signal(str)
    restore_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 30)
        outer.setSpacing(18)
        header = QHBoxLayout()
        text = QVBoxLayout()
        title = QLabel("帳號管理")
        title.setObjectName("PageTitle")
        desc = QLabel("每個 Telegram 帳號各自保存本機登入工作階段，方便切換下載。")
        desc.setObjectName("MutedText")
        text.addWidget(title)
        text.addWidget(desc)
        header.addLayout(text, 1)
        self.restore_button = QPushButton("從備份還原")
        self.restore_button.setMinimumHeight(40)
        self.restore_button.setToolTip("用先前匯出的 .tdl 備份檔加入帳號，不需要重新掃碼")
        self.restore_button.clicked.connect(self.restore_requested.emit)
        header.addWidget(self.restore_button, 0, Qt.AlignTop)
        add = QPushButton("＋ 新增帳號")
        add.setObjectName("PrimaryButton")
        add.setMinimumHeight(40)
        add.clicked.connect(self.add_requested.emit)
        header.addWidget(add, 0, Qt.AlignTop)
        outer.addLayout(header)
        self.operation_status = QLabel("")
        self.operation_status.setObjectName("MutedText")
        self.operation_status.setWordWrap(True)
        self.operation_status.setVisible(False)
        outer.addWidget(self.operation_status)
        self.list_layout = QVBoxLayout()
        self.list_layout.setSpacing(10)
        outer.addLayout(self.list_layout)
        outer.addStretch(1)

    def set_operation_status(self, text: str, *, error: bool = False) -> None:
        """Show the outcome of a backup or restore above the account list."""

        self.operation_status.setText(text)
        self.operation_status.setStyleSheet(f"color: {DANGER};" if error else "")
        self.operation_status.setVisible(bool(text))

    def set_accounts(self, accounts: Sequence[AccountInfo]) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not accounts:
            card = Card()
            layout = QVBoxLayout(card)
            layout.setContentsMargins(24, 28, 24, 28)
            label = QLabel("尚未加入帳號")
            label.setObjectName("SectionTitle")
            body = QLabel("新增帳號後，就可以從下載中心直接選擇要使用的 Telegram 工作階段。")
            body.setObjectName("MutedText")
            body.setWordWrap(True)
            body.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, 0, Qt.AlignCenter)
            layout.addWidget(body)
            self.list_layout.addWidget(card)
            return
        for account in accounts:
            self.list_layout.addWidget(self._account_card(account))

    def _account_card(self, account: AccountInfo) -> QWidget:
        card = Card()
        layout = QHBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        avatar = QLabel((account.label or "T")[:1].upper())
        avatar.setObjectName("Avatar")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(42, 42)
        layout.addWidget(avatar)
        text = QVBoxLayout()
        name = QLabel(account.label)
        name.setObjectName("CardTitle")
        extra = account.display_name or account.username or account.phone or "本機工作階段"
        sub = QLabel(extra)
        sub.setObjectName("MutedText")
        text.addWidget(name)
        text.addWidget(sub)
        layout.addLayout(text, 1)
        state = Pill("已連線" if account.authenticated else "需要登入", "success" if account.authenticated else "warning")
        layout.addWidget(state)
        if not account.authenticated:
            relogin = QPushButton("重新登入")
            relogin.setToolTip("保留這個帳號的下載紀錄，重新建立 Telegram 工作階段")
            relogin.clicked.connect(lambda: self.account_relogin_requested.emit(account.id))
            layout.addWidget(relogin)
        else:
            backup = QPushButton("備份")
            backup.setToolTip("匯出登入資料，之後可在另一台電腦或重灌後還原")
            backup.clicked.connect(lambda: self.backup_requested.emit(account.id))
            layout.addWidget(backup)
        delete = QPushButton("移除")
        delete.setProperty("dangerButton", True)
        delete.setToolTip("移除本機保存的登入工作階段")
        delete.clicked.connect(lambda: self.account_deleted.emit(account.id))
        layout.addWidget(delete)
        return card


class HistoryPage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 30)
        outer.setSpacing(18)
        header = QHBoxLayout()
        text = QVBoxLayout()
        title = QLabel("下載紀錄")
        title.setObjectName("PageTitle")
        desc = QLabel("查找過去的下載項目與儲存位置。")
        desc.setObjectName("MutedText")
        text.addWidget(title)
        text.addWidget(desc)
        header.addLayout(text, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜尋檔名、連結或狀態")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search, 0, Qt.AlignTop)
        outer.addLayout(header)
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 14, 14, 14)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["時間", "項目", "狀態", "儲存位置"])
        self.table.setObjectName("HistoryTable")
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 310)
        self.table.setColumnWidth(2, 100)
        layout.addWidget(self.table)
        outer.addWidget(card, 1)
        self._items: list[HistoryInfo] = []

    def set_history(self, items: Sequence[HistoryInfo]) -> None:
        self._items = list(items)
        self._render(self._items)

    def _filter(self, text: str) -> None:
        self._render([
            item
            for item in self._items
            if search_matches(text, item.title, item.url, item.status, item.path)
        ])

    def _render(self, items: Sequence[HistoryInfo]) -> None:
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(item.created_at or "—"))
            title = item.title if not item.url else f"{item.title}\n{item.url}"
            if item.summary:
                title = f"{item.title}\n{item.summary}"
            title_item = QTableWidgetItem(title)
            title_item.setToolTip("\n".join(part for part in (item.url, item.summary, item.detail) if part))
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, QTableWidgetItem(item.status))
            self.table.setItem(row, 3, QTableWidgetItem(item.path or "—"))
            self.table.setRowHeight(row, 44)


class SettingsPage(QWidget):
    settings_changed = Signal(dict)
    advanced_changed = Signal(dict)

    def __init__(self, bridge: BackendBridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self._preferences: dict[str, Any] = {}
        try:
            prefs = bridge.preferences()
            self._preferences = _preference_values(prefs)
        except Exception:
            self._preferences = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 30)
        outer.setSpacing(18)
        title = QLabel("設定與說明")
        title.setObjectName("PageTitle")
        desc = QLabel("調整下載偏好；變更會自動保存到本機。")
        desc.setObjectName("MutedText")
        outer.addWidget(title)
        outer.addWidget(desc)

        card = Card()
        form = QFormLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(14)
        self.folder_edit = QLineEdit(_safe_str(self._preferences.get("download_folder", "")))
        self.folder_edit.setReadOnly(True)
        folder_wrap = QWidget()
        folder_layout = QHBoxLayout(folder_wrap)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        choose = QPushButton("選擇")
        choose.clicked.connect(self._choose_folder)
        folder_layout.addWidget(self.folder_edit, 1)
        folder_layout.addWidget(choose)
        form.addRow("預設下載資料夾", folder_wrap)
        self.proxy_edit = QLineEdit(_safe_str(self._preferences.get("proxy", "")))
        self.proxy_edit.setPlaceholderText("留白代表直接連線，例如 http://127.0.0.1:7890")
        form.addRow("代理伺服器（選填）", self.proxy_edit)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 16)
        self.concurrency.setValue(int(self._preferences.get("concurrency", 3)))
        self.concurrency.setSuffix(" 個")
        form.addRow("同時下載數", self.concurrency)
        for widget in (self.proxy_edit, self.concurrency):
            if isinstance(widget, QLineEdit):
                widget.editingFinished.connect(self.save)
            else:
                widget.valueChanged.connect(lambda _value: self.save())
        outer.addWidget(card)
        outer.addWidget(self._build_advanced_card())

        info = Card()
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(20, 18, 20, 18)
        info_title = QLabel("使用方式")
        info_title.setObjectName("SectionTitle")
        body = QLabel(
            "1. 在「帳號管理」完成一次 Telegram 登入；換電腦前可先備份，之後用「從備份還原」加入。\n"
            "2. 整個頻道或群組：到「頻道下載」選對話、日期與類型，掃描後開始下載。\n"
            "3. 單則或多則訊息：到「下載中心」貼上訊息連結並選擇資料夾。\n"
            "4. 「上傳與轉傳」可把本機檔案送到 Telegram，或把訊息轉到另一個對話。\n"
            "5. 下載完成後可從下載佇列開啟資料夾，或在「下載紀錄」搜尋。"
        )
        body.setObjectName("BodyText")
        body.setWordWrap(True)
        info_layout.addWidget(info_title)
        info_layout.addWidget(body)
        outer.addWidget(info)

        data = Card()
        data_layout = QVBoxLayout(data)
        data_layout.setContentsMargins(20, 18, 20, 18)
        data_title = QLabel("本機資料與隱私")
        data_title.setObjectName("SectionTitle")
        data_body = QLabel(f"帳號工作階段與下載紀錄由本機後端保存於：\n{self.bridge.data_root}\n\n密碼只在登入當下送給 Telegram 核心，不會保存。移除帳號會刪除該帳號的本機登入工作階段。")
        data_body.setObjectName("BodyText")
        data_body.setWordWrap(True)
        data_layout.addWidget(data_title)
        data_layout.addWidget(data_body)
        outer.addWidget(data)
        outer.addStretch(1)

    def _build_advanced_card(self) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        title = QLabel("進階下載設定")
        title.setObjectName("SectionTitle")
        hint = QLabel("套用後，之後新增的下載會使用這些設定；已在佇列中的工作維持建立時的設定。")
        hint.setObjectName("MutedText")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        saved_template = _safe_str(self._preferences.get("template", ""))
        self.template_combo = QComboBox()
        for label, template in TEMPLATE_PRESETS:
            self.template_combo.addItem(label, template)
        self.template_combo.addItem("自訂格式…", None)
        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText("例如 {{ .MessageID }}_{{ filenamify .FileName }}")
        self.template_edit.setToolTip(
            "可用變數：.DialogID 對話編號、.MessageID 訊息編號、.MessageDate 訊息時間、"
            ".FileName 原檔名、.FileCaption 訊息文字、.FileSize 檔案大小、.DownloadDate 下載時間。\n"
            "可用函式：filenamify（移除不可用於檔名的字元）、formatDate（格式化時間）。"
        )
        preset_index = self.template_combo.findData(saved_template)
        if preset_index < 0:
            preset_index = self.template_combo.count() - 1
            self.template_edit.setText(saved_template)
        self.template_combo.setCurrentIndex(preset_index)
        self.template_combo.currentIndexChanged.connect(self._template_preset_changed)
        template_wrap = QWidget()
        template_layout = QVBoxLayout(template_wrap)
        template_layout.setContentsMargins(0, 0, 0, 0)
        template_layout.setSpacing(6)
        template_layout.addWidget(self.template_combo)
        template_layout.addWidget(self.template_edit)
        form.addRow("檔名格式", template_wrap)

        self.include_edit = QLineEdit(_safe_str(self._preferences.get("include_ext", "")))
        self.include_edit.setPlaceholderText("例如 mp4, mkv（留白代表全部）")
        form.addRow("只下載這些副檔名", self.include_edit)
        self.exclude_edit = QLineEdit(_safe_str(self._preferences.get("exclude_ext", "")))
        self.exclude_edit.setPlaceholderText("例如 jpg, png（與上一欄擇一填寫）")
        form.addRow("排除這些副檔名", self.exclude_edit)

        self.order_combo = QComboBox()
        self.order_combo.addItem("由舊到新（預設）", False)
        self.order_combo.addItem("由新到舊", True)
        self.order_combo.setCurrentIndex(1 if self._preferences.get("desc") else 0)
        form.addRow("下載順序", self.order_combo)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setValue(int(self._preferences.get("threads", 4) or 4))
        self.threads_spin.setSuffix(" 條")
        self.threads_spin.setToolTip("每個檔案同時使用的連線數。大檔案可調高，遇到限速時請調低。")
        form.addRow("每個檔案的連線數", self.threads_spin)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSingleStep(100)
        self.delay_spin.setValue(int(self._preferences.get("delay_ms", 0) or 0))
        self.delay_spin.setSuffix(" 毫秒")
        self.delay_spin.setToolTip("每個檔案開始前的等待時間。Telegram 經常要求稍後再試時可設為 500 以上。")
        form.addRow("檔案之間的間隔", self.delay_spin)
        layout.addLayout(form)

        self.rewrite_check = QCheckBox("依檔案內容修正副檔名（例如沒有副檔名的影片補上 .mp4）")
        self.rewrite_check.setChecked(bool(self._preferences.get("rewrite_ext")))
        layout.addWidget(self.rewrite_check)
        self.takeout_check = QCheckBox("使用 Takeout 模式（大量下載較不易被限速；剛登入的帳號可能需等待 24 小時）")
        self.takeout_check.setChecked(bool(self._preferences.get("takeout")))
        layout.addWidget(self.takeout_check)

        action = QHBoxLayout()
        self.advanced_status = QLabel("")
        self.advanced_status.setObjectName("MutedText")
        self.advanced_status.setWordWrap(True)
        action.addWidget(self.advanced_status, 1)
        reset = QPushButton("恢復預設")
        reset.clicked.connect(self._reset_advanced)
        action.addWidget(reset)
        apply_button = QPushButton("套用")
        apply_button.setObjectName("PrimaryButton")
        apply_button.clicked.connect(self._apply_advanced)
        action.addWidget(apply_button)
        layout.addLayout(action)
        self._template_preset_changed()
        return card

    def _template_preset_changed(self, _index: int = -1) -> None:
        custom = self.template_combo.currentData() is None
        self.template_edit.setVisible(custom)

    def advanced_values(self) -> dict[str, Any]:
        preset = self.template_combo.currentData()
        return {
            "template": self.template_edit.text().strip() if preset is None else _safe_str(preset),
            "include_ext": self.include_edit.text().strip(),
            "exclude_ext": self.exclude_edit.text().strip(),
            "desc": bool(self.order_combo.currentData()),
            "takeout": self.takeout_check.isChecked(),
            "rewrite_ext": self.rewrite_check.isChecked(),
            "threads": self.threads_spin.value(),
            "delay_ms": self.delay_spin.value(),
        }

    def _reset_advanced(self) -> None:
        self.template_combo.setCurrentIndex(0)
        self.template_edit.clear()
        self.include_edit.clear()
        self.exclude_edit.clear()
        self.order_combo.setCurrentIndex(0)
        self.threads_spin.setValue(4)
        self.delay_spin.setValue(0)
        self.rewrite_check.setChecked(False)
        self.takeout_check.setChecked(False)
        self._apply_advanced()

    def _apply_advanced(self) -> None:
        values = self.advanced_values()
        if self.template_combo.currentData() is None and not values["template"]:
            self.set_advanced_status("請填寫自訂檔名格式，或改選其他格式。", error=True)
            return
        # Validation is a pure function of the text, so run it here to show
        # the backend's own message beside the fields instead of a dialog.
        normalize = getattr(self.bridge.module, "normalize_download_advanced", None)
        if callable(normalize):
            try:
                normalize(
                    {
                        "template": values["template"],
                        "include": values["include_ext"],
                        "exclude": values["exclude_ext"],
                    }
                )
            except ValueError as exc:
                self.set_advanced_status(_safe_str(exc), error=True)
                return
        self._preferences.update(values)
        self.set_advanced_status("正在儲存…")
        self.advanced_changed.emit(values)

    def set_advanced_status(self, text: str, *, error: bool = False) -> None:
        self.advanced_status.setText(text)
        self.advanced_status.setStyleSheet(f"color: {DANGER};" if error else "")

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "選擇預設下載資料夾", self.folder_edit.text() or str(Path.home() / "Downloads"))
        if path:
            self.folder_edit.setText(path)
            self.save()

    def save(self) -> None:
        payload = {
            "download_folder": self.folder_edit.text().strip(),
            "proxy": self.proxy_edit.text().strip(),
            "concurrency": self.concurrency.value(),
        }
        self._preferences.update(payload)
        self.settings_changed.emit(payload)

    def values(self) -> dict[str, Any]:
        return {
            "download_folder": self.folder_edit.text().strip(),
            "proxy": self.proxy_edit.text().strip(),
            "concurrency": self.concurrency.value(),
        }


class MainWindow(QMainWindow):
    def __init__(self, bridge: Optional[BackendBridge] = None, parent: Optional[QWidget] = None, *, resume_downloads: bool = True) -> None:
        super().__init__(parent)
        self.bridge = bridge or BackendBridge()
        self.accounts: list[AccountInfo] = []
        self.jobs: dict[str, DownloadJob] = {}
        self._threads: dict[str, QThread] = {}
        self._workers: dict[str, DownloadWorker] = {}
        self._store_threads: list[QThread] = []
        self._store_workers: list[StoreReadWorker] = []
        self._action_threads: list[QThread] = []
        self._action_workers: list[StoreActionWorker] = []
        self._action_relays: list[_StoreActionRelay] = []
        self._resume_downloads = resume_downloads
        self._accounts_loaded = False
        self._queue_loaded = False
        self._resume_attempted: set[str] = set()
        self._preparing_jobs: set[str] = set()
        self._cancelled_before_start: set[str] = set()
        self._closing = False
        self._allow_close = False
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 680)
        self.resize(1180, 800)
        self._build_ui()
        self._load_preferences()
        self.refresh_accounts()
        self.refresh_history()
        self.refresh_queue()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("AppRoot")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(224)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 22, 18, 20)
        side_layout.setSpacing(6)
        brand = QHBoxLayout()
        logo = QLabel("t")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(36, 36)
        brand_text = QVBoxLayout()
        brand_name = QLabel("tdl")
        brand_name.setObjectName("BrandName")
        brand_sub = QLabel("繁中下載器")
        brand_sub.setObjectName("BrandSub")
        brand_text.addWidget(brand_name)
        brand_text.addWidget(brand_sub)
        brand.addWidget(logo)
        brand.addLayout(brand_text)
        side_layout.addLayout(brand)
        side_layout.addSpacing(32)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[SidebarButton] = []
        nav_icons = (
            QStyle.SP_DirOpenIcon,
            QStyle.SP_ArrowDown,
            QStyle.SP_ArrowUp,
            QStyle.SP_DirHomeIcon,
            QStyle.SP_FileDialogDetailedView,
            QStyle.SP_FileDialogContentsView,
        )
        nav_specs = tuple((self.style().standardIcon(icon), text) for icon, text in zip(nav_icons, PAGE_NAMES))
        for index, (icon, text) in enumerate(nav_specs):
            button = SidebarButton(icon, text)
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side_layout.addWidget(button)
        side_layout.addStretch(1)
        tip = QLabel("小提醒\n下載私有訊息前，請確認帳號有權限存取。")
        tip.setObjectName("SidebarTip")
        tip.setWordWrap(True)
        side_layout.addWidget(tip)
        version = QLabel(VERSION_TEXT)
        version.setObjectName("SidebarVersion")
        side_layout.addWidget(version)
        root.addWidget(sidebar)

        content_frame = QFrame()
        content_frame.setObjectName("ContentFrame")
        content_layout = QVBoxLayout(content_frame)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("Topbar")
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(28, 14, 30, 14)
        top_layout.addStretch(1)
        self.top_account = QLabel("未登入")
        self.top_account.setObjectName("TopAccount")
        self.top_account.setProperty("connected", False)
        top_layout.addWidget(self.top_account)
        content_layout.addWidget(topbar)
        self.stack = QStackedWidget()
        self.channel_page = ChannelPage()
        self.home_page = DownloadPage()
        self.transfer_page = TransferPage()
        self.account_page = AccountPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage(self.bridge)
        self.channel_page.set_runner(self.bridge.runner)
        self.transfer_page.set_runner(self.bridge.runner)
        # Same order as PAGE_NAMES and the sidebar buttons.
        for page in (
            self.channel_page, self.home_page, self.transfer_page,
            self.account_page, self.history_page, self.settings_page,
        ):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        content_layout.addWidget(self.stack, 1)
        root.addWidget(content_frame, 1)

        for index, button in enumerate(self.nav_buttons):
            button.clicked.connect(lambda checked=False, i=index: self._show_page(i))
        self.nav_buttons[0].setChecked(True)
        self.home_page.account_requested.connect(lambda: self._show_widget(self.account_page))
        self.home_page.download_requested.connect(self._enqueue_downloads)
        self.home_page.export_requested.connect(self._export_json)
        self.home_page.import_requested.connect(self._import_json)
        self.home_page.cancel_requested.connect(self.cancel_job)
        self.home_page.retry_requested.connect(self.retry_job)
        self.home_page.open_requested.connect(self.open_output)
        self.home_page.account_changed.connect(self._set_active_account)
        self.home_page.clear_requested.connect(self._clear_finished_jobs)
        self.account_page.add_requested.connect(self._add_account)
        self.account_page.account_deleted.connect(self._remove_account)
        self.account_page.backup_requested.connect(self._backup_account)
        self.account_page.restore_requested.connect(self._restore_account)
        self.settings_page.settings_changed.connect(self._settings_changed)
        self.settings_page.advanced_changed.connect(self._advanced_changed)
        self.account_page.account_relogin_requested.connect(self._relogin_account)
        self.channel_page.account_changed.connect(self._set_active_account)
        self.channel_page.download_requested.connect(self._enqueue_channel_downloads)
        self.transfer_page.account_changed.connect(self._set_active_account)

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if 0 <= index < len(self.nav_buttons):
            self.nav_buttons[index].setChecked(True)
        current = self.stack.currentWidget()
        current_page = current.widget() if isinstance(current, QScrollArea) else current
        if current_page is self.history_page:
            self.refresh_history()

    def _show_widget(self, widget: QWidget) -> None:
        for index in range(self.stack.count()):
            container = self.stack.widget(index)
            page = container.widget() if isinstance(container, QScrollArea) else container
            if page is widget:
                self._show_page(index)
                return

    def _load_preferences(self) -> None:
        values = self.settings_page.values()
        self.home_page.folder_edit.setText(_safe_str(values.get("download_folder", "")))
        self.home_page.proxy_value = _safe_str(values.get("proxy", ""))
        concurrency = int(values.get("concurrency", 2) or 2)
        self.home_page.concurrency_spin.setValue(max(1, min(16, concurrency)))
        self.channel_page.set_download_folder(_safe_str(values.get("download_folder", "")))
        self.channel_page.set_download_preferences(
            concurrency=concurrency,
            proxy=_safe_str(values.get("proxy", "")),
            skip_existing=self.home_page.skip_check.isChecked(),
        )
        self.transfer_page.set_proxy(_safe_str(values.get("proxy", "")))

    def _set_top_account(self, text: str, connected: bool = False) -> None:
        self.top_account.setText(text)
        self.top_account.setProperty("connected", bool(connected))
        self.top_account.style().unpolish(self.top_account)
        self.top_account.style().polish(self.top_account)

    def _settings_changed(self, values: Mapping[str, Any]) -> None:
        if values.get("download_folder") is not None:
            self.home_page.folder_edit.setText(_safe_str(values.get("download_folder")))
        if values.get("concurrency") is not None:
            self.home_page.concurrency_spin.setValue(int(values["concurrency"]))
        if values.get("proxy") is not None:
            self.home_page.proxy_value = _safe_str(values["proxy"])
        self.channel_page.set_download_folder(_safe_str(values.get("download_folder", self.channel_page.folder_edit.text())))
        self.channel_page.set_download_preferences(
            concurrency=int(values.get("concurrency", self.home_page.concurrency_spin.value()) or 2),
            proxy=_safe_str(values.get("proxy", self.home_page.proxy_value)),
            skip_existing=self.home_page.skip_check.isChecked(),
        )
        self.transfer_page.set_proxy(self.home_page.proxy_value)
        self._start_store_action(
            lambda: self.bridge.update_preferences(
                download_dir=_safe_str(values.get("download_folder", "")),
                proxy=_safe_str(values.get("proxy", "")),
                concurrency=int(values.get("concurrency", 2) or 2),
            )
        )

    def _advanced_changed(self, values: Mapping[str, Any]) -> None:
        payload = dict(values)
        self._start_store_action(
            lambda: self.bridge.update_preferences(**payload),
            lambda _result: self.settings_page.set_advanced_status("已套用，之後新增的下載會使用這些設定。"),
            lambda detail: self.settings_page.set_advanced_status(
                _last_error_line(detail) or "設定沒有儲存，請檢查欄位內容。", error=True
            ),
        )

    def refresh_accounts(self) -> None:
        self._set_top_account("正在讀取帳號…")
        self._start_store_read("accounts")

    def refresh_history(self) -> None:
        self._start_store_read("history")

    def refresh_queue(self) -> None:
        self._start_store_read("queue")

    def _start_store_read(self, kind: str) -> None:
        thread = QThread(self)
        worker = StoreReadWorker(self.bridge, kind)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._store_ready)
        worker.failed.connect(self._store_failed)
        worker.ready.connect(lambda *_: thread.quit())
        worker.failed.connect(lambda *_: thread.quit())
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda thread=thread, worker=worker: self._forget_aux_thread(thread, worker))
        thread.finished.connect(thread.deleteLater)
        self._store_threads.append(thread)
        self._store_workers.append(worker)
        thread.start()

    @Slot(str, object)
    def _store_ready(self, kind: str, values: Any) -> None:
        if kind == "accounts":
            active_id = _safe_str(values.get("active_id", "")) if isinstance(values, Mapping) else ""
            account_values = values.get("items", []) if isinstance(values, Mapping) else values
            self.accounts = list(account_values or [])
            self.home_page.set_accounts(self.accounts, active_id)
            self.account_page.set_accounts(self.accounts)
            self.channel_page.set_accounts(self.accounts, active_id)
            self.transfer_page.set_accounts(self.accounts, active_id)
            selected_id = self.home_page.account_combo.currentData() or ""
            selected = next((item for item in self.accounts if item.id == selected_id), None)
            # The first launch has no active_account_id yet.  Select a useful
            # account once and persist that choice through the Store so the
            # next launch opens on the same account.
            if selected is not None and selected.id != active_id:
                self._set_active_account(selected.id)
            if selected is not None:
                state = "已連線" if selected.authenticated else "未完成登入"
                self._set_top_account(f"{selected.label} · {state}", selected.authenticated)
            else:
                self._set_top_account("未登入")
            self._accounts_loaded = True
        elif kind == "history":
            self.history_page.set_history(list(values or []))
        else:
            restored: list[DownloadJob] = []
            for index, item in enumerate(values or []):
                job = self._restore_job(item, index)
                existing_ids = {current.backend_job_id or current.id for current in self.jobs.values()}
                if job.status != "已完成" and job.id not in existing_ids:
                    restored.append(job)
            if restored:
                for job in restored:
                    self.jobs[job.id] = job
                self.home_page.add_jobs(restored)
            self._queue_loaded = True
        self._resume_saved_jobs()

    def _resume_saved_jobs(self) -> None:
        if self._closing or not self._resume_downloads or not (self._accounts_loaded and self._queue_loaded):
            return
        authenticated = {account.id for account in self.accounts if account.authenticated}
        started = False
        for job in list(self.jobs.values()):
            if not job.resume_on_start or job.id in self._resume_attempted:
                continue
            if job.account_id not in authenticated:
                job.status = "已暫停"
                job.error = "登入原下載帳號後即可繼續"
                self.home_page.update_job(job)
                continue
            self._resume_attempted.add(job.id)
            job.resume_on_start = False
            options = dict(job.options)
            options.update(account_id=job.account_id, output_dir=job.output_dir, continue_download=True, skip_existing=True)
            self._start_job(job, options)
            started = True
        if started:
            self._show_widget(self.home_page)

    @Slot(str, str)
    def _store_failed(self, kind: str, detail: str) -> None:
        if kind == "accounts":
            self.accounts = []
            self.home_page.set_accounts([])
            self.account_page.set_accounts([])
            self.channel_page.set_accounts([])
            self.transfer_page.set_accounts([])
            self._set_top_account("帳號資料暫不可用")
        elif kind == "history":
            self.history_page.set_history([])

    def _set_active_account(self, account_id: str, on_success: Optional[Callable[[Any], None]] = None) -> None:
        if not account_id:
            return
        selected = next((item for item in self.accounts if item.id == account_id), None)
        if selected is not None:
            state = "已連線" if selected.authenticated else "未完成登入"
            self._set_top_account(f"{selected.label} · {state}", selected.authenticated)
        if self.channel_page.active_account_id() != account_id:
            self.channel_page.cancel_operations()
        # Keep the two download entry points on one active account.  Both
        # combo boxes block their own signals while being synchronized, so a
        # channel selection never causes a second Store write loop.
        self.home_page.set_accounts(self.accounts, account_id)
        self.channel_page.set_accounts(self.accounts, account_id)
        # An upload or forward keeps the account it was started with.
        if not self.transfer_page.is_busy():
            self.transfer_page.set_accounts(self.accounts, account_id)

        def done(result: Any) -> None:
            if on_success is not None:
                on_success(result)

        self._start_store_action(lambda: self.bridge.set_active_account(account_id), on_success=done)

    @staticmethod
    def _restore_job(value: Any, index: int) -> DownloadJob:
        links = list(_extract(value, "links", default=[]) or [])
        files = list(_extract(value, "files", default=[]) or [])
        links = [str(link) for link in links if isinstance(link, str)]
        files = [str(path) for path in files if isinstance(path, str)]
        backend_id = _safe_str(_extract(value, "id", default=f"restored-{index}"))
        account_id = _safe_str(_extract(value, "account_id", default=""))
        output_dir = _safe_str(_extract(value, "output_dir", default=""))
        filename = _safe_str(_extract(value, "filename", default=""))
        saved_title = _safe_str(_extract(value, "title", "name", default=""))
        raw_status = _safe_str(_extract(value, "status", default="queued")).lower()
        status_map = {
            "queued": "已暫停",
            "running": "已暫停",
            "interrupted": "已暫停",
            "paused": "已暫停",
            "failed": "失敗",
            "cancelled": "已取消",
            "canceled": "已取消",
            "completed": "已完成",
        }
        status = status_map.get(raw_status, "等待中")
        title = saved_title or filename or (Path(files[0]).name if files else f"已恢復下載 {index + 1}")
        if len(links) > 1:
            title = f"{title}（{len(links)} 個連結）"
        options = {
            "account_id": account_id,
            "output_dir": output_dir,
            "continue_download": bool(_extract(value, "continue_download", default=True)),
            "skip_existing": bool(_extract(value, "skip_same", default=True)),
            "group_album": bool(_extract(value, "group", default=True)),
            "group_by_chat": bool(_extract(value, "group_by_chat", default=False)),
            "concurrency": int(_extract(value, "concurrency", default=2) or 2),
        }
        saved_files = _extract(value, "file_progress", default={})
        file_progress = {
            str(key): dict(item) for key, item in saved_files.items() if isinstance(item, Mapping)
        } if isinstance(saved_files, Mapping) else {}
        return DownloadJob(
            id=backend_id,
            url=links[0] if links else (files[0] if files else "JSON 匯入項目"),
            title=title,
            output_dir=output_dir,
            status=status,
            progress=_extract(value, "percent", default=None),
            transferred=int(_extract(value, "current", "transferred", default=0) or 0),
            total=int(_extract(value, "total", "size", default=0) or 0),
            speed=_safe_str(_extract(value, "speed", default="")),
            error=_safe_str(_extract(value, "error", default="")),
            account_id=account_id,
            links=links,
            files=files,
            backend_job_id=backend_id,
            resume_on_start=raw_status in {"queued", "running", "interrupted", "paused"},
            file_progress=file_progress,
            expected_files=int(_extract(value, "expected_files", default=0) or 0),
            expected_bytes=int(_extract(value, "expected_bytes", default=0) or 0),
            options=options,
        )

    def _start_store_action(
        self,
        action: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_failure: Optional[Callable[[str], None]] = None,
    ) -> None:
        thread = QThread(self)
        worker = StoreActionWorker(action)
        relay = _StoreActionRelay(on_success, on_failure, self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # ``relay`` belongs to MainWindow's GUI thread.  Connecting the
        # worker signals to its decorated slots therefore queues callback
        # delivery back to the GUI thread, even when callers pass lambdas or
        # nested functions.  The worker is kept alive until the callback has
        # run, then the relay asks its QThread to stop.
        worker.finished.connect(relay.success, Qt.QueuedConnection)
        worker.failed.connect(relay.failure, Qt.QueuedConnection)
        relay.completed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(relay.deleteLater)
        thread.finished.connect(lambda thread=thread, worker=worker, relay=relay: self._forget_aux_thread(thread, worker, relay))
        thread.finished.connect(thread.deleteLater)
        self._action_threads.append(thread)
        self._action_workers.append(worker)
        self._action_relays.append(relay)
        thread.start()

    def _forget_aux_thread(
        self,
        thread: QThread,
        worker: QObject,
        relay: Optional[_StoreActionRelay] = None,
    ) -> None:
        """Drop finished Store wrappers so shutdown never calls dead Qt objects."""

        try:
            self._store_threads.remove(thread)
        except ValueError:
            pass
        try:
            self._store_workers.remove(worker)  # type: ignore[arg-type]
        except ValueError:
            pass
        try:
            self._action_threads.remove(thread)
        except ValueError:
            pass
        try:
            self._action_workers.remove(worker)  # type: ignore[arg-type]
        except ValueError:
            pass
        if relay is not None:
            try:
                self._action_relays.remove(relay)
            except ValueError:
                pass

    def _add_account(self) -> None:
        dialog = LoginDialog(self.bridge, self)
        if dialog.exec() == QDialog.Accepted:
            if dialog.account_id:
                # Refresh only after the Store has written the active id; it
                # avoids a race where the account list briefly selects the
                # previous account after a successful login.
                self._set_active_account(dialog.account_id, lambda _result: self.refresh_accounts())
            else:
                self.refresh_accounts()
            self._show_widget(self.channel_page)
        else:
            # A cancelled/failed login removes an account that was created for
            # that attempt.  Refresh the list as well so a stale placeholder
            # can never remain visible after the dialog closes.
            self.refresh_accounts()

    def _relogin_account(self, account_id: str) -> None:
        account = next((item for item in self.accounts if item.id == account_id), None)
        if account is None:
            self.refresh_accounts()
            return
        dialog = LoginDialog(self.bridge, self, account=account)
        if dialog.exec() == QDialog.Accepted:
            repaired_id = dialog.account_id or account.id
            self._set_active_account(repaired_id, lambda _result: self.refresh_accounts())
            self._show_widget(self.channel_page)
        else:
            # Existing records are intentionally left in place on cancel or
            # failed authentication; only the next explicit retry can repair
            # their session.
            self.refresh_accounts()

    def _remove_account(self, account_id: str) -> None:
        account = next((item for item in self.accounts if item.id == account_id), None)
        label = account.label if account else "此帳號"
        if not ask_remove_account(self, label):
            return
        self._start_store_action(
            lambda: self.bridge.delete_account(account_id),
            lambda _result: self.refresh_accounts(),
            lambda detail: show_error(self, "無法移除帳號", "帳號工作階段沒有被移除，請稍後再試。", detail),
        )

    def _backup_account(self, account_id: str) -> None:
        account = next((item for item in self.accounts if item.id == account_id), None)
        if account is None:
            return
        if not ask_confirm(
            self,
            "備份帳號登入資料",
            f"要匯出「{account.label}」的登入資料嗎？",
            "備份檔沒有加密：任何拿到這個檔案的人都能直接登入你的 Telegram 帳號。"
            "請只存放在自己信任的位置，不要上傳或傳給別人。",
            "選擇儲存位置",
        ):
            return
        suggested = Path.home() / f"tdl-{datetime.now().strftime('%Y%m%d')}.backup.tdl"
        path, _selected = QFileDialog.getSaveFileName(self, "儲存帳號備份", str(suggested), "tdl 備份檔 (*.tdl)")
        if not path:
            return
        if not path.lower().endswith(".tdl"):
            path += ".tdl"
        self.account_page.set_operation_status(f"正在備份「{account.label}」…")
        self._start_store_action(
            lambda: self.bridge.backup_account(account_id, path),
            lambda saved: self.account_page.set_operation_status(f"已備份「{account.label}」到 {saved}"),
            lambda detail: self.account_page.set_operation_status(
                f"備份失敗：{_last_error_line(detail) or '請確認儲存位置可以寫入。'}", error=True
            ),
        )

    def _restore_account(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, "選擇帳號備份檔", str(Path.home()), "tdl 備份檔 (*.tdl)")
        if not path:
            return
        label, accepted = QInputDialog.getText(
            self, "從備份還原", "為這個帳號取一個方便辨識的名稱：", text=Path(path).name.split(".")[0][:40]
        )
        label = label.strip()
        if not accepted or not label:
            return
        self.account_page.restore_button.setEnabled(False)
        self.account_page.set_operation_status("正在還原備份並向 Telegram 確認登入狀態…")

        def restored(account_id: Any) -> None:
            self.account_page.restore_button.setEnabled(True)
            self.account_page.set_operation_status(f"已還原帳號「{label}」。")
            self._set_active_account(_safe_str(account_id), lambda _result: self.refresh_accounts())

        def failed(detail: str) -> None:
            self.account_page.restore_button.setEnabled(True)
            self.account_page.set_operation_status(
                f"還原失敗：{_last_error_line(detail) or '備份檔無法使用。'}", error=True
            )
            self.refresh_accounts()

        proxy = self.home_page.proxy_value
        self._start_store_action(lambda: self.bridge.restore_account(label, path, proxy), restored, failed)

    def _clear_finished_jobs(self) -> None:
        ended = [job_id for job_id, job in self.jobs.items() if job.status in ("已完成", "失敗", "已取消")]
        if not ended:
            return

        def cleared(_removed: Any) -> None:
            for job_id in ended:
                self.jobs.pop(job_id, None)
            self.home_page.remove_jobs(ended)

        self._start_store_action(
            self.bridge.clear_finished_jobs,
            cleared,
            lambda detail: show_error(self, "無法清除", "下載佇列沒有變更，請稍後再試。", detail),
        )

    def _enqueue_downloads(self, urls: list[str], options: dict[str, Any]) -> None:
        account_id = options.get("account_id", "")
        files = [str(path) for path in (options.get("files") or []) if isinstance(path, str) and path.strip()]
        account = next((item for item in self.accounts if item.id == account_id), None)
        if account_id and account and not account.authenticated:
            show_error(self, "帳號尚未登入", "請先在帳號管理完成 Telegram 登入，再開始下載。")
            self._show_widget(self.account_page)
            return
        if not account_id:
            show_error(self, "請選擇帳號", "請先新增並連線一個 Telegram 帳號，再開始下載。")
            return
        if not urls and not files:
            show_error(self, "沒有可下載項目", "請先貼上連結或匯入官方 Telegram JSON 檔案。")
            return
        jobs: list[DownloadJob] = []
        folder = _safe_str(options.get("output_dir"))
        for index, url in enumerate(urls):
            # The title stays neutral until the core gives us the real media
            # name. Never invent a percentage or pretend metadata was fetched.
            job = DownloadJob(
                id=f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{index}",
                url=url,
                title=f"Telegram 訊息 {index + 1}",
                output_dir=folder,
                account_id=account_id,
                links=[url],
                options=dict(options),
            )
            jobs.append(job)
            self.jobs[job.id] = job
        if files:
            index = len(jobs)
            file_title = _safe_str(options.get("title")).strip() or f"官方 Telegram JSON（{len(files)} 個檔案）"
            job = DownloadJob(
                id=f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{index}",
                url=files[0],
                title=file_title,
                output_dir=folder,
                account_id=account_id,
                links=[],
                files=files,
                expected_files=int(options.get("expected_files", 0) or 0),
                expected_bytes=int(options.get("expected_bytes", 0) or 0),
                options=dict(options),
            )
            jobs.append(job)
            self.jobs[job.id] = job
        self.home_page.add_jobs(jobs)
        for job in jobs:
            self._start_job(job, options)

    def _enqueue_channel_downloads(self, urls: list[str], options: dict[str, Any]) -> None:
        """Queue a scanned channel export, then show its real queue row."""

        before = set(self.jobs)
        self._enqueue_downloads(urls, options)
        if any(job_id not in before for job_id in self.jobs):
            self._show_widget(self.home_page)

    def _start_job(self, job: DownloadJob, options: Mapping[str, Any]) -> None:
        old_thread = self._threads.get(job.id)
        if job.id in self._preparing_jobs or (old_thread is not None and _thread_is_running(old_thread)):
            return
        job.options = dict(options)
        job.account_id = _safe_str(options.get("account_id"), job.account_id)
        job.status = "處理中"
        job.closing_pause = False
        job.error = ""
        self._preparing_jobs.add(job.id)
        self.home_page.update_job(job)

        def prepared(saved: Any) -> None:
            self._preparing_jobs.discard(job.id)
            job.backend_job_id = _safe_str(_extract(saved, "id", default=job.backend_job_id))
            job.files = list(_extract(saved, "files", default=job.files) or [])
            if job.id in self._cancelled_before_start:
                self._cancelled_before_start.discard(job.id)
                job.status = "已取消"
                self._start_store_action(lambda: self.bridge.store.finish_job(job.backend_job_id, "cancelled"))
            elif self._closing:
                job.status = "已暫停"
                # A queued record is already durable and restartable.  Mark
                # paused for a clearer history entry, without starting core.
                self._start_store_action(lambda: self.bridge.store.finish_job(job.backend_job_id, "paused"))
            else:
                self._launch_download(job, job.options)
            self.home_page.update_job(job)

        def failed(detail: str) -> None:
            self._preparing_jobs.discard(job.id)
            self._cancelled_before_start.discard(job.id)
            self._job_failed(job.id, "無法保存下載工作，請確認下載清單及本機資料夾。", detail)

        self._start_store_action(lambda: self.bridge.prepare_download(job, job.options), prepared, failed)

    def _launch_download(self, job: DownloadJob, options: Mapping[str, Any]) -> None:
        job.status = "下載中"
        self.home_page.update_job(job)
        thread = QThread(self)
        worker = DownloadWorker(self.bridge, job, options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._job_progress)
        worker.succeeded.connect(self._job_succeeded)
        worker.failed.connect(self._job_failed)
        worker.canceled.connect(self._job_canceled)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(lambda *_: thread.quit())
        worker.canceled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda job_id=job.id: self._forget_job_worker(job_id))
        thread.finished.connect(thread.deleteLater)
        self._threads[job.id] = thread
        self._workers[job.id] = worker
        thread.start()

    def _forget_job_worker(self, job_id: str) -> None:
        self._threads.pop(job_id, None)
        self._workers.pop(job_id, None)

    @Slot(str, object)
    def _job_progress(self, job_id: str, value: Any) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        if isinstance(value, Mapping):
            event_name = value.get("event", "progress")
            if event_name not in {"progress", "error"}:
                return
            file_id = _safe_str(value.get("file_id") or value.get("filename"))
            if file_id:
                previous = job.file_progress.get(file_id, {})
                updated = {**previous, **dict(value)}
                if event_name == "error":
                    updated["state"] = "failed"
                job.file_progress[file_id] = updated
                job.progress = None
                # The row keeps its own running totals; rescanning every file
                # here made a large channel repaint thousands of rows per event.
                job.speed = ""
                job.status = "下載中"
                self.home_page.update_job_file(job, file_id)
                return
            if event_name == "error":
                return
        progress, detail = self._parse_progress(value)
        if progress is not None:
            job.progress = progress
        if detail:
            job.speed = detail
        job.status = "下載中"
        self.home_page.update_job(job)

    @staticmethod
    def _parse_progress(value: Any) -> tuple[Optional[float], str]:
        detail = ""
        number: Any = value
        if isinstance(value, Mapping):
            number = value.get("percent", value.get("percentage", value.get("progress")))
            transferred = value.get("transferred", value.get("current"))
            total = value.get("total", value.get("size"))
            speed = value.get("speed", value.get("rate", ""))
            filename = value.get("filename", value.get("file", ""))
            if transferred is not None and total:
                detail = f"{_format_bytes(transferred)} / {_format_bytes(total)}"
            elif speed:
                detail = _safe_str(speed)
            if filename:
                detail = f"目前檔案：{_safe_str(filename)}" + (f" · {detail}" if detail else "")
        try:
            parsed = float(number)
        except (TypeError, ValueError):
            return None, detail or _safe_str(value)
        if parsed < 0 or parsed > 100:
            return None, detail or "核心尚未回報進度"
        return parsed, detail

    @Slot(str, object)
    def _job_succeeded(self, job_id: str, result: Any) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = "已完成"
        job.progress = 100
        job.speed = ""
        for item in job.file_progress.values():
            item.update(state="completed", percent=100)
        if isinstance(result, Mapping):
            job.result_path = _safe_str(result.get("path", result.get("file_path", "")))
            job.title = _safe_str(result.get("title", result.get("filename", job.title)))
        self.home_page.update_job(job)
        self.refresh_history()

    @Slot(str, str, str)
    def _job_failed(self, job_id: str, message: str, detail: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = "失敗"
        job.error = message
        job.speed = ""
        self.home_page.update_job(job)
        self.refresh_history()
        self.refresh_accounts()
        if not self._closing:
            show_error(self, "下載失敗", f"{job.title}：{message}", detail)

    @Slot(str)
    def _job_canceled(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        # The worker can already be removed by QThread.finished. Keep the
        # user's close-versus-cancel intent on the longer-lived queue model.
        paused = job.closing_pause
        job.status = "已暫停" if paused else "已取消"
        job.speed = "重開程式後自動繼續" if paused else ""
        self.home_page.update_job(job)
        self.refresh_history()

    def cancel_job(self, job_id: str) -> None:
        if job_id in self._preparing_jobs:
            self._cancelled_before_start.add(job_id)
            return
        worker = self._workers.get(job_id)
        if worker is not None:
            worker.cancel()

    def retry_job(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        options = dict(job.options)
        options.update({"output_dir": job.output_dir, "account_id": job.account_id})
        options.setdefault("skip_existing", self.home_page.skip_check.isChecked())
        options.setdefault("group_album", self.home_page.group_check.isChecked())
        options.setdefault("concurrency", self.home_page.concurrency_spin.value())
        options.setdefault("proxy", self.home_page.proxy_value)
        options["continue_download"] = True
        options["skip_existing"] = True
        job.status = "等待中"
        job.progress = None
        job.speed = ""
        job.error = ""
        for item in job.file_progress.values():
            if item.get("state") != "completed":
                item.update(state="queued", percent=0, current=0)
        self.home_page.update_job(job)
        self._start_job(job, options)

    def open_output(self, path: str) -> None:
        output = Path(path)
        if output.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
        else:
            show_error(self, "找不到資料夾", "下載資料夾目前不存在，請確認路徑是否仍然有效。")

    def shutdown_workers(self, timeout_ms: int = 7000) -> None:
        """Stop background work before QApplication tears down Qt objects."""

        self.channel_page.shutdown(timeout_ms=timeout_ms)
        self.transfer_page.shutdown(timeout_ms=timeout_ms)
        for worker in list(self._workers.values()):
            worker.pause()
        for thread in list(self._threads.values()):
            if _thread_is_running(thread):
                thread.quit()
                thread.wait(timeout_ms)

    def _active_download_count(self) -> int:
        return len(self._preparing_jobs) + sum(1 for thread in self._threads.values() if _thread_is_running(thread))

    def _background_work_running(self) -> bool:
        all_threads = [*self._threads.values(), *self._store_threads, *self._action_threads]
        return (
            bool(self._preparing_jobs)
            or self.channel_page.is_busy()
            or self.transfer_page.is_busy()
            or any(_thread_is_running(thread) for thread in all_threads)
        )

    def _cancel_background_work(self) -> None:
        # Closing is a durable pause, distinct from the row's explicit cancel.
        # Keep the Qt loop alive until core checkpoints and session are sealed.
        for worker in list(self._workers.values()):
            if _qt_object_is_valid(worker):
                worker.pause()
        self.channel_page.cancel_operations()
        self.transfer_page.cancel_operations()
        cancel_account_operations = getattr(self.bridge, "cancel_account_operations", None)
        if callable(cancel_account_operations):
            cancel_account_operations()

    def _poll_close(self) -> None:
        if not self._closing:
            return
        if self._background_work_running():
            QTimer.singleShot(50, self._poll_close)
            return
        self._allow_close = True
        self.close()

    def _export_json(self) -> None:
        urls = self.home_page.parse_urls()
        files = list(self.home_page.pending_files)
        if not urls and not files:
            show_error(self, "沒有可匯出的項目", "請先貼上 Telegram 訊息連結，或匯入官方 Telegram JSON 檔案。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "匯出下載清單", "tdl-downloads.json", "JSON 檔案 (*.json)")
        if not path:
            return
        folder = self.home_page.folder_edit.text().strip()
        jobs = [DownloadJob(f"export-{i}", url, f"Telegram 訊息 {i + 1}", folder) for i, url in enumerate(urls)]
        if files:
            jobs.append(DownloadJob("export-files", files[0], f"官方 Telegram JSON（{len(files)} 個檔案）", folder, files=files))
        options = {"output_dir": folder, "skip_existing": self.home_page.skip_check.isChecked(), "group_album": self.home_page.group_check.isChecked(), "concurrency": self.home_page.concurrency_spin.value()}
        self._start_store_action(
            lambda: self.bridge.export_jobs(path, jobs, options),
            lambda _result: show_info(self, "已匯出", f"下載清單已保存到：\n{path}"),
            lambda detail: show_error(self, "匯出失敗", "下載清單沒有被保存。", detail),
        )

    def _import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "匯入下載清單", "", "JSON 檔案 (*.json)")
        if not path:
            return

        def read_payload() -> dict[str, list[str]]:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            links: list[str] = []
            files: list[str] = []

            def add_link(value: Any) -> None:
                normalised = _normalise_message_url(value)
                if normalised and normalised not in links:
                    links.append(normalised)

            def add_file(value: Any) -> None:
                if not isinstance(value, str):
                    return
                text = value.strip()
                if text and text not in files:
                    files.append(text)

            def add_file_values(value: Any) -> None:
                # ``files`` is deliberately a file-only field.  This keeps a
                # path such as ``C:\\Downloads\\export.json`` away from the
                # Telegram URL validator and lets the backend validate the
                # official Telegram export schema before starting the core.
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, Mapping):
                            add_file(item.get("path", item.get("file", item.get("filename"))))
                        else:
                            add_file(item)
                elif isinstance(value, Mapping):
                    add_file(value.get("path", value.get("file", value.get("filename"))))
                else:
                    add_file(value)

            def add_download_item(item: Any) -> None:
                if not isinstance(item, Mapping):
                    return
                nested = item.get("links", item.get("urls"))
                if isinstance(nested, list):
                    for value in nested:
                        add_link(value)
                elif nested is not None:
                    add_link(nested)
                add_link(item.get("url", item.get("link", item.get("message_link"))))
                add_file_values(item.get("files"))

            if isinstance(payload, Mapping):
                for key in ("urls", "links"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, str):
                                add_link(item)
                            elif isinstance(item, Mapping):
                                add_link(item.get("url", item.get("link", item.get("message_link"))))
                    elif value is not None:
                        add_link(value)
                # Our own export format and the backend's durable export both
                # use this field for official Telegram JSON files.
                add_file_values(payload.get("files"))
                # A Telegram Desktop export is identified by numeric chat id
                # plus a messages array.  Passing the selected JSON path to
                # the backend preserves message ids, media and captions that
                # cannot be reconstructed safely from plain text URLs.
                chat_id = payload.get("id")
                if (
                    isinstance(chat_id, (int, float))
                    and not isinstance(chat_id, bool)
                    and not (isinstance(chat_id, float) and not chat_id.is_integer())
                    and isinstance(payload.get("messages"), list)
                ):
                    add_file(path)
                downloads = payload.get("downloads")
                if isinstance(downloads, list):
                    for item in downloads:
                        add_download_item(item)
            elif isinstance(payload, list):
                for item in payload:
                    if isinstance(item, Mapping):
                        add_download_item(item)
                    elif isinstance(item, str):
                        if _normalise_message_url(item):
                            add_link(item)
                        elif Path(item).suffix.lower() == ".json":
                            add_file(item)
            return {"links": links, "files": files}

        self._start_store_action(
            read_payload,
            lambda imported: self._apply_imported_links(imported, path),
            lambda detail: show_error(self, "匯入失敗", "JSON 清單格式無法讀取。", detail),
        )

    def _apply_imported_links(self, imported: Any, path: str) -> None:
        if isinstance(imported, Mapping):
            links = [str(value) for value in imported.get("links", []) if isinstance(value, str)]
            files = [str(value) for value in imported.get("files", []) if isinstance(value, str)]
        else:
            # Keep this small compatibility path for callers/tests that pass
            # the pre-files list shape directly.
            links = [str(value) for value in (imported or []) if isinstance(value, str)]
            files = []
        if not links and not files:
            show_error(self, "找不到可用項目", "這個 JSON 沒有 Telegram 連結，也沒有可下載的官方匯出檔案。")
            return
        existing = self.home_page.parse_urls()
        merged: list[str] = []
        for value in [*existing, *list(links)]:
            if value not in merged:
                merged.append(value)
        self.home_page.url_edit.setPlainText("\n".join(merged))
        for value in files:
            if value not in self.home_page.pending_files:
                self.home_page.pending_files.append(value)
        self.home_page._update_count()

    def closeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        if self._allow_close:
            self._allow_close = False
            # The polling path only reaches here after every thread has
            # finished.  This final bounded call is defensive and does not
            # dispose a still-running QThread.
            self.shutdown_workers(timeout_ms=0)
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        # Downloads pause and resume on the next launch; an upload or forward
        # cannot, so ask before throwing the rest of it away.
        if self.transfer_page.is_busy() and not ask_confirm(
            self,
            "仍有上傳或轉傳進行中",
            "現在關閉會中止尚未完成的上傳／轉傳。",
            "已送出的項目不會收回，未送出的需要重新開始。",
            "仍要關閉",
        ):
            event.ignore()
            return

        self._closing = True
        active = self._active_download_count()

        if not active:
            # Account/history reads are short local operations.  Finish them
            # inline when there is no download confirmation to show, which
            # preserves the normal QWidget.close() contract for callers and
            # tests while still refusing to destroy a live QThread.
            self.shutdown_workers()
            if not self._background_work_running():
                event.accept()
                return

        self._closing = True
        self.setEnabled(False)
        self._set_top_account("正在保存進度，重開後會自動繼續…")
        self._cancel_background_work()
        event.ignore()
        QTimer.singleShot(0, self._poll_close)


def _format_bytes(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _safe_str(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def _format_duration(seconds: float) -> str:
    """Round a remaining time to the unit a person would say out loud."""

    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分鐘"
    hours, minutes = divmod(total // 60, 60)
    if hours < 24:
        return f"{hours} 小時 {minutes} 分" if minutes else f"{hours} 小時"
    return f"{hours // 24} 天 {hours % 24} 小時"


# Qt style sheets cannot draw an arrow themselves, and styling a combo or spin
# box replaces the native one, so the glyphs ship as small images.
_ARROW_DOWN = _resource_path("assets", "arrow-down.png").as_posix()
_ARROW_UP = _resource_path("assets", "arrow-up.png").as_posix()

STYLE = f"""
* {{
    font-family: 'Microsoft JhengHei UI', 'Microsoft JhengHei', 'Segoe UI', sans-serif;
    color: {TEXT};
    font-size: 14px;
}}
QMainWindow, #AppRoot {{ background: {BG}; }}
#Sidebar {{ background: #fbfcff; border-right: 1px solid {BORDER}; }}
#ContentFrame {{ background: {BG}; }}
#Topbar {{ background: rgba(255,255,255,0.82); border-bottom: 1px solid {BORDER}; }}
#Logo {{ background: {PRIMARY}; color: white; border-radius: 10px; font-size: 25px; font-weight: 800; }}
#BrandName {{ font-size: 20px; font-weight: 800; color: {TEXT}; }}
#BrandSub {{ font-size: 12px; color: {MUTED}; }}
#SidebarTip {{ background: {PRIMARY_LIGHT}; color: {PRIMARY_DARK}; border-radius: 10px; padding: 12px; line-height: 1.5; }}
#SidebarVersion {{ color: {SUBTLE}; font-size: 11px; padding: 10px 2px 0; }}
QPushButton {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 14px; color: {TEXT}; }}
QPushButton:hover {{ border-color: #b8c3d8; background: #fbfcff; }}
QPushButton:pressed {{ background: #eef2f8; }}
QPushButton:disabled {{ color: {SUBTLE}; background: #f3f5f9; }}
#PrimaryButton {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; color: #ffffff; font-weight: 700; padding: 9px 18px; }}
#PrimaryButton:hover {{ background: {PRIMARY_DARK}; border-color: {PRIMARY_DARK}; }}
#PrimaryButton:pressed {{ background: #312e81; }}
QPushButton[dangerButton="true"] {{ color: {DANGER}; background: {DANGER_LIGHT}; border-color: #ffd5dc; }}
QPushButton[dangerButton="true"]:hover {{ background: #ffe3e7; }}
.QPushButton {{ min-height: 20px; }}
#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame[card="true"] {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; }}
QFrame#HeroBadge {{ background: {PRIMARY_LIGHT}; border: 1px solid #dfe3ff; border-radius: 12px; }}
#HeroTitle {{ font-size: 28px; font-weight: 800; color: {TEXT}; margin-top: 3px; }}
#HeroDescription {{ color: {MUTED}; font-size: 15px; }}
#Kicker {{ color: {PRIMARY}; font-weight: 700; }}
#PageTitle {{ font-size: 26px; font-weight: 800; }}
#SectionTitle {{ font-size: 18px; font-weight: 800; }}
#CardTitle {{ font-size: 15px; font-weight: 700; }}
#DialogTitle {{ font-size: 21px; font-weight: 800; }}
#MutedText, #QueueUrl, #QueueProgressText {{ color: {MUTED}; }}
#QueueStats {{ color: {PRIMARY_DARK}; font-size: 12px; font-weight: 600; }}
QPushButton[segment="true"] {{ padding: 8px 16px; }}
QPushButton[segment="true"]:checked {{ background: {PRIMARY_LIGHT}; border-color: #c7d2fe; color: {PRIMARY_DARK}; font-weight: 700; }}
#TransferProgress, #UploadList {{ background: #ffffff; border: 1px solid #d8e0ed; border-radius: 8px; padding: 4px; }}
#BodyText {{ color: #39465c; line-height: 1.5; }}
#EmptyText {{ color: {SUBTLE}; padding: 22px; }}
#FieldLabel {{ color: {MUTED}; font-weight: 700; }}
#TopAccount {{ background: #eef1f6; color: {MUTED}; border-radius: 16px; padding: 7px 14px; font-weight: 700; }}
#TopAccount[connected="true"] {{ background: {SUCCESS_LIGHT}; color: {SUCCESS}; }}
#AccountDot {{ color: {SUBTLE}; font-size: 17px; }}
#AccountDot[connected="true"] {{ color: {SUCCESS}; }}
#Avatar {{ background: {PRIMARY_LIGHT}; color: {PRIMARY}; border-radius: 21px; font-size: 18px; font-weight: 800; }}
#QrImage {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px; }}
#UrlEdit, #FolderEdit, QLineEdit, QTextEdit, QComboBox, QSpinBox {{ background: #ffffff; border: 1px solid #d8e0ed; border-radius: 8px; padding: 8px 10px; selection-background-color: #c7d2fe; }}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {PRIMARY}; }}
#UrlEdit {{ padding: 10px; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow, QSpinBox::down-arrow {{ image: url("{_ARROW_DOWN}"); width: 10px; height: 6px; }}
QSpinBox::up-arrow {{ image: url("{_ARROW_UP}"); width: 10px; height: 6px; }}
QSpinBox {{ padding-right: 26px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 24px; border: none; background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 3px; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 3px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {PRIMARY_LIGHT}; border-radius: 4px; }}
#QueueHost {{ background: {CARD}; }}
QProgressBar {{ background: #edf1f7; border: none; border-radius: 4px; }}
QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 4px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid #c7d2e2; border-radius: 5px; background: #ffffff; }}
QCheckBox::indicator:checked {{ background: {PRIMARY}; border-color: {PRIMARY}; }}
QScrollArea {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: #cfd8e8; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTableWidget {{ background: #ffffff; border: none; gridline-color: {BORDER}; alternate-background-color: #fbfcff; }}
QHeaderView::section {{ background: #f6f8fc; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 9px; font-weight: 700; }}
QTableWidget::item {{ padding: 7px; }}
QToolButton {{ border: none; color: {PRIMARY}; padding: 6px; }}
QToolButton:hover {{ color: {PRIMARY_DARK}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; }}
QRadioButton::indicator:checked {{ background: {PRIMARY}; border: 3px solid white; outline: 1px solid {PRIMARY}; border-radius: 8px; }}
QRadioButton::indicator:unchecked {{ background: white; border: 1px solid #c7d2e2; border-radius: 8px; }}
QDialog {{ background: {BG}; }}
QMessageBox {{ background: {BG}; }}
QPushButton[sidebarButton="true"] {{ text-align: left; border: none; background: transparent; color: {MUTED}; padding: 10px 12px; font-size: 15px; font-weight: 600; }}
QPushButton[sidebarButton="true"]:hover {{ background: #f0f3fb; color: {TEXT}; }}
QPushButton[sidebarButton="true"]:checked {{ background: {PRIMARY_LIGHT}; color: {PRIMARY_DARK}; font-weight: 800; }}
QLabel[pill="true"] {{ border-radius: 12px; font-size: 12px; font-weight: 700; padding: 3px 9px; }}
QLabel[pill="true"][tone="neutral"] {{ background: #eef1f6; color: {MUTED}; }}
QLabel[pill="true"][tone="blue"] {{ background: {PRIMARY_LIGHT}; color: {PRIMARY_DARK}; }}
QLabel[pill="true"][tone="success"] {{ background: {SUCCESS_LIGHT}; color: {SUCCESS}; }}
QLabel[pill="true"][tone="warning"] {{ background: {WARNING_LIGHT}; color: {WARNING}; }}
QLabel[pill="true"][tone="danger"] {{ background: {DANGER_LIGHT}; color: {DANGER}; }}
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="tdl 繁中下載器桌面介面")
    parser.add_argument("--smoke-test", action="store_true", help="建立主視窗並輸出結構檢查結果後離開")
    parser.add_argument("--screenshot", metavar="PATH", help="啟動後擷取主視窗畫面並離開")
    parser.add_argument("--smoke-report", metavar="PATH", help="將 smoke-test 結果寫入 UTF-8 JSON")
    parser.add_argument("--verify-qr", action="store_true", help="在隔離資料夾驗證真實 QR Code 顯示後自動取消，不登入帳號")
    parser.add_argument("--verify-channels", action="store_true", help="使用隔離帳號副本驗證頻道清單與公開小型影片下載")
    parser.add_argument("--verify-channel-ui", action="store_true", help="使用隔離帳號副本實際點擊更新清單並驗證視窗執行緒")
    parser.add_argument("--verify-restart", choices=("before-close", "after-reopen"), help=argparse.SUPPRESS)
    parser.add_argument("--verify-download-child", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verify_download_child:
        from desktop.restart_probe import fixture_core
        return fixture_core(*args.verify_download_child)
    if args.verify_restart:
        from desktop.restart_probe import run_probe
        return run_probe(args.verify_restart, args.smoke_report)

    if args.verify_qr:
        from desktop.qr_probe import run_probe
        return run_probe(args.smoke_report)

    if args.verify_channels:
        from desktop.channel_probe import run_probe
        return run_probe(args.smoke_report)

    if args.verify_channel_ui:
        from desktop.channel_ui_probe import run_probe
        return run_probe(args.smoke_report)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    global _TRANSLATOR
    translator = QTranslator(app)
    translations_path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    for translation_name in ("qtbase_zh_TW", "qtbase_zh_CN"):
        if translator.load(translation_name, translations_path):
            app.installTranslator(translator)
            break
    # Keep the translator alive for the full QApplication lifetime.
    _TRANSLATOR = translator
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    # Headless screenshots on a clean test host may not expose Microsoft's
    # CJK fonts through Qt's font directory.  Register the installed TTC when
    # available; ordinary Windows launches simply use the normal system font
    # lookup path.
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        for font_path in (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"):
            if Path(font_path).exists():
                QFontDatabase.addApplicationFont(font_path)
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    icon_path = _resource_path("assets", "app.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    lock_path = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation) or tempfile.gettempdir()) / "tdl-zh-gui.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        box = QMessageBox()
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(APP_NAME)
        box.setText("tdl 繁中下載器已在執行中。")
        box.setStandardButtons(QMessageBox.Ok)
        button = box.button(QMessageBox.Ok)
        if button is not None:
            button.setText("確定")
        box.exec()
        return 2

    window = MainWindow()
    window.show()
    smoke_exit_code = 0

    def close_window_and_quit() -> None:
        # MainWindow closes asynchronously after cancelling workers.  Keep
        # the event loop alive until closeEvent has observed every QThread as
        # finished; this is especially important for frozen screenshot/smoke
        # launches where there is no user to wait for the window.
        if window.isVisible():
            window.close()
        if window.isVisible():
            QTimer.singleShot(50, close_window_and_quit)
        else:
            app.quit()

    if args.smoke_test:
        search_conversion = all(
            search_matches(query, value)
            for query, value in (
                ("台湾群组", "🎬台灣群組"),
                ("學習頻道", "📚学习频道"),
            )
        )
        core_path = ""
        core_error = ""
        if window.bridge.available:
            try:
                core_path = str(window.bridge.runner.resolve_executable())
            except Exception as exc:
                core_error = _diagnostic(exc)
        else:
            core_error = window.bridge.backend_error or "Python 下載後端尚未安裝。"
        payload = {
            "window": window.windowTitle(),
            "size": [window.width(), window.height()],
            "minimum": [window.minimumWidth(), window.minimumHeight()],
            "pages": list(PAGE_NAMES),
            "backend": type(window.bridge.runner).__name__ if window.bridge.runner is not None else "",
            "backend_available": bool(window.bridge.available),
            "core": core_path,
            "traditional_simplified_search": search_conversion,
            "ok": bool(window.bridge.available and core_path and search_conversion),
        }
        if core_error:
            payload["error"] = core_error
        if args.smoke_report:
            report_path = Path(args.smoke_report).expanduser()
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                payload["ok"] = False
                payload["error"] = _diagnostic(exc)
        print(json.dumps(payload, ensure_ascii=False))
        smoke_exit_code = 0 if payload["ok"] else 1
        QTimer.singleShot(0, close_window_and_quit)
    elif args.screenshot:
        output = Path(args.screenshot).expanduser()

        def take_screenshot() -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(output))
            close_window_and_quit()

        # Let the initial Store reads finish so the captured page reflects the
        # real account/queue state instead of a transient "讀取中" label.
        QTimer.singleShot(900, take_screenshot)

    code = app.exec()
    if not window.isHidden():
        window.close()
    lock.unlock()
    return int(smoke_exit_code if args.smoke_test else code)


if __name__ == "__main__":
    raise SystemExit(main())
