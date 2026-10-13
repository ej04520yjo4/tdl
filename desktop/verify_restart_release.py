"""Verify remembered downloads across two actual EXE processes, offline."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import tempfile
from pathlib import Path
from verify_release import _run_frozen_check


def main(source=None):
    root = Path(__file__).resolve().parents[1]
    executable = Path(source).resolve() if source else root / "TDL下載助手.exe"
    logs = root / ".build" / "standalone-check" / "verification-logs" / "restart"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "GOROOT", "GOPATH", "TDL_GUI", "TDL_DATA_DIR"):
        env.pop(key, None)
    env.update(PATH=str(Path(env.get("SystemRoot", "C:/Windows")) / "System32"), QT_QPA_PLATFORM="offscreen", TDL_DESKTOP_VERIFY_FIXTURE="1")
    receipts = []
    with tempfile.TemporaryDirectory(prefix="restart-fixture-", dir=logs) as directory:
        env["TDL_DESKTOP_DATA_DIR"] = directory
        for phase in ("before-close", "after-reopen"):
            report = logs / f"{phase}.json"
            _run_frozen_check([str(executable), "--verify-restart", phase, "--smoke-report", str(report)], cwd=root, env=env, log_dir=logs / phase, timeout=60)
            receipts.append(json.loads(report.read_text(encoding="utf-8")))
    result = {"ok": all(item["ok"] for item in receipts), "version": "1.2.0", "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "offline_fixture": True, "separate_exe_processes": 2, "phases": receipts}
    (root / "dist" / "restart-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="驗證兩次啟動 EXE 的離線接續下載流程")
    parser.add_argument("--source")
    main(parser.parse_args().source)
