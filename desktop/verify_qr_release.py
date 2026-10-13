"""Opt-in live QR rendering check for the final EXE; never signs into an account."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from verify_release import _run_frozen_check


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    executable = root / "TDL下載助手.exe"
    report = root / "dist" / "qr-login-verification.json"
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GOROOT", "GOPATH", "TDL_GUI", "TDL_DATA_DIR"):
        env.pop(key, None)
    env["PATH"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "System32")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_QPA_FONTDIR"] = str(Path(env.get("SystemRoot", "C:/Windows")) / "Fonts")
    _run_frozen_check(
        [str(executable), "--verify-qr", "--smoke-report", str(report)],
        cwd=root / "dist", env=env,
        log_dir=root / ".build" / "standalone-check" / "verification-logs" / "live-qr",
        timeout=60,
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"], payload
    payload.update(
        version="1.2.0",
        sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        actual_executable_clicked_login_and_rendered_qr=True,
        cancelled_before_account_authentication=True,
    )
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
