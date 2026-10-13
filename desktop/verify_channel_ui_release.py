"""Run the native Qt channel-list check against the final bundled EXE."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from verify_release import _run_frozen_check


def main(report_path: str | os.PathLike[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    executable = root / "TDL下載助手.exe"
    report = Path(report_path or root / "dist" / "channel-ui-verification.json").resolve()
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GOROOT", "GOPATH", "TDL_GUI", "TDL_DATA_DIR"):
        env.pop(key, None)
    # This check must exercise the native Windows Qt platform.  Explicitly
    # remove headless overrides inherited from QR/smoke tests.
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("QT_QPA_FONTDIR", None)
    env["PATH"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32")
    _run_frozen_check(
        [str(executable), "--verify-channel-ui", "--smoke-report", str(report)],
        cwd=root / "dist",
        env=env,
        log_dir=root / ".build" / "standalone-check" / "verification-logs" / "channel-ui",
        timeout=1500,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    if not payload.get("ok"):
        raise RuntimeError("native channel UI verification reported failure")
    payload.update(
        version="1.2.0",
        sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="驗證最終 EXE 的原生頻道清單視窗")
    parser.add_argument("--report", default=str(root / "dist" / "channel-ui-verification.json"))
    raise SystemExit(main(parser.parse_args().report))
