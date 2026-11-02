"""Verify the final EXE's batch forwarding UI using isolated offline data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from verify_release import _run_frozen_check
from version import VERSION


def main(source: Path | None = None, report: Path | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    executable = (source or root / "TDL下載助手.exe").resolve()
    report = (report or root / "dist/batch-forward-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "GOROOT", "GOPATH", "TDL_GUI", "TDL_DATA_DIR", "TDL_DESKTOP_DATA_DIR", "QT_QPA_PLATFORM", "QT_QPA_FONTDIR"):
        env.pop(name, None)
    env["PATH"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32")
    _run_frozen_check(
        [str(executable), "--verify-batch-forward", "--smoke-report", str(report)],
        cwd=executable.parent,
        env=env,
        log_dir=root / ".build/standalone-check/verification-logs/batch-forward",
        timeout=120,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("Offline batch forwarding verification failed")
    screenshots = payload.get("screenshot_files", [])
    if not screenshots:
        raise RuntimeError("Missing batch forwarding screenshots")
    for name in screenshots:
        path = (report.parent / name).resolve()
        if path.parent != report.parent or not path.is_file() or path.stat().st_size < 1000:
            raise RuntimeError("Invalid batch forwarding screenshot")
    payload.update(
        result="PASS", version=VERSION,
        sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        offline_fixture=True,
        telegram_live_account_test="NOT_RUN_BY_OFFLINE_BATCH_FORWARD_CHECK",
    )
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="驗證 EXE 的群組批次轉傳流程（離線）")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    raise SystemExit(main(args.source, args.report))
