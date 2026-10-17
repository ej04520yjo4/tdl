"""Durable queue and graceful pause/resume contract tests."""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from desktop.backend import (
    EVENT_PREFIX,
    DownloadRequest,
    ExportRequest,
    ListTopicsRequest,
    Runner,
    Store,
    ValidationError,
    normalize_event,
    validate_topic_id,
)


class _FixtureRunner(Runner):
    def __init__(self, store: Store, script: str) -> None:
        super().__init__(store, executable=sys.executable)
        self.script = script
        self.requests = []

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


class ResumeBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-resume-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("可恢復帳號")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_progress_event_preserves_file_identity_and_state(self) -> None:
        event = normalize_event(
            {
                "event": "progress",
                "percent": 42,
                "current": 1024,
                "total": 2048,
                "filename": "video.mp4\n",
                "file_id": "message-123",
                "state": "downloading",
            }
        )
        self.assertEqual(event["file_id"], "message-123")
        self.assertEqual(event["state"], "downloading")
        self.assertEqual(event["percent"], 42)
        self.assertIsInstance(event["filename"], str)
        failed = normalize_event(
            {"event": "error", "message": "failed", "file_id": "message-123", "state": "failed"}
        )
        self.assertEqual(failed["file_id"], "message-123")
        self.assertEqual(failed["state"], "failed")

    def test_worker_persists_per_file_progress_and_error_state(self) -> None:
        script = f"""
import json
import sys
prefix = {EVENT_PREFIX!r}
def emit(value):
    print(prefix + json.dumps(value), flush=True)
emit({{'event':'progress','file_id':'1','state':'downloading','current':3,'total':10,'percent':30,'filename':'one.mp4'}})
emit({{'event':'progress','file_id':'1','state':'completed','current':10,'total':10,'percent':100,'filename':'one.mp4'}})
emit({{'event':'progress','file_id':'2','state':'downloading','current':1,'total':8,'percent':12.5,'filename':'two.mp4'}})
emit({{'event':'error','file_id':'2','state':'failed','message':'media download failed'}})
sys.exit(1)
"""
        runner = _FixtureRunner(self.store, script)
        result = runner.start_download(
            self.account.id,
            ["https://t.me/telegram/202"],
            title="每檔進度",
        ).wait(timeout=5)
        self.assertFalse(result.success)
        saved = self.store.get_job(result.job_id)
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.file_progress["1"]["state"], "completed")
        self.assertEqual(saved.file_progress["1"]["current"], 10)
        self.assertEqual(saved.file_progress["2"]["state"], "failed")
        self.assertEqual(saved.file_progress["2"]["filename"], "two.mp4")
        self.assertEqual(saved.file_progress["2"]["percent"], 12.5)
        reopened = Store(self.root)
        self.assertEqual(reopened.get_job(saved.id).file_progress, saved.file_progress)

    def test_forum_topic_id_is_validated_and_forwarded_to_core(self) -> None:
        self.assertEqual(validate_topic_id("7"), 7)
        self.assertIsNone(validate_topic_id(None))
        with self.assertRaises(ValidationError):
            validate_topic_id("0")
        with self.assertRaises(ValidationError):
            validate_topic_id(str(2**31))
        self.assertEqual(validate_topic_id(str(2**31 - 1)), 2**31 - 1)

        runner = _FixtureRunner(self.store, "import sys;sys.exit(0)")
        output = self.store.account_root(self.account.id) / "exports" / "topic.json"
        request = ExportRequest(
            account_id=self.account.id,
            chat_id="channel:123",
            start_date="2026-01-01",
            end_date="2026-01-01",
            media_types=("video",),
            topic_id=7,
            output_path=str(output),
        )
        args = Runner._build_args(runner, request, self.store.account_root(self.account.id) / "runtime")
        self.assertEqual(args[-2:], ["--topic", "7"])

    def test_list_topics_filters_typed_chat_without_download_history(self) -> None:
        script = (
            "import json;"
            "print(" + repr(EVENT_PREFIX) + "+json.dumps({"
            "'event':'topics','chat_ref':'channel:123','topics':["
            "{'id':1,'title':'一般'},{'id':7,'title':'公告'}]}),flush=True)"
        )
        runner = _FixtureRunner(self.store, script)
        result = runner.run(ListTopicsRequest(self.account.id, "channel:123"))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data["topics"][1]["id"], "7")
        self.assertEqual(self.store.list_history(), [])

        request = ListTopicsRequest(self.account.id, "channel:123")
        args = Runner._build_args(runner, request, self.store.account_root(self.account.id) / "runtime")
        self.assertEqual(args[-6:], ["chat", "topics", "--chat", "channel:123", "--output", "json"])

    def test_reopen_marks_crash_running_as_interrupted_without_network(self) -> None:
        job = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/193"],
            output_dir=self.root / "downloads",
            continue_download=True,
            skip_same=True,
            group=False,
            concurrency=3,
            title="頻道｜2026-03-01～2026-03-02",
        )
        self.store.mark_job_running(job.id)
        reopened = Store(self.root)

        restored = reopened.get_job(job.id)
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual(restored.account_id, self.account.id)
        self.assertEqual(restored.links, ["https://t.me/telegram/193"])
        self.assertEqual(restored.output_dir, str((self.root / "downloads").resolve()))
        self.assertFalse(restored.group)
        self.assertEqual(restored.concurrency, 3)
        self.assertEqual(restored.title, "頻道｜2026-03-01～2026-03-02")
        self.assertEqual([item.id for item in reopened.list_resumable_jobs()], [job.id])

    def test_external_json_manifest_snapshot_survives_source_deletion(self) -> None:
        source = self.root / "temporary-export.json"
        source.write_text(json.dumps({"id": -100123, "messages": []}), encoding="utf-8")
        job = self.store.create_download_job(self.account.id, [], files=[source])

        self.assertEqual(job.files, [str(source.resolve())])
        self.assertEqual(len(job.file_snapshots), 1)
        snapshot = Path(job.file_snapshots[0])
        self.assertTrue(snapshot.is_file())
        self.assertEqual(snapshot.parent.parent.parent, self.store.account_root(self.account.id) / "jobdata")
        source.unlink()

        runner = _FixtureRunner(self.store, "import sys;sys.exit(0)")
        result = runner.resume_download(self.store.get_job(job.id)).wait(timeout=5)
        self.assertTrue(result.success, result.error)
        self.assertEqual(runner.requests[-1].files, (str(snapshot),))
        self.assertEqual(self.store.get_job(job.id).status, "completed")

    def test_pause_is_distinct_from_cancel_and_resume_reuses_same_manifest(self) -> None:
        marker = self.root / "checkpoint.txt"
        self_script = f"""
import json
import sys
from pathlib import Path
print({EVENT_PREFIX!r} + json.dumps({{'event':'progress','percent':31,'current':31,'total':100,'filename':'part.bin'}}), flush=True)
for line in sys.stdin:
    if json.loads(line).get('command') == 'cancel':
        Path({str(marker)!r}).write_text('checkpoint', encoding='utf-8')
        raise SystemExit(0)
"""
        runner = _FixtureRunner(self.store, self_script)
        handle = runner.start_download(
            self.account.id,
            ["https://t.me/telegram/193"],
            output_dir=self.root / "downloads",
            continue_download=True,
            skip_same=False,
            group=False,
            concurrency=4,
            title="可恢復頻道｜日期範圍",
        )
        self.assertIsNotNone(handle.get_event(timeout=5))
        self.assertTrue(handle.pause())
        paused = handle.wait(timeout=5)
        self.assertIsNotNone(paused)
        self.assertFalse(paused.success)
        self.assertFalse(paused.cancelled)
        self.assertTrue(paused.paused)
        self.assertTrue(marker.exists())

        saved = self.store.get_job(paused.job_id)
        self.assertEqual(saved.status, "paused")
        self.assertEqual(saved.title, "可恢復頻道｜日期範圍")
        self.assertFalse(saved.skip_same)
        self.assertEqual(saved.concurrency, 4)

        # The next core invocation exits successfully.  It must use the
        # persisted manifest and update the same queue/history row.
        runner.script = "import sys;sys.exit(0)"
        resumed_handle = runner.resume_download(saved)
        resumed = resumed_handle.wait(timeout=5)
        self.assertTrue(resumed.success, resumed.error)
        self.assertFalse(resumed.paused)
        self.assertEqual(resumed.job_id, job_id := saved.id)
        self.assertEqual(len(self.store.list_history()), 1)
        self.assertEqual(len(self.store.list_queue()), 1)
        self.assertEqual(self.store.get_job(job_id).status, "completed")
        request = runner.requests[-1]
        self.assertEqual(request.job_id, job_id)
        self.assertEqual(list(request.links), saved.links)
        self.assertEqual(request.output_dir, saved.output_dir)
        self.assertTrue(request.continue_download)
        self.assertTrue(request.skip_same)
        self.assertEqual(request.group, saved.group)
        self.assertEqual(request.concurrency, saved.concurrency)
        self.assertEqual(request.title, saved.title)

        # A direct existing-job start still honors explicit options supplied
        # by a fresh UI request instead of silently changing the user's
        # initial download policy.
        fresh = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/201"],
            continue_download=False,
            skip_same=False,
        )
        explicit = runner.start(
            DownloadRequest(
                account_id=self.account.id,
                links=fresh.links,
                files=fresh.files,
                output_dir=fresh.output_dir,
                continue_download=False,
                skip_same=False,
                job_id=fresh.id,
            )
        )
        self.assertTrue(explicit.wait(timeout=5).success)
        self.assertFalse(runner.requests[-1].continue_download)
        self.assertFalse(runner.requests[-1].skip_same)

    def test_explicit_cancel_stays_cancelled_and_is_not_resumable(self) -> None:
        runner = _FixtureRunner(
            self.store,
            "import json,sys\nfor line in sys.stdin:\n\n    if json.loads(line).get('command') == 'cancel': sys.exit(0)\n",
        )
        handle = runner.start_download(self.account.id, ["https://t.me/telegram/194"])
        self.assertTrue(handle.cancel())
        result = handle.wait(timeout=5)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.paused)
        self.assertEqual(self.store.get_job(result.job_id).status, "cancelled")
        self.assertEqual(self.store.list_resumable_jobs(), [])

    def test_queue_manifest_is_persisted_before_account_lock_and_pause_keeps_it(self) -> None:
        runner = _FixtureRunner(self.store, "import time;time.sleep(0.2)")
        account_lock = runner._account_lock(self.account.id)
        self.assertTrue(account_lock.acquire(blocking=False))
        try:
            handle = runner.start_download(
                self.account.id,
                ["https://t.me/telegram/195"],
                output_dir=self.root / "downloads",
                title="尚未取得鎖的工作",
            )
            queued = self.store.list_resumable_jobs()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].status, "queued")
            self.assertEqual(queued[0].title, "尚未取得鎖的工作")
            self.assertTrue(handle.pause())
            result = handle.wait(timeout=5)
            self.assertTrue(result.paused)
            self.assertEqual(self.store.get_job(queued[0].id).status, "paused")
        finally:
            account_lock.release()

    def test_resumable_status_filter_excludes_finished_jobs(self) -> None:
        queued = self.store.create_download_job(self.account.id, ["https://t.me/telegram/196"])
        interrupted = self.store.create_download_job(self.account.id, ["https://t.me/telegram/197"])
        paused = self.store.create_download_job(self.account.id, ["https://t.me/telegram/198"])
        cancelled = self.store.create_download_job(self.account.id, ["https://t.me/telegram/199"])
        completed = self.store.create_download_job(self.account.id, ["https://t.me/telegram/200"])
        self.store.mark_job_running(interrupted.id)
        self.store.finish_job(paused.id, "paused", error="暫停")
        self.store.finish_job(cancelled.id, "cancelled", error="取消")
        self.store.finish_job(completed.id, "completed")
        self.assertEqual(
            {job.id for job in self.store.list_resumable_jobs()},
            {queued.id, interrupted.id, paused.id},
        )
        with self.assertRaises(ValidationError):
            _FixtureRunner(self.store, "import sys;sys.exit(0)").resume_download(
                self.store.get_job(completed.id)
            )


if __name__ == "__main__":
    unittest.main()
