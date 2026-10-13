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
    EVENT_PREFIX,
    DownloadJob,
    DownloadRequest,
    ExportRequest,
    LoginRequest,
    ListChatsRequest,
    Runner,
    SessionVault,
    Store,
    StoreError,
    ValidationError,
    normalize_event,
    validate_export_window,
    validate_export_file,
    validate_chat_id,
    validate_message_link,
    validate_media_types,
)


class _WorkspaceStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="tdl-desktop-test-", dir=Path(__file__).parent))
        self.store = Store(self.temp_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


class StoreTests(_WorkspaceStore):
    def test_accounts_preferences_and_history_survive_reopen(self) -> None:
        account = self.store.add_account("我的帳號", user_id=123, username="@alice")
        self.store.set_active_account(account.id)
        self.store.update_preferences(concurrency=4, skip_same=False)
        job = self.store.create_download_job(
            account.id,
            ["https://t.me/telegram/193"],
            output_dir=self.temp_root / "downloads",
            continue_download=True,
            skip_same=False,
            group=True,
            title="台灣攝影頻道｜2026-01-01～2026-01-31",
        )
        self.store.mark_job_running(job.id)
        self.store.update_job_progress(job.id, percent=45, current=4, total=9, filename="a.txt")
        self.store.finish_job(job.id, "completed")

        reopened = Store(self.temp_root)
        self.assertEqual(reopened.get_active_account().id, account.id)
        self.assertEqual(reopened.get_account(account.id).username, "alice")
        self.assertEqual(reopened.get_preferences().concurrency, 4)
        history = reopened.list_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].status, "completed")
        self.assertEqual(history[0].percent, 45)
        self.assertEqual(history[0].title, "台灣攝影頻道｜2026-01-01～2026-01-31")

    def test_download_title_is_optional_and_filename_cannot_replace_it(self) -> None:
        account = self.store.add_account("標題測試")
        job = self.store.create_download_job(
            account.id,
            ["https://t.me/telegram/193"],
            title="頻道｜2026-02-01～2026-02-02",
        )
        self.store.update_job_progress(job.id, filename="photo.jpg")
        restored = self.store.get_job(job.id)
        self.assertEqual(restored.title, "頻道｜2026-02-01～2026-02-02")
        self.assertEqual(restored.to_dict()["title"], "頻道｜2026-02-01～2026-02-02")

        legacy = DownloadJob.from_dict(
            {
                "id": "legacy",
                "account_id": account.id,
                "links": ["https://t.me/telegram/194"],
                "files": [],
                "output_dir": str(self.temp_root),
                "continue_download": True,
                "skip_same": True,
                "group": True,
                "concurrency": 2,
                "filename": "legacy.jpg",
            }
        )
        self.assertEqual(legacy.title, "")

    def test_running_jobs_are_recovered_as_interrupted(self) -> None:
        account = self.store.add_account("帳號")
        job = self.store.create_download_job(account.id, ["https://t.me/telegram/193"])
        self.store.mark_job_running(job.id)
        reopened = Store(self.temp_root)
        self.assertEqual(reopened.get_job(job.id).status, "interrupted")
        self.assertIn("尚未完成", reopened.get_job(job.id).error)

    def test_remove_account_is_confined_to_uuid_directory(self) -> None:
        account = self.store.add_account("待刪除")
        outside = self.temp_root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.store.remove_account(account.id)
        self.assertFalse((self.temp_root / "accounts" / account.id).exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        with self.assertRaises(Exception):
            self.store.account_root("../../outside")

    def test_export_contains_json_download_records(self) -> None:
        account = self.store.add_account("匯出")
        self.store.create_download_job(account.id, ["https://t.me/c/1697797156/151"])
        destination = self.temp_root / "export" / "downloads.json"
        self.store.export_downloads(destination)
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(len(data["downloads"]), 1)
        self.assertEqual(data["downloads"][0]["links"], ["https://t.me/c/1697797156/151"])

    def test_json_export_file_is_persisted_as_a_safe_file_argument(self) -> None:
        account = self.store.add_account("匯入")
        source = self.temp_root / "telegram-export.json"
        source.write_text(json.dumps({"id": -100123, "messages": []}), encoding="utf-8")
        job = self.store.create_download_job(account.id, [], files=[source])
        self.assertEqual(job.files, [str(source.resolve())])
        self.assertEqual(self.store.get_job(job.id).files, [str(source.resolve())])

    def test_json_export_schema_is_checked_before_network(self) -> None:
        account = self.store.add_account("匯入驗證")
        source = self.temp_root / "invalid-export.json"
        source.write_text(json.dumps({"id": "not-a-chat", "messages": []}), encoding="utf-8")
        marker = self.temp_root / "child-started.txt"
        runner = RunnerTests.FakeRunner(self.store, f"from pathlib import Path;Path({str(marker)!r}).write_text('started')")
        with self.assertRaises(ValidationError):
            runner.start_download(account.id, [], files=[source])
        self.assertFalse(marker.exists())

    def test_empty_export_file_is_rejected_before_download_child(self) -> None:
        account = self.store.add_account("空匯出")
        source = self.temp_root / "empty-export.json"
        source.write_text("", encoding="utf-8")
        with self.assertRaises(ValidationError):
            validate_export_file(source)
        runner = RunnerTests.FakeRunner(self.store, "import sys;sys.exit(0)")
        with self.assertRaises(ValidationError):
            runner.start_download(account.id, [], files=[source])

    def test_proxy_credentials_are_not_written_as_plain_preferences(self) -> None:
        self.store.update_preferences(proxy="socks5://alice:secret@example.test:1080")
        raw = (self.temp_root / "preferences.json").read_text(encoding="utf-8")
        self.assertNotIn("alice:secret", raw)
        self.assertEqual(self.store.get_preferences().proxy, "socks5://alice:secret@example.test:1080")


class LinkAndProtocolTests(unittest.TestCase):
    def test_message_links_allow_public_private_and_topics(self) -> None:
        accepted = {
            "https://t.me/telegram/193": "https://t.me/telegram/193",
            "https://www.t.me/c/1697797156/151": "https://t.me/c/1697797156/151",
            "https://t.me/c/1492447836/251015/251021": "https://t.me/c/1492447836/251015/251021",
            "https://t.me/myhostloc/1485524?thread=1485523": "https://t.me/myhostloc/1485524?thread=1485523",
            "https://t.me/s/telegram/193?single": "https://t.me/telegram/193",
        }
        for value, expected in accepted.items():
            self.assertEqual(validate_message_link(value), expected)

    def test_message_links_reject_non_message_and_arbitrary_hosts(self) -> None:
        rejected = [
            "http://t.me/telegram/193",
            "https://evil.example/telegram/193",
            "https://t.me/+invitehash",
            "https://t.me/joinchat/AAAAAE",
            "https://t.me/telegram/0",
            "https://t.me/telegram/193?exec=whoami",
            "https://t.me/telegram/193?single=0",
            "https://t.me/telegram/193#fragment",
        ]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_message_link(value)

    def test_channel_home_link_explains_channel_download_flow(self) -> None:
        with self.assertRaisesRegex(ValidationError, "頻道／群組首頁") as raised:
            validate_message_link("https://t.me/examplechannel")
        self.assertIn("頻道下載", str(raised.exception))
        with self.assertRaisesRegex(ValidationError, "邀請連結"):
            validate_message_link("https://t.me/+invitehash")

    def test_protocol_normalizes_auth_and_redacts_secrets(self) -> None:
        from desktop.backend import normalize_event, sanitize_text

        event = normalize_event({"event": "authenticated", "id": 42, "username": "@a", "name": "A"})
        self.assertEqual(event, {"event": "authenticated", "id": "42", "username": "a", "name": "A"})
        text = sanitize_text("proxy=socks5://alice:secret@example.test:1080 phone=+886 912 345 678")
        self.assertNotIn("secret", text)
        self.assertNotIn("912 345 678", text)

    def test_protocol_normalizes_chat_and_export_events(self) -> None:
        chats = normalize_event(
            {
                "event": "chats",
                "partial": True,
                "message": "部分對話無法讀取",
                "skipped": 2,
                "chats": [
                    {
                        "id": -100123,
                        "ref": "channel:123456789",
                        "type": "channel",
                        "title": "測試頻道",
                        "username": "@test_channel",
                        "topics": [{"id": 7, "title": "公告"}],
                    }
                ],
            }
        )
        self.assertEqual(chats["chats"][0]["id"], "-100123")
        self.assertEqual(chats["chats"][0]["ref"], "channel:123456789")
        self.assertEqual(chats["chats"][0]["title"], "測試頻道")
        self.assertEqual(chats["chats"][0]["username"], "test_channel")
        self.assertEqual(chats["chats"][0]["topics"][0]["id"], "7")
        self.assertEqual(chats["skipped"], 2)
        self.assertTrue(chats["partial"])
        self.assertEqual(chats["message"], "部分對話無法讀取")
        self.assertEqual(
            normalize_event(
                {
                    "event": "exported",
                    "path": "C:/exports/a.json",
                    "count": 0,
                    "total_bytes": 12,
                }
            ),
            {
                "event": "exported",
                "path": "C:/exports/a.json",
                "count": 0,
                "total_bytes": 12,
            },
        )

    def test_export_window_is_taipei_local_and_inclusive(self) -> None:
        start_text, end_text, start_unix, end_unix = validate_export_window("2026-01-01", "2026-01-01")
        self.assertEqual((start_text, end_text), ("2026-01-01", "2026-01-01"))
        self.assertEqual(end_unix - start_unix, 86_399)
        self.assertEqual(validate_media_types(["PHOTO", "video", "photo"]), ("video", "photo"))
        self.assertEqual(validate_chat_id("channel:123"), "channel:123")
        self.assertEqual(validate_chat_id("CHAT:123"), "chat:123")
        with self.assertRaises(ValidationError):
            validate_export_window("2026-01-02", "2026-01-01")
        with self.assertRaises(ValidationError):
            validate_media_types([])
        with self.assertRaises(ValidationError):
            validate_media_types(["archive"])


class SessionVaultTests(_WorkspaceStore):
    def test_store_startup_reseals_orphaned_runtime(self) -> None:
        account = self.store.add_account("啟動保護")
        runtime = self.store.account_root(account.id) / "runtime"
        (runtime / "data").mkdir(parents=True)
        (runtime / "data" / "session.bin").write_bytes(b"orphaned-session")
        reopened = Store(self.temp_root)
        self.assertFalse((reopened.account_root(account.id) / "runtime").exists())
        self.assertTrue((reopened.account_root(account.id) / "session.dpapi").exists())

    def test_core_runtime_is_removed_and_restored_through_envelope(self) -> None:
        account = self.store.add_account("工作階段")
        vault = SessionVault(self.store)
        runtime = vault.prepare(account.id)
        secret = b"session-bytes-that-must-not-stay-plain"
        (runtime / "data").mkdir(parents=True)
        (runtime / "data" / "session.bin").write_bytes(secret)
        vault.seal(account.id)
        envelope = self.store.account_root(account.id) / "session.dpapi"
        self.assertTrue(envelope.exists())
        self.assertFalse(runtime.exists())
        self.assertNotIn(secret, envelope.read_bytes())
        restored = vault.prepare(account.id)
        self.assertEqual((restored / "data" / "session.bin").read_bytes(), secret)
        vault.cleanup(account.id)
        self.assertFalse(restored.exists())


class RunnerTests(_WorkspaceStore):
    class FakeRunner(Runner):
        def __init__(self, store: Store, script: str) -> None:
            super().__init__(store, executable=sys.executable)
            self.script = script

        def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
            encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
            return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]

    def test_background_runner_emits_prompt_auth_and_persists_account(self) -> None:
        account = self.store.add_account("登入測試")
        script = (
            "import json,sys;"
            "print('" + EVENT_PREFIX + "'+json.dumps({'event':'prompt','kind':'code'}),flush=True);"
            "sys.stdin.readline();"
            "print('" + EVENT_PREFIX + "'+json.dumps({'event':'authenticated','id':9,'username':'bob','name':'Bob'}),flush=True)"
        )
        runner = self.FakeRunner(self.store, script)
        handle = runner.start(LoginRequest(account.id))
        self.assertIsNotNone(handle.get_event(timeout=5))
        self.assertTrue(handle.respond("12345"))
        result = handle.wait(timeout=5)
        self.assertTrue(result.success)
        saved = self.store.get_account(account.id)
        self.assertEqual(saved.user_id, "9")
        self.assertEqual(saved.username, "bob")
        self.assertTrue(saved.authenticated)

    def test_core_receives_gui_and_account_scoped_environment_for_login_and_download(self) -> None:
        account = self.store.add_account("環境隔離測試")
        runtime = self.store.account_root(account.id) / "runtime"

        def environment_script(marker: Path, *, authenticated: bool) -> str:
            event = (
                f"print({EVENT_PREFIX!r}+json.dumps({{'event':'authenticated','id':11,"
                "'username':'environment-test','name':'Environment Test'}),flush=True)"
                if authenticated
                else ""
            )
            return f"""
import json
import os
from pathlib import Path
Path({str(marker)!r}).write_text(json.dumps({{
    'gui': os.environ.get('TDL_GUI'),
    'data': os.environ.get('TDL_DATA_DIR'),
    'storage': os.environ.get('TDL_STORAGE', ''),
    'ns': os.environ.get('TDL_NS', ''),
}}), encoding='utf-8')
{event}
"""

        login_marker = self.temp_root / "login-environment.json"
        inherited = {
            "TDL_GUI": "inherited-value",
            "TDL_DATA_DIR": "inherited-data",
            "TDL_STORAGE": "inherited-storage",
            "TDL_NS": "inherited-namespace",
        }
        with patch.dict(os.environ, inherited, clear=False):
            login = self.FakeRunner(
                self.store,
                environment_script(login_marker, authenticated=True),
            ).run(LoginRequest(account.id, mode="qr"))
        self.assertTrue(login.success, login.error)
        login_environment = json.loads(login_marker.read_text(encoding="utf-8"))
        self.assertEqual(login_environment["gui"], "1")
        self.assertEqual(Path(login_environment["data"]).resolve(), runtime.resolve())
        self.assertEqual(login_environment["storage"], "")
        self.assertEqual(login_environment["ns"], "")

        download_marker = self.temp_root / "download-environment.json"
        with patch.dict(os.environ, inherited, clear=False):
            download = self.FakeRunner(
                self.store,
                environment_script(download_marker, authenticated=False),
            ).run(DownloadRequest(account.id, ["https://t.me/telegram/193"]))
        self.assertTrue(download.success)
        download_environment = json.loads(download_marker.read_text(encoding="utf-8"))
        self.assertEqual(download_environment["gui"], "1")
        self.assertEqual(Path(download_environment["data"]).resolve(), runtime.resolve())
        self.assertEqual(download_environment["storage"], "")
        self.assertEqual(download_environment["ns"], "")

    def test_list_chats_returns_structured_data_without_download_history(self) -> None:
        account = self.store.add_account("聊天清單")
        script = (
            "import json;"
            "print(" + repr(EVENT_PREFIX) + "+json.dumps({"
            "'event':'chats','partial':True,'message':'部分對話無法讀取','skipped':1,'chats':["
            "{'id':'-100123','ref':'channel:123456789','type':'channel','title':'頻道','username':'demo','topics':[]}"
            "]}),flush=True)"
        )
        result = self.FakeRunner(self.store, script).run(ListChatsRequest(account.id))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data["skipped"], 1)
        self.assertTrue(result.data["partial"])
        self.assertEqual(result.data["message"], "部分對話無法讀取")
        self.assertEqual(result.data["chats"][0]["id"], "-100123")
        self.assertEqual(result.data["chats"][0]["ref"], "channel:123456789")
        self.assertEqual(self.store.list_history(), [])

    def test_list_chat_waiting_emits_status_and_can_cancel_without_history(self) -> None:
        account = self.store.add_account("聊天排隊")
        runner = self.FakeRunner(self.store, "import time;time.sleep(0.1)")
        account_lock = runner._account_lock(account.id)
        self.assertTrue(account_lock.acquire(blocking=False))
        try:
            handle = runner.start_list_chats(account.id)
            event = handle.get_event(timeout=2)
            self.assertEqual(event, {"event": "status", "message": "前面仍有帳號操作，查詢已排隊。"})
            handle.cancel()
            result = handle.wait(timeout=5)
            self.assertTrue(result.cancelled)
            self.assertEqual(self.store.list_history(), [])
        finally:
            account_lock.release()

    def test_export_uses_taipei_window_and_validates_count_and_size(self) -> None:
        account = self.store.add_account("頻道匯出")
        captured: dict[str, object] = {}

        class ExportFakeRunner(self.FakeRunner):
            def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
                if isinstance(request, ExportRequest):
                    captured.update(
                        {
                            "start_unix": request.start_unix,
                            "end_unix": request.end_unix,
                            "media_types": tuple(request.media_types),
                        }
                    )
                    script = (
                        "import json\n"
                        "from pathlib import Path\n"
                        f"path=Path({str(request.output_path)!r})\n"
                        "path.write_text(json.dumps({'id': -100123, 'messages': [{'id': 7, 'size': 12}]}), encoding='utf-8')\n"
                        f"print({EVENT_PREFIX!r}+json.dumps({{'event':'exported','path':str(path.resolve()),'count':1,'total_bytes':12}}),flush=True)\n"
                    )
                    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
                    return [
                        sys.executable,
                        "-c",
                        "import base64;exec(base64.b64decode(%r))" % encoded,
                    ]
                return super()._build_args(request, runtime)

        result = ExportFakeRunner(self.store, "").run(
            ExportRequest(
                account_id=account.id,
                chat_id="-100123",
                start_date="2026-01-01",
                end_date="2026-01-01",
                media_types=["photo", "video", "photo"],
            )
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(captured["end_unix"] - captured["start_unix"], 86_399)
        self.assertEqual(captured["media_types"], ("video", "photo"))
        self.assertEqual(result.data["count"], 1)
        self.assertEqual(result.data["total_bytes"], 12)
        export_path = Path(result.data["path"])
        self.assertTrue(export_path.is_file())
        self.assertEqual(export_path.parent, self.store.account_root(account.id) / "exports")
        self.assertEqual(self.store.list_history(), [])

    def test_export_rejects_invalid_range_media_and_external_output_before_child(self) -> None:
        account = self.store.add_account("匯出驗證")
        runner = self.FakeRunner(self.store, "import sys;sys.exit(0)")
        with self.assertRaises(ValidationError):
            runner.start_export(account.id, "-100123", "2026-02-02", "2026-02-01", ["video"])
        with self.assertRaises(ValidationError):
            runner.start_export(account.id, "-100123", "2026-02-01", "2026-02-01", [])
        with self.assertRaises(ValidationError):
            runner.start_export(
                account.id,
                "-100123",
                "2026-02-01",
                "2026-02-01",
                ["video"],
                output_path=self.temp_root / "outside.json",
            )

    def test_download_failure_is_persisted_without_proxy_credentials(self) -> None:
        account = self.store.add_account("失敗測試")
        script = "import sys;sys.stderr.write('proxy=socks5://u:verysecret@example.test:1080\\n');sys.exit(7)"
        runner = self.FakeRunner(self.store, script)
        handle = runner.start_download(
            account.id,
            ["https://t.me/telegram/193"],
            proxy="socks5://u:verysecret@example.test:1080",
        )
        result = handle.wait(timeout=5)
        self.assertFalse(result.success)
        self.assertIn("***:***", result.error or "")
        self.assertNotIn("verysecret", result.error or "")
        records = self.store.list_history()
        self.assertEqual(records[0].status, "failed")
        saved_json = "\n".join(path.read_text(encoding="utf-8") for path in self.temp_root.glob("*.json"))
        self.assertNotIn("verysecret", saved_json)

    def test_stale_authenticated_account_is_marked_signed_out(self) -> None:
        account = self.store.add_account("工作階段失效", user_id=7, username="alice")
        self.store.update_account(account.id, authenticated=True)
        script = (
            "import json;print("
            + repr(EVENT_PREFIX)
            + "+json.dumps({'event':'error','message':'AUTH_KEY_UNREGISTERED'}),flush=True);"
            "raise SystemExit(1)"
        )
        result = self.FakeRunner(self.store, script).run(
            DownloadRequest(account.id, ["https://t.me/telegram/193"])
        )
        self.assertFalse(result.success)
        self.assertFalse(self.store.get_account(account.id).authenticated)

    def test_cancel_sends_graceful_command_before_watchdog(self) -> None:
        account = self.store.add_account("取消測試")
        checkpoint = self.temp_root / "checkpoint.txt"
        script = f"""
import json
import sys
print({EVENT_PREFIX!r} + json.dumps({{'event': 'progress', 'percent': 1}}), flush=True)
for line in sys.stdin:
    data = json.loads(line)
    if data.get('command') == 'cancel':
        open({str(checkpoint)!r}, 'w', encoding='utf-8').write('saved')
        raise SystemExit(0)
"""
        runner = self.FakeRunner(self.store, script)
        handle = runner.start_download(account.id, ["https://t.me/telegram/193"])
        self.assertIsNotNone(handle.get_event(timeout=5))
        self.assertTrue(handle.cancel())
        result = handle.wait(timeout=5)
        self.assertIsNotNone(result)
        self.assertTrue(result.cancelled)
        self.assertTrue(checkpoint.exists())
        self.assertEqual(self.store.list_history()[0].status, "cancelled")

    def test_download_command_validation_happens_before_child(self) -> None:
        account = self.store.add_account("參數測試")
        runner = self.FakeRunner(self.store, "import sys;sys.exit(0)")
        with self.assertRaises(ValidationError):
            runner.start_download(account.id, ["https://evil.example/$(whoami)"])

    def test_persisted_job_cannot_be_run_under_another_account(self) -> None:
        first = self.store.add_account("工作一")
        second = self.store.add_account("工作二")
        job = self.store.create_download_job(first.id, ["https://t.me/telegram/193"])
        runner = self.FakeRunner(self.store, "import sys;sys.exit(0)")
        with self.assertRaises(ValidationError):
            runner.start(
                DownloadRequest(
                    account_id=second.id,
                    links=["https://t.me/telegram/193"],
                    job_id=job.id,
                )
            )

    def test_login_without_authenticated_event_cannot_fake_success(self) -> None:
        account = self.store.add_account("未驗證測試")
        runner = self.FakeRunner(self.store, "import sys;sys.exit(0)")
        result = runner.run(LoginRequest(account.id))
        self.assertFalse(result.success)
        self.assertIn("未回報", result.error or "")
        self.assertFalse(self.store.get_account(account.id).authenticated)

    def test_same_account_downloads_queue_behind_one_core_process(self) -> None:
        account = self.store.add_account("佇列測試")
        runner = self.FakeRunner(self.store, "import time;time.sleep(0.15)")
        first = runner.start_download(account.id, ["https://t.me/telegram/193"])
        second = runner.start_download(account.id, ["https://t.me/telegram/194"])
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertTrue(first.wait(timeout=5).success)
        self.assertTrue(second.wait(timeout=5).success)
        self.assertEqual({job.status for job in self.store.list_history()}, {"completed"})

    def test_queued_download_cancellation_marks_persisted_job_and_keeps_id(self) -> None:
        account = self.store.add_account("取消排隊工作")
        runner = self.FakeRunner(self.store, "import time;time.sleep(0.2)")
        account_lock = runner._account_lock(account.id)
        self.assertTrue(account_lock.acquire(blocking=False))
        try:
            handle = runner.start_download(account.id, ["https://t.me/telegram/197"])
            queued = self.store.list_history()
            self.assertEqual(len(queued), 1)
            job_id = queued[0].id
            self.assertEqual(queued[0].status, "queued")

            handle.cancel()
            result = handle.wait(timeout=5)
            self.assertIsNotNone(result)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.job_id, job_id)

            history_job = self.store.get_job(job_id)
            self.assertEqual(history_job.status, "cancelled")
            self.assertIn("取消", history_job.error or "")
            self.assertEqual(self.store.list_queue()[0].status, "cancelled")
        finally:
            account_lock.release()

    def test_same_account_serializes_but_different_accounts_can_overlap(self) -> None:
        first = self.store.add_account("序列帳號")
        second = self.store.add_account("並行帳號")
        running = self.temp_root / "running"
        overlap = self.temp_root / "overlap"
        script = f"""
import time
from pathlib import Path
running = Path({str(running)!r})
overlap = Path({str(overlap)!r})
if running.exists():
    overlap.write_text('yes')
running.write_text('yes')
time.sleep(0.8)
running.unlink(missing_ok=True)
"""
        runner = self.FakeRunner(self.store, script)
        same_a = runner.start_download(first.id, ["https://t.me/telegram/193"])
        same_b = runner.start_download(first.id, ["https://t.me/telegram/194"])
        self.assertTrue(same_a.wait(timeout=5).success)
        self.assertTrue(same_b.wait(timeout=5).success)
        self.assertFalse(overlap.exists())

        different_a = runner.start_download(first.id, ["https://t.me/telegram/195"])
        different_b = runner.start_download(second.id, ["https://t.me/telegram/196"])
        self.assertTrue(different_a.wait(timeout=5).success)
        self.assertTrue(different_b.wait(timeout=5).success)
        self.assertTrue(overlap.exists())

    def test_remove_account_is_blocked_while_queued_or_running(self) -> None:
        account = self.store.add_account("刪除保護")
        script = "import time;time.sleep(0.4)"
        runner = self.FakeRunner(self.store, script)
        handle = runner.start_download(account.id, ["https://t.me/telegram/193"])
        with self.assertRaises(StoreError):
            self.store.remove_account(account.id)
        self.assertTrue(handle.wait(timeout=5).success)
        self.store.remove_account(account.id)


if __name__ == "__main__":
    unittest.main()
