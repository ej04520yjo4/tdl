"""Durable per-chat download-folder option tests."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from desktop.backend import DownloadRequest, Runner, Store


class _CaptureRunner(Runner):
    """Run a no-op child while retaining the real argument list."""

    def __init__(self, store: Store) -> None:
        super().__init__(store, executable=sys.executable)
        self.requests: list[DownloadRequest] = []
        self.arguments: list[list[str]] = []

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        self.arguments.append(super()._build_args(request, runtime))
        return [sys.executable, "-c", "import sys;sys.exit(0)"]


class GroupByChatBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-group-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("分組測試帳號")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_new_job_and_direct_request_default_to_chat_folder(self) -> None:
        job = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/193"],
        )
        self.assertTrue(job.group_by_chat)
        raw = json.loads(self.store._queue_path.read_text(encoding="utf-8"))
        self.assertTrue(raw["items"][0]["group_by_chat"])

        runner = Runner(self.store, executable=sys.executable)
        request = DownloadRequest(self.account.id, ["https://t.me/telegram/194"])
        self.assertTrue(request.group_by_chat)
        args = runner._build_args(request, self.store.account_root(self.account.id) / "runtime")
        self.assertIn("--group-by-chat", args)

        explicit_flat = DownloadRequest(
            self.account.id,
            ["https://t.me/telegram/195"],
            group_by_chat=False,
        )
        flat_args = runner._build_args(
            explicit_flat,
            self.store.account_root(self.account.id) / "runtime",
        )
        self.assertNotIn("--group-by-chat", flat_args)

    def test_start_download_persists_and_forwards_new_default(self) -> None:
        runner = _CaptureRunner(self.store)
        result = runner.start_download(
            self.account.id,
            ["https://t.me/telegram/196"],
        ).wait(timeout=5)
        self.assertIsNotNone(result)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(runner.requests), 1)
        self.assertTrue(runner.requests[0].group_by_chat)
        self.assertIn("--group-by-chat", runner.arguments[0])
        saved = self.store.get_job(result.job_id)
        self.assertTrue(saved.group_by_chat)

    def test_resume_uses_persisted_new_grouping_option(self) -> None:
        job = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/198"],
        )
        runner = _CaptureRunner(self.store)
        result = runner.resume_download(job).wait(timeout=5)
        self.assertIsNotNone(result)
        self.assertTrue(result.success, result.error)
        self.assertTrue(runner.requests[0].group_by_chat)
        self.assertIn("--group-by-chat", runner.arguments[0])

    def test_legacy_manifest_defaults_false_and_stays_flat_on_resume(self) -> None:
        job = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/197"],
            output_dir=self.root / "downloads",
        )
        queue_path = self.store._queue_path
        history_path = self.store._history_path
        for path in (queue_path, history_path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload["items"]:
                if item.get("id") == job.id:
                    item.pop("group_by_chat", None)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        reopened = Store(self.root)
        legacy = reopened.get_job(job.id)
        self.assertFalse(legacy.group_by_chat)

        runner = _CaptureRunner(reopened)
        result = runner.resume_download(legacy).wait(timeout=5)
        self.assertIsNotNone(result)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(runner.requests), 1)
        self.assertFalse(runner.requests[0].group_by_chat)
        self.assertNotIn("--group-by-chat", runner.arguments[0])

        saved = reopened.get_job(job.id)
        self.assertEqual(saved.output_dir, str((self.root / "downloads").resolve()))
        self.assertFalse(saved.group_by_chat)
        self.assertEqual(len(reopened.list_history()), 1)
        self.assertEqual(len(reopened.list_queue()), 1)
        for path in (reopened._queue_path, reopened._history_path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("group_by_chat", payload["items"][0])


if __name__ == "__main__":
    unittest.main()
