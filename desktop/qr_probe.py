"""Opt-in real-network probe for the GUI login transport, without account login.

The probe starts the actual bundled ``tdl-core`` through ``BackendBridge`` and
waits for the first QR image to reach :class:`LoginDialog`.  It cancels as
soon as Qt has rendered that image; it never enters a phone/code/password
prompt and never prints or persists the QR bytes.

Source-tree usage::

    .build\\venv\\Scripts\\python.exe -m desktop.qr_probe

The command writes ``.build/qr-probe-report.json`` unless ``--report`` is
provided.  The report intentionally contains only ``ok``, ``elapsed`` and a
redacted ``error`` string.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch


_SECRET_RE = re.compile(
    r"(?i)(password|passcode|phone|code|token|session|api[_ -]?hash)\s*([:=])\s*[^\s,;]+"
)


def _safe_error(value: Any) -> str:
    """Keep the report useful without allowing auth material into it."""

    text = str(value or "").strip()
    text = re.sub(r"(?i)(https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@", r"\1://***:***@", text)
    text = _SECRET_RE.sub(r"\1\2***", text)
    return text[:4000]


def _thread_running(thread: Any) -> bool:
    if thread is None:
        return False
    try:
        return bool(thread.isRunning())
    except (AttributeError, RuntimeError):
        return False


def _write_report(report_path: Path, *, ok: bool, elapsed: float, error: str = "") -> int:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {"ok": bool(ok), "elapsed": round(float(elapsed), 3), "error": _safe_error(error)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if ok else 1


def run_probe(report_path: str | os.PathLike[str] | None) -> int:
    """Run the real QR transport probe and write a three-field JSON report.

    The caller may invoke this from the frozen application's argument parser
    or from ``python -m desktop.qr_probe``.  No account is pre-created here:
    the normal ``LoginDialog`` -> ``BackendBridge.login`` path creates a
    temporary account, and its cancellation cleanup removes it.
    """

    started = time.monotonic()
    report = Path(report_path or (Path(tempfile.gettempdir()) / "TDL-qr-probe-report.json")).expanduser()
    report.parent.mkdir(parents=True, exist_ok=True)
    # The GUI is intentionally headless for this probe; a real application can
    # still call this function after QApplication has already been created.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication

        from desktop import app as app_module
        from desktop.app import BackendBridge, LoginDialog
    except Exception as exc:  # pragma: no cover - packaging diagnostic path
        return _write_report(report, ok=False, elapsed=time.monotonic() - started, error=exc)

    application = QApplication.instance() or QApplication([])
    event_loop = QEventLoop()
    errors: list[str] = []
    dialog: Any = None
    finished = False
    result = {"ok": False, "error": ""}
    deadline = started + 45.0

    def quiet_error(_parent: Any, title: Any, message: Any, detail: Any = "") -> None:
        # LoginDialog normally opens a modal QMessageBox on a worker failure.
        # A probe must finish unattended; capture only a redacted summary.
        errors.append(_safe_error(message or title or detail))

    def stop_and_finish(ok: bool, error: str = "") -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        result["ok"] = bool(ok)
        result["error"] = _safe_error(error)
        if dialog is not None:
            try:
                if _thread_running(dialog.thread):
                    # This calls the same safe cancellation path as the user
                    # pressing「取消」and waits for the real core process.
                    dialog._cancel_login()
                elif dialog.isVisible():
                    dialog.close()
            except Exception as exc:  # pragma: no cover - Qt shutdown guard
                result["ok"] = False
                result["error"] = _safe_error(exc)
        if event_loop.isRunning():
            event_loop.quit()

    def poll() -> None:
        if finished:
            return
        try:
            pixmap = dialog.qr_image.pixmap() if dialog is not None else None
            if pixmap is not None and not pixmap.isNull():
                stop_and_finish(True)
                return
            if errors:
                stop_and_finish(False, errors[-1])
                return
            if time.monotonic() >= deadline:
                stop_and_finish(False, "在 45 秒內沒有收到 QR Code。請檢查網路後重試。")
                return
            if dialog is not None and dialog.thread is not None and not _thread_running(dialog.thread):
                stop_and_finish(False, "登入核心在收到 QR Code 前結束。")
                return
        except Exception as exc:  # pragma: no cover - Qt shutdown guard
            stop_and_finish(False, exc)
            return
        QTimer.singleShot(100, poll)

    # Suppress only the modal error box while this unattended probe runs.  The
    # production dialog and all normal user-visible error handling are intact.
    with patch.object(app_module, "show_error", side_effect=quiet_error):
        try:
            with tempfile.TemporaryDirectory(prefix="tdl-qr-probe-", dir=report.parent) as data_dir:
                with patch.dict(
                    os.environ,
                    {
                        "TDL_DESKTOP_DATA_DIR": data_dir,
                        "TDL_GUI": "",
                        "TDL_STORAGE": "",
                        "TDL_NS": "",
                    },
                ):
                    bridge = BackendBridge()
                    if not bridge.available:
                        return _write_report(
                            report,
                            ok=False,
                            elapsed=time.monotonic() - started,
                            error=bridge.backend_error,
                        )
                    try:
                        bridge.runner.resolve_executable()
                    except Exception as exc:
                        return _write_report(report, ok=False, elapsed=time.monotonic() - started, error=exc)

                    dialog = LoginDialog(bridge)
                    dialog.show()
                    # Use the actual push-button path rather than invoking the
                    # private slot directly; this catches disabled/hidden UI
                    # regressions in the packaged dialog.
                    QTimer.singleShot(0, dialog.start_button.click)
                    QTimer.singleShot(50, poll)
                    event_loop.exec()
                    # Ensure the nested loop has processed the final close,
                    # while never destroying a live QThread.
                    for _ in range(80):
                        application.processEvents()
                        if not _thread_running(dialog.thread):
                            break
                        time.sleep(0.01)
                    if _thread_running(dialog.thread):
                        result["ok"] = False
                        result["error"] = "登入工作執行緒未能在取消後結束。"
                    dialog.deleteLater()
                    application.processEvents()
        except Exception as exc:
            result["ok"] = False
            result["error"] = _safe_error(exc)
        finally:
            if dialog is not None and _thread_running(getattr(dialog, "thread", None)):
                try:
                    dialog._cancel_login()
                except Exception:
                    pass

    return _write_report(
        report,
        ok=bool(result["ok"]),
        elapsed=time.monotonic() - started,
        error=_safe_error(result["error"] or (errors[-1] if errors else "")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="檢查實際 tdl-core QR 登入事件是否能送到視窗")
    parser.add_argument(
        "--report",
        default=str(Path(".build") / "qr-probe-report.json"),
        help="報告輸出路徑（只含 ok、elapsed、error）",
    )
    args = parser.parse_args()
    return run_probe(args.report)


if __name__ == "__main__":  # pragma: no cover - command-line entry point
    raise SystemExit(main())
