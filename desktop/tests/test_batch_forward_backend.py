"""Offline contracts for scan-manifest batch forwarding.

These tests use a Python fixture in place of the Go core.  They exercise the
account/path/hash/count boundary without logging in to Telegram or sending a
real message.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from desktop.backend import (
    EVENT_PREFIX,
    ExportRequest,
    ForwardRequest,
    Runner,
    Store,
    ValidationError,
)


class _FixtureRunner(Runner):
    def __init__(self, store: Store, *, exit_code: int = 0) -> None:
        super().__init__(store, executable=sys.executable)
        self.exit_code = exit_code
        self.built: list[list[str]] = []
        self.snapshot_bytes: bytes | None = None
        self.export_path: Path | None = None

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        args = super()._build_args(request, runtime)
        self.built.append(args)
        if isinstance(request, ForwardRequest):
            source = Path(args[args.index("--from") + 1])
            if source.is_file():
                self.snapshot_bytes = source.read_bytes()
            event = {"event": "status", "count": request.expected_count or 0}
            script = (
                "import json,sys; "
                f"print({EVENT_PREFIX!r}+json.dumps({event!r}), flush=True); "
                f"sys.exit({self.exit_code})"
            )
            return [sys.executable, "-c", script]
        if isinstance(request, ExportRequest):
            self.export_path = Path(request.output_path or "")
            self.export_path.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "peer_type": "channel",
                        "messages": [
                            {"id": 11, "file": "one.mp4", "size": 2},
                            {"id": 12, "file": "two.jpg", "size": 1},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            event = {
                "event": "exported",
                "path": str(self.export_path),
                "count": 2,
                "total_bytes": 3,
            }
            script = f"import json; print({EVENT_PREFIX!r}+json.dumps({event!r}), flush=True)"
            return [sys.executable, "-c", script]
        return [sys.executable, "-c", "pass"]


class BatchForwardBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-batch-forward-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("批次轉傳測試")
        self.runner = _FixtureRunner(self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def manifest(
        self,
        account_id: str,
        *,
        source_chat: str = "channel:123",
        messages: list[dict] | None = None,
        name: str = "scan.json",
    ) -> tuple[Path, str, int]:
        exports = self.runner._exports_root(account_id)
        path = exports / name
        payload = {
            "id": 123,
            "peer_type": "channel",
            "source_chat": source_chat,
            "messages": messages if messages is not None else [
                {"id": 11, "file": "one.mp4", "size": 2},
                {"id": 12, "file": "two.jpg", "size": 1},
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest, len(payload["messages"])

    @staticmethod
    def arg_values(args: list[str], flag: str) -> list[str]:
        return [args[index + 1] for index, value in enumerate(args[:-1]) if value == flag]

    def test_batch_forward_uses_exact_snapshot_ids_single_and_cleans_snapshot(self) -> None:
        path, digest, count = self.manifest(self.account.id)
        handle = self.runner.start_batch_forward(
            self.account.id,
            path,
            source_chat="channel:123",
            to="chat:456",
            manifest_sha256=digest,
            expected_count=count,
            topic_id=7,
            silent=True,
            dry_run=True,
        )
        # Mutating the original after the call cannot affect the queued copy.
        path.write_text('{"id":123,"source_chat":"channel:123","messages":[]}', encoding="utf-8")
        result = handle.wait(timeout=10)

        self.assertIsNotNone(result)
        self.assertTrue(result.success, result.error if result else "")
        args = self.runner.built[-1]
        sources = self.arg_values(args, "--from")
        self.assertEqual(len(sources), 1)
        self.assertNotEqual(Path(sources[0]), path.resolve())
        self.assertIn(b'"id": 11', self.runner.snapshot_bytes or b"")
        self.assertIn("--single", args)
        self.assertIn("--dry-run", args)
        self.assertIn("--silent", args)
        self.assertEqual(self.arg_values(args, "--topic"), ["7"])
        self.assertEqual(self.arg_values(args, "--to"), ["chat:456"])
        self.assertFalse(Path(sources[0]).exists())
        self.assertEqual(result.data["expected_count"], count)
        self.assertEqual(result.data["source_chat"], "channel:123")

    def test_batch_forward_rejects_cross_account_and_outside_manifests(self) -> None:
        other = self.store.add_account("另一個帳號")
        other_path, other_hash, other_count = self.manifest(other.id, name="other.json")
        with self.assertRaises(ValidationError):
            self.runner.start_batch_forward(
                self.account.id,
                other_path,
                source_chat="channel:123",
                to="chat:456",
                manifest_sha256=other_hash,
                expected_count=other_count,
            )

        outside = self.root / "outside.json"
        outside.write_text(
            json.dumps({"id": 123, "source_chat": "channel:123", "messages": [{"id": 1}]}),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            self.runner.start_batch_forward(
                self.account.id,
                outside,
                source_chat="channel:123",
                to="chat:456",
                manifest_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                expected_count=1,
            )

    def test_batch_forward_rejects_changed_empty_same_source_and_invalid_targets(self) -> None:
        path, digest, count = self.manifest(self.account.id)

        cases = [
            {"manifest_sha256": "0" * 64, "expected_count": count, "source_chat": "channel:123", "to": "chat:456"},
            {"manifest_sha256": digest, "expected_count": count + 1, "source_chat": "channel:123", "to": "chat:456"},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "channel:999", "to": "chat:456"},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "channel:123", "to": "channel:123"},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "channel:123", "to": "channel:00123"},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "group:123", "to": "chat:123"},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "channel:123", "to": 'From.ID == 1 ? "x" : "y"'},
            {"manifest_sha256": digest, "expected_count": count, "source_chat": "channel:123", "to": "user:789"},
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValidationError):
                    self.runner.start_batch_forward(self.account.id, path, **case)

        mismatched_type = self.runner._exports_root(self.account.id) / "mismatched-type.json"
        mismatched_type.write_text(
            json.dumps(
                {
                    "id": 123,
                    "peer_type": "chat",
                    "source_chat": "channel:123",
                    "messages": [{"id": 1, "size": 1}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValidationError):
            self.runner.start_batch_forward(
                self.account.id,
                mismatched_type,
                source_chat="channel:123",
                to="chat:456",
                manifest_sha256=hashlib.sha256(mismatched_type.read_bytes()).hexdigest(),
                expected_count=1,
            )

        empty, empty_hash, _ = self.manifest(self.account.id, name="empty.json", messages=[])
        with self.assertRaises(ValidationError):
            self.runner.start_batch_forward(
                self.account.id,
                empty,
                source_chat="channel:123",
                to="chat:456",
                manifest_sha256=empty_hash,
                expected_count=0,
            )

    def test_direct_forward_request_cannot_bypass_snapshot_boundary(self) -> None:
        path, digest, count = self.manifest(self.account.id)
        request = ForwardRequest(
            self.account.id,
            [str(path)],
            to="chat:456",
            single=False,
            batch_manifest=True,
            source_chat="channel:123",
            manifest_sha256=digest,
            expected_count=count,
        )
        with self.assertRaises(ValidationError):
            self.runner.start(request)
        self.assertTrue(request.single)

    def test_queued_snapshot_is_rechecked_before_core_starts(self) -> None:
        path, digest, count = self.manifest(self.account.id, name="queued.json")
        account_lock = self.runner._account_lock(self.account.id)
        self.assertTrue(account_lock.acquire(blocking=False))
        try:
            handle = self.runner.start_batch_forward(
                self.account.id,
                path,
                source_chat="channel:123",
                to="chat:456",
                manifest_sha256=digest,
                expected_count=count,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                event = handle.get_event(timeout=0.1)
                if event and event.get("event") == "status":
                    break
            snapshots = list((self.store.account_root(self.account.id) / "forward-snapshots").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            snapshots[0].write_text(
                json.dumps(
                    {
                        "id": 123,
                        "peer_type": "channel",
                        "source_chat": "channel:123",
                        "messages": [{"id": 99, "size": 1}, {"id": 12, "size": 1}],
                    }
                ),
                encoding="utf-8",
            )
        finally:
            account_lock.release()
        result = handle.wait(timeout=10)
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertIn("內容已變更", result.error or "")
        self.assertEqual(self.runner.built, [])
        self.assertEqual(list((self.store.account_root(self.account.id) / "forward-snapshots").glob("*.json")), [])

    def test_legacy_forward_accepts_topic_flag_without_batch_provenance(self) -> None:
        handle = self.runner.start_forward(
            self.account.id,
            ["https://t.me/telegram/193"],
            to="chat:456",
            topic_id=9,
        )
        result = handle.wait(timeout=10)
        self.assertTrue(result.success, result.error if result else "")
        args = self.runner.built[-1]
        self.assertEqual(self.arg_values(args, "--topic"), ["9"])
        self.assertEqual(self.arg_values(args, "--single"), [])

    def test_export_result_contains_sha_and_source_binding(self) -> None:
        runner = self.runner
        result = runner.start_export(
            self.account.id,
            "channel:123",
            "2026-01-01",
            "2026-01-31",
            ["video"],
        ).wait(timeout=10)
        self.assertTrue(result.success, result.error if result else "")
        self.assertIsNotNone(runner.export_path)
        payload = json.loads(runner.export_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(runner.export_path.read_bytes()).hexdigest()
        self.assertEqual(result.data["sha256"], digest)
        self.assertEqual(payload["source_chat"], "channel:123")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total_bytes"], 3)

    def test_failed_batch_forward_also_removes_snapshot(self) -> None:
        path, digest, count = self.manifest(self.account.id, name="failed.json")
        runner = _FixtureRunner(self.store, exit_code=1)
        result = runner.start_batch_forward(
            self.account.id,
            path,
            source_chat="channel:123",
            to="chat:456",
            manifest_sha256=digest,
            expected_count=count,
        ).wait(timeout=10)
        self.assertFalse(result.success)
        snapshot = Path(self.arg_values(runner.built[-1], "--from")[0])
        self.assertFalse(snapshot.exists())


if __name__ == "__main__":
    unittest.main()
