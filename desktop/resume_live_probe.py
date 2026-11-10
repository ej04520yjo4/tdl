"""Opt-in live check of pause/continue and of a batch-forward dry run.

Uses an isolated copy of the remembered account; the original data is
hashed before and after and must not change.  It downloads one public
@telegram video of a few tens of megabytes twice: once paused part-way and
continued, once straight through, and compares the two files.  The batch
forward runs with --dry-run only, which sends nothing.  The report holds no
account details, chat names or media; everything else is deleted.

Run from the project root after building the core:
    .build\\venv\\Scripts\\python.exe desktop\\resume_live_probe.py --report dist\\resume-live-verification.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.backend import (  # noqa: E402
    PART_LIST_SUFFIX,
    PARTIAL_DOWNLOAD_SUFFIX,
    ExportRequest,
    Runner,
    Store,
    _atomic_write_json,
    _restrict_permissions,
)

TAIWAN = timezone(timedelta(hours=8))
MIN_BYTES = int(os.environ.get("TDL_PROBE_MIN_MB", "20")) * 1024 * 1024
MAX_BYTES = int(os.environ.get("TDL_PROBE_MAX_MB", "200")) * 1024 * 1024
# Fraction of the file after which the probe asks for a pause.  A small video
# on a fast connection can finish before the pause arrives; pause earlier then.
PAUSE_AT = float(os.environ.get("TDL_PROBE_PAUSE_AT", "0.4"))


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _PublicVideoRunner(Runner):
    """Resolve the public @telegram channel and keep mid-sized videos."""

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        args = super()._build_args(request, runtime)
        if isinstance(request, ExportRequest) and request.chat_id == "1":
            args[args.index("--chat") + 1] = "telegram"
            args.extend(["--filter", f"Media.Size >= {MIN_BYTES} && Media.Size <= {MAX_BYTES}"])
        return args


def _wait(handle, stage: str, limit: float = 900, on_event=None):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + limit
    while not handle.done.is_set():
        if time.monotonic() > deadline:
            handle.cancel()
            handle.wait(timeout=15)
            raise RuntimeError(f"{stage}: timed out")
        event = handle.get_event(timeout=0.2)
        if event and on_event is not None:
            on_event(event)
    while (event := handle.get_event(timeout=0)) is not None:
        if on_event is not None:
            on_event(event)
    return handle.wait(timeout=0)


def _files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


def run(report_path: Path, *, batch_dry_run: bool = True) -> dict:
    source = Path(os.environ["LOCALAPPDATA"]) / "TDLDesktop"
    accounts = json.loads((source / "accounts.json").read_text(encoding="utf-8"))
    preferences = json.loads((source / "preferences.json").read_text(encoding="utf-8"))
    active = preferences.get("values", {}).get("active_account_id")
    eligible = [account for account in accounts["accounts"] if account.get("authenticated")]
    if not eligible:
        raise RuntimeError("no logged-in account")
    account = next((item for item in eligible if item["id"] == active), eligible[0])
    envelope = source / "accounts" / account["id"] / "session.dpapi"
    originals = {path: _digest(path) for path in sorted(source.rglob("*")) if path.is_file()}

    report: dict = {"ok": False, "isolated_account_copy": True}
    sandbox = ROOT / ".build"
    with tempfile.TemporaryDirectory(prefix="resume-live-", dir=sandbox) as temporary:
        state = Path(temporary).resolve()
        _restrict_permissions(state, strict=True)
        account_dir = state / "accounts" / account["id"]
        account_dir.mkdir(parents=True)
        shutil.copyfile(envelope, account_dir / "session.dpapi")
        _atomic_write_json(state / "accounts.json", {"version": 1, "accounts": [account]})
        prefs = dict(preferences)
        prefs.setdefault("values", {}).update(download_dir=str(state / "downloads"), concurrency=1, group=False)
        _atomic_write_json(state / "preferences.json", prefs)
        store = Store(state)
        runner = _PublicVideoRunner(store)
        try:
            # 1. Find one public video of a few tens of MB.
            today = datetime.now(TAIWAN).date().isoformat()
            sample = _wait(runner.start_export(account["id"], "1", "2015-01-01", today, ["video"]), "sample scan")
            if not sample or not sample.success:
                raise RuntimeError(f"sample scan failed: {sample.error if sample else ''}")
            sample_manifest = json.loads(Path(sample.data["path"]).read_text(encoding="utf-8"))
            if not sample_manifest["messages"]:
                raise RuntimeError("no public video in the size range")
            target = sample_manifest["messages"][0]
            day = datetime.fromtimestamp(target["date"], TAIWAN).date().isoformat()
            scanned = _wait(
                runner.start_export(account["id"], "channel:" + str(sample_manifest["id"]), day, day, ["video"]),
                "date scan",
            )
            if not scanned or not scanned.success:
                raise RuntimeError(f"date scan failed: {scanned.error if scanned else ''}")
            manifest = json.loads(Path(scanned.data["path"]).read_text(encoding="utf-8"))
            selected = next(message for message in manifest["messages"] if message["id"] == target["id"])
            one = store.account_root(account["id"]) / "exports" / "one-public-video.json"
            _atomic_write_json(one, dict(manifest, messages=[selected]))
            report["public_video_bytes"] = int(selected.get("file_size") or selected.get("size") or 0)

            # 2. Download, pause at about 40 %, check the kept parts.
            paused_dir = state / "paused"
            handle = runner.start_download(
                account["id"], [], files=[str(one)], output_dir=str(paused_dir),
                continue_download=True, skip_same=False, group=False, concurrency=1,
            )
            seen = {"paused_at": 0, "total": 0, "requested": False}

            def pause_midway(event):  # type: ignore[no-untyped-def]
                current, total = event.get("current"), event.get("total")
                if event.get("event") == "progress" and isinstance(current, int) and isinstance(total, int) and total:
                    seen["total"] = total
                    if not seen["requested"] and current >= total * PAUSE_AT:
                        seen["requested"] = True
                        seen["paused_at"] = current
                        handle.pause()

            paused = _wait(handle, "first download", on_event=pause_midway)
            if not seen["requested"] or not paused or not paused.paused:
                raise RuntimeError("the download did not pause part-way")
            kept = [path for path in _files(paused_dir) if path.name.endswith(PARTIAL_DOWNLOAD_SUFFIX)]
            lists = [path for path in _files(paused_dir) if path.name.endswith(PARTIAL_DOWNLOAD_SUFFIX + PART_LIST_SUFFIX)]
            if len(kept) != 1 or len(lists) != 1:
                left = sorted("".join(path.suffixes[-2:]) or "(none)" for path in _files(paused_dir))
                raise RuntimeError(
                    f"expected one kept file and one part list, found {len(kept)} and {len(lists)}; "
                    f"files left by suffix: {left}; job status: {store.get_job(paused.job_id).status}; "
                    f"pause requested at {round(seen['paused_at'] * 100 / max(seen['total'], 1), 1)}%"
                )
            kept_parts = json.loads(lists[0].read_text(encoding="utf-8"))["parts"]
            report.update(
                paused_at_percent=round(seen["paused_at"] * 100 / seen["total"], 1),
                job_status_after_pause=store.get_job(paused.job_id).status,
                kept_parts=len(kept_parts),
                kept_megabytes=round(len(kept_parts) * 1.0, 1),
            )

            # 3. Continue: the first progress must start from the kept parts.
            first = {"current": None}

            def first_progress(event):  # type: ignore[no-untyped-def]
                if event.get("event") == "progress" and first["current"] is None and isinstance(event.get("current"), int):
                    first["current"] = event["current"]

            started = time.monotonic()
            resumed = _wait(runner.resume_download(store.get_job(paused.job_id)), "continue", on_event=first_progress)
            if not resumed or not resumed.success:
                raise RuntimeError(f"continue failed: {resumed.error if resumed else ''}")
            report["continue_seconds"] = round(time.monotonic() - started, 1)
            report["continue_first_progress_bytes"] = first["current"]
            report["continue_started_from_kept_parts"] = bool(
                # The last part is usually shorter than 1 MiB, so cap at the file size.
                first["current"] is not None
                and first["current"] >= min(len(kept_parts) * 1024 * 1024, seen["total"]) * 0.99
            )
            finished = _files(paused_dir)
            leftovers = [path.name for path in finished if path.name.endswith((PARTIAL_DOWNLOAD_SUFFIX, PART_LIST_SUFFIX))]
            if leftovers or len(finished) != 1:
                raise RuntimeError(f"unexpected files after continuing: {len(finished)} files, {len(leftovers)} leftovers")
            resumed_file = finished[0]

            # 4. Straight download of the same video for comparison.
            straight_dir = state / "straight"
            straight = _wait(
                runner.start_download(
                    account["id"], [], files=[str(one)], output_dir=str(straight_dir),
                    continue_download=False, skip_same=False, group=False, concurrency=1,
                ),
                "reference download",
            )
            if not straight or not straight.success:
                raise RuntimeError(f"reference download failed: {straight.error if straight else ''}")
            reference = _files(straight_dir)
            if len(reference) != 1:
                raise RuntimeError("reference download did not produce one file")
            report.update(
                resumed_bytes=resumed_file.stat().st_size,
                reference_bytes=reference[0].stat().st_size,
                resumed_sha256=_digest(resumed_file),
                reference_sha256=_digest(reference[0]),
            )
            report["files_identical"] = report["resumed_sha256"] == report["reference_sha256"]

            # 5. Batch forward dry run of the scanned manifest: sends nothing.
            # Skipped when the user agreed to download checks only.
            if not batch_dry_run:
                report["batch_dry_run"] = "SKIPPED_BY_REQUEST"
                return _finish(report, report_path, source, originals)
            listed = _wait(runner.start_list_chats(account["id"]), "chat list")
            if not listed or not listed.success:
                raise RuntimeError("chat list failed")
            source_ref = "channel:" + str(manifest["id"])
            destination = next(
                (
                    chat for chat in listed.data["chats"]
                    if chat.get("type") == "group" and str(chat.get("ref", "")).split(":")[0] in {"channel", "chat"}
                    and chat.get("ref") != source_ref and not chat.get("forum")
                ),
                None,
            )
            if destination is None:
                report["batch_dry_run"] = "SKIPPED_NO_GROUP"
            else:
                dry = _wait(
                    runner.start_batch_forward(
                        account["id"], scanned.data["path"], source_chat=source_ref, to=destination["ref"],
                        manifest_sha256=scanned.data["sha256"], expected_count=int(scanned.data["count"]),
                        dry_run=True,
                    ),
                    "batch dry run",
                )
                report["batch_dry_run"] = "PASS" if dry and dry.success else f"FAIL: {dry.error if dry else ''}"
                report["batch_dry_run_messages"] = int(scanned.data["count"])
        finally:
            runner.cancel_all()

    return _finish(report, report_path, source, originals)


def _finish(report: dict, report_path: Path, source: Path, originals: dict) -> dict:
    changed = [str(path.relative_to(source)) for path, digest in originals.items() if not path.is_file() or _digest(path) != digest]
    report["original_data_unchanged"] = not changed
    report["ok"] = bool(
        report.get("files_identical")
        and report.get("continue_started_from_kept_parts")
        and report.get("job_status_after_pause") == "paused"
        and report["original_data_unchanged"]
        and str(report.get("batch_dry_run", "")).startswith(("PASS", "SKIPPED"))
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "dist" / "resume-live-verification.json")
    parser.add_argument(
        "--skip-batch-dry-run", action="store_true",
        help="only download, pause and continue; do not run the batch forward dry run",
    )
    args = parser.parse_args()
    try:
        report = run(args.report, batch_dry_run=not args.skip_batch_dry_run)
    except Exception as exc:  # report the failure instead of a traceback with paths
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
