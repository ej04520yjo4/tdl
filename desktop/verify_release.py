"""Exercise the one-file release from an isolated directory, entirely offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
from pathlib import Path

import pefile


_FROZEN_TIMEOUT_SECONDS = 45
_LOG_TAIL_BYTES = 12_000


def _log_tail(path: Path, limit: int = _LOG_TAIL_BYTES) -> str:
    """Return a bounded, decoded tail so a broken frozen app cannot flood output."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"<unable to read {path}: {exc}>"
    if not data:
        return "<empty>"
    truncated = len(data) > limit
    text = data[-limit:].decode("utf-8", errors="replace")
    if truncated:
        return f"<older output omitted>\n{text}"
    return text


def _diagnostic_failure(
    header: str,
    stdout_log: Path,
    stderr_log: Path,
    *,
    cleanup: str | None = None,
) -> str:
    """Build an actionable failure that points to the complete on-disk logs."""

    cleanup_line = f"\nCleanup: {cleanup}" if cleanup else ""
    return (
        f"{header}{cleanup_line}\n"
        f"stdout log: {stdout_log}\n"
        f"stderr log: {stderr_log}\n"
        "--- stdout tail ---\n"
        f"{_log_tail(stdout_log)}\n"
        "--- stderr tail ---\n"
        f"{_log_tail(stderr_log)}"
    )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> str:
    """Stop only the process launched by this check and its descendants.

    On Windows the PID came directly from ``Popen`` above, so ``taskkill /T``
    targets precisely this frozen test process tree.  The verifier never uses a
    broad image-name kill.  The POSIX branch is useful when this script is
    linted or exercised on a development machine outside Windows.
    """

    if process.poll() is not None:
        return f"process already exited with code {process.returncode}"

    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        taskkill_command = [
            str(taskkill) if taskkill.exists() else "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
        ]
        try:
            # Keep the cleanup command detached from the frozen app's logs.
            result = subprocess.run(
                taskkill_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            taskkill_status = f"taskkill exit {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            taskkill_status = f"taskkill unavailable or timed out: {exc}"
        try:
            process.wait(timeout=5)
            return f"{taskkill_status}; process tree ended"
        except subprocess.TimeoutExpired:
            # This fallback still targets the exact PID obtained from Popen.
            try:
                process.kill()
                process.wait(timeout=5)
                return f"{taskkill_status}; direct PID kill completed"
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"{taskkill_status}; direct PID kill failed: {exc}"

    # Development/test fallback for non-Windows hosts.  start_new_session is
    # set when launching below, so descendants share this process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.wait(timeout=5)
        return "process group terminated"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        try:
            process.wait(timeout=5)
            return "process group force-killed"
        except subprocess.TimeoutExpired as exc:
            return f"force kill did not finish: {exc}"


def _run_frozen_check(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_dir: Path,
    timeout: int = _FROZEN_TIMEOUT_SECONDS,
) -> None:
    """Run one frozen check with file-backed diagnostics and bounded cleanup."""

    label = command[1] if len(command) > 1 else "launch"
    safe_label = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in label.lstrip("-")
    ) or "launch"
    stdout_log = log_dir / f"{safe_label}.stdout.log"
    stderr_log = log_dir / f"{safe_label}.stderr.log"
    command_text = subprocess.list2cmdline(command)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process: subprocess.Popen[bytes] | None = None
    timeout_cleanup: str | None = None

    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        # File handles avoid the classic frozen-child failure mode where a
        # descendant keeps a PIPE open after the bootloader parent is gone.
        with stdout_log.open("wb") as stdout, stderr_log.open("wb") as stderr:
            popen_kwargs = {
                "cwd": cwd,
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": stdout,
                "stderr": stderr,
                "creationflags": creationflags,
            }
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except OSError as exc:
                raise RuntimeError(
                    _diagnostic_failure(
                        f"Unable to start frozen check: {command_text}; {exc}",
                        stdout_log,
                        stderr_log,
                    )
                ) from exc
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timeout_cleanup = _terminate_process_tree(process)
                stdout.flush()
                stderr.flush()
                returncode = None
    except RuntimeError:
        raise

    if timeout_cleanup is not None:
        raise RuntimeError(
            _diagnostic_failure(
                f"Frozen check timed out after {timeout}s: {command_text} "
                f"(pid {process.pid if process else 'unknown'})",
                stdout_log,
                stderr_log,
                cleanup=timeout_cleanup,
            )
        )

    if returncode:
        raise RuntimeError(
            _diagnostic_failure(
                f"Frozen app failed with exit code {returncode}: {command_text}",
                stdout_log,
                stderr_log,
            )
        )

    stdout_bytes = stdout_log.read_bytes()
    stderr_bytes = stderr_log.read_bytes()
    combined = stdout_bytes + b"\n" + stderr_bytes
    if b"Traceback" in combined or b"QThread: Destroyed" in combined:
        raise RuntimeError(
            _diagnostic_failure(
                f"Frozen app emitted an exception or Qt shutdown warning: {command_text}",
                stdout_log,
                stderr_log,
            )
        )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="獨立啟動檢查")
    parser.add_argument("--source", type=Path, default=root / "TDL下載助手.exe")
    source = parser.parse_args().source.resolve()
    standalone = root / ".build" / "standalone-check"
    standalone.mkdir(parents=True, exist_ok=True)
    executable = standalone / source.name
    print("Copying the release to an independent directory...", flush=True)
    shutil.copyfile(source, executable)
    portable = pefile.PE(str(executable), fast_load=True)
    assert portable.FILE_HEADER.Machine == 0x8664, "Expected Windows x64"
    assert portable.OPTIONAL_HEADER.Subsystem == 2, "Expected a windowed executable"
    portable.close()
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GOROOT", "GOPATH", "TDL_GUI", "TDL_DATA_DIR"):
        env.pop(key, None)
    env["PATH"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QPA_FONTDIR"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "Fonts")
    env["TDL_DESKTOP_DATA_DIR"] = str(standalone / "isolated-user-data")
    report = standalone / "smoke-report.json"
    capture = root / "desktop" / "qa" / "frozen-home.png"
    log_dir = standalone / "verification-logs"
    commands = [
        [str(executable), "--smoke-test", "--smoke-report", str(report)],
        [str(executable), "--screenshot", str(capture)],
    ]
    for command in commands:
        print(f"Running frozen check: {command[1]}", flush=True)
        _run_frozen_check(command, cwd=standalone, env=env, log_dir=log_dir)
        print(f"Completed frozen check: {command[1]}", flush=True)
    native_report = standalone / "native-smoke-report.json"
    native_env = env.copy()
    native_env.pop("QT_QPA_PLATFORM", None)
    native_env.pop("QT_QPA_FONTDIR", None)
    print("Checking the native Windows window...", flush=True)
    _run_frozen_check(
        [str(executable), "--smoke-test", "--smoke-report", str(native_report)],
        cwd=standalone, env=native_env, log_dir=log_dir / "native",
    )
    assert json.loads(native_report.read_text(encoding="utf-8"))["ok"]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] and payload["backend_available"], payload
    assert "_MEI" in payload["core"], "Core must come from the one-file bundle"
    assert capture.exists() and capture.stat().st_size > 10_000, "Expected a rendered window"
    receipt = {
        "result": "PASS",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "architecture": "Windows x64 GUI",
        "copied_executable_to_independent_directory": True,
        "python_and_go_removed_from_path": True,
        "bundled_backend_and_core": payload["ok"],
        "window_rendered": True,
        "native_windows_window_started": True,
        "screenshot": str(capture),
        "telegram_live_account_test": "NOT_RUN_BY_OFFLINE_CHECK_SEE_CHANNEL_RECEIPT",
    }
    (root / "dist" / "verification.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
