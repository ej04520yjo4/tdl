"""1.5.0 backend contracts behind the friendlier UI: account names, error
explanations, proxy validation, history removal and reported folders."""

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
    Runner,
    Store,
    ValidationError,
    normalize_event,
    suggest_next_step,
    translate_error,
)


class _FixtureRunner(Runner):
    def __init__(self, store: Store, script: str) -> None:
        super().__init__(store, executable=sys.executable)
        self.script = script

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-humane-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class AccountNameTests(_Workspace):
    def test_phone_number_names_are_replaced_on_start(self) -> None:
        phone_named = self.store.add_account("+886 912-345-678")
        self.store.update_account(phone_named.id, display_name="王小明", username="xiaoming")
        username_only = self.store.add_account("0912345678")
        self.store.update_account(username_only.id, username="only_username")
        anonymous = self.store.add_account("+886912345678")
        chosen = self.store.add_account("工作帳號 2024")

        reopened = Store(self.root)
        names = {account.id: account.name for account in reopened.list_accounts()}
        self.assertEqual(names[phone_named.id], "王小明")
        self.assertEqual(names[username_only.id], "@only_username")
        self.assertEqual(names[anonymous.id], "Telegram 帳號")
        self.assertEqual(names[chosen.id], "工作帳號 2024")
        self.assertNotIn("912", (self.root / "accounts.json").read_text(encoding="utf-8"))


class ErrorExplanationTests(unittest.TestCase):
    def test_file_system_errors_in_both_languages(self) -> None:
        for raw in (
            r"write D:\x\a.mp4.tmp: There is not enough space on the disk.",
            r"write D:\x\a.mp4.tmp: 磁碟空間不足。",
        ):
            self.assertEqual(translate_error(raw), "下載資料夾所在的磁碟空間不足。")
        self.assertEqual(translate_error("open C:\\Windows\\a: Access is denied."), "沒有寫入這個資料夾的權限。")
        self.assertEqual(translate_error("open x: 存取被拒。"), "沒有寫入這個資料夾的權限。")
        self.assertIn("路徑太長", translate_error("open x: The filename or extension is too long."))

    def test_flood_wait_names_the_wait(self) -> None:
        self.assertEqual(translate_error("rpc error code 420: FLOOD_WAIT (45)"), "Telegram 限制了操作頻率，約 45 秒後可再試。")
        self.assertEqual(translate_error("FLOOD_WAIT_125"), "Telegram 限制了操作頻率，約 3 分鐘後可再試。")

    def test_every_common_failure_has_a_next_step(self) -> None:
        for raw in (
            "There is not enough space on the disk",
            "Access is denied",
            "AUTH_KEY_UNREGISTERED",
            "FLOOD_WAIT (60)",
            "context deadline exceeded",
            "CHANNEL_PRIVATE",
            "CHAT_FORWARDS_RESTRICTED",
            "有檔案下載失敗，請查看錯誤訊息後重試",
        ):
            self.assertTrue(suggest_next_step(translate_error(raw)), raw)
        self.assertEqual(suggest_next_step("完全無關的內容"), "")


class PreferenceTests(_Workspace):
    def test_malformed_proxy_is_refused_when_saved(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.update_preferences(proxy="127.0.0.1:7890")
        self.assertEqual(self.store.get_preferences().proxy, "")
        self.store.update_preferences(proxy="socks5://127.0.0.1:1080")
        self.assertEqual(self.store.get_preferences().proxy, "socks5://127.0.0.1:1080")
        self.store.update_preferences(proxy="")
        self.assertEqual(self.store.get_preferences().proxy, "")


class HistoryRemovalTests(_Workspace):
    def test_only_ended_records_are_removed_and_files_stay(self) -> None:
        account = self.store.add_account("紀錄")
        output = self.root / "downloads"
        output.mkdir()
        (output / "kept.mp4").write_bytes(b"x")
        ended = []
        for status in ("completed", "failed", "cancelled"):
            job = self.store.create_download_job(account.id, ["https://t.me/telegram/1"], output_dir=output)
            self.store.finish_job(job.id, status)
            ended.append(job.id)
        paused = self.store.create_download_job(account.id, ["https://t.me/telegram/2"], output_dir=output)
        self.store.finish_job(paused.id, "paused")

        removed = self.store.remove_history_items([*ended, paused.id])
        self.assertEqual(sorted(removed), sorted(ended))
        self.assertEqual([job.id for job in self.store.list_history()], [paused.id])
        self.assertEqual([job.id for job in self.store.list_queue()], [paused.id])
        self.assertTrue((output / "kept.mp4").exists())


class FolderReportTests(_Workspace):
    def test_progress_keeps_the_folder_and_the_job_remembers_it(self) -> None:
        account = self.store.add_account("資料夾")
        folder = str(self.root / "downloads" / "旅行影片 1234567890")
        event = normalize_event({"event": "progress", "file_id": "1", "state": "downloading", "path": folder})
        # A long digit run in a folder name is not a phone number.
        self.assertEqual(event["path"], folder)

        lines = [
            {"event": "progress", "file_id": "1", "state": "downloading", "current": 1, "total": 2, "path": folder},
            {"event": "progress", "file_id": "1", "state": "completed", "current": 2, "total": 2, "path": folder},
        ]
        script = "import json\nfor line in %r:\n    print(%r + json.dumps(line), flush=True)\n" % (lines, EVENT_PREFIX)
        result = _FixtureRunner(self.store, script).start_download(
            account.id, ["https://t.me/telegram/193"]
        ).wait(timeout=20)
        self.assertTrue(result.success, result.error)
        saved = Store(self.root).get_job(result.job_id)
        self.assertEqual(saved.folders, [folder])
        raw = json.loads((self.root / "downloads.json").read_text(encoding="utf-8"))["items"][0]
        self.assertEqual(raw["folders"], [folder])


if __name__ == "__main__":
    unittest.main()
