"""Offline checks against the real compiled Go core (no Telegram access)."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "bin" / "tdl-core.exe"


@unittest.skipUnless(CORE.exists(), "Compile bin/tdl-core.exe to check the real protocol")
class CoreContractTests(unittest.TestCase):
    def test_empty_download_reports_structured_error_in_isolated_directory(self):
        with tempfile.TemporaryDirectory(prefix="core-contract-", dir=ROOT / ".build") as temporary:
            env = dict(os.environ, TDL_GUI="1", TDL_DATA_DIR=temporary)
            # Keep stdin open until process exit. EOF intentionally cancels GUI tasks.
            child = subprocess.Popen(
                [str(CORE), "dl"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                child.wait(timeout=10)
                output = child.stdout.read().decode("utf-8")
                self.assertNotEqual(child.returncode, 0)
                events = [json.loads(line[len("@@TDL_GUI@@"):]) for line in output.splitlines() if line.startswith("@@TDL_GUI@@")]
                self.assertEqual(len(events), 1, output)
                self.assertEqual(events[0]["event"], "error")
                self.assertIn("no urls or files", events[0]["message"])
                self.assertTrue((Path(temporary) / "extensions").is_dir())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    def _run_core(self, args, temporary, timeout=20):
        env = dict(os.environ, TDL_GUI="1", TDL_DATA_DIR=temporary)
        child = subprocess.Popen(
            [str(CORE), *args], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            child.wait(timeout=timeout)
            return child.returncode, child.stdout.read().decode("utf-8"), child.stderr.read().decode("utf-8")
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

    def test_core_accepts_every_flag_the_desktop_can_pass(self):
        """The fixture cores in the other tests accept anything; the real one
        must know each flag.  ``--help`` makes cobra parse the flags and stop
        before it opens storage or the network."""
        from desktop.backend import (
            BackupRequest, DownloadRequest, ExportRequest, ForwardRequest, ListChatsRequest,
            ListTopicsRequest, LoginRequest, RecoverRequest, Runner, Store, UploadRequest, WhoamiRequest,
        )

        with tempfile.TemporaryDirectory(prefix="core-flags-", dir=ROOT / ".build") as temporary:
            store = Store(Path(temporary) / "data")
            account = store.add_account("旗標檢查")
            store.update_preferences(proxy="socks5://127.0.0.1:1080")
            runner = Runner(store, executable=CORE)
            manifest = Path(temporary) / "export.json"
            manifest.write_text(json.dumps({"id": 1, "messages": [{"id": 5}]}), encoding="utf-8")
            backup = Path(temporary) / "account.tdl"
            backup.write_bytes(b"x")
            advanced = {
                "template": "{{ .MessageID }}_{{ filenamify .FileName }}", "include": "mp4, mkv",
                "desc": True, "takeout": True, "rewrite_ext": True, "threads": 8, "delay_ms": 500,
            }
            requests = [
                LoginRequest(account.id, mode="qr"),
                ListChatsRequest(account.id),
                ListTopicsRequest(account.id, "channel:123"),
                ExportRequest(account.id, "channel:123", "2026-01-01", "2026-01-31", ("video", "photo"), topic_id=7),
                DownloadRequest(account.id, ["https://t.me/telegram/193"], files=[str(manifest)],
                                output_dir=temporary, continue_download=True, skip_same=True, group=True,
                                concurrency=3, advanced=advanced, group_by_chat=True),
                DownloadRequest(account.id, ["https://t.me/telegram/193"], advanced={"exclude": "jpg"}),
                UploadRequest(account.id, [str(manifest)], chat="channel:123", topic_id=7, as_photo=True,
                              remove_after=True, caption_mode="text", caption_text='說明 "x"',
                              exclude=["tmp"], concurrency=2, threads=4),
                UploadRequest(account.id, [str(manifest)], caption_mode="filename", include=["json"]),
                ForwardRequest(account.id, ["https://t.me/telegram/193", str(manifest)], to="channel:123",
                               mode="clone", silent=True, dry_run=True, single=True, desc=True),
                BackupRequest(account.id, str(Path(temporary) / "new.tdl")),
                RecoverRequest(account.id, str(backup)),
                WhoamiRequest(account.id),
            ]
            runtime = store.account_root(account.id) / "runtime"
            for request in requests:
                with self.subTest(request=type(request).__name__):
                    args = runner._build_args(request, runtime)[1:]
                    code, output, errors = self._run_core([*args, "--help"], temporary)
                    self.assertEqual(code, 0, f"{args}\n{output}\n{errors}")
                    self.assertNotIn("unknown flag", output + errors)

    def test_backup_and_recover_round_trip_offline(self):
        """Both commands only touch local storage, so the real core can run."""
        with tempfile.TemporaryDirectory(prefix="core-backup-", dir=ROOT / ".build") as temporary:
            backup = Path(temporary) / "account.tdl"
            backup.write_bytes(b"existing backup fixture")
            for command, storage in (
                (["backup", "--dst", str(backup)], "source"),
                (["recover", "--file", str(backup)], "target"),
            ):
                args = ["--storage", f"type=bolt,path={Path(temporary) / storage}", "--ns", "default", *command]
                code, output, errors = self._run_core(args, temporary)
                self.assertEqual(code, 0, output + errors)
                events = [json.loads(line[len("@@TDL_GUI@@"):]) for line in output.splitlines() if line.startswith("@@TDL_GUI@@")]
                # GUI mode reports through the protocol, not coloured text.
                self.assertEqual([event["event"] for event in events], ["status"], output)
                self.assertEqual(output.strip().count("\n"), 0, output)
            self.assertGreater(backup.stat().st_size, 0)
            self.assertNotEqual(backup.read_bytes(), b"existing backup fixture")
            self.assertEqual(list(Path(temporary).glob(".account.tdl-*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows destination-sharing semantics")
    def test_backup_locked_destination_preserves_previous_bytes(self):
        """An actual core replacement failure must retain the old backup."""
        with tempfile.TemporaryDirectory(prefix="core-backup-locked-", dir=ROOT / ".build") as temporary:
            backup = Path(temporary) / "account.tdl"
            original = b"existing backup fixture that must survive"
            backup.write_bytes(original)
            args = ["--storage", f"type=bolt,path={Path(temporary) / 'source'}",
                    "--ns", "default", "backup", "--dst", str(backup)]
            # The normal Windows file handle permits reads/writes but does
            # not share deletion, so replacement must fail without truncating.
            with backup.open("rb") as held:
                code, output, errors = self._run_core(args, temporary)
                self.assertNotEqual(code, 0, output + errors)
                self.assertEqual(held.read(), original)
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(list(Path(temporary).glob(".account.tdl-*.tmp")), [])
            events = [json.loads(line[len("@@TDL_GUI@@"):]) for line in output.splitlines()
                      if line.startswith("@@TDL_GUI@@")]
            self.assertTrue(any(event["event"] == "error" for event in events), output)
            self.assertFalse(any(event["event"] == "status" for event in events), output)


if __name__ == "__main__":
    unittest.main()
