"""Run the offline channel workflow check against the final EXE."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    from verify_release import _run_frozen_check
    from version import VERSION
except ModuleNotFoundError:
    # Direct ``import desktop.verify_workflow_release`` does not put the
    # desktop package directory on ``sys.path``; the existing verifier modules
    # intentionally use sibling imports for PyInstaller/script execution.
    _desktop_dir = str(Path(__file__).resolve().parent)
    if _desktop_dir not in sys.path:
        sys.path.insert(0, _desktop_dir)
    from verify_release import _run_frozen_check
    from version import VERSION


def main(
    source: str | os.PathLike[str] | None = None,
    report_path: str | os.PathLike[str] | None = None,
) -> int:
    root = Path(__file__).resolve().parents[1]
    executable = Path(source).resolve() if source else root / "TDL下載助手.exe"
    report = Path(report_path or root / "dist" / "workflow-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    logs = root / ".build" / "standalone-check" / "verification-logs" / "workflow"
    env = os.environ.copy()
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "GOROOT",
        "GOPATH",
        "TDL_GUI",
        "TDL_DATA_DIR",
        "TDL_DESKTOP_DATA_DIR",
        "QT_QPA_PLATFORM",
        "QT_QPA_FONTDIR",
    ):
        env.pop(key, None)
    env["PATH"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32")

    _run_frozen_check(
        [str(executable), "--verify-workflow", "--smoke-report", str(report)],
        cwd=executable.parent,
        env=env,
        log_dir=logs,
        timeout=120,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("offline workflow verification reported failure")

    screenshot_names = payload.get("screenshot_files", [])
    if not isinstance(screenshot_names, list) or not screenshot_names:
        raise RuntimeError("workflow verification did not produce screenshots")
    screenshots = []
    for value in screenshot_names:
        if not isinstance(value, str) or not value:
            raise RuntimeError("workflow screenshot name is invalid")
        path = (report.parent / value).resolve()
        if path.parent != report.parent or not path.is_file() or path.stat().st_size <= 1000:
            raise RuntimeError("workflow screenshot is missing or empty")
        screenshots.append(path.name)

    payload.update(
        result="PASS",
        version=VERSION,
        sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        executable=str(executable),
        offline_fixture=True,
        screenshots_present=True,
        screenshot_files=screenshots,
        telegram_live_account_test="NOT_RUN_BY_OFFLINE_WORKFLOW_CHECK",
    )
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="驗證最終 EXE 的離線頻道下載流程")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    raise SystemExit(main(args.source, args.report))
