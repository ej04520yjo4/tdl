"""1.6.2: a failed progress save no longer stops a download, file replacement
waits out short Windows locks, login data is never stored readable, and the
files a pause kept are removed once a job will not continue.  Never contacts
Telegram."""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop import backend
from desktop.backend import (
    EVENT_PREFIX,
    DataProtectionError,
    Runner,
    SessionVault,
    Store,
    StoreError,
)


class _FixtureRunner(Runner):
    def __init__(self, store: Store, script: str) -> None:
        super().__init__(store, executable=sys.executable)
        self.script = script

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        encoded = base64.b64encode(self.script.encode("utf-8")).decode("ascii")
        return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-safer-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("存檔測試")
        self.store.update_account(self.account.id, authenticated=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ProgressSaveTests(_StoreCase):
    def test_one_failed_progress_save_does_not_stop_the_download(self) -> None:
        script = (
            "import json, time\n"
            "for i in range(15):\n"
            f"    print({EVENT_PREFIX!r} + json.dumps({{'event': 'progress', 'file_id': '1', 'state': 'downloading',"
            " 'current': i, 'total': 15, 'percent': i * 100 / 15, 'filename': 'a.mp4'}), flush=True)\n"
            "    time.sleep(0.2)\n"
        )
        original = Store._save_items
        calls = {"queue": 0, "failed": False}

        def flaky(store, path, values):  # type: ignore[no-untyped-def]
            if path.name == "queue.json":
                calls["queue"] += 1
                # Fail one write once the download is running, as when another
                # program briefly holds queue.json.
                if calls["queue"] == 4 and not calls["failed"]:
                    calls["failed"] = True
                    raise StoreError("寫入資料失敗：queue.json")
            return original(store, path, values)

        with patch.object(Store, "_save_items", flaky), patch.object(backend, "_PROGRESS_PERSIST_INTERVAL", 0.3):
            handle = _FixtureRunner(self.store, script).start_download(
                self.account.id, ["https://t.me/telegram/1"], output_dir=self.root / "out"
            )
            result = handle.wait(timeout=30)
        self.assertTrue(calls["failed"], "the injected failure never happened")
        self.assertIsNotNone(result)
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.store.get_job(result.job_id).status, "completed")


class ReplaceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "the retry is for Windows file locks")
    def test_replace_waits_out_a_short_lock(self) -> None:
        real_replace = os.replace
        attempts = []

        def locked_twice(source, target):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(13, "Access is denied")
            return real_replace(source, target)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "queue.json"
            with patch.object(backend.os, "replace", locked_twice), patch.object(backend.time, "sleep"):
                backend._atomic_write_bytes(target, b"{}")
            self.assertEqual(target.read_bytes(), b"{}")
            self.assertEqual(len(attempts), 3)

    @unittest.skipUnless(os.name == "nt", "the retry is for Windows file locks")
    def test_a_lasting_lock_is_reported(self) -> None:
        def always_locked(source, target):  # type: ignore[no-untyped-def]
            raise PermissionError(13, "Access is denied")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "queue.json"
            with patch.object(backend.os, "replace", always_locked), patch.object(backend.time, "sleep"):
                with self.assertRaises(StoreError):
                    backend._atomic_write_bytes(target, b"{}")
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [], "the temporary file is cleaned up")


@unittest.skipUnless(os.name == "nt", "DPAPI protects data on Windows only")
class DataProtectionTests(_StoreCase):
    def test_failed_encryption_never_writes_readable_data(self) -> None:
        with patch.object(backend.ctypes, "WinDLL", side_effect=OSError("DPAPI unavailable")):
            with self.assertRaises(DataProtectionError):
                backend._dpapi_protect(b"secret session")

    def test_failed_seal_keeps_the_previous_session_and_purges_plaintext(self) -> None:
        vault = SessionVault(self.store)
        runtime = vault.prepare(self.account.id)
        (runtime / "data").write_bytes(b"first session")
        vault.seal(self.account.id)
        _root, _runtime, envelope = vault.account_paths(self.account.id)
        sealed = envelope.read_bytes()
        self.assertTrue(sealed.startswith(b"TDL-DPAPI-1\n"))

        runtime = vault.prepare(self.account.id)
        (runtime / "data").write_bytes(b"second session")
        with patch.object(backend.ctypes, "WinDLL", side_effect=OSError("DPAPI unavailable")):
            with self.assertRaises(DataProtectionError):
                vault.seal(self.account.id)
        self.assertEqual(envelope.read_bytes(), sealed, "the last good envelope stays")
        self.assertFalse(runtime.exists(), "no readable session is left behind")
        self.assertNotIn(b"second session", envelope.read_bytes())


class PartialFileTests(_StoreCase):
    def _job_with_partials(self):  # type: ignore[no-untyped-def]
        output = self.root / "downloads"
        chat = output / "旅行影片收藏"
        chat.mkdir(parents=True)
        outside = self.root / "elsewhere"
        outside.mkdir()
        job = self.store.create_download_job(self.account.id, ["https://t.me/telegram/1"], output_dir=output)
        files = {
            "kept": chat / "10_1_a.mp4.tmp",
            "kept_list": chat / "10_1_a.mp4.tmp.part",
            # A crash leaves a .tmp without a part list: not ours to judge.
            "no_list": chat / "10_2_b.mp4.tmp",
            "done": chat / "10_3_c.mp4",
            "outside": outside / "10_4_d.mp4.tmp",
            "outside_list": outside / "10_4_d.mp4.tmp.part",
        }
        for path in files.values():
            path.write_bytes(b"x")
        self.store.update_job_progress(job.id, file_progress={
            "1": {"filename": "10_1_a.mp4", "path": str(chat), "state": "downloading", "current": 1, "total": 9},
            "2": {"filename": "10_2_b.mp4", "path": str(chat), "state": "downloading", "current": 1, "total": 9},
            "3": {"filename": "10_3_c.mp4", "path": str(chat), "state": "completed", "current": 9, "total": 9},
            "4": {"filename": "10_4_d.mp4", "path": str(outside), "state": "downloading", "current": 1, "total": 9},
        })
        return job, files

    def test_pause_keeps_and_cancel_removes_the_kept_parts(self) -> None:
        job, files = self._job_with_partials()
        self.store.finish_job(job.id, "paused")
        self.assertTrue(all(path.exists() for path in files.values()))

        self.store.finish_job(job.id, "cancelled")
        self.assertFalse(files["kept"].exists())
        self.assertFalse(files["kept_list"].exists())
        for name in ("no_list", "done", "outside", "outside_list"):
            self.assertTrue(files[name].exists(), name)

    def test_clearing_an_ended_job_removes_the_kept_parts(self) -> None:
        job, files = self._job_with_partials()
        self.store.finish_job(job.id, "failed")
        self.assertTrue(files["kept"].exists(), "a failed job can still be retried")
        self.assertEqual(self.store.clear_finished_jobs(), [job.id])
        self.assertFalse(files["kept"].exists())
        self.assertFalse(files["kept_list"].exists())
        self.assertTrue(files["outside"].exists())


if __name__ == "__main__":
    unittest.main()
