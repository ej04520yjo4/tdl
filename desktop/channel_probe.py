"""Opt-in live release check using an isolated copy of a remembered account.

No account contents, channel titles, session bytes or media are retained in the
report. The only downloaded attachment is a <= 2 MiB public @telegram video.
Normal application launches never run this diagnostic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop.backend import (
    ExportRequest, Runner, Store, _atomic_write_json, _restrict_permissions,
    sanitize_text,
)

TAIWAN = timezone(timedelta(hours=8))
MAX_SAMPLE_BYTES = 2 * 1024 * 1024


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _await_result(handle, *, stage: str, progress_path: Path, limit: int = 360):
    deadline = time.monotonic() + limit
    event_names: set[str] = set()
    started = time.monotonic()
    next_update = 0.0
    progress = {"stage": stage, "elapsed": 0.0, "event_types": []}
    while not handle.done.is_set() and time.monotonic() < deadline:
        event = handle.get_event(timeout=0.2)
        if event:
            event_names.add(str(event.get("event")))
            for key in ("current", "total"):
                if isinstance(event.get(key), (int, float)):
                    progress[key] = event[key]
        if time.monotonic() >= next_update:
            progress.update(elapsed=round(time.monotonic() - started, 1), event_types=sorted(event_names))
            _atomic_write_json(progress_path, progress)
            next_update = time.monotonic() + 10
    if not handle.done.is_set():
        handle.cancel()
        handle.wait(timeout=8)
        raise RuntimeError(f"{stage} 驗證操作逾時，已取消。")
    result = handle.wait(timeout=0)
    if not result or not result.success:
        raise RuntimeError(str(result.error if result else "核心沒有傳回結果"))
    return result, sorted(event_names)


class _PublicSampleRunner(Runner):
    def _build_args(self, request, runtime):
        args = super()._build_args(request, runtime)
        if isinstance(request, ExportRequest):
            # Resolve the known public sample once. The subsequent date scan
            # uses the exact typed peer reference returned by this export.
            args[args.index("--chat") + 1] = "telegram"
            args.extend(["--filter", f"Media.Size > 0 && Media.Size <= {MAX_SAMPLE_BYTES}"])
        return args


def _verify(report: Path) -> dict:
    source = Path(os.environ["LOCALAPPDATA"]) / "TDLDesktop"
    account_path = source / "accounts.json"
    preferences_path = source / "preferences.json"
    accounts = json.loads(account_path.read_text(encoding="utf-8"))
    preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    active = preferences.get("values", {}).get("active_account_id")
    eligible = [account for account in accounts["accounts"] if account.get("authenticated")]
    if not eligible:
        raise RuntimeError("找不到已登入的帳號。")
    account = next((item for item in eligible if item["id"] == active), eligible[0])
    envelope = source / "accounts" / account["id"] / "session.dpapi"
    originals = {path: _digest(path) for path in (envelope, account_path, preferences_path)}
    sandbox_parent = report.parent.resolve()
    progress_path = report.with_suffix(".progress.json")

    with tempfile.TemporaryDirectory(prefix="channel-acceptance-", dir=sandbox_parent) as temporary:
        state = Path(temporary).resolve()
        assert state.is_relative_to(sandbox_parent) and state != sandbox_parent
        _restrict_permissions(state, strict=True)
        account_dir = state / "accounts" / account["id"]
        account_dir.mkdir(parents=True)
        _restrict_permissions(account_dir, strict=True)
        shutil.copyfile(envelope, account_dir / "session.dpapi")
        _restrict_permissions(account_dir / "session.dpapi", strict=True)
        _atomic_write_json(state / "accounts.json", {"version": 1, "accounts": [account]})
        preferences.setdefault("values", {}).update(
            download_dir=str(state / "downloads"), concurrency=1, group=False,
        )
        _atomic_write_json(state / "preferences.json", preferences)
        store = Store(state)
        runner = Runner(store)
        try:
            listed, events = _await_result(
                runner.start_list_chats(account["id"]), stage="channel_list", progress_path=progress_path,
            )
            receipt = {
                "channel_list_ok": True,
                "chat_count": len(listed.data["chats"]),
                "channel_list_partial": bool(listed.data.get("partial")),
                "channel_list_skipped": int(listed.data.get("skipped", 0)),
                "event_types": events,
            }
            sampler = _PublicSampleRunner(store)
            try:
                sample, _ = _await_result(sampler.start_export(
                    account["id"], "1", "2020-01-01",
                    datetime.now(TAIWAN).date().isoformat(), ["video"],
                ), stage="public_sample_scan", progress_path=progress_path)
            finally:
                sampler.cancel_all()
            sample_payload = json.loads(Path(sample.data["path"]).read_text(encoding="utf-8"))
            if not sample_payload["messages"]:
                raise RuntimeError("公開測試頻道沒有符合大小限制的影片。")
            target = sample_payload["messages"][0]
            day = datetime.fromtimestamp(target["date"], TAIWAN).date().isoformat()
            scanned, _ = _await_result(runner.start_export(
                account["id"], "channel:" + str(sample_payload["id"]), day, day, ["video"],
            ), stage="inclusive_date_scan", progress_path=progress_path)
            manifest = json.loads(Path(scanned.data["path"]).read_text(encoding="utf-8"))
            assert len(manifest["messages"]) == scanned.data["count"]
            assert manifest["peer_type"] == "channel"
            for message in manifest["messages"]:
                assert datetime.fromtimestamp(message["date"], TAIWAN).date().isoformat() == day
                assert message["media_type"] == "video"
            selected = next(message for message in manifest["messages"] if message["id"] == target["id"])
            sample_file = store.account_root(account["id"]) / "exports" / "one-public-video.json"
            _atomic_write_json(sample_file, dict(manifest, messages=[selected]))
            downloaded, _ = _await_result(runner.start_download(
                account["id"], [], files=[str(sample_file)], output_dir=str(state / "downloads"),
                continue_download=False, skip_same=True, group=False, concurrency=1,
                title="公開頻道下載驗證",
            ), stage="public_video_download", progress_path=progress_path)
            files = [path for path in (state / "downloads").rglob("*") if path.is_file()]
            assert len(files) == 1 and 0 < files[0].stat().st_size <= MAX_SAMPLE_BYTES
            receipt.update(
                public_sample="https://t.me/telegram", inclusive_date_filter_ok=True,
                video_filter_ok=True, scan_count=scanned.data["count"],
                actual_download_ok=downloaded.success, downloaded_files=1,
                downloaded_bytes=files[0].stat().st_size,
                core_sha256=_digest(runner.resolve_executable()),
            )
        finally:
            runner.cancel_all()
    assert all(_digest(path) == digest for path, digest in originals.items())
    receipt["original_account_files_unchanged"] = True
    return receipt


def run_probe(report_path: str | os.PathLike[str] | None) -> int:
    report = Path(report_path or "channel-live-verification.json").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        payload = dict(_verify(report), ok=True)
    except Exception as exc:
        payload = {"ok": False, "error": sanitize_text(str(exc))}
    progress_path = report.with_suffix(".progress.json")
    if progress_path.exists():
        if not payload["ok"]:
            payload["progress"] = json.loads(progress_path.read_text(encoding="utf-8"))
        progress_path.unlink()
    payload["elapsed"] = round(time.monotonic() - started, 3)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="隔離驗證頻道清單與公開小型影片下載")
    parser.add_argument("--report", default=".build/channel-live-verification.json")
    raise SystemExit(run_probe(parser.parse_args().report))
