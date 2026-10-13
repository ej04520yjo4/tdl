"""1.3.0 backend contracts: advanced download options, throttled progress
persistence, queue hygiene, and the upload/forward/backup operations."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop.backend import (
    DEFAULT_TEMPLATE,
    EVENT_PREFIX,
    BackupRequest,
    DownloadRequest,
    ForwardRequest,
    ListChatsRequest,
    RecoverRequest,
    Runner,
    Store,
    UploadRequest,
    ValidationError,
    WhoamiRequest,
    normalize_download_advanced,
    normalize_extensions,
    upload_caption_expression,
    validate_destination,
    validate_template,
)


class _FixtureRunner(Runner):
    """Run a small Python script in place of the Go core."""

    def __init__(self, store: Store, script: str = "") -> None:
        super().__init__(store, executable=sys.executable)
        self.script = script
        self.built: list[list[str]] = []

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        self.built.append(super()._build_args(request, runtime))
        encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


def _emit_script(*events: dict, exit_code: int = 0) -> str:
    lines = ["import json, sys", f"prefix = {EVENT_PREFIX!r}"]
    lines += [f"print(prefix + json.dumps({event!r}), flush=True)" for event in events]
    lines.append(f"sys.exit({exit_code})")
    return "\n".join(lines)


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-transfer-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("傳輸測試")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def flag_value(self, args: list[str], flag: str) -> str:
        return args[args.index(flag) + 1]


class AdvancedOptionTests(_Workspace):
    def test_extensions_are_normalized_and_validated(self) -> None:
        self.assertEqual(normalize_extensions(" .MP4, mkv；mp4 "), ["mp4", "mkv"])
        self.assertEqual(normalize_extensions(["Zip", ".7z"]), ["zip", "7z"])
        self.assertEqual(normalize_extensions(""), [])
        with self.assertRaises(ValidationError):
            normalize_extensions("mp4, ../x")

    def test_template_rejects_paths_and_keeps_core_default_empty(self) -> None:
        self.assertEqual(validate_template(""), "")
        self.assertEqual(validate_template(DEFAULT_TEMPLATE), "")
        dated = '{{ formatDate .MessageDate "20060102" }}_{{ .MessageID }}_{{ filenamify .FileName }}'
        self.assertEqual(validate_template(dated), dated)
        for bad in ("plain-name", "..\\{{ .FileName }}", "sub/{{ .FileName }}", "{{ .MessageID }}\n{{ .FileName }}"):
            with self.assertRaises(ValidationError):
                validate_template(bad)

    def test_include_and_exclude_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_download_advanced({"include": "mp4", "exclude": "jpg"})
        with self.assertRaises(ValidationError):
            self.store.update_preferences(include_ext="mp4", exclude_ext="jpg")
        # The rejected value must not have been written.
        self.assertEqual(self.store.get_preferences().include_ext, "")

    def test_preferences_flow_into_download_arguments(self) -> None:
        self.store.update_preferences(
            template="{{ .MessageID }}_{{ filenamify .FileName }}",
            include_ext="MP4, mkv",
            desc=True,
            takeout=True,
            rewrite_ext=True,
            threads=8,
            delay_ms=500,
        )
        runner = _FixtureRunner(self.store, "import sys;sys.exit(0)")
        result = runner.start_download(self.account.id, ["https://t.me/telegram/193"]).wait(timeout=10)
        self.assertIsNotNone(result)
        args = runner.built[-1]
        self.assertEqual(self.flag_value(args, "--template"), "{{ .MessageID }}_{{ filenamify .FileName }}")
        self.assertEqual(self.flag_value(args, "--include"), "mp4,mkv")
        self.assertNotIn("--exclude", args)
        for flag in ("--desc", "--takeout", "--rewrite-ext"):
            self.assertIn(flag, args)
        self.assertEqual(self.flag_value(args, "--threads"), "8")
        self.assertEqual(self.flag_value(args, "--delay"), "500ms")

    def test_default_options_add_no_extra_flags(self) -> None:
        runner = _FixtureRunner(self.store, "import sys;sys.exit(0)")
        runner.start_download(self.account.id, ["https://t.me/telegram/193"]).wait(timeout=10)
        args = runner.built[-1]
        for flag in ("--template", "--include", "--exclude", "--desc", "--takeout", "--rewrite-ext", "--threads", "--delay"):
            self.assertNotIn(flag, args)

    def test_resumed_job_keeps_the_options_it_was_created_with(self) -> None:
        self.store.update_preferences(exclude_ext="jpg", threads=2)
        job = self.store.create_download_job(
            self.account.id,
            ["https://t.me/telegram/193"],
            expected_files=12,
            expected_bytes=3456,
        )
        self.assertEqual(job.advanced["exclude"], ["jpg"])
        self.store.finish_job(job.id, "paused")
        # Changing the settings later must not change a job already queued.
        self.store.update_preferences(exclude_ext="", include_ext="mp4", threads=6)

        reopened = Store(self.root)
        saved = reopened.get_job(job.id)
        self.assertEqual((saved.expected_files, saved.expected_bytes), (12, 3456))
        runner = _FixtureRunner(reopened, "import sys;sys.exit(0)")
        runner.resume_download(saved).wait(timeout=10)
        args = runner.built[-1]
        self.assertEqual(self.flag_value(args, "--exclude"), "jpg")
        self.assertNotIn("--include", args)
        self.assertEqual(self.flag_value(args, "--threads"), "2")

    def test_inherited_core_environment_is_not_passed_on(self) -> None:
        marker = self.root / "environment.json"
        script = (
            "import json, os\n"
            f"open({str(marker)!r}, 'w', encoding='utf-8').write(json.dumps("
            "{k: v for k, v in os.environ.items() if k.upper().startswith('TDL_')}))\n"
        )
        inherited = {"TDL_THREADS": "1", "TDL_PROXY": "http://example.invalid:1", "TDL_DESKTOP_KEEP": "yes"}
        with patch.dict(os.environ, inherited, clear=False):
            _FixtureRunner(self.store, script).run(DownloadRequest(self.account.id, ["https://t.me/telegram/193"]))
        seen = {key.upper(): value for key, value in json.loads(marker.read_text(encoding="utf-8")).items()}
        self.assertNotIn("TDL_THREADS", seen)
        self.assertNotIn("TDL_PROXY", seen)
        self.assertEqual(seen["TDL_GUI"], "1")
        self.assertEqual(seen["TDL_DESKTOP_KEEP"], "yes")


class ProgressPersistenceTests(_Workspace):
    def test_progress_burst_is_not_written_once_per_event(self) -> None:
        # Generated in the child: 420 literal events exceed the Windows
        # command-line limit of this fixture.
        script = f"""
import json
prefix = {EVENT_PREFIX!r}
def emit(file_id, state, current):
    print(prefix + json.dumps({{'event': 'progress', 'file_id': str(file_id), 'state': state,
        'current': current, 'total': 1000, 'percent': current / 10, 'filename': f'{{file_id}}.mp4'}}), flush=True)
for index in range(400):
    emit(index % 20, 'downloading', index)
for index in range(20):
    emit(index, 'completed', 1000)
"""
        runner = _FixtureRunner(self.store, script)
        writes = 0
        original = self.store.update_job_progress

        def counting(job_id, **progress):  # type: ignore[no-untyped-def]
            nonlocal writes
            writes += 1
            return original(job_id, **progress)

        with patch.object(self.store, "update_job_progress", side_effect=counting):
            result = runner.start_download(self.account.id, ["https://t.me/telegram/193"]).wait(timeout=20)
        self.assertTrue(result.success, result.error)
        # 420 events used to mean 420 rewrites of both JSON files.
        self.assertLess(writes, 20)
        raw = json.loads((self.root / "downloads.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual((raw["status"], raw["file_count"], raw["total_bytes"]), ("completed", 20, 20000))

    def test_final_file_states_survive_an_interrupted_run(self) -> None:
        script = _emit_script(
            {"event": "progress", "file_id": "1", "state": "completed", "current": 10, "total": 10, "percent": 100, "filename": "a.mp4"},
            {"event": "progress", "file_id": "2", "state": "downloading", "current": 4, "total": 10, "percent": 40, "filename": "b.mp4"},
            exit_code=1,
        )
        result = _FixtureRunner(self.store, script).start_download(
            self.account.id, ["https://t.me/telegram/193"]
        ).wait(timeout=10)
        saved = Store(self.root).get_job(result.job_id)
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.file_progress["1"]["state"], "completed")
        self.assertEqual(saved.file_progress["2"]["current"], 4)


class QueueHygieneTests(_Workspace):
    def _job(self, status: str) -> str:
        job = self.store.create_download_job(self.account.id, ["https://t.me/telegram/193"])
        self.store.mark_job_running(job.id)
        self.store.update_job_progress(
            job.id,
            file_progress={"1": {"file_id": "1", "state": "completed", "total": 700, "filename": "a.mp4"},
                           "2": {"file_id": "2", "state": "completed", "total": 300, "filename": "b.mp4"}},
        )
        self.store.finish_job(job.id, status)
        return job.id

    def test_completed_job_drops_file_rows_but_keeps_totals(self) -> None:
        job_id = self._job("completed")
        self.assertEqual(self.store.get_job(job_id).file_progress, {})
        raw = json.loads((self.root / "downloads.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual((raw["file_count"], raw["total_bytes"]), (2, 1000))
        # Still listed in this session so a resumed job is not seen twice...
        self.assertEqual(len(self.store.list_queue()), 1)
        # ...and gone from the queue on the next launch, while history stays.
        reopened = Store(self.root)
        self.assertEqual(reopened.list_queue(), [])
        self.assertEqual(len(reopened.list_history()), 1)

    def test_failed_job_keeps_rows_until_cleared(self) -> None:
        failed = self._job("failed")
        paused = self._job("paused")
        self.assertEqual(len(self.store.get_job(failed).file_progress), 2)

        removed = self.store.clear_finished_jobs()
        self.assertEqual(removed, [failed])
        self.assertEqual([job.id for job in self.store.list_queue()], [paused])
        # History remains searchable; only the bulky rows are released.
        self.assertEqual(len(self.store.list_history()), 2)
        self.assertEqual(self.store.get_job(failed).file_progress, {})
        self.assertEqual(len(self.store.get_job(paused).file_progress), 2)

    def test_stale_scan_manifests_are_purged_unless_a_job_needs_them(self) -> None:
        exports = self.store.account_root(self.account.id) / "exports"
        exports.mkdir()
        payload = json.dumps({"id": 1, "messages": []})
        old, kept, fresh = exports / "old.json", exports / "kept.json", exports / "fresh.json"
        for path in (old, kept, fresh):
            path.write_text(payload, encoding="utf-8")
        job = self.store.create_download_job(self.account.id, [], files=[str(kept)])
        self.store.finish_job(job.id, "paused")
        long_ago = 1_000_000_000
        os.utime(old, (long_ago, long_ago))
        os.utime(kept, (long_ago, long_ago))

        self.assertEqual(self.store.purge_stale_exports(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(fresh.exists())


class ChatCacheTests(_Workspace):
    def test_successful_chat_list_is_cached_encrypted(self) -> None:
        chats = [{"id": "-100123", "ref": "channel:123", "type": "channel", "title": "私人頻道名稱", "username": "demo", "topics": []}]
        result = _FixtureRunner(self.store, _emit_script({"event": "chats", "chats": chats})).run(
            ListChatsRequest(self.account.id)
        )
        self.assertTrue(result.success, result.error)
        cached, saved_at = Store(self.root).load_chat_cache(self.account.id)
        self.assertEqual(cached[0]["ref"], "channel:123")
        self.assertEqual(cached[0]["title"], "私人頻道名稱")
        self.assertTrue(saved_at)
        raw = (self.store.account_root(self.account.id) / "chats.dpapi").read_bytes()
        if os.name == "nt":
            self.assertNotIn("私人頻道名稱".encode("utf-8"), raw)

    def test_missing_or_damaged_cache_is_empty(self) -> None:
        self.assertEqual(self.store.load_chat_cache(self.account.id), ([], ""))
        (self.store.account_root(self.account.id) / "chats.dpapi").write_bytes(b"not an envelope")
        self.assertEqual(self.store.load_chat_cache(self.account.id), ([], ""))


class TransferRequestTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.media = self.root / "media"
        self.media.mkdir()
        self.file = self.media / "a,b.mp4"
        self.file.write_bytes(b"x")

    def test_destination_accepts_only_known_reference_forms(self) -> None:
        self.assertEqual(validate_destination(""), "")
        self.assertEqual(validate_destination("Channel:123"), "channel:123")
        self.assertEqual(validate_destination("@telegram"), "telegram")
        self.assertEqual(validate_destination("-100123"), "-100123")
        for bad in ('From.ID == 1 ? "a" : "b"', "https://t.me/telegram", "a b", "bot:1"):
            with self.assertRaises(ValidationError):
                validate_destination(bad)

    def test_caption_expression_is_quoted_and_html_escaped(self) -> None:
        self.assertEqual(upload_caption_expression("none"), '""')
        self.assertEqual(upload_caption_expression("filename"), "FileName + FileExt")
        self.assertEqual(upload_caption_expression("text", ' a<b> "q" '), '"a&lt;b&gt; \\"q\\""')
        self.assertEqual(upload_caption_expression("text", "  "), '""')
        with self.assertRaises(ValidationError):
            upload_caption_expression("expression")

    def test_upload_arguments(self) -> None:
        runner = _FixtureRunner(
            self.store,
            _emit_script(
                {"event": "status", "kind": "upload", "count": 2, "message": "共 2 個檔案等待上傳"},
                {"event": "progress", "file_id": "a", "state": "completed", "current": 1, "total": 1, "percent": 100},
                {"event": "progress", "file_id": "b", "state": "completed", "current": 1, "total": 1, "percent": 100},
            ),
        )
        result = runner.start_upload(
            self.account.id,
            [self.file, self.media],
            chat="channel:123",
            topic_id=7,
            as_photo=True,
            remove_after=True,
            caption_mode="filename",
            exclude="tmp, .part",
            concurrency=3,
        ).wait(timeout=10)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data, {"total": 2, "completed": 2, "failed": 0})
        args = runner.built[-1]
        self.assertIn("up", args)
        # A comma would otherwise split one path into two.
        self.assertIn(f'"{self.file.resolve()}"', args)
        self.assertEqual(self.flag_value(args, "--chat"), "channel:123")
        self.assertEqual(self.flag_value(args, "--topic"), "7")
        self.assertEqual(self.flag_value(args, "--caption"), "FileName + FileExt")
        self.assertEqual(self.flag_value(args, "--exclude"), "tmp,part")
        self.assertEqual(self.flag_value(args, "--limit"), "3")
        self.assertIn("--photo", args)
        self.assertIn("--rm", args)
        self.assertEqual(self.store.list_history(), [])

    def test_upload_to_saved_messages_omits_chat_and_reports_failures(self) -> None:
        runner = _FixtureRunner(
            self.store,
            _emit_script(
                {"event": "status", "count": 1},
                {"event": "error", "file_id": "a", "state": "failed", "message": "CHAT_WRITE_FORBIDDEN"},
                exit_code=1,
            ),
        )
        result = runner.start_upload(self.account.id, [self.file]).wait(timeout=10)
        self.assertFalse(result.success)
        self.assertEqual(result.data["failed"], 1)
        self.assertIn("權限", result.error)
        args = runner.built[-1]
        self.assertNotIn("--chat", args)
        self.assertNotIn("--rm", args)
        self.assertEqual(self.flag_value(args, "--caption"), '""')

    def test_upload_rejects_missing_paths_and_topic_without_chat(self) -> None:
        runner = _FixtureRunner(self.store)
        with self.assertRaises(ValidationError):
            runner.start(UploadRequest(self.account.id, [str(self.root / "missing.bin")]))
        with self.assertRaises(ValidationError):
            runner.start(UploadRequest(self.account.id, [str(self.file)], topic_id=3))
        with self.assertRaises(ValidationError):
            runner.start(UploadRequest(self.account.id, []))

    def test_forward_arguments(self) -> None:
        manifest = self.root / "export.json"
        manifest.write_text(json.dumps({"id": 1, "messages": [{"id": 5}]}), encoding="utf-8")
        runner = _FixtureRunner(self.store, _emit_script({"event": "status", "count": 2}))
        result = runner.start_forward(
            self.account.id,
            ["https://t.me/telegram/193", str(manifest)],
            to="@durov",
            mode="Clone",
            silent=True,
            dry_run=True,
            single=True,
            desc=True,
        ).wait(timeout=10)
        self.assertTrue(result.success, result.error)
        args = runner.built[-1]
        self.assertIn("forward", args)
        sources = [args[index + 1] for index, value in enumerate(args) if value == "--from"]
        self.assertEqual(sources, ["https://t.me/telegram/193", str(manifest.resolve())])
        self.assertEqual(self.flag_value(args, "--to"), "durov")
        self.assertEqual(self.flag_value(args, "--mode"), "clone")
        for flag in ("--silent", "--dry-run", "--single", "--desc"):
            self.assertIn(flag, args)

    def test_forward_rejects_expressions_and_unknown_modes(self) -> None:
        runner = _FixtureRunner(self.store)
        with self.assertRaises(ValidationError):
            runner.start(ForwardRequest(self.account.id, ["https://t.me/telegram/193"], to='"a" + "b"'))
        with self.assertRaises(ValidationError):
            runner.start(ForwardRequest(self.account.id, ["https://t.me/telegram/193"], mode="copy"))
        with self.assertRaises(ValidationError):
            runner.start(ForwardRequest(self.account.id, ["https://t.me/telegram"]))

    def test_backup_and_recover_arguments(self) -> None:
        destination = self.root / "account.tdl"
        runner = _FixtureRunner(self.store, "import sys;sys.exit(0)")
        self.assertTrue(runner.run(BackupRequest(self.account.id, str(destination))).success)
        self.assertEqual(runner.built[-1][-3:], ["backup", "--dst", str(destination.resolve())])

        with self.assertRaises(ValidationError):
            runner.start(BackupRequest(self.account.id, str(self.root / "account.zip")))
        with self.assertRaises(ValidationError):
            runner.start(RecoverRequest(self.account.id, str(destination)))  # does not exist yet

        destination.write_bytes(b"backup")
        self.assertTrue(runner.run(RecoverRequest(self.account.id, str(destination))).success)
        self.assertEqual(runner.built[-1][-3:], ["recover", "--file", str(destination.resolve())])

    def test_whoami_marks_a_restored_account_as_logged_in(self) -> None:
        self.assertFalse(self.store.get_account(self.account.id).authenticated)
        runner = _FixtureRunner(
            self.store,
            _emit_script({"event": "authenticated", "id": "42", "username": "restored", "name": "Restored User"}),
        )
        result = runner.run(WhoamiRequest(self.account.id))
        self.assertTrue(result.success, result.error)
        self.assertEqual(runner.built[-1][-1], "whoami")
        saved = self.store.get_account(self.account.id)
        self.assertTrue(saved.authenticated)
        self.assertEqual((saved.user_id, saved.username), ("42", "restored"))

    def test_whoami_without_identity_does_not_log_in(self) -> None:
        result = _FixtureRunner(self.store, "import sys;sys.exit(0)").run(WhoamiRequest(self.account.id))
        self.assertFalse(result.success)
        self.assertFalse(self.store.get_account(self.account.id).authenticated)


if __name__ == "__main__":
    unittest.main()
