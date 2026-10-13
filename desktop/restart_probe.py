"""Opt-in, offline two-process restart check. Never opens a real account."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def fixture_core(runtime_text: str, output_text: str) -> int:
    if os.environ.get("TDL_DESKTOP_VERIFY_FIXTURE") != "1":
        return 2
    root = Path(os.environ["TDL_DESKTOP_DATA_DIR"]).resolve()
    runtime, output = Path(runtime_text).resolve(), Path(output_text).resolve()
    if not all(path.is_relative_to(root) and path != root for path in (runtime, output)):
        return 2
    # A PyInstaller windowed executable can have None streams even when the
    # caller supplied pipes.  Restore only those fixture pipes from Windows.
    if os.name == "nt" and (sys.stdout is None or sys.stdin is None):
        import ctypes
        import msvcrt
        get_handle = ctypes.windll.kernel32.GetStdHandle
        get_handle.argtypes = [ctypes.c_uint32]
        get_handle.restype = ctypes.c_void_p
        if sys.stdout is None:
            fd = msvcrt.open_osfhandle(get_handle(-11 & 0xFFFFFFFF), os.O_WRONLY | os.O_BINARY)
            sys.stdout = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
        if sys.stdin is None:
            fd = msvcrt.open_osfhandle(get_handle(-10 & 0xFFFFFFFF), os.O_RDONLY | os.O_BINARY)
            sys.stdin = os.fdopen(fd, "r", encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    marker = runtime / "offline-checkpoint.txt"

    def emit(data):
        print("@@TDL_GUI@@" + json.dumps(data), flush=True)

    if not marker.exists():
        (output / "first.mp4").write_bytes(b"offline-completed-file")
        marker.write_text("first-file-finished", encoding="utf-8")
        emit(dict(event="progress", file_id="first", filename="已完成的測試影片.mp4", state="completed", current=22, total=22, percent=100))
        emit(dict(event="progress", file_id="second", filename="正在下載的測試影片.mp4", state="downloading", current=5, total=20, percent=25))
        sys.stdin.readline()
    else:
        (output / "second.mp4").write_bytes(b"offline-remaining-file")
        emit(dict(event="progress", file_id="second", filename="正在下載的測試影片.mp4", state="completed", current=22, total=22, percent=100))
    return 0


def run_probe(phase: str, report_path: str) -> int:
    if os.environ.get("TDL_DESKTOP_VERIFY_FIXTURE") != "1" or not os.environ.get("TDL_DESKTOP_DATA_DIR"):
        return 2
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    from desktop.app import BackendBridge, MainWindow, STYLE
    from desktop.backend import Runner

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    for font_path in (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"):
        if Path(font_path).exists():
            QFontDatabase.addApplicationFont(font_path)
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    report = Path(report_path).resolve()
    root = Path(os.environ["TDL_DESKTOP_DATA_DIR"]).resolve()
    output = root / "downloads"
    marker = root / "verification-expectations.json"
    command_flags = []

    class FixtureRunner(Runner):
        def _build_args(self, request, runtime):
            command_flags.append(super()._build_args(request, runtime))
            entry = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "desktop.app"]
            return [*entry, "--verify-download-child", str(runtime), str(output)]

    def pump(predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if not predicate():
            raise RuntimeError("offline workflow timed out")

    bridge = BackendBridge()
    bridge.runner = FixtureRunner(bridge.store, executable=sys.executable)
    if phase == "before-close":
        if bridge.store.list_accounts() or bridge.store.list_queue():
            return 2
        account = bridge.store.add_account("離線續下載驗證")
        bridge.store.update_account(account.id, authenticated=True)
    window = MainWindow(bridge)
    window.show()
    metrics = {"ok": False, "phase": phase, "offline_fixture": True}
    try:
        pump(lambda: window._accounts_loaded and window._queue_loaded)
        if phase == "before-close":
            window._enqueue_downloads(["https://t.me/telegram/193"], {"account_id": account.id, "output_dir": str(output)})
            pump(lambda: bool(window.jobs) and "second" in next(iter(window.jobs.values())).file_progress)
            job = next(iter(window.jobs.values()))
            marker.write_text(json.dumps({"id": job.backend_job_id, "account_id": account.id, "first_mtime": (output / "first.mp4").stat().st_mtime_ns}), encoding="utf-8")
            window._show_widget(window.home_page)
            app.processEvents()
            window.home_page.queue_scroll.grab().save(str(report.with_suffix(".png")))
            window.close()
            pump(lambda: window.isHidden() and not window._background_work_running())
            metrics["saved_as_paused"] = bridge.store.get_job(job.backend_job_id).status == "paused"
            metrics["ok"] = metrics["saved_as_paused"]
        elif phase == "after-reopen":
            expected = json.loads(marker.read_text(encoding="utf-8"))
            pump(lambda: bridge.store.get_job(expected["id"]).status == "completed" and not window._threads and not window._preparing_jobs)
            metrics.update(
                resumed_same_job=len(bridge.store.list_queue()) == 1 and len(bridge.store.list_history()) == 1,
                original_account=bridge.store.get_job(expected["id"]).account_id == expected["account_id"],
                completed_file_unchanged=(output / "first.mp4").stat().st_mtime_ns == expected["first_mtime"],
                remaining_file_written=(output / "second.mp4").is_file(),
                continue_flag="--continue" in command_flags[-1],
                file_rows_restored=next(iter(window.jobs.values())).file_progress.get("first", {}).get("state") == "completed",
            )
            window.close()
            pump(lambda: window.isHidden() and not window._background_work_running())
            metrics["ok"] = all(metrics[key] for key in ("resumed_same_job", "original_account", "completed_file_unchanged", "remaining_file_written", "continue_flag", "file_rows_restored"))
        else:
            return 2
    except Exception as exc:
        metrics["failure_type"] = type(exc).__name__
    finally:
        if not window.isHidden() or window._background_work_running():
            window.close()
            try:
                pump(lambda: window.isHidden() and not window._background_work_running(), 15)
            except RuntimeError:
                metrics["ok"] = False
        window.deleteLater()
        app.processEvents()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if metrics["ok"] else 1
