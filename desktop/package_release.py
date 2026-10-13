"""Collect local source and third-party licenses for a reproducible release."""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def copy_release_file(source: Path, target: Path) -> None:
    """Go's module cache is read-only; do not propagate that flag to releases."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    shutil.copyfile(source, target)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    release = root / "dist"
    release.mkdir(exist_ok=True)
    licenses = release / "licenses"
    licenses.mkdir(exist_ok=True)
    copy_release_file(root / "LICENSE", licenses / "TDL-AGPL-3.0.txt")
    copy_release_file(root / "desktop" / "THIRD_PARTY_NOTICES.md", licenses / "THIRD_PARTY_NOTICES.md")
    copy_release_file(root / "README_zh-TW.md", release / "使用說明.md")
    copy_release_file(root / "desktop" / "handovers" / "1.2.0.md", release / "TDL桌面版交接清冊-1.2.0.md")
    copy_release_file(root / "desktop" / "VALIDATION.md", release / "驗證結果.md")
    python_root = Path(sys.prefix)
    base_license = Path(sys.base_prefix) / "LICENSE.txt"
    if base_license.exists():
        copy_release_file(base_license, licenses / "Python-PSF.txt")
    for pattern in ("LICENSE*", "Lib/site-packages/*.dist-info/licenses/**/*", "Lib/site-packages/PySide6/doc/licenses/*"):
        for item in python_root.glob(pattern):
            if item.is_file():
                relative = item.relative_to(python_root)
                target = licenses / "python" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_release_file(item, target)
    go_license = root / ".build" / "go" / "LICENSE"
    if go_license.is_file():
        copy_release_file(go_license, licenses / "Go-BSD.txt")
    module_cache = root / ".build" / "gopath" / "pkg" / "mod"
    if module_cache.exists():
        for item in module_cache.rglob("*"):
            if item.is_file() and item.name.lower() in {"license", "license.txt", "license.md", "copying", "notice"}:
                relative = item.relative_to(module_cache)
                if relative.parts[0] == "cache":
                    continue
                target = licenses / "go-modules" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_release_file(item, target)
    tracked = subprocess.check_output(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"], cwd=root
    ).decode("utf-8").split("\x00")
    with zipfile.ZipFile(release / "TDL桌面版原始碼.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(tracked)):
            path = root / name
            if name and path.is_file() and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
                archive.write(path, "tdl/" + name)
    executable = release / "TDL下載助手.exe"
    if executable.exists():
        report = {
            "application": "TDL 繁體中文下載助手",
            "version": "1.2.0",
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "bytes": executable.stat().st_size,
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
            "source_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip()),
            "source_archive_sha256": hashlib.sha256((release / "TDL桌面版原始碼.zip").read_bytes()).hexdigest(),
            "telegram_live_account_test": "NOT_RUN_FOR_THIS_BINARY",
        }
        live_receipt = release / "channel-live-verification.json"
        if live_receipt.exists():
            live = json.loads(live_receipt.read_text(encoding="utf-8"))
            if live.get("ok") and live.get("sha256") == report["sha256"]:
                report["telegram_live_account_test"] = "PASS_ISOLATED_ACCOUNT_PUBLIC_VIDEO"
                report["live_verification_receipt"] = live_receipt.name
        ui_receipt = release / "channel-ui-verification.json"
        if ui_receipt.exists():
            ui = json.loads(ui_receipt.read_text(encoding="utf-8"))
            if ui.get("ok") and ui.get("sha256") == report["sha256"]:
                report["channel_ui_live_test"] = "PASS_ACTUAL_REFRESH_BUTTON_AND_GUI_THREAD"
                report["channel_ui_verification_receipt"] = ui_receipt.name
        restart_receipt = release / "restart-verification.json"
        if restart_receipt.exists():
            restart = json.loads(restart_receipt.read_text(encoding="utf-8"))
            if restart.get("ok") and restart.get("sha256") == report["sha256"]:
                report["restart_test"] = "PASS_TWO_EXE_PROCESSES_OFFLINE_FIXTURE"
                report["restart_verification_receipt"] = restart_receipt.name
        qr_receipt = release / "qr-login-verification.json"
        if qr_receipt.exists():
            qr = json.loads(qr_receipt.read_text(encoding="utf-8"))
            if qr.get("ok") and qr.get("sha256") == report["sha256"]:
                report["qr_display_test"] = "PASS_CANCELLED_BEFORE_AUTHENTICATION"
                report["qr_verification_receipt"] = qr_receipt.name
        (release / "build-info.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Release source and licenses collected.")


if __name__ == "__main__":
    main()
