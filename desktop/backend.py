"""Backend services for the TDL Desktop application.

The desktop application deliberately talks to the Go binary as a child process.
This module owns the parts that should not be mixed into a Qt window:

* small, atomic JSON stores for accounts, preferences, queue, and history;
* account-scoped core data directories;
* a Windows DPAPI envelope for the core session data;
* a line based ``@@TDL_GUI@@`` protocol adapter; and
* strict input validation and a background runner with cancellation.

The Go program is not an in-process library.  Keeping this boundary explicit
means a GUI failure cannot corrupt the core process and it also makes the
backend usable from tests without PySide6 installed.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import io
import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit


EVENT_PREFIX = "@@TDL_GUI@@"
APP_NAME = "TDLDesktop"
DATA_ENV = "TDL_DATA_DIR"
GUI_ENV = "TDL_GUI"
SESSION_FILE_NAME = "session.dpapi"
_JSON_VERSION = 1
_TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")
EXPORT_MEDIA_TYPES = frozenset({"video", "photo", "audio", "document"})
CHAT_CACHE_FILE_NAME = "chats.dpapi"
# The core reports progress several times per second and file.  Rewriting the
# queue and history JSON for each event made the core block on its stdout pipe,
# which slowed the transfer itself; state changes are still written at once.
_PROGRESS_PERSIST_INTERVAL = 2.0
# Scan manifests that were never turned into a download are only useful for
# the current session; resumable jobs keep their own snapshot.
_EXPORT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TEMPLATE = "{{ .DialogID }}_{{ .MessageID }}_{{ filenamify .FileName }}"
FORWARD_MODES = frozenset({"direct", "clone"})
# New GUI download jobs keep media under a per-chat folder.  This option is
# deliberately a job option rather than a global preference: the durable
# manifest must decide how a resumed job lays out files, while old manifests
# (which have no such field) must retain their original flat output behavior.
_DEFAULT_GROUP_BY_CHAT = True

# Operations are tracked at module scope rather than on ``Runner`` instances.
# The window normally owns one Runner, but keeping the registry shared also
# prevents a second backend adapter in the same process from deleting an
# account or opening another Bolt-backed core process underneath the first
# adapter.  Counts (instead of a set) are needed because downloads for one
# account are intentionally queued behind one another.
_ACCOUNT_ACTIVITY_GUARD = threading.RLock()
_ACCOUNT_ACTIVITY: dict[str, int] = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[str, threading.Lock] = {}


def _reserve_account_activity(account_id: str) -> None:
    with _ACCOUNT_ACTIVITY_GUARD:
        key = str(account_id)
        _ACCOUNT_ACTIVITY[key] = _ACCOUNT_ACTIVITY.get(key, 0) + 1


def _release_account_activity(account_id: str) -> None:
    with _ACCOUNT_ACTIVITY_GUARD:
        key = str(account_id)
        count = _ACCOUNT_ACTIVITY.get(key, 0)
        if count <= 1:
            _ACCOUNT_ACTIVITY.pop(key, None)
        else:
            _ACCOUNT_ACTIVITY[key] = count - 1


def _account_activity_count(account_id: str) -> int:
    with _ACCOUNT_ACTIVITY_GUARD:
        return _ACCOUNT_ACTIVITY.get(str(account_id), 0)


def _shared_account_lock(account_id: str) -> threading.Lock:
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(str(account_id), threading.Lock())


class BackendError(RuntimeError):
    """Base class for predictable, user-facing backend errors."""


class ValidationError(BackendError, ValueError):
    """An input is not valid for the desktop workflow."""


class StoreError(BackendError):
    """Persistent application data could not be read or written."""


class RunnerError(BackendError):
    """The core process could not be started or completed."""


class RunnerBusy(RunnerError):
    """An account already has a core operation in progress."""


class CoreNotFound(RunnerError):
    """The bundled/source ``tdl-core`` executable is not available."""


class DataProtectionError(BackendError):
    """Session data could not be protected or restored."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_job_title(value: Any) -> str:
    """Keep a queue label readable without treating dates as phone numbers."""

    text = str(value or "").strip()
    return "".join(char for char in text if ord(char) >= 0x20)[:200]


_FILE_PROGRESS_FIELDS = frozenset(
    {"file_id", "state", "current", "total", "percent", "filename"}
)


def _normalize_file_progress(value: Any) -> dict[str, dict[str, Any]]:
    """Keep a bounded, JSON-safe per-file progress map for queue recovery."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_key, raw_item in value.items():
        if not isinstance(raw_item, Mapping):
            continue
        file_id = str(raw_item.get("file_id", raw_key) or raw_key).strip()
        if not file_id:
            continue
        file_id = sanitize_text(file_id)[:200]
        item: dict[str, Any] = {"file_id": file_id}
        for field_name in _FILE_PROGRESS_FIELDS - {"file_id"}:
            if field_name not in raw_item or raw_item[field_name] is None:
                continue
            field_value = raw_item[field_name]
            if field_name in {"state", "filename"}:
                item[field_name] = sanitize_text(field_value)
                continue
            if field_name in {"current", "total"}:
                if isinstance(field_value, bool):
                    continue
                try:
                    item[field_name] = max(0, int(field_value))
                except (TypeError, ValueError, OverflowError):
                    continue
                continue
            if field_name == "percent":
                if isinstance(field_value, bool):
                    continue
                try:
                    percent = float(field_value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(percent):
                    continue
                item[field_name] = max(0.0, min(100.0, percent))
        result[file_id] = item
    return result


def _deep_copy(value: Any) -> Any:
    """Copy JSON-shaped data without importing a third-party package."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def _default_data_root() -> Path:
    """Return the per-user application root.

    ``TDL_DESKTOP_DATA_DIR`` is intentionally a test/developer override.  It
    is not passed to the Go core and therefore cannot accidentally redirect a
    user's existing CLI storage.
    """

    override = os.environ.get("TDL_DESKTOP_DATA_DIR")
    if override:
        return Path(override).expanduser()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME

    # Windows normally always has LOCALAPPDATA.  The fallback keeps the
    # backend usable in tests and on development machines without it.
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def _default_download_directory() -> Path:
    """Return the user's visible Telegram download directory.

    Windows can redirect Downloads (for example to OneDrive), so consult the
    per-user ``User Shell Folders`` registry value when available.  The
    environment/home path is a safe fallback for portable and non-Windows
    launches.  Test/development Store roots deliberately bypass this helper.
    """

    if os.name == "nt":
        try:
            import winreg  # type: ignore[import-not-found]

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            if value:
                return Path(os.path.expandvars(str(value))).expanduser() / "Telegram"
        except (OSError, ImportError):
            pass
    return Path.home() / "Downloads" / "Telegram"


def _restrict_permissions(path: Path, *, strict: bool = False) -> None:
    """Best-effort user-only permissions for local application files.

    ``chmod`` is meaningful on POSIX and is still useful as documentation on
    Windows.  When requested on Windows, tighten the ACL using ``icacls`` with
    an argument list (never through a shell).  ACL failures do not make a
    successful download fail; DPAPI remains the primary session protection.
    """

    try:
        mode = stat.S_IRUSR | stat.S_IWUSR
        if path.is_dir():
            mode |= stat.S_IXUSR
        os.chmod(path, mode)
    except OSError:
        pass

    if not strict or os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            capture_output=True,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _atomic_write_bytes(path: Path, data: bytes, *, strict_permissions: bool = False) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_permissions(temporary, strict=strict_permissions)
        os.replace(temporary, path)
        _restrict_permissions(path, strict=strict_permissions)
    except OSError as exc:
        raise StoreError(f"寫入資料失敗：{path.name}") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _atomic_write_json(path: Path, value: Any) -> None:
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StoreError(f"資料格式無法儲存：{path.name}") from exc
    _atomic_write_bytes(path, payload)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        _atomic_write_json(path, default)
        return _deep_copy(default)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"無法讀取資料檔：{path.name}") from exc
    return value


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _safe_child(root: Path, child: str | Path, *, require_existing: bool = False) -> Path:
    """Resolve a child and enforce that it stays below ``root``."""

    root_resolved = root.resolve()
    candidate = (root_resolved / Path(child)).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValidationError("資料路徑超出應用程式目錄") from exc
    if require_existing and not candidate.exists():
        raise StoreError("找不到帳號資料")
    return candidate


@dataclass
class Account:
    id: str
    name: str
    user_id: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    authenticated: bool = False
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Account":
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            user_id=None if raw.get("user_id") in (None, "") else str(raw.get("user_id")),
            username=None if raw.get("username") in (None, "") else str(raw.get("username")),
            display_name=None
            if raw.get("display_name") in (None, "")
            else str(raw.get("display_name")),
            authenticated=bool(raw.get("authenticated", False)),
            created_at=str(raw.get("created_at") or _utc_now()),
            updated_at=str(raw.get("updated_at") or _utc_now()),
        )


_EXTENSION_RE = re.compile(r"^[A-Za-z0-9_+-]{1,16}$")


def normalize_extensions(value: Any) -> list[str]:
    """Normalize ``mp4, .MKV`` style input for ``--include``/``--exclude``."""

    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_items: Iterable[Any] = re.split(r"[\s,，;；]+", value)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        raw_items = value
    else:
        raise ValidationError("副檔名清單格式無效")
    result: list[str] = []
    for raw_item in raw_items:
        item = str(raw_item).strip().lstrip(".").lower()
        if not item:
            continue
        if not _EXTENSION_RE.fullmatch(item):
            raise ValidationError(f"副檔名格式無效：{sanitize_text(item)[:20]}")
        if item not in result:
            result.append(item)
    if len(result) > 50:
        raise ValidationError("副檔名最多只能填 50 個")
    return result


def validate_template(value: Any) -> str:
    """Validate a ``tdl dl --template`` value; empty keeps the core default."""

    text = str(value or "").strip()
    if not text or text == DEFAULT_TEMPLATE:
        return ""
    if len(text) > 300 or any(ord(char) < 0x20 for char in text):
        raise ValidationError("檔名格式過長或包含換行")
    if "{{" not in text or "}}" not in text:
        raise ValidationError("檔名格式需要包含 {{ }} 變數，例如 {{ .MessageID }}")
    # The template is one file name below the download folder.  Path
    # separators outside the {{ }} actions would point the core elsewhere.
    literal = re.sub(r"\{\{.*?\}\}", "", text)
    if any(char in literal for char in '\\/:*?"<>|') or ".." in literal:
        raise ValidationError("檔名格式不可包含路徑或 Windows 不允許的檔名字元")
    return text


def normalize_download_advanced(raw: Any) -> dict[str, Any]:
    """Return the validated advanced ``tdl dl`` options stored with a job.

    The result is a JSON-shaped mapping so a paused job resumes with exactly
    the options it was created with, even after the preferences change.
    """

    data = dict(raw) if isinstance(raw, Mapping) else {}
    include = normalize_extensions(data.get("include"))
    exclude = normalize_extensions(data.get("exclude"))
    if include and exclude:
        raise ValidationError("「只下載」與「排除」副檔名只能擇一設定")

    def bounded(name: str, default: int, low: int, high: int) -> int:
        value = data.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError, OverflowError):
            return default

    return {
        "template": validate_template(data.get("template")),
        "include": include,
        "exclude": exclude,
        "desc": bool(data.get("desc", False)),
        "takeout": bool(data.get("takeout", False)),
        "rewrite_ext": bool(data.get("rewrite_ext", False)),
        "threads": bounded("threads", 4, 1, 16),
        "delay_ms": bounded("delay_ms", 0, 0, 60000),
    }


@dataclass
class Preferences:
    download_dir: str = ""
    concurrency: int = 2
    continue_download: bool = True
    skip_same: bool = True
    group: bool = True
    proxy: str = ""
    language: str = "zh-TW"
    # Advanced ``tdl dl`` options.  Extensions are kept as the comma-separated
    # text the user typed so the settings page can show it back unchanged.
    template: str = ""
    include_ext: str = ""
    exclude_ext: str = ""
    desc: bool = False
    takeout: bool = False
    rewrite_ext: bool = False
    threads: int = 4
    delay_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def download_advanced(self) -> dict[str, Any]:
        """Advanced options a new download job inherits from the settings."""

        return normalize_download_advanced(
            {
                "template": self.template,
                "include": self.include_ext,
                "exclude": self.exclude_ext,
                "desc": self.desc,
                "takeout": self.takeout,
                "rewrite_ext": self.rewrite_ext,
                "threads": self.threads,
                "delay_ms": self.delay_ms,
            }
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], default_download_dir: str) -> "Preferences":
        data = dict(raw)
        directory = str(data.get("download_dir") or default_download_dir)
        try:
            concurrency = int(data.get("concurrency", 2))
        except (ValueError, TypeError):
            concurrency = 2
        # A hand-edited or older preferences file must not make the whole
        # settings page unreadable; fall back to the defaults instead.
        try:
            advanced = normalize_download_advanced(
                {
                    "template": data.get("template"),
                    "include": data.get("include_ext"),
                    "exclude": data.get("exclude_ext"),
                    "desc": data.get("desc"),
                    "takeout": data.get("takeout"),
                    "rewrite_ext": data.get("rewrite_ext"),
                    "threads": data.get("threads", 4),
                    "delay_ms": data.get("delay_ms", 0),
                }
            )
        except ValidationError:
            advanced = normalize_download_advanced({})
        return cls(
            download_dir=directory,
            concurrency=max(1, min(concurrency, 64)),
            continue_download=bool(data.get("continue_download", True)),
            skip_same=bool(data.get("skip_same", True)),
            group=bool(data.get("group", True)),
            proxy=str(data.get("proxy") or ""),
            language=str(data.get("language") or "zh-TW"),
            template=advanced["template"],
            include_ext=", ".join(advanced["include"]),
            exclude_ext=", ".join(advanced["exclude"]),
            desc=advanced["desc"],
            takeout=advanced["takeout"],
            rewrite_ext=advanced["rewrite_ext"],
            threads=advanced["threads"],
            delay_ms=advanced["delay_ms"],
        )


@dataclass
class DownloadJob:
    id: str
    account_id: str
    links: list[str]
    files: list[str]
    output_dir: str
    continue_download: bool
    skip_same: bool
    group: bool
    concurrency: int
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    percent: Optional[float] = None
    current: Optional[int] = None
    total: Optional[int] = None
    speed: Optional[str] = None
    filename: Optional[str] = None
    # A user-facing label supplied by the channel/date workflow.  Keep this
    # separate from ``filename``: the core may report a media filename while
    # the queue row should continue to show the channel and date range.
    title: str = ""
    # JSON imports are kept under the owning account so a queued job remains
    # resumable even when the user deletes or moves the original file.  The
    # public ``files`` field keeps the original paths for history/export
    # compatibility; the runner chooses these snapshots when available.
    file_snapshots: list[str] = field(default_factory=list)
    # Stable per-file progress survives a close/reopen and is intentionally a
    # top-level JSON mapping so the Qt queue can restore rows directly.
    file_progress: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Advanced ``tdl dl`` options captured when the job was created; see
    # ``normalize_download_advanced``.
    advanced: dict[str, Any] = field(default_factory=dict)
    # Totals reported by a channel scan, so the queue can show overall
    # progress before the core has discovered every file.  Zero is unknown.
    expected_files: int = 0
    expected_bytes: int = 0
    # What a completed job actually transferred.  They replace the per-file
    # rows, which are dropped once nothing can resume; see ``_complete_job``.
    file_count: int = 0
    total_bytes: int = 0
    # ``group_by_chat`` was added after the initial queue schema.  Missing
    # values are intentionally false in ``from_dict`` so resuming an old job
    # never moves its files into newly-created chat folders.
    group_by_chat: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Keep the storage schema explicit while exposing the small aliases
        # used by older UI adapters and exported download lists.
        payload["directory"] = self.output_dir
        payload["path"] = self.output_dir
        payload["url"] = self.links[0] if len(self.links) == 1 else ""
        if self.title:
            payload["title"] = self.title
        elif self.filename:
            payload["title"] = self.filename
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DownloadJob":
        links = [str(link) for link in raw.get("links", []) if isinstance(link, str)]
        return cls(
            id=str(raw.get("id", "")),
            account_id=str(raw.get("account_id", "")),
            links=links,
            files=[str(value) for value in raw.get("files", []) if isinstance(value, str)],
            output_dir=str(raw.get("output_dir", raw.get("directory", raw.get("path", "")))),
            continue_download=bool(raw.get("continue_download", False)),
            skip_same=bool(raw.get("skip_same", False)),
            group=bool(raw.get("group", False)),
            concurrency=max(1, min(int(raw.get("concurrency", 1) or 1), 64)),
            status=str(raw.get("status", "queued")),
            created_at=str(raw.get("created_at") or _utc_now()),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            error=raw.get("error"),
            percent=raw.get("percent"),
            current=raw.get("current"),
            total=raw.get("total"),
            speed=raw.get("speed"),
            filename=raw.get("filename"),
            title=str(raw.get("title") or ""),
            file_snapshots=[
                str(value)
                for value in raw.get("file_snapshots", [])
                if isinstance(value, str)
            ],
            file_progress=_normalize_file_progress(raw.get("file_progress", {})),
            advanced=_stored_download_advanced(raw.get("advanced")),
            expected_files=_non_negative_int(raw.get("expected_files")),
            expected_bytes=_non_negative_int(raw.get("expected_bytes")),
            file_count=_non_negative_int(raw.get("file_count")),
            total_bytes=_non_negative_int(raw.get("total_bytes")),
            group_by_chat=bool(raw.get("group_by_chat", False)),
        )


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _stored_download_advanced(value: Any) -> dict[str, Any]:
    """Read a persisted job's options; a damaged record uses the defaults."""

    try:
        return normalize_download_advanced(value)
    except ValidationError:
        return normalize_download_advanced({})


class Store:
    """Persistent application state with atomic JSON replacement.

    All core session bytes live below ``accounts/<uuid>`` and are managed by
    :class:`SessionVault`.  The JSON files never contain phone numbers,
    passwords, OTPs, proxy credentials, or Telegram session bytes.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        isolated_root = root is not None or bool(os.environ.get("TDL_DESKTOP_DATA_DIR"))
        self.root = Path(root).expanduser().resolve() if root is not None else _default_data_root().resolve()
        self._default_download_dir = (
            self.root / "downloads" if isolated_root else _default_download_directory()
        )
        self.accounts_dir = self.root / "accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.root)
        _restrict_permissions(self.accounts_dir)
        self._accounts_path = self.root / "accounts.json"
        self._preferences_path = self.root / "preferences.json"
        self._queue_path = self.root / "queue.json"
        self._history_path = self.root / "downloads.json"
        self._lock = threading.RLock()
        self._ensure_files()
        self.mark_interrupted_jobs()
        self.prune_completed_queue()
        # A forced close can leave a plaintext core runtime behind.  Seal it
        # before the first new operation so the normal startup path does not
        # rely on a user opening each account once before credentials are
        # protected again.
        self.reseal_plaintext_runtimes()
        self.purge_stale_exports()

    def _ensure_files(self) -> None:
        with self._lock:
            _read_json(self._accounts_path, {"version": _JSON_VERSION, "accounts": []})
            _read_json(self._preferences_path, {"version": _JSON_VERSION, "values": {}})
            _read_json(self._queue_path, {"version": _JSON_VERSION, "items": []})
            _read_json(self._history_path, {"version": _JSON_VERSION, "items": []})

    def _accounts_raw(self) -> list[dict[str, Any]]:
        raw = _read_json(self._accounts_path, {"version": _JSON_VERSION, "accounts": []})
        if not isinstance(raw, Mapping) or not isinstance(raw.get("accounts"), list):
            raise StoreError("帳號資料格式無效")
        return [dict(item) for item in raw["accounts"] if isinstance(item, Mapping)]

    def _save_accounts(self, values: Iterable[Mapping[str, Any]]) -> None:
        _atomic_write_json(
            self._accounts_path,
            {"version": _JSON_VERSION, "accounts": [dict(value) for value in values]},
        )

    def list_accounts(self) -> list[Account]:
        with self._lock:
            return [Account.from_dict(item) for item in self._accounts_raw()]

    def get_account(self, account_id: str) -> Account:
        with self._lock:
            for account in self.list_accounts():
                if account.id == str(account_id):
                    return account
        raise StoreError("找不到指定帳號")

    @staticmethod
    def _validate_account_name(name: str) -> str:
        value = str(name or "").strip()
        if not value or len(value) > 128 or any(char in value for char in ("/", "\\", "\0")):
            raise ValidationError("帳號名稱不可為空，也不可包含路徑字元")
        return value

    def add_account(
        self,
        name: str,
        *,
        user_id: str | int | None = None,
        username: str | None = None,
        display_name: str | None = None,
    ) -> Account:
        clean_name = self._validate_account_name(name)
        account = Account(
            id=str(uuid.uuid4()),
            name=clean_name,
            user_id=None if user_id in (None, "") else str(user_id),
            username=None if not username else str(username).lstrip("@"),
            display_name=None if not display_name else str(display_name),
        )
        with self._lock:
            records = self._accounts_raw()
            records.append(account.to_dict())
            self._save_accounts(records)
            account_root = self.account_root(account.id, create=True)
            (account_root / "runtime").mkdir(exist_ok=True)
            # The runtime directory is removed before the first operation; it
            # is created here so a newly added account is visibly isolated.
        return account

    def update_account(self, account_id: str, **updates: Any) -> Account:
        with self._lock:
            records = self._accounts_raw()
            found: Optional[Account] = None
            for index, raw in enumerate(records):
                if str(raw.get("id")) != str(account_id):
                    continue
                found = Account.from_dict(raw)
                for key in ("name", "user_id", "username", "display_name", "authenticated"):
                    if key not in updates:
                        continue
                    value = updates[key]
                    if key == "name":
                        value = self._validate_account_name(str(value))
                    elif key in ("user_id", "username", "display_name"):
                        value = None if value in (None, "") else str(value)
                        if key == "username" and value:
                            value = value.lstrip("@")
                    setattr(found, key, value)
                found.updated_at = _utc_now()
                records[index] = found.to_dict()
                break
            if found is None:
                raise StoreError("找不到指定帳號")
            self._save_accounts(records)
            return found

    def rename_account(self, account_id: str, name: str) -> Account:
        return self.update_account(account_id, name=name)

    def account_root(self, account_id: str, *, create: bool = False) -> Path:
        account_id = str(account_id)
        if not _is_uuid(account_id):
            raise ValidationError("帳號識別碼格式無效")
        path = _safe_child(self.accounts_dir, account_id, require_existing=not create)
        if create:
            path.mkdir(parents=True, exist_ok=True)
            _restrict_permissions(path)
        return path

    def remove_account(self, account_id: str) -> None:
        # Hold the activity guard while taking the store lock.  Runner.start
        # uses the same order (activity -> store), so an account cannot be
        # removed in the small window between validation and queue creation.
        # This also covers jobs waiting for the per-account core lock.
        with _ACCOUNT_ACTIVITY_GUARD:
            if _ACCOUNT_ACTIVITY.get(str(account_id), 0):
                raise StoreError("帳號正在使用中，請先等待或取消下載工作。")
            with self._lock:
                account = self.get_account(account_id)
                path = self.account_root(account.id, create=False)
                # Validate both the UUID and its parent before any recursive
                # removal.  This intentionally refuses symlink/junction escape.
                if path.parent != self.accounts_dir.resolve() or path.name != account.id:
                    raise StoreError("帳號資料路徑不安全")
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path)
                records = [item for item in self._accounts_raw() if str(item.get("id")) != account.id]
                self._save_accounts(records)
                prefs = self._read_preferences_raw()
                if prefs.get("active_account_id") == account.id:
                    prefs.pop("active_account_id", None)
                    self._save_preferences_raw(prefs)

    def _read_preferences_raw(self) -> dict[str, Any]:
        raw = _read_json(self._preferences_path, {"version": _JSON_VERSION, "values": {}})
        if not isinstance(raw, Mapping) or not isinstance(raw.get("values"), Mapping):
            raise StoreError("偏好設定格式無效")
        values = dict(raw["values"])
        protected = values.get("proxy_protected")
        if protected and not values.get("proxy"):
            try:
                encoded = base64.b64decode(str(protected), validate=True)
                values["proxy"] = _dpapi_unprotect(encoded).decode("utf-8")
            except (ValueError, UnicodeError, DataProtectionError) as exc:
                raise StoreError("無法讀取已保存的代理設定") from exc
        return values

    def _save_preferences_raw(self, values: Mapping[str, Any]) -> None:
        safe_values = dict(values)
        proxy = str(safe_values.pop("proxy", "") or "")
        safe_values.pop("proxy_protected", None)
        if proxy:
            try:
                protected = _dpapi_protect(proxy.encode("utf-8"))
                safe_values["proxy_protected"] = base64.b64encode(protected).decode("ascii")
            except (OSError, TypeError, ValueError) as exc:
                raise StoreError("無法安全保存代理設定") from exc
        _atomic_write_json(self._preferences_path, {"version": _JSON_VERSION, "values": safe_values})

    def get_preferences(self) -> Preferences:
        with self._lock:
            return Preferences.from_dict(self._read_preferences_raw(), str(self._default_download_dir))

    # Alias used by callers that prefer ``preferences()``.
    preferences = get_preferences

    def update_preferences(self, **updates: Any) -> Preferences:
        with self._lock:
            values = self._read_preferences_raw()
            allowed = set(Preferences().__dict__)
            for key, value in updates.items():
                if key not in allowed:
                    raise ValidationError(f"不支援的偏好設定：{key}")
                values[key] = value
            # ``Preferences.from_dict`` forgives a damaged file by falling back
            # to defaults; a value the user just typed must be rejected instead.
            normalize_download_advanced(
                {
                    "template": values.get("template"),
                    "include": values.get("include_ext"),
                    "exclude": values.get("exclude_ext"),
                }
            )
            current = Preferences.from_dict(values, str(self._default_download_dir))
            # Keep metadata such as active_account_id that is stored beside
            # the user-facing preference fields.
            values.update(current.to_dict())
            self._save_preferences_raw(values)
            return current

    def set_active_account(self, account_id: str | None) -> None:
        with self._lock:
            if account_id is not None:
                self.get_account(account_id)
            values = self._read_preferences_raw()
            if account_id is None:
                values.pop("active_account_id", None)
            else:
                values["active_account_id"] = str(account_id)
            self._save_preferences_raw(values)

    def get_active_account(self) -> Optional[Account]:
        with self._lock:
            account_id = self._read_preferences_raw().get("active_account_id")
            if not account_id:
                return None
            try:
                return self.get_account(str(account_id))
            except StoreError:
                return None

    def _read_items(self, path: Path) -> list[dict[str, Any]]:
        raw = _read_json(path, {"version": _JSON_VERSION, "items": []})
        if not isinstance(raw, Mapping) or not isinstance(raw.get("items"), list):
            raise StoreError(f"資料格式無效：{path.name}")
        return [dict(item) for item in raw["items"] if isinstance(item, Mapping)]

    def _save_items(self, path: Path, values: Iterable[Mapping[str, Any]]) -> None:
        _atomic_write_json(path, {"version": _JSON_VERSION, "items": [dict(value) for value in values]})

    def mark_interrupted_jobs(self) -> int:
        """Mark jobs left running by a previous process as interrupted.

        This is a local metadata repair only.  It never starts a core process
        or touches Telegram; the GUI decides whether and when to resume the
        returned jobs.
        """

        with self._lock:
            queue_items = self._read_items(self._queue_path)
            history_items = self._read_items(self._history_path)
            changed = 0
            interrupted_ids: set[str] = set()
            for item in queue_items + history_items:
                if item.get("status") == "running":
                    item["status"] = "interrupted"
                    item["finished_at"] = _utc_now()
                    item["error"] = "程式關閉時下載尚未完成，請重新執行。"
                    interrupted_ids.add(str(item.get("id", "")))
                    changed += 1
            if interrupted_ids:
                self._save_items(self._queue_path, queue_items)
                self._save_items(self._history_path, history_items)
            return changed

    # Keep the original public name for callers and older desktop builds.
    recover_interrupted_jobs = mark_interrupted_jobs

    def list_resumable_jobs(self, *, account_id: str | None = None) -> list[DownloadJob]:
        """Return durable jobs that may be resumed without creating a copy."""

        values = self.list_queue(statuses=("queued", "running", "interrupted", "paused"))
        if account_id is None:
            return values
        return [item for item in values if item.account_id == str(account_id)]

    def reseal_plaintext_runtimes(self) -> int:
        """Seal orphaned account runtimes left by an interrupted GUI process.

        The method is intentionally idempotent.  A runtime that is currently
        owned by a Runner is skipped; a runtime from a previous process is
        packed into its DPAPI envelope and removed even when protection fails.
        """

        sealed = 0
        # SessionVault is defined below Store; the name is resolved when this
        # method is called after module import has completed.
        vault = SessionVault(self)
        for account in self.list_accounts():
            with _ACCOUNT_ACTIVITY_GUARD:
                if _ACCOUNT_ACTIVITY.get(account.id, 0):
                    continue
                root = self.account_root(account.id, create=False)
                runtime = root / "runtime"
                if runtime.is_symlink():
                    # Never follow an attacker-controlled runtime link during
                    # startup.  The account root itself is already confined.
                    runtime.unlink()
                    continue
                if not runtime.exists():
                    continue
                if any(runtime.iterdir()):
                    try:
                        vault.seal(account.id)
                    except (BackendError, OSError, zipfile.BadZipFile):
                        # SessionVault.seal purges plaintext in its finally
                        # block.  Keep opening the other accounts so one bad
                        # envelope does not make the whole GUI unusable.
                        pass
                    sealed += 1
                else:
                    vault.cleanup(account.id)
        return sealed

    def _snapshot_export_files(
        self,
        account_id: str,
        job_id: str,
        files: Sequence[str],
    ) -> list[str]:
        """Copy validated JSON imports into the account's durable job data.

        The queue stores the user-selected paths for history readability, but
        those paths may point to a temporary export outside TDLDesktop.  A
        private copy closes the restart window where that source is moved or
        deleted before a paused job is resumed.  The copy is created before
        the queue record is published, and each destination is replaced only
        after it has been flushed to disk.
        """

        if not files:
            return []
        account_root = self.account_root(account_id, create=True)
        snapshot_dir = account_root / "jobdata" / str(job_id) / "imports"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(snapshot_dir.parent)
        _restrict_permissions(snapshot_dir)
        snapshots: list[str] = []
        temporary_paths: list[Path] = []
        try:
            for index, value in enumerate(files):
                source = Path(value).expanduser().resolve()
                if source.is_symlink() or not source.is_file():
                    raise ValidationError("JSON 匯入檔不存在或已被移除")
                destination = snapshot_dir / f"{index:04d}-{uuid.uuid4().hex}.json"
                temporary = destination.with_name(f".{destination.name}.tmp")
                temporary_paths.append(temporary)
                with source.open("rb") as input_file, temporary.open("xb") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                    output_file.flush()
                    os.fsync(output_file.fileno())
                _restrict_permissions(temporary)
                os.replace(temporary, destination)
                _restrict_permissions(destination)
                snapshots.append(str(destination.resolve()))
            return snapshots
        except (OSError, ValueError, ValidationError) as exc:
            raise StoreError("無法保存 JSON 匯入檔快照") from exc
        finally:
            for temporary in temporary_paths:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if len(snapshots) != len(files):
                try:
                    shutil.rmtree(snapshot_dir.parent)
                except OSError:
                    pass

    def resolve_job_files(self, job: DownloadJob) -> list[str]:
        """Return the durable file arguments for a persisted download job."""

        resolved: list[str] = []
        snapshots = list(job.file_snapshots)
        account_root = self.account_root(job.account_id, create=False).resolve()
        for index, original in enumerate(job.files):
            if index < len(snapshots):
                snapshot = Path(snapshots[index]).expanduser()
                try:
                    snapshot = snapshot.resolve()
                except OSError:
                    snapshot = Path("")
                try:
                    snapshot.relative_to(account_root)
                    confined = True
                except ValueError:
                    confined = False
                if confined and snapshot.is_file() and not snapshot.is_symlink():
                    resolved.append(str(snapshot))
                    continue
            resolved.append(str(original))
        return resolved

    def create_download_job(
        self,
        account_id: str,
        links: Sequence[str],
        *,
        files: Sequence[str] | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        continue_download: bool | None = None,
        skip_same: bool | None = None,
        group: bool | None = None,
        concurrency: int | None = None,
        title: str | None = None,
        advanced: Mapping[str, Any] | None = None,
        expected_files: int = 0,
        expected_bytes: int = 0,
        group_by_chat: bool | None = _DEFAULT_GROUP_BY_CHAT,
    ) -> DownloadJob:
        self.get_account(account_id)
        clean_links = validate_message_links(links) if links else []
        clean_files = validate_export_files(files or [])
        if not clean_links and not clean_files:
            raise ValidationError("至少需要一個 Telegram 訊息連結或 JSON 匯入檔")
        prefs = self.get_preferences()
        target = Path(output_dir or prefs.download_dir).expanduser().resolve()
        count = prefs.concurrency if concurrency is None else int(concurrency)
        if count < 1 or count > 64:
            raise ValidationError("同時下載數量必須介於 1 到 64")
        clean_title = _normalize_job_title(title)
        job = DownloadJob(
            id=str(uuid.uuid4()),
            account_id=str(account_id),
            links=clean_links,
            files=clean_files,
            output_dir=str(target),
            continue_download=prefs.continue_download if continue_download is None else bool(continue_download),
            skip_same=prefs.skip_same if skip_same is None else bool(skip_same),
            group=prefs.group if group is None else bool(group),
            concurrency=count,
            title=clean_title,
            advanced=(
                prefs.download_advanced()
                if advanced is None
                else normalize_download_advanced(advanced)
            ),
            expected_files=_non_negative_int(expected_files),
            expected_bytes=_non_negative_int(expected_bytes),
            group_by_chat=(
                _DEFAULT_GROUP_BY_CHAT
                if group_by_chat is None
                else bool(group_by_chat)
            ),
        )
        # Preserve the manifest before publishing the durable queue row.  A
        # failed copy therefore cannot leave a resumable job pointing at a
        # half-written import.
        job.file_snapshots = self._snapshot_export_files(account_id, job.id, clean_files)
        with self._lock:
            queue_items = self._read_items(self._queue_path)
            history_items = self._read_items(self._history_path)
            queue_items.append(job.to_dict())
            history_items.append(job.to_dict())
            self._save_items(self._queue_path, queue_items)
            self._save_items(self._history_path, history_items)
        return job

    def _update_job(self, job_id: str, **updates: Any) -> DownloadJob:
        with self._lock:
            queue_items = self._read_items(self._queue_path)
            history_items = self._read_items(self._history_path)
            found: Optional[dict[str, Any]] = None
            for items in (queue_items, history_items):
                for item in items:
                    if str(item.get("id")) == str(job_id):
                        item.update(updates)
                        if found is None:
                            found = item
            if found is None:
                raise StoreError("找不到下載工作")
            self._save_items(self._queue_path, queue_items)
            self._save_items(self._history_path, history_items)
            return DownloadJob.from_dict(found)

    def mark_job_running(self, job_id: str) -> DownloadJob:
        return self._update_job(
            job_id,
            status="running",
            started_at=_utc_now(),
            finished_at=None,
            error=None,
        )

    def update_job_progress(self, job_id: str, **progress: Any) -> DownloadJob:
        allowed = {"percent", "current", "total", "speed", "filename", "file_progress"}
        updates = {key: value for key, value in progress.items() if key in allowed}
        if "file_progress" in updates:
            updates["file_progress"] = _normalize_file_progress(updates["file_progress"])
        return self._update_job(job_id, **updates)

    def finish_job(self, job_id: str, status: str, *, error: str | None = None) -> DownloadJob:
        if status not in {"completed", "failed", "cancelled", "interrupted", "paused"}:
            raise ValidationError("下載完成狀態無效")
        if status != "completed":
            return self._update_job(job_id, status=status, finished_at=_utc_now(), error=error)
        return self._complete_job(job_id, error=error)

    def _complete_job(self, job_id: str, *, error: str | None) -> DownloadJob:
        """Record a completed job and release everything kept for a resume.

        A finished job is never restarted, so its per-file rows (thousands
        for a channel) would only make every later queue write slower.  Both
        records keep the totals instead; the queue copy itself is dropped by
        ``prune_completed_queue`` on the next launch.
        """

        with self._lock:
            queue_items = self._read_items(self._queue_path)
            history_items = self._read_items(self._history_path)
            found: Optional[dict[str, Any]] = None
            for items in (queue_items, history_items):
                for item in items:
                    if str(item.get("id")) != str(job_id):
                        continue
                    rows = _normalize_file_progress(item.get("file_progress", {}))
                    item.update(status="completed", finished_at=_utc_now(), error=error)
                    if rows:
                        item["file_count"] = len(rows)
                        item["total_bytes"] = sum(int(row.get("total", 0) or 0) for row in rows.values())
                    item["file_progress"] = {}
                    item["file_snapshots"] = []
                    if found is None:
                        found = item
            if found is None:
                raise StoreError("找不到下載工作")
            self._save_items(self._queue_path, queue_items)
            self._save_items(self._history_path, history_items)
            job = DownloadJob.from_dict(found)
        self._remove_job_data(job.account_id, job.id)
        return job

    def prune_completed_queue(self) -> int:
        """Drop completed jobs of earlier sessions from the queue file."""

        with self._lock:
            queue_items = self._read_items(self._queue_path)
            kept = [item for item in queue_items if str(item.get("status")) != "completed"]
            if len(kept) != len(queue_items):
                self._save_items(self._queue_path, kept)
            return len(queue_items) - len(kept)

    def _remove_job_data(self, account_id: str, job_id: str) -> None:
        """Delete one job's private manifest copies; never fails the caller."""

        if not _is_uuid(str(job_id)):
            return
        try:
            jobdata = self.account_root(account_id, create=False) / "jobdata"
            target = _safe_child(jobdata, str(job_id))
            if target.parent != jobdata.resolve() or target.is_symlink():
                return
            shutil.rmtree(target)
        except (BackendError, OSError):
            pass

    def clear_finished_jobs(self) -> list[str]:
        """Drop ended jobs from the queue; the history keeps their records.

        Failed and cancelled jobs stay in the queue so they can be retried,
        which also means they return on every launch until the user clears
        them.  Jobs that may still resume on their own are left untouched.
        """

        removable = {"completed", "failed", "cancelled"}
        removed: list[tuple[str, str]] = []
        with self._lock:
            kept: list[dict[str, Any]] = []
            for item in self._read_items(self._queue_path):
                if str(item.get("status")) in removable:
                    removed.append((str(item.get("account_id", "")), str(item.get("id", ""))))
                else:
                    kept.append(item)
            if removed:
                self._save_items(self._queue_path, kept)
                history_items = self._read_items(self._history_path)
                cleared = {job_id for _account_id, job_id in removed}
                for item in history_items:
                    if str(item.get("id")) in cleared:
                        item["file_progress"] = {}
                        item["file_snapshots"] = []
                self._save_items(self._history_path, history_items)
        for account_id, job_id in removed:
            self._remove_job_data(account_id, job_id)
        return [job_id for _account_id, job_id in removed]

    def purge_stale_exports(self, *, max_age_seconds: float = _EXPORT_RETENTION_SECONDS) -> int:
        """Remove old channel scan manifests no resumable job still names."""

        with self._lock:
            referenced = {
                os.path.normcase(str(Path(path)))
                for job in self.list_resumable_jobs()
                for path in job.files
            }
        cutoff = time.time() - max_age_seconds
        removed = 0
        for account in self.list_accounts():
            try:
                exports = self.account_root(account.id, create=False) / "exports"
                if exports.is_symlink() or not exports.is_dir():
                    continue
                for path in exports.glob("*.json"):
                    if path.is_symlink() or os.path.normcase(str(path.resolve())) in referenced:
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
            except (BackendError, OSError):
                continue
        return removed

    def save_chat_cache(self, account_id: str, chats: Sequence[Mapping[str, Any]]) -> None:
        """Remember one account's chat list so pickers work before a refresh.

        Chat titles are private, so the list gets the same DPAPI envelope as
        the session instead of a readable JSON file.
        """

        payload = json.dumps(
            {"version": _JSON_VERSION, "saved_at": _utc_now(), "chats": [dict(chat) for chat in chats]},
            ensure_ascii=False,
        ).encode("utf-8")
        path = self.account_root(account_id, create=True) / CHAT_CACHE_FILE_NAME
        _atomic_write_bytes(path, _dpapi_protect(payload), strict_permissions=True)

    def load_chat_cache(self, account_id: str) -> tuple[list[dict[str, Any]], str]:
        """Return ``(chats, saved_at)``; an absent or unreadable cache is empty."""

        try:
            path = self.account_root(account_id, create=False) / CHAT_CACHE_FILE_NAME
            if path.is_symlink() or not path.is_file():
                return [], ""
            raw = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
        except (BackendError, OSError, ValueError):
            return [], ""
        if not isinstance(raw, Mapping) or not isinstance(raw.get("chats"), list):
            return [], ""
        chats = [dict(chat) for chat in raw["chats"] if isinstance(chat, Mapping)]
        return chats, str(raw.get("saved_at") or "")

    def get_job(self, job_id: str) -> DownloadJob:
        with self._lock:
            for item in self._read_items(self._history_path):
                if str(item.get("id")) == str(job_id):
                    return DownloadJob.from_dict(item)
        raise StoreError("找不到下載工作")

    def list_queue(self, *, statuses: Sequence[str] | None = None) -> list[DownloadJob]:
        with self._lock:
            values = [DownloadJob.from_dict(item) for item in self._read_items(self._queue_path)]
        if statuses is None:
            return values
        wanted = set(statuses)
        return [item for item in values if item.status in wanted]

    def list_history(self, *, account_id: str | None = None) -> list[DownloadJob]:
        with self._lock:
            values = [DownloadJob.from_dict(item) for item in self._read_items(self._history_path)]
        if account_id is None:
            return values
        return [item for item in values if item.account_id == str(account_id)]

    def export_downloads(self, destination: str | os.PathLike[str]) -> Path:
        target = Path(destination).expanduser().resolve()
        payload = {
            "version": _JSON_VERSION,
            "exported_at": _utc_now(),
            "downloads": [job.to_dict() for job in self.list_history()],
        }
        _atomic_write_json(target, payload)
        return target


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_DPAPI_ENTROPY = b"TDLDesktop-session-v1"
_DPAPI_MAGIC = b"TDL-DPAPI-1\n"
_PLAIN_MAGIC = b"TDL-LOCAL-1\n"


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return _PLAIN_MAGIC + data
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_buffer = ctypes.create_string_buffer(data)
        entropy_buffer = ctypes.create_string_buffer(_DPAPI_ENTROPY)
        input_blob = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
        entropy_blob = _DataBlob(
            len(_DPAPI_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_byte))
        )
        output_blob = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            ctypes.c_wchar_p("TDL Desktop session"),
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error())
        try:
            result = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
        return _DPAPI_MAGIC + result
    except (AttributeError, OSError, TypeError) as exc:
        # This fallback still prevents an unencrypted directory tree from
        # remaining on disk.  On Windows, the UI can surface a warning if the
        # platform DPAPI call itself is unavailable.
        return _PLAIN_MAGIC + data


def _dpapi_unprotect(data: bytes) -> bytes:
    if data.startswith(_PLAIN_MAGIC):
        return data[len(_PLAIN_MAGIC) :]
    if not data.startswith(_DPAPI_MAGIC):
        raise DataProtectionError("帳號工作階段格式無法辨識")
    if os.name != "nt":
        raise DataProtectionError("此工作階段需要 Windows DPAPI")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        protected = data[len(_DPAPI_MAGIC) :]
        input_buffer = ctypes.create_string_buffer(protected)
        entropy_buffer = ctypes.create_string_buffer(_DPAPI_ENTROPY)
        input_blob = _DataBlob(len(protected), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
        entropy_blob = _DataBlob(
            len(_DPAPI_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_byte))
        )
        output_blob = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error())
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
    except (AttributeError, OSError, TypeError) as exc:
        raise DataProtectionError("無法解開帳號工作階段資料") from exc


def _pack_directory(directory: Path) -> bytes:
    if not directory.exists():
        return b""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        base = directory.resolve()
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(base)
            except ValueError as exc:
                raise DataProtectionError("帳號資料包含越界路徑") from exc
            archive.write(resolved, str(relative).replace("\\", "/"))
    return buffer.getvalue()


def _unpack_directory(payload: bytes, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        base = directory.resolve()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
                raise DataProtectionError("帳號工作階段包含不安全路徑")
            target = (base / name).resolve()
            try:
                target.relative_to(base)
            except ValueError as exc:
                raise DataProtectionError("帳號工作階段包含越界路徑") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            _restrict_permissions(target)


class SessionVault:
    """Move one account's temporary Go data through an encrypted envelope."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def account_paths(self, account_id: str) -> tuple[Path, Path, Path]:
        root = self.store.account_root(account_id, create=True)
        return root, root / "runtime", root / SESSION_FILE_NAME

    def _purge_runtime(self, runtime: Path) -> None:
        accounts_root = self.store.accounts_dir.resolve()
        if (
            runtime.name != "runtime"
            or runtime.parent.parent != accounts_root
            or not _is_uuid(runtime.parent.name)
        ):
            raise DataProtectionError("帳號執行期路徑不安全")
        if runtime.exists():
            if runtime.is_symlink():
                runtime.unlink()
            else:
                shutil.rmtree(runtime)

    def prepare(self, account_id: str) -> Path:
        root, runtime, envelope = self.account_paths(account_id)
        # A previous hard termination can leave a plaintext runtime directory.
        # Seal it before replacing it with the last known envelope.
        if runtime.is_symlink():
            # Never traverse a runtime link while packing credentials.  The
            # account root is confined, but a local attacker could still have
            # planted a link after the previous process exited.
            self._purge_runtime(runtime)
        elif runtime.exists() and any(runtime.iterdir()):
            self.seal(account_id)
        self._purge_runtime(runtime)
        runtime.mkdir(parents=True, exist_ok=True)
        if envelope.exists():
            try:
                payload = _dpapi_unprotect(envelope.read_bytes())
                if payload:
                    _unpack_directory(payload, runtime)
            except (OSError, zipfile.BadZipFile) as exc:
                self._purge_runtime(runtime)
                raise DataProtectionError("無法還原帳號工作階段資料") from exc
        _restrict_permissions(runtime)
        return runtime

    def seal(self, account_id: str) -> None:
        root, runtime, envelope = self.account_paths(account_id)
        if not runtime.exists():
            return
        try:
            payload = _pack_directory(runtime)
            if payload:
                protected = _dpapi_protect(payload)
                _atomic_write_bytes(envelope, protected, strict_permissions=True)
            elif envelope.exists():
                # A valid empty runtime should not erase a previous session.
                # Core normally writes at least its Bolt data; this branch is
                # for a failed start before any data was created.
                pass
        finally:
            # Never leave the plaintext runtime behind after a normal return,
            # even if the envelope write fails.  The previous envelope remains
            # available for a later retry; losing a new session is safer than
            # leaving Telegram credentials readable on disk.
            self._purge_runtime(runtime)

    def cleanup(self, account_id: str) -> None:
        _root, runtime, _envelope = self.account_paths(account_id)
        self._purge_runtime(runtime)


_PUBLIC_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_NUMBER_RE = re.compile(r"^[0-9]{1,20}$")
_ALLOWED_QUERY_KEYS = {"comment", "thread", "single"}


def validate_message_link(link: str) -> str:
    """Validate a Telegram message URL understood by the Go parser.

    Accepted forms are public ``t.me/<username>/<message>`` links, Telegram
    preview ``t.me/s/<username>/<message>`` links, private channel
    ``t.me/c/<channel>/<message>`` links, and their topic variants.  A
    harmless ``single`` preview flag is normalized away before the core sees
    the URL.
    Invite links, arbitrary hosts, fragments, shell-like strings, and unknown
    query parameters are rejected before they can reach the child process.
    """

    if not isinstance(link, str) or not link.strip():
        raise ValidationError("Telegram 連結不可為空")
    value = link.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValidationError("Telegram 連結格式無效") from exc
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise ValidationError("只接受 HTTPS Telegram 訊息連結")
    if parsed.hostname.lower() not in {"t.me", "www.t.me"}:
        raise ValidationError("連結網域必須是 t.me")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Telegram 連結連接埠無效") from exc
    if parsed.username or parsed.password or port:
        raise ValidationError("Telegram 連結不可包含登入資訊或連接埠")
    if parsed.fragment:
        raise ValidationError("Telegram 連結不可包含片段")
    queries = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in _ALLOWED_QUERY_KEYS for key, _value in queries):
        raise ValidationError("Telegram 連結包含不支援的參數")
    for key, query_value in queries:
        if key == "single":
            if query_value not in {"", "1", "true"}:
                raise ValidationError("Telegram 連結 single 參數格式無效")
            continue
        if key in _ALLOWED_QUERY_KEYS and not _NUMBER_RE.fullmatch(query_value):
            raise ValidationError("Telegram 連結參數必須是數字")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) not in {2, 3, 4}:
        if segments and (
            segments[0].startswith("+") or segments[0].lower() == "joinchat"
        ):
            raise ValidationError("這是 Telegram 邀請連結，請先加入頻道後使用頻道下載。")
        if (
            (len(segments) == 1 and (_PUBLIC_USERNAME_RE.fullmatch(segments[0]) or _NUMBER_RE.fullmatch(segments[0])))
            or (
                len(segments) == 2
                and segments[0].lower() in {"s", "c"}
                and (_PUBLIC_USERNAME_RE.fullmatch(segments[1]) or _NUMBER_RE.fullmatch(segments[1]))
            )
        ):
            raise ValidationError(
                "這是頻道／群組首頁，請改用「頻道下載」選取頻道，或貼上包含訊息編號的連結。"
            )
        raise ValidationError("Telegram 訊息連結格式無效")

    # Telegram's public preview links use /s/<channel>/<message>.  The core
    # parser intentionally understands the canonical two-segment form, so
    # remove only this harmless preview marker before passing the URL on.
    if len(segments) == 3 and segments[0].lower() == "s":
        segments = segments[1:]

    if len(segments) == 2:
        peer, message = segments
        if not (_PUBLIC_USERNAME_RE.fullmatch(peer) or _NUMBER_RE.fullmatch(peer)):
            raise ValidationError("Telegram 公開頻道名稱無效")
        if not _NUMBER_RE.fullmatch(message) or int(message) <= 0:
            raise ValidationError("Telegram 訊息編號無效")
    elif len(segments) == 3:
        first, second, message = segments
        if first == "c":
            if not _NUMBER_RE.fullmatch(second):
                raise ValidationError("Telegram 私人頻道編號無效")
        elif not _PUBLIC_USERNAME_RE.fullmatch(first) or not _NUMBER_RE.fullmatch(second):
            raise ValidationError("Telegram 主題連結格式無效")
        if not _NUMBER_RE.fullmatch(message) or int(message) <= 0:
            raise ValidationError("Telegram 訊息編號無效")
    else:
        first, channel, topic, message = segments
        if first != "c" or not _NUMBER_RE.fullmatch(channel):
            raise ValidationError("Telegram 私人主題連結格式無效")
        if not _NUMBER_RE.fullmatch(topic) or int(topic) <= 0:
            raise ValidationError("Telegram 主題編號無效")
        if not _NUMBER_RE.fullmatch(message) or int(message) <= 0:
            raise ValidationError("Telegram 訊息編號無效")

    # Rebuild the URL to remove surrounding whitespace and normalize the host.
    normalized_path = "/" + "/".join(segments)
    normalized_query = "&".join(
        f"{key}={query_value}" for key, query_value in queries if key != "single"
    )
    return urlunsplit(("https", "t.me", normalized_path, normalized_query, ""))


def validate_message_links(links: Sequence[str]) -> list[str]:
    if isinstance(links, (str, bytes)) or not links:
        raise ValidationError("至少需要一個 Telegram 訊息連結")
    result = [validate_message_link(link) for link in links]
    if len(result) > 1000:
        raise ValidationError("一次最多只能加入 1000 個訊息連結")
    return result


def validate_export_file(path: str | os.PathLike[str]) -> str:
    """Validate one official Telegram export JSON file for ``tdl dl --file``."""

    if not isinstance(path, (str, os.PathLike)):
        raise ValidationError("JSON 匯入檔路徑格式無效")
    raw = Path(path).expanduser()
    # Check the link before resolve(); checking the resolved target would turn
    # a symlink into an ordinary file and defeat the confinement guarantee.
    if raw.is_symlink():
        raise ValidationError("JSON 匯入檔不可是符號連結")
    value = raw.resolve()
    if value.suffix.lower() != ".json" or not value.is_file():
        raise ValidationError("JSON 匯入檔不存在或副檔名不是 .json")
    try:
        with value.open("r", encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("JSON 匯入檔不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("JSON 匯入檔頂層必須是 Telegram 匯出物件")
    chat_id = payload.get("id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, (int, float)):
        raise ValidationError("JSON 匯入檔缺少有效的 Telegram 對話 id")
    if isinstance(chat_id, float) and not chat_id.is_integer():
        raise ValidationError("JSON 匯入檔的 Telegram 對話 id 必須是整數")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValidationError("JSON 匯入檔缺少 messages 訊息清單")
    # The Go parser intentionally ignores non-message records, but malformed
    # message objects should fail before a network client is ever started.
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValidationError(f"JSON 匯入檔第 {index + 1} 筆訊息格式無效")
        message_id = message.get("id")
        if message_id is not None and (
            isinstance(message_id, bool) or not isinstance(message_id, (int, float))
        ):
            raise ValidationError(f"JSON 匯入檔第 {index + 1} 筆訊息 id 無效")
        if isinstance(message_id, float) and not message_id.is_integer():
            raise ValidationError(f"JSON 匯入檔第 {index + 1} 筆訊息 id 必須是整數")
    return str(value)


def validate_export_files(files: Sequence[str | os.PathLike[str]]) -> list[str]:
    if isinstance(files, (str, bytes)):
        raise ValidationError("JSON 匯入檔必須是清單")
    result = [validate_export_file(path) for path in files]
    if len(result) > 100:
        raise ValidationError("一次最多只能加入 100 個 JSON 匯入檔")
    return result


def _read_export_message_stats(path: str | os.PathLike[str]) -> tuple[int, int]:
    """Validate an export JSON and return its message count and byte total."""

    validated = Path(validate_export_file(path))
    try:
        with validated.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("匯出 JSON 檔案無法讀取") from exc
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    if not isinstance(messages, list):
        raise ValidationError("匯出 JSON 檔案缺少 messages 訊息清單")
    total_bytes = 0
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValidationError(f"匯出 JSON 第 {index + 1} 筆訊息格式無效")
        size = message.get("size", 0)
        if size in (None, ""):
            size = 0
        if isinstance(size, bool):
            raise ValidationError(f"匯出 JSON 第 {index + 1} 筆檔案大小無效")
        try:
            numeric_size = int(size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(f"匯出 JSON 第 {index + 1} 筆檔案大小無效") from exc
        if numeric_size < 0 or (isinstance(size, float) and not size.is_integer()):
            raise ValidationError(f"匯出 JSON 第 {index + 1} 筆檔案大小無效")
        total_bytes += numeric_size
    return len(messages), total_bytes


def _read_export_message_count(path: str | os.PathLike[str]) -> int:
    """Backward-compatible count-only helper for callers outside Runner."""

    count, _total_bytes = _read_export_message_stats(path)
    return count


def validate_proxy(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValidationError("代理設定格式無效") from exc
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValidationError("代理只支援 http、https、socks5 或 socks5h")
    if parsed.fragment or parsed.path not in ("", "/") or parsed.query:
        raise ValidationError("代理設定包含不支援的內容")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValidationError("代理連接埠無效") from exc
    return value


_CHAT_ID_RE = re.compile(r"^-?[0-9]{1,20}$")
_CHAT_REF_RE = re.compile(r"^(channel|chat|group|user|private):[0-9]{1,20}$", re.IGNORECASE)


def validate_chat_id(chat_id: str | int) -> str:
    """Accept a numeric ID or typed peer reference returned by ``chat ls``.

    Typed refs such as ``channel:123`` are necessary because Telegram can
    reuse a numeric ID across channels, groups, and users.
    """

    if isinstance(chat_id, bool):
        raise ValidationError("Telegram 頻道或群組識別碼無效")
    value = str(chat_id).strip()
    if _CHAT_ID_RE.fullmatch(value):
        return value
    match = _CHAT_REF_RE.fullmatch(value)
    if match is not None:
        return f"{match.group(1).lower()}:{value.split(':', 1)[1]}"
    raise ValidationError("Telegram 頻道或群組識別碼無效")


def validate_topic_id(topic_id: str | int | None) -> Optional[int]:
    """Validate one forum topic id; ``None`` means the whole chat."""

    if topic_id in (None, ""):
        return None
    if isinstance(topic_id, bool):
        raise ValidationError("Telegram 論壇主題識別碼無效")
    try:
        value = int(str(topic_id).strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("Telegram 論壇主題識別碼無效") from exc
    # Telegram topic/message identifiers are signed MTProto int32 values.
    if value <= 0 or value > 2**31 - 1:
        raise ValidationError("Telegram 論壇主題識別碼無效")
    return value


_DESTINATION_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def validate_destination(value: Any) -> str:
    """Validate an upload/forward target chat; empty means Saved Messages.

    Accepts the typed reference from the chat list (``channel:123``), a
    numeric ID, or a public username with or without ``@``.  Anything else is
    refused because the core would otherwise evaluate it as an expression.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    if _CHAT_ID_RE.fullmatch(text) or _CHAT_REF_RE.fullmatch(text):
        return validate_chat_id(text)
    username = text.lstrip("@")
    if _DESTINATION_USERNAME_RE.fullmatch(username):
        return username
    raise ValidationError("目的地必須是清單中的對話、數字 ID 或 @username")


def validate_upload_paths(paths: Sequence[str | os.PathLike[str]]) -> list[str]:
    """Validate the local files or folders handed to ``tdl up --path``."""

    if isinstance(paths, (str, bytes)) or not paths:
        raise ValidationError("請先選擇要上傳的檔案或資料夾")
    result: list[str] = []
    for raw in paths:
        if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
            raise ValidationError("上傳路徑格式無效")
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise ValidationError(f"找不到要上傳的項目：{path.name}")
        if str(path) not in result:
            result.append(str(path))
    if len(result) > 500:
        raise ValidationError("一次最多只能加入 500 個上傳項目")
    return result


def validate_forward_sources(sources: Sequence[str | os.PathLike[str]]) -> list[str]:
    """Validate ``tdl forward --from`` values: message links or export JSON."""

    if isinstance(sources, (str, bytes)) or not sources:
        raise ValidationError("請先加入要轉傳的訊息連結或 JSON 清單")
    result: list[str] = []
    for raw in sources:
        text = str(raw or "").strip()
        if not text:
            continue
        # The core decides link versus file by the ``http`` prefix.
        value = validate_message_link(text) if text.lower().startswith("http") else validate_export_file(text)
        if value not in result:
            result.append(value)
    if not result:
        raise ValidationError("請先加入要轉傳的訊息連結或 JSON 清單")
    if len(result) > 1000:
        raise ValidationError("一次最多只能加入 1000 個轉傳來源")
    return result


def validate_backup_destination(path: str | os.PathLike[str]) -> str:
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ValidationError("請選擇備份檔的儲存位置")
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != ".tdl":
        raise ValidationError("備份檔必須使用 .tdl 副檔名")
    if not target.parent.is_dir():
        raise ValidationError("備份檔的資料夾不存在")
    if target.is_dir():
        raise ValidationError("備份檔位置是一個資料夾")
    return str(target)


def validate_backup_source(path: str | os.PathLike[str]) -> str:
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ValidationError("請選擇要還原的備份檔")
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValidationError("備份檔不可是符號連結")
    source = source.resolve()
    if source.suffix.lower() != ".tdl" or not source.is_file():
        raise ValidationError("備份檔不存在或副檔名不是 .tdl")
    return str(source)


def upload_caption_expression(mode: Any, text: Any = "") -> str:
    """Build the ``tdl up --caption`` expression for a friendly caption mode.

    The core evaluates the flag as an expression and then parses the result
    as HTML, so literal text is HTML-escaped and quoted as a string literal.
    """

    kind = str(mode or "none").strip().lower()
    if kind == "filename":
        return "FileName + FileExt"
    if kind == "text":
        clean = str(text or "").strip()
        if len(clean) > 1024:
            raise ValidationError("說明文字最多 1024 個字元")
        if clean:
            escaped = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return json.dumps(escaped, ensure_ascii=False)
    elif kind != "none":
        raise ValidationError("說明文字模式無效")
    return '""'


def validate_media_types(media_types: Sequence[str]) -> tuple[str, ...]:
    """Normalize the four media filters accepted by the desktop workflow."""

    if media_types is None or not isinstance(media_types, Sequence):
        raise ValidationError("媒體類型格式無效")
    if isinstance(media_types, str):
        values: Sequence[str] = media_types.split(",")
    else:
        values = media_types
    if isinstance(values, (bytes, bytearray)):
        raise ValidationError("媒體類型格式無效")
    normalized: list[str] = []
    for value in values:
        item = str(value).strip().lower()
        if item not in EXPORT_MEDIA_TYPES:
            raise ValidationError("媒體類型只能是影片、照片、音訊或文件")
        if item not in normalized:
            normalized.append(item)
    if not normalized:
        raise ValidationError("至少選擇一種媒體類型")
    # Stable ordering keeps command lines and saved diagnostics deterministic.
    return tuple(item for item in ("video", "photo", "audio", "document") if item in normalized)


def _parse_export_date(value: str | date | datetime, label: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(_TAIPEI_TZ).date()
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}必須是 YYYY-MM-DD 日期")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{label}必須是 YYYY-MM-DD 日期") from exc


def validate_export_window(
    start_date: str | date | datetime,
    end_date: str | date | datetime,
) -> tuple[str, str, int, int]:
    """Return Taiwan-local date strings and inclusive UTC Unix boundaries."""

    start = _parse_export_date(start_date, "開始日期")
    end = _parse_export_date(end_date, "結束日期")
    if start > end:
        raise ValidationError("開始日期不可晚於結束日期")
    start_local = datetime.combine(start, datetime_time.min, tzinfo=_TAIPEI_TZ)
    end_local = datetime.combine(end, datetime_time(23, 59, 59), tzinfo=_TAIPEI_TZ)
    return start.isoformat(), end.isoformat(), int(start_local.timestamp()), int(end_local.timestamp())


_SECRET_URL_RE = re.compile(
    r"(?i)(\b(?:https?|socks5?h?)://)([^\s/@:]+):([^\s/@]+)@"
)
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(phone|password|passcode|code|token|session|api[_ -]?hash)\s*([:=])\s*([^\s,;]+)"
)
_PHONE_RE = re.compile(r"(?<!\w)\+?[0-9][0-9 ()-]{6,}[0-9](?!\w)")


def sanitize_text(text: Any) -> str:
    """Remove proxy credentials and common login secrets from logs/errors."""

    value = str(text or "")
    value = _SECRET_URL_RE.sub(r"\1***:***@", value)
    value = _SECRET_FIELD_RE.sub(r"\1\2***", value)
    value = _PHONE_RE.sub("<電話>", value)
    return value[:4000]


_ERROR_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("session_password_needed", "需要輸入 Telegram 兩步驟驗證密碼。"),
    ("phone_code_invalid", "Telegram 驗證碼錯誤，請重新輸入。"),
    ("phone_number_invalid", "手機號碼格式無效，請確認國碼。"),
    ("phone_number_banned", "此手機號碼無法登入 Telegram。"),
    ("auth_key_unregistered", "Telegram 工作階段已失效，請重新登入。"),
    ("session_revoked", "Telegram 工作階段已被撤銷，請重新登入。"),
    ("user_deactivated", "此 Telegram 帳號已被停用。"),
    ("channel_private", "這個頻道是私人頻道，請確認登入帳號有權限。"),
    ("code_expired", "Telegram 驗證碼已過期，請重新取得驗證碼。"),
    ("password_hash_invalid", "Telegram 兩步驟驗證密碼無效，請重新輸入。"),
    ("flood_wait", "Telegram 要求稍後再試，請等待一段時間。"),
    ("takeout_init_delay", "Telegram 要求等待一段時間（或在其他裝置確認）後才能使用 Takeout 模式，請先到設定關閉此選項。"),
    ("chat_forwards_restricted", "來源對話禁止轉傳，請改用「複製」方式。"),
    ("chat_write_forbidden", "這個帳號沒有在目的地發送訊息的權限。"),
    ("chat_admin_required", "這個操作需要目的地的管理員權限。"),
    ("user_banned_in_channel", "這個帳號在目的地被限制發言。"),
    ("username_not_occupied", "找不到這個 @username，請確認拼字。"),
    ("username_invalid", "找不到這個 @username，請確認拼字。"),
    ("peer_id_invalid", "找不到目的地對話，請確認帳號已加入該對話後更新清單。"),
    ("execute template", "檔名格式有誤，請到「設定」檢查檔名格式。"),
    ("template: dl", "檔名格式有誤，請到「設定」檢查檔名格式。"),
    ("timeout", "網路連線逾時，請檢查網路後重試。"),
    ("deadline exceeded", "網路連線逾時，請檢查網路後重試。"),
    ("network is unreachable", "目前無法連線網路，請檢查網路設定。"),
    ("not authorized", "尚未登入 Telegram，請先新增帳號並登入。"),
    ("invalid message link", "Telegram 訊息連結無效。"),
    ("no urls or files", "沒有可下載的連結。"),
    ("interrupted", "操作已中止。"),
    ("cancel", "操作已取消。"),
    ("database is locked", "帳號資料正在使用中，請稍後再試。"),
    ("current database is used", "帳號資料正在使用中，請稍後再試。"),
)

_AUTH_INVALID_MARKERS = (
    "auth_key_unregistered",
    "auth key unregistered",
    "session_revoked",
    "session revoked",
    "session revoked",
    "user_deactivated",
    "user deactivated",
    "account deactivated",
    "unauthorized",
    "not authorized",
    "工作階段已失效",
    "帳號已被停用",
    "帳號已停用",
    "尚未登入 telegram",
)


def translate_error(message: Any) -> str:
    clean = sanitize_text(message)
    lowered = clean.lower()
    for needle, translation in _ERROR_TRANSLATIONS:
        if needle in lowered:
            return translation
    return clean or "核心程式執行失敗。"


def _is_auth_invalid_error(*values: Any) -> bool:
    """Return whether a core error means the saved Telegram session is stale."""

    haystack = " ".join(sanitize_text(value).lower() for value in values if value not in (None, ""))
    return any(marker in haystack for marker in _AUTH_INVALID_MARKERS)


def _event_name(raw: Mapping[str, Any]) -> str:
    value = raw.get("event", raw.get("type", raw.get("kind", "")))
    return str(value or "").strip().lower()


def normalize_event(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize protocol aliases while dropping unneeded sensitive fields."""

    name = _event_name(raw)
    if name in {"prompt", "input", "ask"}:
        kind = str(raw.get("kind", raw.get("prompt", ""))).lower()
        if kind not in {"phone", "code", "password"}:
            kind = "code"
        result: dict[str, Any] = {"event": "prompt", "kind": kind}
        if raw.get("message"):
            result["message"] = sanitize_text(raw["message"])
        return result
    if name in {"qr", "qrcode", "qr_code"}:
        encoded = raw.get("png_base64", raw.get("base64", raw.get("png", "")))
        if not isinstance(encoded, str):
            encoded = ""
        # Validate only the base64 shape here; GUI can decide how to render it.
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            encoded = ""
        return {"event": "qr", "png_base64": encoded}
    if name in {"authenticated", "auth", "login_success", "logged_in"}:
        user_id = raw.get("id", raw.get("user_id"))
        return {
            "event": "authenticated",
            "id": None if user_id in (None, "") else str(user_id),
            "username": None if raw.get("username") in (None, "") else str(raw.get("username")).lstrip("@"),
            "name": None
            if raw.get("name", raw.get("display_name")) in (None, "")
            else str(raw.get("name", raw.get("display_name"))),
        }
    if name in {"topics", "topic_list"}:
        topics: list[dict[str, str]] = []
        raw_topics = raw.get("topics", raw.get("items", []))
        if isinstance(raw_topics, Sequence) and not isinstance(raw_topics, (str, bytes, bytearray)):
            for raw_topic in raw_topics:
                if not isinstance(raw_topic, Mapping):
                    continue
                topic_id = raw_topic.get("id", raw_topic.get("topic_id", raw_topic.get("thread_id", "")))
                if topic_id in (None, ""):
                    continue
                try:
                    clean_id = str(validate_topic_id(topic_id))
                except ValidationError:
                    continue
                topics.append(
                    {
                        "id": clean_id,
                        "title": sanitize_text(raw_topic.get("title", raw_topic.get("name", ""))),
                    }
                )
        result = {
            "event": "topics",
            "topics": topics,
            "chat_ref": sanitize_text(raw.get("chat_ref", raw.get("ref", ""))),
        }
        if raw.get("partial") or raw.get("skipped") not in (None, "", 0):
            result["partial"] = bool(raw.get("partial", False))
            try:
                result["skipped"] = max(0, int(raw.get("skipped", 0) or 0))
            except (TypeError, ValueError):
                result["skipped"] = 0
        if raw.get("message"):
            result["message"] = sanitize_text(raw.get("message"))
        return result
    if name in {"chats", "chat_list", "dialogs"}:
        chats: list[dict[str, Any]] = []
        raw_chats = raw.get("chats", [])
        if isinstance(raw_chats, Sequence) and not isinstance(raw_chats, (str, bytes, bytearray)):
            for raw_chat in raw_chats:
                if not isinstance(raw_chat, Mapping):
                    continue
                try:
                    chat_id = validate_chat_id(raw_chat.get("id", ""))
                except ValidationError:
                    continue
                title = raw_chat.get("title", raw_chat.get("visible_name", raw_chat.get("name", "")))
                username = raw_chat.get("username", "")
                chat_ref = str(raw_chat.get("ref", "") or "").strip()
                if not _CHAT_REF_RE.fullmatch(chat_ref):
                    chat_ref = ""
                topics: list[dict[str, str]] = []
                raw_topics = raw_chat.get("topics", [])
                if isinstance(raw_topics, Sequence) and not isinstance(raw_topics, (str, bytes, bytearray)):
                    for raw_topic in raw_topics:
                        if not isinstance(raw_topic, Mapping):
                            continue
                        topic_id = raw_topic.get("id", "")
                        if topic_id in (None, ""):
                            continue
                        topics.append(
                            {
                                "id": str(topic_id),
                                "title": sanitize_text(raw_topic.get("title", "")),
                            }
                        )
                chats.append(
                    {
                        "id": chat_id,
                        # Keep the typed peer reference (for example
                        # ``channel:123``). Numeric IDs are not unique across
                        # Telegram peer kinds, so the UI must retain this
                        # value when it starts an export.
                        "ref": chat_ref,
                        "type": str(raw_chat.get("type", "unknown") or "unknown").lower(),
                        "forum": bool(raw_chat.get("forum", raw_chat.get("is_forum", False))),
                        "title": sanitize_text(title),
                        "username": str(username or "").lstrip("@"),
                        "topics": topics,
                    }
                )
        try:
            skipped = max(0, int(raw.get("skipped", 0) or 0))
        except (TypeError, ValueError):
            skipped = 0
        return {
            "event": "chats",
            "chats": chats,
            "skipped": skipped,
            "partial": bool(raw.get("partial", False)),
            "message": sanitize_text(raw.get("message", "")),
        }
    if name in {"exported", "export_complete", "exported_file"}:
        path = raw.get("path", raw.get("output", ""))
        try:
            count = max(0, int(raw.get("count", 0) or 0))
        except (TypeError, ValueError):
            count = 0
        result = {
            "event": "exported",
            "path": str(path or ""),
            "count": count,
        }
        if raw.get("total_bytes") not in (None, ""):
            try:
                total_bytes = int(raw.get("total_bytes"))
            except (TypeError, ValueError, OverflowError):
                total_bytes = None
            if total_bytes is not None:
                result["total_bytes"] = max(0, total_bytes)
        return result
    if name in {"status", "state", "message"}:
        result = {
            "event": "status",
            "message": sanitize_text(raw.get("message", raw.get("text", raw.get("status", "")))),
        }
        # Upload and forward announce how many items they are about to send.
        if raw.get("count") not in (None, "") and not isinstance(raw.get("count"), bool):
            try:
                result["count"] = max(0, int(raw["count"]))
            except (TypeError, ValueError, OverflowError):
                pass
        return result
    if name in {"progress", "download_progress"}:
        result = {"event": "progress"}
        # ``file_id`` and ``state`` are part of the desktop protocol's
        # per-file progress contract.  Keep them through the backend relay so
        # the GUI can update a stable row after a restart.  Text fields are
        # sanitized just like the older filename/speed fields; numeric
        # counters intentionally retain their core-provided JSON values.
        for key in ("percent", "current", "total", "speed", "filename", "file_id", "state"):
            if key in raw:
                value = raw[key]
                result[key] = sanitize_text(value) if key in {"speed", "filename", "file_id", "state"} else value
        return result
    if name in {"error", "failed", "failure"}:
        diagnostic = sanitize_text(raw.get("message", raw.get("error", "")))
        translated = translate_error(diagnostic)
        result = {"event": "error", "message": translated}
        # The core may associate an error with one manifest item.  Preserve
        # the stable identity/state so the queue row can mark that file
        # failed instead of losing the relationship at the protocol boundary.
        for key in ("file_id", "state"):
            if raw.get(key) not in (None, ""):
                result[key] = sanitize_text(raw.get(key))
        # Keep a redacted diagnostic for the details pane/log export without
        # replacing the Traditional Chinese message shown to most users.
        if diagnostic and diagnostic != translated:
            result["detail"] = diagnostic
        return result
    if name in {"done", "complete", "completed", "success"}:
        return {"event": "completed"}
    message = raw.get("message", raw.get("text", ""))
    result = {"event": "log", "message": sanitize_text(message)}
    if raw.get("level"):
        result["level"] = str(raw["level"])
    return result


@dataclass
class LoginRequest:
    account_id: str
    mode: str = "code"
    desktop_path: Optional[str] = None
    passcode: Optional[str] = None
    proxy: Optional[str] = None


@dataclass
class ListChatsRequest:
    account_id: str
    proxy: Optional[str] = None


@dataclass
class ListTopicsRequest:
    account_id: str
    chat_id: str
    proxy: Optional[str] = None


@dataclass
class ExportRequest:
    account_id: str
    chat_id: str
    start_date: str | date | datetime
    end_date: str | date | datetime
    media_types: Sequence[str]
    output_path: Optional[str] = None
    proxy: Optional[str] = None
    start_unix: Optional[int] = None
    end_unix: Optional[int] = None
    # Added last to preserve positional compatibility with existing callers.
    topic_id: Optional[int] = None


@dataclass
class DownloadRequest:
    account_id: str
    links: Sequence[str]
    files: Sequence[str] | None = None
    output_dir: Optional[str] = None
    continue_download: Optional[bool] = None
    skip_same: Optional[bool] = None
    group: Optional[bool] = None
    concurrency: Optional[int] = None
    proxy: Optional[str] = None
    job_id: Optional[str] = None
    title: Optional[str] = None
    # ``None`` inherits the preferences; see ``normalize_download_advanced``.
    advanced: Optional[Mapping[str, Any]] = None
    expected_files: int = 0
    expected_bytes: int = 0
    # New direct requests default to chat folders.  A persisted job overrides
    # this value in ``Runner.start`` so legacy manifests remain flat.
    group_by_chat: bool = _DEFAULT_GROUP_BY_CHAT


@dataclass
class UploadRequest:
    account_id: str
    paths: Sequence[str]
    # Empty uploads to Saved Messages; see ``validate_destination``.
    chat: str = ""
    topic_id: Optional[int] = None
    as_photo: bool = False
    remove_after: bool = False
    caption_mode: str = "none"
    caption_text: str = ""
    include: Sequence[str] = ()
    exclude: Sequence[str] = ()
    concurrency: Optional[int] = None
    threads: Optional[int] = None
    proxy: Optional[str] = None


@dataclass
class ForwardRequest:
    account_id: str
    # Message links and/or export JSON files.
    sources: Sequence[str]
    to: str = ""
    mode: str = "direct"
    silent: bool = False
    dry_run: bool = False
    single: bool = False
    desc: bool = False
    proxy: Optional[str] = None


@dataclass
class BackupRequest:
    account_id: str
    destination: str
    proxy: Optional[str] = None


@dataclass
class RecoverRequest:
    account_id: str
    source: str
    proxy: Optional[str] = None


@dataclass
class WhoamiRequest:
    """Ask Telegram who the stored session belongs to and record the answer."""

    account_id: str
    proxy: Optional[str] = None


# Operations that never create a download record.  They share the per-account
# core lock with downloads and wait their turn instead of failing fast.
_TransferRequest = (UploadRequest, ForwardRequest, BackupRequest, RecoverRequest, WhoamiRequest)

CoreRequest = (
    LoginRequest
    | ListChatsRequest
    | ListTopicsRequest
    | ExportRequest
    | DownloadRequest
    | UploadRequest
    | ForwardRequest
    | BackupRequest
    | RecoverRequest
    | WhoamiRequest
)


def _csv_arg(value: str) -> str:
    """Quote one value for a cobra StringSlice flag, which splits on commas."""

    text = str(value)
    if "," in text or '"' in text:
        return '"' + text.replace('"', '""') + '"'
    return text


@dataclass
class RunResult:
    success: bool
    returncode: Optional[int]
    cancelled: bool = False
    error: Optional[str] = None
    job_id: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    paused: bool = False


class RunHandle:
    """Thread-safe handle returned by :meth:`Runner.start`."""

    def __init__(self, account_id: str, callback: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        self.account_id = account_id
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.done = threading.Event()
        self.cancel_requested = threading.Event()
        self.pause_requested = threading.Event()
        self.stop_requested = threading.Event()
        self._callback = callback
        self._process: Optional[subprocess.Popen[str]] = None
        self._process_lock = threading.RLock()
        self._input_lock = threading.Lock()
        self._cancel_watchdog_started = False
        self._result: Optional[RunResult] = None
        self._thread: Optional[threading.Thread] = None

    def _set_thread(self, thread: threading.Thread) -> None:
        self._thread = thread

    def _set_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._process = process

    def _send(self, payload: Mapping[str, Any]) -> bool:
        with self._process_lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                return False
            try:
                with self._input_lock:
                    process.stdin.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
                    process.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def respond(self, value: str) -> bool:
        """Answer a visible phone/code/password prompt without logging it."""

        if not isinstance(value, str):
            raise ValidationError("輸入內容必須是文字")
        return self._send({"value": value})

    def _request_cancel(self) -> bool:
        sent = self._send({"command": "cancel"})
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is None:
                # Give the Go bridge time to cancel its context and flush the
                # downloader checkpoint.  A daemon watchdog guarantees that a
                # stuck network call cannot keep the GUI blocked forever.
                if not self._cancel_watchdog_started:
                    self._cancel_watchdog_started = True
                    threading.Thread(
                        target=self._cancel_watchdog,
                        args=(process,),
                        name="tdl-core-cancel",
                        daemon=True,
                    ).start()
                sent = True
        return sent

    def cancel(self) -> bool:
        """Explicitly cancel the operation and mark a download cancelled."""

        self.cancel_requested.set()
        self.stop_requested.set()
        return self._request_cancel() or not self.done.is_set()

    def pause(self) -> bool:
        """Gracefully pause the operation so a download can resume later.

        The core receives the same cancellation command used for an explicit
        cancel, allowing it to flush its checkpoint.  The durable Store state
        and :class:`RunResult` retain the distinction between pause and cancel.
        """

        self.pause_requested.set()
        self.stop_requested.set()
        return self._request_cancel() or not self.done.is_set()

    @staticmethod
    def _cancel_watchdog(process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 3.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

    def get_event(self, timeout: float | None = None) -> Optional[dict[str, Any]]:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait(self, timeout: float | None = None) -> Optional[RunResult]:
        if not self.done.wait(timeout):
            return None
        return self._result

    @property
    def result(self) -> Optional[RunResult]:
        return self._result


class Runner:
    """Run one safe, account-scoped core operation in a background thread."""

    def __init__(
        self,
        store: Store,
        *,
        executable: str | os.PathLike[str] | None = None,
        project_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.store = store
        self.project_root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[1]
        self.executable = Path(executable).resolve() if executable else None
        self.vault = SessionVault(store)
        self._handles: set[RunHandle] = set()
        self._handles_guard = threading.Lock()

    def resolve_executable(self) -> Path:
        candidates: list[Path] = []
        if self.executable is not None:
            candidates.append(self.executable)
        candidates.extend(
            [
                self.project_root / "bin" / "tdl-core.exe",
                self.project_root / "bin" / "tdl-core",
            ]
        )
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.extend([Path(meipass) / "bin" / "tdl-core.exe", Path(meipass) / "bin" / "tdl-core"])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise CoreNotFound("找不到核心程式 tdl-core.exe，請確認 bin 目錄完整。")

    def _account_lock(self, account_id: str) -> threading.Lock:
        return _shared_account_lock(account_id)

    def _exports_root(self, account_id: str) -> Path:
        root = self.store.account_root(account_id, create=True)
        exports = root / "exports"
        if exports.is_symlink():
            raise ValidationError("帳號匯出目錄不安全")
        exports.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(exports)
        return exports.resolve()

    def _validate_export_output_path(self, account_id: str, output_path: str | os.PathLike[str] | None) -> None:
        if output_path in (None, ""):
            return
        exports = self._exports_root(account_id)
        raw = Path(output_path).expanduser()
        candidate = (raw if raw.is_absolute() else exports / raw).resolve()
        try:
            candidate.relative_to(exports)
        except ValueError as exc:
            raise ValidationError("匯出檔案必須位於帳號匯出目錄") from exc
        if candidate.suffix.lower() != ".json":
            raise ValidationError("匯出檔案必須使用 .json 副檔名")
        if candidate.exists():
            raise ValidationError("匯出檔案已存在，請重新開始匯出")

    def _allocate_export_output_path(
        self,
        account_id: str,
        output_path: str | os.PathLike[str] | None,
    ) -> str:
        exports = self._exports_root(account_id)
        if output_path not in (None, ""):
            self._validate_export_output_path(account_id, output_path)
            raw = Path(output_path).expanduser()
            candidate = (raw if raw.is_absolute() else exports / raw).resolve()
        else:
            candidate = exports / f"{uuid.uuid4()}.json"
        if candidate.exists():
            raise ValidationError("匯出檔案已存在，請重新開始匯出")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(candidate.parent)
        return str(candidate)

    @staticmethod
    def _validate_transfer(request: Any) -> None:
        """Normalize an upload/forward/backup request before a worker starts."""

        request.proxy = validate_proxy(request.proxy) if request.proxy is not None else None
        if isinstance(request, UploadRequest):
            request.paths = validate_upload_paths(request.paths)
            request.chat = validate_destination(request.chat)
            request.topic_id = validate_topic_id(request.topic_id)
            if request.topic_id is not None and not request.chat:
                raise ValidationError("指定論壇主題時必須同時選擇目的地群組")
            request.include = normalize_extensions(request.include)
            request.exclude = normalize_extensions(request.exclude)
            if request.include and request.exclude:
                raise ValidationError("「只上傳」與「排除」副檔名只能擇一設定")
            # Fail on a bad caption here rather than after the core connects.
            upload_caption_expression(request.caption_mode, request.caption_text)
            for name in ("concurrency", "threads"):
                value = getattr(request, name)
                if value is not None and not 1 <= int(value) <= 16:
                    raise ValidationError("同時數量必須介於 1 到 16")
        elif isinstance(request, ForwardRequest):
            request.sources = validate_forward_sources(request.sources)
            request.to = validate_destination(request.to)
            request.mode = str(request.mode or "direct").strip().lower()
            if request.mode not in FORWARD_MODES:
                raise ValidationError("轉傳方式只能是直接轉傳或複製")
        elif isinstance(request, BackupRequest):
            request.destination = validate_backup_destination(request.destination)
        elif isinstance(request, RecoverRequest):
            request.source = validate_backup_source(request.source)

    def start(
        self,
        request: CoreRequest,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        # Reserve before touching the account store.  Store.remove_account
        # takes the same activity guard first, so deletion cannot interleave
        # with durable queue creation or a login waiting for another job.
        _reserve_account_activity(request.account_id)
        reserved = True
        lock_preacquired = False
        try:
            self.store.get_account(request.account_id)
            if isinstance(request, ListChatsRequest):
                request.proxy = validate_proxy(request.proxy) if request.proxy is not None else None
            elif isinstance(request, ListTopicsRequest):
                request.chat_id = validate_chat_id(request.chat_id)
                request.proxy = validate_proxy(request.proxy) if request.proxy is not None else None
            elif isinstance(request, ExportRequest):
                request.chat_id = validate_chat_id(request.chat_id)
                request.topic_id = validate_topic_id(request.topic_id)
                request.media_types = validate_media_types(request.media_types)
                _start_text, _end_text, start_unix, end_unix = validate_export_window(
                    request.start_date,
                    request.end_date,
                )
                request.start_unix = start_unix
                request.end_unix = end_unix
                request.proxy = validate_proxy(request.proxy) if request.proxy is not None else None
                self._validate_export_output_path(request.account_id, request.output_path)
            elif isinstance(request, _TransferRequest):
                self._validate_transfer(request)
            if isinstance(request, DownloadRequest) and request.job_id:
                stored_job = self.store.get_job(request.job_id)
                if stored_job.account_id != str(request.account_id):
                    raise ValidationError("下載工作不屬於目前選取的帳號")
                # A resume request is a reference to the durable manifest.
                # Ignore mutable caller copies so a retry keeps the original
                # links, files, output directory, and download options.
                request.links = tuple(stored_job.links)
                request.files = tuple(self.store.resolve_job_files(stored_job))
                request.output_dir = stored_job.output_dir
                # Keep explicit caller choices (the GUI prepares a fresh
                # request before starting it).  Older callers that omit the
                # values inherit the durable manifest defaults.
                request.continue_download = (
                    stored_job.continue_download
                    if request.continue_download is None
                    else bool(request.continue_download)
                )
                request.skip_same = (
                    stored_job.skip_same if request.skip_same is None else bool(request.skip_same)
                )
                request.group = stored_job.group
                request.group_by_chat = stored_job.group_by_chat
                request.concurrency = stored_job.concurrency
                request.title = stored_job.title
                request.advanced = stored_job.advanced
            lock = self._account_lock(request.account_id)
            # A single account must never have two Bolt-backed core processes
            # at once.  Downloads and chat operations are queued in the
            # background so the Qt worker can return a handle immediately;
            # login remains fail-fast and explicit.
            if isinstance(request, LoginRequest):
                if not lock.acquire(blocking=False):
                    raise RunnerBusy("這個帳號已有操作正在進行。")
                lock_preacquired = True
            elif isinstance(request, DownloadRequest) and request.job_id is None:
                # Direct callers of start(DownloadRequest(...)) still get a
                # durable queue record before waiting for the account lock.
                job = self.store.create_download_job(
                    request.account_id,
                    request.links,
                    files=request.files,
                    output_dir=request.output_dir,
                    continue_download=request.continue_download,
                    skip_same=request.skip_same,
                    group=request.group,
                    concurrency=request.concurrency,
                    title=request.title,
                    advanced=request.advanced,
                    expected_files=request.expected_files,
                    expected_bytes=request.expected_bytes,
                    group_by_chat=request.group_by_chat,
                )
                request.job_id = job.id
                request.advanced = job.advanced
                request.group_by_chat = job.group_by_chat
            handle = RunHandle(request.account_id, callback=on_event)
            with self._handles_guard:
                self._handles.add(handle)
            thread = threading.Thread(
                target=self._worker,
                args=(request, handle, lock, lock_preacquired),
                name=f"tdl-core-{request.account_id[:8]}",
                daemon=True,
            )
            handle._set_thread(thread)
            try:
                thread.start()
            except BaseException:
                with self._handles_guard:
                    self._handles.discard(handle)
                raise
            reserved = False  # the worker owns release for its full lifetime
            return handle
        finally:
            if reserved:
                if lock_preacquired:
                    lock.release()
                _release_account_activity(request.account_id)

    def start_login(
        self,
        account_id: str,
        *,
        mode: str = "code",
        desktop_path: str | None = None,
        passcode: str | None = None,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        mode = str(mode).lower().strip()
        if mode not in {"code", "qr", "desktop"}:
            raise ValidationError("登入方式只能是驗證碼、QR Code 或桌面工作階段")
        if mode == "desktop" and passcode:
            # The Go desktop importer currently accepts this value only as a
            # CLI flag.  A password in process arguments is visible to other
            # local diagnostics, so do not silently expose it; the shipped UI
            # uses QR/code login and does not offer this unsupported path.
            raise ValidationError("桌面工作階段匯入目前不接受密碼，請先在 Telegram Desktop 解鎖。")
        if mode == "desktop" and desktop_path:
            desktop = Path(desktop_path).expanduser().resolve()
            if not desktop.exists():
                raise ValidationError("找不到 Telegram Desktop 資料夾")
            desktop_path = str(desktop)
        return self.start(
            LoginRequest(
                account_id=account_id,
                mode=mode,
                desktop_path=desktop_path,
                passcode=passcode,
                proxy=validate_proxy(proxy),
            ),
            on_event=on_event,
        )

    def start_list_chats(
        self,
        account_id: str,
        *,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """List dialogs for one account without creating a download record."""

        return self.start(
            ListChatsRequest(
                account_id=str(account_id),
                proxy=validate_proxy(proxy) if proxy is not None else None,
            ),
            on_event=on_event,
        )

    def start_list_topics(
        self,
        account_id: str,
        chat_id: str | int,
        *,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Load forum topics for one typed peer without creating history.

        The desktop-aware ``chat ls --topics`` command returns the same
        metadata used by the main chat list.  Filtering it here keeps the
        channel page contract small while avoiding a second Telegram API
        implementation in Python.
        """

        return self.start(
            ListTopicsRequest(
                account_id=str(account_id),
                chat_id=validate_chat_id(chat_id),
                proxy=validate_proxy(proxy) if proxy is not None else None,
            ),
            on_event=on_event,
        )

    def start_export(
        self,
        account_id: str,
        chat_id: str | int,
        start_date: str | date | datetime,
        end_date: str | date | datetime,
        media_types: Sequence[str],
        output_path: str | os.PathLike[str] | None = None,
        *,
        topic_id: str | int | None = None,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Export a Taiwan-local inclusive date range into account storage."""

        normalized_start, normalized_end, start_unix, end_unix = validate_export_window(
            start_date,
            end_date,
        )
        clean_chat_id = validate_chat_id(chat_id)
        clean_topic_id = validate_topic_id(topic_id)
        clean_media_types = validate_media_types(media_types)
        clean_output = None if output_path in (None, "") else str(Path(output_path).expanduser())
        request = ExportRequest(
            account_id=str(account_id),
            chat_id=clean_chat_id,
            start_date=normalized_start,
            end_date=normalized_end,
            media_types=clean_media_types,
            topic_id=clean_topic_id,
            output_path=clean_output,
            proxy=validate_proxy(proxy) if proxy is not None else None,
            start_unix=start_unix,
            end_unix=end_unix,
        )
        return self.start(request, on_event=on_event)

    def start_download(
        self,
        account_id: str,
        links: Sequence[str],
        *,
        files: Sequence[str | os.PathLike[str]] | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        continue_download: bool | None = None,
        skip_same: bool | None = None,
        group: bool | None = None,
        concurrency: int | None = None,
        proxy: str | None = None,
        title: str | None = None,
        group_by_chat: bool | None = _DEFAULT_GROUP_BY_CHAT,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        clean_proxy = validate_proxy(proxy) if proxy is not None else None
        clean_links = validate_message_links(links) if links else []
        clean_files = validate_export_files(files or [])
        if not clean_links and not clean_files:
            raise ValidationError("至少需要一個 Telegram 訊息連結或 JSON 匯入檔")
        request = DownloadRequest(
            account_id=account_id,
            links=clean_links,
            files=clean_files,
            output_dir=None if output_dir is None else str(Path(output_dir).expanduser().resolve()),
            continue_download=continue_download,
            skip_same=skip_same,
            group=group,
            concurrency=concurrency,
            proxy=clean_proxy,
            title=_normalize_job_title(title),
            group_by_chat=group_by_chat,
        )
        # ``start`` reserves the account and persists this queue item before
        # starting the worker.  That ordering keeps a queued job visible on a
        # restart and closes the account-removal race.
        return self.start(request, on_event=on_event)

    def resume_download(
        self,
        job: DownloadJob,
        *,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Resume one persisted queued/interrupted/paused download in place."""

        stored = self.store.get_job(job.id)
        if stored.status not in {"queued", "running", "interrupted", "paused"}:
            raise ValidationError("這個下載工作目前不能繼續")
        return self.start(
            DownloadRequest(
                account_id=stored.account_id,
                links=tuple(stored.links),
                files=tuple(stored.files),
                output_dir=stored.output_dir,
                continue_download=True,
                skip_same=True,
                group=stored.group,
                concurrency=stored.concurrency,
                proxy=validate_proxy(proxy) if proxy is not None else None,
                job_id=stored.id,
                title=stored.title,
                group_by_chat=stored.group_by_chat,
            ),
            on_event=on_event,
        )

    def start_upload(
        self,
        account_id: str,
        paths: Sequence[str | os.PathLike[str]],
        *,
        chat: str = "",
        topic_id: str | int | None = None,
        as_photo: bool = False,
        remove_after: bool = False,
        caption_mode: str = "none",
        caption_text: str = "",
        include: Sequence[str] | str = (),
        exclude: Sequence[str] | str = (),
        concurrency: int | None = None,
        threads: int | None = None,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Upload local files to a chat; an empty chat is Saved Messages."""

        return self.start(
            UploadRequest(
                account_id=str(account_id),
                paths=list(paths),
                chat=chat,
                topic_id=validate_topic_id(topic_id),
                as_photo=bool(as_photo),
                remove_after=bool(remove_after),
                caption_mode=caption_mode,
                caption_text=caption_text,
                include=include,
                exclude=exclude,
                concurrency=concurrency,
                threads=threads,
                proxy=proxy,
            ),
            on_event=on_event,
        )

    def start_forward(
        self,
        account_id: str,
        sources: Sequence[str | os.PathLike[str]],
        *,
        to: str = "",
        mode: str = "direct",
        silent: bool = False,
        dry_run: bool = False,
        single: bool = False,
        desc: bool = False,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Forward messages named by links or export JSON files."""

        return self.start(
            ForwardRequest(
                account_id=str(account_id),
                sources=list(sources),
                to=to,
                mode=mode,
                silent=bool(silent),
                dry_run=bool(dry_run),
                single=bool(single),
                desc=bool(desc),
                proxy=proxy,
            ),
            on_event=on_event,
        )

    def start_backup(
        self,
        account_id: str,
        destination: str | os.PathLike[str],
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Write the account's session to a portable ``.tdl`` backup file.

        The backup is the core's own format and is not DPAPI-protected, which
        is what lets it move to another computer; callers must warn the user.
        """

        return self.start(
            BackupRequest(account_id=str(account_id), destination=str(destination)),
            on_event=on_event,
        )

    def start_recover(
        self,
        account_id: str,
        source: str | os.PathLike[str],
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Load a ``.tdl`` backup into an account; follow with ``start_whoami``."""

        return self.start(
            RecoverRequest(account_id=str(account_id), source=str(source)),
            on_event=on_event,
        )

    def start_whoami(
        self,
        account_id: str,
        *,
        proxy: str | None = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> RunHandle:
        """Confirm the stored session with Telegram and mark it logged in."""

        return self.start(
            WhoamiRequest(account_id=str(account_id), proxy=proxy),
            on_event=on_event,
        )

    def run(
        self,
        request: CoreRequest,
    ) -> RunResult:
        handle = self.start(request)
        result = handle.wait()
        if result is None:
            raise RunnerError("核心程式工作未完成")
        return result

    def cancel_all(self) -> None:
        with self._handles_guard:
            handles = list(self._handles)
        for handle in handles:
            handle.cancel()

    def pause_all(self) -> None:
        """Pause active operations for a graceful GUI shutdown."""

        with self._handles_guard:
            handles = list(self._handles)
        for handle in handles:
            handle.pause()

    def _build_args(
        self,
        request: CoreRequest,
        runtime: Path,
    ) -> list[str]:
        executable = self.resolve_executable()
        # Account UUIDs are generated by Store; the path contains no user
        # supplied command fragments.  Passing each option as a separate list
        # element is intentional: shell=True is never used.
        args = [
            str(executable),
            "--storage",
            f"type=bolt,path={runtime / 'data'}",
            "--ns",
            "default",
        ]
        proxy = request.proxy
        if proxy is None:
            proxy = self.store.get_preferences().proxy
        proxy = validate_proxy(proxy)
        if proxy:
            args.extend(["--proxy", proxy])
        if isinstance(request, LoginRequest):
            args.extend(["login", "--type", request.mode])
            if request.mode == "desktop" and request.desktop_path:
                args.extend(["--desktop", request.desktop_path])
            return args

        if isinstance(request, ListChatsRequest):
            # The GUI receives the lightweight forum marker here and asks for
            # topics only after the user selects a forum chat.
            args.extend(["chat", "ls", "--output", "json"])
            return args

        if isinstance(request, ListTopicsRequest):
            args.extend(
                [
                    "chat",
                    "topics",
                    "--chat",
                    validate_chat_id(request.chat_id),
                    "--output",
                    "json",
                ]
            )
            return args

        if isinstance(request, ExportRequest):
            if request.start_unix is None or request.end_unix is None:
                _start_text, _end_text, start_unix, end_unix = validate_export_window(
                    request.start_date,
                    request.end_date,
                )
                request.start_unix = start_unix
                request.end_unix = end_unix
            if not request.output_path:
                request.output_path = self._allocate_export_output_path(request.account_id, None)
            else:
                self._validate_export_output_path(request.account_id, request.output_path)
            args.extend(
                [
                    "chat",
                    "export",
                    "--chat",
                    validate_chat_id(request.chat_id),
                    "--type",
                    "time",
                    "--input",
                    f"{int(request.start_unix)},{int(request.end_unix)}",
                    "--media-types",
                    ",".join(validate_media_types(request.media_types)),
                    "--output",
                    str(Path(request.output_path).resolve()),
                ]
            )
            if request.topic_id is not None:
                args.extend(["--topic", str(validate_topic_id(request.topic_id))])
            return args

        if isinstance(request, UploadRequest):
            args.append("up")
            for path in validate_upload_paths(request.paths):
                args.extend(["--path", _csv_arg(path)])
            # Without --chat the core uploads to Saved Messages.
            destination = validate_destination(request.chat)
            if destination:
                args.extend(["--chat", destination])
            if request.topic_id is not None:
                args.extend(["--topic", str(validate_topic_id(request.topic_id))])
            args.extend(["--caption", upload_caption_expression(request.caption_mode, request.caption_text)])
            include = normalize_extensions(request.include)
            exclude = normalize_extensions(request.exclude)
            if include:
                args.extend(["--include", ",".join(include)])
            elif exclude:
                args.extend(["--exclude", ",".join(exclude)])
            if request.as_photo:
                args.append("--photo")
            if request.remove_after:
                args.append("--rm")
            if request.concurrency is not None:
                args.extend(["--limit", str(int(request.concurrency))])
            if request.threads is not None:
                args.extend(["--threads", str(int(request.threads))])
            return args

        if isinstance(request, ForwardRequest):
            args.append("forward")
            for source in validate_forward_sources(request.sources):
                args.extend(["--from", source])
            destination = validate_destination(request.to)
            if destination:
                args.extend(["--to", destination])
            args.extend(["--mode", request.mode])
            for enabled, flag in (
                (request.silent, "--silent"),
                (request.dry_run, "--dry-run"),
                (request.single, "--single"),
                (request.desc, "--desc"),
            ):
                if enabled:
                    args.append(flag)
            return args

        if isinstance(request, BackupRequest):
            args.extend(["backup", "--dst", validate_backup_destination(request.destination)])
            return args

        if isinstance(request, RecoverRequest):
            args.extend(["recover", "--file", validate_backup_source(request.source)])
            return args

        if isinstance(request, WhoamiRequest):
            args.append("whoami")
            return args

        prefs = self.store.get_preferences()
        output = Path(request.output_dir or prefs.download_dir).expanduser().resolve()
        args.extend(["dl"])
        for link in validate_message_links(request.links) if request.links else []:
            args.extend(["--url", link])
        for exported_file in validate_export_files(request.files or []):
            args.extend(["--file", _csv_arg(exported_file)])
        args.extend(["--dir", str(output)])
        if request.continue_download if request.continue_download is not None else prefs.continue_download:
            args.append("--continue")
        if request.skip_same if request.skip_same is not None else prefs.skip_same:
            args.append("--skip-same")
        if request.group if request.group is not None else prefs.group:
            args.append("--group")
        # This is independent from ``--group`` (album detection).  New
        # requests default to a chat folder; an old durable job is assigned
        # ``False`` when it is read and therefore omits this flag.
        if (
            request.group_by_chat
            if request.group_by_chat is not None
            else _DEFAULT_GROUP_BY_CHAT
        ):
            args.append("--group-by-chat")
        concurrency = request.concurrency if request.concurrency is not None else prefs.concurrency
        if int(concurrency) < 1 or int(concurrency) > 64:
            raise ValidationError("同時下載數量必須介於 1 到 64")
        args.extend(["--limit", str(int(concurrency))])
        advanced = (
            prefs.download_advanced()
            if request.advanced is None
            else normalize_download_advanced(request.advanced)
        )
        if advanced["template"]:
            args.extend(["--template", advanced["template"]])
        if advanced["include"]:
            args.extend(["--include", ",".join(advanced["include"])])
        elif advanced["exclude"]:
            args.extend(["--exclude", ",".join(advanced["exclude"])])
        for key, flag in (("desc", "--desc"), ("takeout", "--takeout"), ("rewrite_ext", "--rewrite-ext")):
            if advanced[key]:
                args.append(flag)
        if advanced["threads"] != 4:  # the core default
            args.extend(["--threads", str(advanced["threads"])])
        if advanced["delay_ms"]:
            args.extend(["--delay", f"{advanced['delay_ms']}ms"])
        return args

    @staticmethod
    def _emit(handle: RunHandle, event: dict[str, Any]) -> None:
        handle.events.put(event)
        if handle._callback is not None:
            try:
                handle._callback(event)
            except Exception:
                # A UI callback must not kill the core worker or skip sealing.
                pass

    def _worker(
        self,
        request: CoreRequest,
        handle: RunHandle,
        lock: threading.Lock,
        lock_preacquired: bool,
    ) -> None:
        job: Optional[DownloadJob] = None
        process: Optional[subprocess.Popen[str]] = None
        stderr_lines: list[str] = []
        last_error: Optional[str] = None
        returncode: Optional[int] = None
        authenticated_event: Optional[dict[str, Any]] = None
        operation_data: Optional[dict[str, Any]] = None
        auth_invalid_seen = False
        seal_failed = False
        lock_held = lock_preacquired
        runtime_prepared = False
        export_output_created = False
        # Per-file rows live here between durable writes; see
        # ``_PROGRESS_PERSIST_INTERVAL``.
        file_progress: dict[str, dict[str, Any]] = {}
        pending_progress: dict[str, Any] = {}
        last_progress_write = 0.0
        transfer_counts = {"total": 0, "completed": 0, "failed": 0}

        def persist_progress(*, force: bool = False) -> None:
            nonlocal last_progress_write
            if job is None or not pending_progress:
                return
            now = time.monotonic()
            if not force and now - last_progress_write < _PROGRESS_PERSIST_INTERVAL:
                return
            updates = dict(pending_progress)
            pending_progress.clear()
            last_progress_write = now
            self.store.update_job_progress(job.id, **updates)

        try:
            if not lock_held:
                waiting_status_sent = False
                while not lock.acquire(blocking=False):
                    if (
                        isinstance(request, (ListChatsRequest, ListTopicsRequest, ExportRequest, *_TransferRequest))
                        and not waiting_status_sent
                    ):
                        self._emit(
                            handle,
                            {"event": "status", "message": "前面仍有帳號操作，查詢已排隊。"},
                        )
                        waiting_status_sent = True
                    if handle.stop_requested.wait(0.1):
                        # ``start`` persists a new download before it waits
                        # for the per-account core lock.  If cancellation
                        # happens in this queue-only window, no ``job`` has
                        # been loaded into the worker yet, so the normal
                        # finally block cannot update its durable status.
                        # Mark it here and keep the id in the result so the UI
                        # retry reuses this record instead of creating a
                        # duplicate download.
                        cancelled = handle.cancel_requested.is_set()
                        paused = handle.pause_requested.is_set() and not cancelled
                        stop_status = "cancelled" if cancelled else "paused"
                        stop_error = "操作已取消。" if cancelled else "操作已暫停，可重新啟動後繼續下載。"
                        if isinstance(request, DownloadRequest) and request.job_id:
                            try:
                                job = self.store.get_job(request.job_id)
                                self.store.finish_job(job.id, stop_status, error=stop_error)
                            except StoreError:
                                # The final cleanup still reports cancellation;
                                # this guard only protects a corrupted store
                                # from masking the user's cancellation.
                                pass
                        raise RunnerError(stop_error)
                lock_held = True
                if waiting_status_sent:
                    self._emit(handle, {"event": "status", "message": "已輪到目前帳號，正在準備查詢。"})
            if isinstance(request, DownloadRequest):
                if request.job_id:
                    job = self.store.get_job(request.job_id)
                else:
                    job = self.store.create_download_job(
                        request.account_id,
                        request.links,
                        files=request.files,
                        output_dir=request.output_dir,
                        continue_download=request.continue_download,
                        skip_same=request.skip_same,
                        group=request.group,
                        concurrency=request.concurrency,
                        title=request.title,
                        advanced=request.advanced,
                        expected_files=request.expected_files,
                        expected_bytes=request.expected_bytes,
                        group_by_chat=request.group_by_chat,
                    )
                    request.group_by_chat = job.group_by_chat
                self.store.mark_job_running(job.id)
                file_progress = {key: dict(row) for key, row in job.file_progress.items()}

            runtime = self.vault.prepare(request.account_id)
            runtime_prepared = True
            if isinstance(request, ExportRequest):
                request.output_path = self._allocate_export_output_path(
                    request.account_id,
                    request.output_path,
                )
                export_output_created = True
            args = self._build_args(request, runtime)
            if handle.stop_requested.is_set():
                stop_error = (
                    "操作已取消。"
                    if handle.cancel_requested.is_set()
                    else "操作已暫停，可重新啟動後繼續下載。"
                )
                raise RunnerError(stop_error)
            env = os.environ.copy()
            env[GUI_ENV] = "1"
            env[DATA_ENV] = str(runtime)
            # The core reads every TDL_<FLAG> variable as a default.  Drop
            # inherited ones so a developer shell cannot select an unrelated
            # storage file or change options the GUI did not pass explicitly.
            # TDL_DESKTOP_* belongs to this front end (data root, fixtures).
            for name in [key for key in env if key.upper().startswith("TDL_")]:
                if name.upper() not in {GUI_ENV, DATA_ENV} and not name.upper().startswith("TDL_DESKTOP_"):
                    del env[name]
            process = subprocess.Popen(
                args,
                cwd=str(self.project_root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                bufsize=1,
            )
            handle._set_process(process)
            # A pause/cancel can arrive in the small window before Popen has
            # installed the process handle.  Send the graceful command now so
            # the child still has a chance to save its checkpoint.
            if handle.stop_requested.is_set():
                handle._request_cancel()

            def drain_stderr() -> None:
                if process is None or process.stderr is None:
                    return
                try:
                    for line in process.stderr:
                        if len(stderr_lines) < 200:
                            stderr_lines.append(sanitize_text(line.rstrip()))
                except (OSError, ValueError):
                    pass

            stderr_thread = threading.Thread(target=drain_stderr, name="tdl-core-stderr", daemon=True)
            stderr_thread.start()
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\r\n")
                    if not line.startswith(EVENT_PREFIX):
                        if line.strip():
                            self._emit(handle, {"event": "log", "message": sanitize_text(line)})
                        continue
                    try:
                        decoded = json.loads(line[len(EVENT_PREFIX) :])
                    except json.JSONDecodeError:
                        self._emit(handle, {"event": "log", "level": "warning", "message": "核心事件格式無效。"})
                        continue
                    if not isinstance(decoded, Mapping):
                        self._emit(handle, {"event": "log", "level": "warning", "message": "核心事件格式無效。"})
                        continue
                    event = normalize_event(decoded)
                    self._emit(handle, event)
                    if event.get("event") == "error":
                        auth_invalid_seen = auth_invalid_seen or _is_auth_invalid_error(decoded, event)
                        last_error = str(event.get("message") or "核心程式執行失敗。")
                        if event.get("file_id"):
                            transfer_counts["failed"] += 1
                        if job is not None and event.get("file_id"):
                            file_id = sanitize_text(event.get("file_id"))[:200]
                            row = file_progress.setdefault(file_id, {"file_id": file_id})
                            row["state"] = sanitize_text(event.get("state") or "failed")
                            pending_progress["file_progress"] = file_progress
                            persist_progress(force=True)
                    elif event.get("event") == "authenticated":
                        # Do not mark an account logged in until the child has
                        # exited successfully and the session vault has sealed
                        # the resulting core data.
                        authenticated_event = event
                    elif event.get("event") == "topics" and isinstance(request, ListTopicsRequest):
                        operation_data = {
                            "topics": event.get("topics", []),
                            "chat_ref": sanitize_text(event.get("chat_ref", request.chat_id)),
                        }
                        if event.get("partial") or int(event.get("skipped", 0) or 0) > 0:
                            operation_data["partial"] = bool(event.get("partial", False))
                            operation_data["skipped"] = max(0, int(event.get("skipped", 0) or 0))
                            operation_data["message"] = sanitize_text(event.get("message", ""))
                    elif event.get("event") == "chats" and isinstance(request, (ListChatsRequest, ListTopicsRequest)):
                        if isinstance(request, ListChatsRequest):
                            operation_data = {
                                "chats": event.get("chats", []),
                                "skipped": event.get("skipped", 0),
                                "partial": bool(event.get("partial", False)),
                                "message": sanitize_text(event.get("message", "")),
                            }
                        else:
                            target = validate_chat_id(request.chat_id)
                            selected: Optional[Mapping[str, Any]] = None
                            raw_chats = event.get("chats", [])
                            if isinstance(raw_chats, Sequence) and not isinstance(raw_chats, (str, bytes, bytearray)):
                                for candidate in raw_chats:
                                    if not isinstance(candidate, Mapping):
                                        continue
                                    try:
                                        candidate_id = validate_chat_id(candidate.get("id", ""))
                                    except ValidationError:
                                        continue
                                    candidate_ref = str(candidate.get("ref", "") or "").strip()
                                    if target in {candidate_id, candidate_ref}:
                                        selected = candidate
                                        break
                            if selected is None:
                                last_error = "找不到所選的論壇群組。"
                                self._emit(handle, {"event": "error", "message": last_error})
                            else:
                                raw_topics = selected.get("topics", [])
                                topics = raw_topics if isinstance(raw_topics, list) else []
                                operation_data = {"topics": topics}
                                self._emit(handle, {"event": "topics", "topics": topics})
                    elif event.get("event") == "exported" and isinstance(request, ExportRequest):
                        expected_path = Path(request.output_path or "").resolve()
                        reported_path = str(event.get("path") or request.output_path or "")
                        if reported_path:
                            try:
                                reported = Path(reported_path).expanduser().resolve()
                            except OSError:
                                reported = Path("")
                            if reported != expected_path:
                                last_error = "匯出核心回報了不符合預期的檔案路徑。"
                                self._emit(handle, {"event": "error", "message": last_error})
                                continue
                        try:
                            count = max(0, int(event.get("count", 0) or 0))
                        except (TypeError, ValueError):
                            count = 0
                        operation_data = {"path": str(expected_path), "count": count}
                        if "total_bytes" in event:
                            try:
                                operation_data["total_bytes"] = max(
                                    0,
                                    int(event.get("total_bytes", 0) or 0),
                                )
                            except (TypeError, ValueError, OverflowError):
                                last_error = "匯出核心回報的檔案大小無效。"
                                self._emit(handle, {"event": "error", "message": last_error})
                    elif event.get("event") == "progress" and job is not None:
                        pending_progress.update(
                            percent=event.get("percent"),
                            current=event.get("current"),
                            total=event.get("total"),
                            speed=event.get("speed"),
                            filename=event.get("filename"),
                        )
                        if event.get("file_id"):
                            file_id = sanitize_text(event.get("file_id"))[:200]
                            row = file_progress.setdefault(file_id, {"file_id": file_id})
                            for key in ("state", "current", "total", "percent", "filename"):
                                if event.get(key) is not None:
                                    row[key] = event.get(key)
                            pending_progress["file_progress"] = file_progress
                        # Completions are not forced to disk either: a channel
                        # of small photos finishes many files per second.  The
                        # core's own checkpoint and --skip-same decide what a
                        # resumed job downloads, not these display rows.
                        persist_progress()
                    elif event.get("event") == "progress" and isinstance(request, (UploadRequest, ForwardRequest)):
                        if event.get("state") == "completed":
                            transfer_counts["completed"] += 1
                    elif event.get("event") == "status" and isinstance(request, (UploadRequest, ForwardRequest)):
                        transfer_counts["total"] = int(event.get("count", transfer_counts["total"]) or 0)
            returncode = process.wait()
            stderr_thread.join(timeout=2)
            if handle.cancel_requested.is_set():
                last_error = last_error or "操作已取消。"
            elif handle.pause_requested.is_set():
                last_error = last_error or "操作已暫停，可重新啟動後繼續下載。"
            elif returncode != 0 and not last_error:
                stderr_error = "\n".join(stderr_lines[-10:])
                auth_invalid_seen = auth_invalid_seen or _is_auth_invalid_error(stderr_error)
                last_error = translate_error(stderr_error)
            elif returncode == 0 and not last_error:
                if isinstance(request, (ListChatsRequest, ListTopicsRequest)) and operation_data is None:
                    last_error = (
                        "聊天清單核心未回報查詢結果。"
                        if isinstance(request, ListChatsRequest)
                        else "論壇主題清單核心未回報查詢結果。"
                    )
                    self._emit(handle, {"event": "error", "message": last_error})
                elif isinstance(request, ExportRequest):
                    if operation_data is None:
                        last_error = "匯出核心未回報完成資訊。"
                        self._emit(handle, {"event": "error", "message": last_error})
                    else:
                        export_path = Path(str(operation_data.get("path", "")))
                        try:
                            actual_count, actual_total_bytes = _read_export_message_stats(export_path)
                            if actual_count != int(operation_data.get("count", 0) or 0):
                                raise ValidationError("匯出筆數與 JSON 檔案內容不一致")
                            if (
                                "total_bytes" in operation_data
                                and actual_total_bytes != int(operation_data.get("total_bytes", 0) or 0)
                            ):
                                raise ValidationError("匯出檔案大小與 JSON 檔案內容不一致")
                            operation_data["count"] = actual_count
                        except ValidationError:
                            last_error = "匯出檔案格式無效，無法交給下載核心。"
                            self._emit(handle, {"event": "error", "message": last_error})
        except (BackendError, OSError, ValueError) as exc:
            last_error = translate_error(exc)
            if not handle.pause_requested.is_set() or handle.cancel_requested.is_set():
                self._emit(handle, {"event": "error", "message": last_error})
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    returncode = process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            if last_error:
                auth_invalid_seen = auth_invalid_seen or _is_auth_invalid_error(last_error)
            if lock_held and runtime_prepared:
                try:
                    self.vault.seal(request.account_id)
                except (BackendError, OSError, zipfile.BadZipFile) as exc:
                    seal_failed = True
                    seal_error = translate_error(exc)
                    last_error = last_error or f"{seal_error} 工作階段未能安全保存。"
                    self._emit(handle, {"event": "error", "message": last_error})

            cancelled = handle.cancel_requested.is_set()
            paused = handle.pause_requested.is_set() and not cancelled
            stopped = cancelled or paused
            if (
                isinstance(request, ExportRequest)
                and export_output_created
                and (stopped or last_error or returncode not in (0, None))
                and request.output_path
            ):
                try:
                    Path(request.output_path).unlink(missing_ok=True)
                except OSError:
                    pass
            if auth_invalid_seen:
                try:
                    # Keep the friendly account label and old Telegram name
                    # for the account list, but require a fresh login before
                    # the account can be used again.
                    self.store.update_account(request.account_id, authenticated=False)
                except StoreError:
                    # Account removal is blocked while this worker is active;
                    # this is only a defensive guard for a corrupted store.
                    pass
            authenticated_ok = authenticated_event is not None and bool(authenticated_event.get("id"))
            identifies_account = isinstance(request, (LoginRequest, WhoamiRequest))
            if (
                identifies_account
                and not stopped
                and returncode == 0
                and not last_error
                and not seal_failed
                and not authenticated_ok
            ):
                last_error = "登入核心未回報完整的已驗證帳號資訊。"
                self._emit(handle, {"event": "error", "message": last_error})
            if (
                identifies_account
                and authenticated_ok
                and not stopped
                and returncode == 0
                and not last_error
                and not seal_failed
            ):
                try:
                    self.store.update_account(
                        request.account_id,
                        user_id=authenticated_event.get("id"),
                        username=authenticated_event.get("username"),
                        display_name=authenticated_event.get("name"),
                        authenticated=True,
                    )
                except StoreError as exc:
                    last_error = translate_error(exc)
                    self._emit(handle, {"event": "error", "message": last_error})
            if isinstance(request, DownloadRequest) and job is not None:
                if cancelled:
                    status = "cancelled"
                elif paused:
                    status = "paused"
                elif last_error or returncode not in (0, None):
                    status = "failed"
                else:
                    status = "completed"
                try:
                    persist_progress(force=True)
                    self.store.finish_job(job.id, status, error=last_error)
                except StoreError:
                    pass
            success = not stopped and not last_error and returncode == 0
            if isinstance(request, (UploadRequest, ForwardRequest)):
                operation_data = dict(transfer_counts)
            elif isinstance(request, ListChatsRequest) and success and operation_data is not None:
                try:
                    self.store.save_chat_cache(request.account_id, operation_data.get("chats", []))
                except (BackendError, OSError, TypeError, ValueError):
                    # The cache only spares the user a refresh on the next
                    # launch; the list just returned is still valid.
                    pass
            result = RunResult(
                success=success,
                returncode=returncode,
                cancelled=cancelled,
                error=last_error,
                job_id=job.id if job is not None else None,
                data=operation_data,
                paused=paused,
            )
            handle._result = result
            with self._handles_guard:
                self._handles.discard(handle)
            if lock_held:
                lock.release()
            _release_account_activity(request.account_id)
            # A caller may remove an account immediately after ``wait``
            # returns (restore uses that path when Telegram rejects a backup).
            # Publish completion only after every guard held by this worker
            # has been released, otherwise the caller can observe a finished
            # handle while ``Store.remove_account`` still sees it as active.
            handle.done.set()


__all__ = [
    "Account",
    "BackendError",
    "BackupRequest",
    "CoreNotFound",
    "DEFAULT_TEMPLATE",
    "DataProtectionError",
    "DownloadJob",
    "DownloadRequest",
    "EVENT_PREFIX",
    "EXPORT_MEDIA_TYPES",
    "ExportRequest",
    "ForwardRequest",
    "LoginRequest",
    "ListChatsRequest",
    "ListTopicsRequest",
    "Preferences",
    "RecoverRequest",
    "RunHandle",
    "RunResult",
    "Runner",
    "RunnerBusy",
    "RunnerError",
    "SessionVault",
    "Store",
    "StoreError",
    "UploadRequest",
    "ValidationError",
    "WhoamiRequest",
    "normalize_download_advanced",
    "normalize_event",
    "normalize_extensions",
    "sanitize_text",
    "translate_error",
    "upload_caption_expression",
    "validate_backup_destination",
    "validate_backup_source",
    "validate_chat_id",
    "validate_destination",
    "validate_export_window",
    "validate_forward_sources",
    "validate_message_link",
    "validate_message_links",
    "validate_export_file",
    "validate_export_files",
    "validate_media_types",
    "validate_template",
    "validate_topic_id",
    "validate_upload_paths",
    "validate_proxy",
]
