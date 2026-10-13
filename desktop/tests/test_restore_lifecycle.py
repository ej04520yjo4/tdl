"""Regression tests for account-operation completion ordering.

These tests use a local Python child in place of tdl-core.  They never contact
Telegram and exercise the same activity guard used by restore cleanup.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop.backend as backend
from desktop.app import BackendBridge, OperationFailed
from desktop.backend import RecoverRequest, Runner, Store, WhoamiRequest


class _ScriptRunner(Runner):
    """Run a short local script instead of the Go core."""

    def __init__(self, store: Store, scripts: dict[str, str]) -> None:
        super().__init__(store, executable=sys.executable)
        self.scripts = scripts

    def _build_args(self, request, runtime):  # type: ignore[no-untyped-def]
        if isinstance(request, RecoverRequest):
            script = self.scripts.get("recover", "import sys; sys.exit(0)")
        elif isinstance(request, WhoamiRequest):
            script = self.scripts.get("whoami", "import sys; sys.exit(0)")
        else:
            script = self.scripts.get("default", "import sys; sys.exit(0)")
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return [
            sys.executable,
            "-c",
            "import base64;exec(base64.b64decode(%r))" % encoded,
        ]


class RestoreLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tdl-restore-test-", dir=Path(__file__).parent))
        self.store = Store(self.root)
        self.account = self.store.add_account("待還原帳號")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_failed_whoami_publishes_done_after_activity_release(self) -> None:
        """Immediate restore cleanup must not race the activity guard."""

        runner = _ScriptRunner(self.store, {"whoami": "import sys; sys.exit(1)"})
        release_entered = threading.Event()
        allow_release = threading.Event()
        real_release = backend._release_account_activity

        def gated_release(account_id: str) -> None:
            release_entered.set()
            if not allow_release.wait(5):
                raise AssertionError("test release gate timed out")
            real_release(account_id)

        with patch.object(backend, "_release_account_activity", side_effect=gated_release):
            handle = runner.start(WhoamiRequest(self.account.id))
            self.assertTrue(release_entered.wait(5), "worker did not reach activity cleanup")
            try:
                # The old ordering set done before entering this release gate.
                self.assertFalse(handle.done.is_set())
            finally:
                allow_release.set()
            result = handle.wait(5)

        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        # This is the cleanup performed by BackendBridge.restore_account after
        # a failed recover/whoami pair.  It must be safe immediately after wait.
        self.store.remove_account(self.account.id)
        self.assertEqual(self.store.list_accounts(), [])

        # The shared lock is reusable after cleanup; this catches a leaked
        # lock/activity count even when the account directory was removed.
        account_lock = backend._shared_account_lock(self.account.id)
        self.assertTrue(account_lock.acquire(blocking=False))
        account_lock.release()
        replacement = self.store.add_account("同一識別碼之後的工作")
        self.assertEqual(replacement.name, "同一識別碼之後的工作")

    def _assert_bridge_restore_failure_cleanup(
        self,
        scripts: dict[str, str],
        *,
        expected_release_calls: int,
    ) -> None:
        """Run the real bridge restore flow while holding its final release."""

        bridge_root = self.root / "bridge"
        source = bridge_root / "source.tdl"
        bridge_root.mkdir()
        source.write_bytes(b"offline fixture")
        finished = threading.Event()
        release_entered = threading.Event()
        allow_release = threading.Event()
        release_calls = 0
        real_release = backend._release_account_activity

        def gated_release(account_id: str) -> None:
            nonlocal release_calls
            release_calls += 1
            if release_calls == expected_release_calls:
                release_entered.set()
                if not allow_release.wait(5):
                    raise AssertionError("test release gate timed out")
            real_release(account_id)

        errors: list[BaseException] = []

        with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": str(bridge_root)}):
            bridge = BackendBridge()
            bridge.runner = _ScriptRunner(bridge.store, scripts)

            def restore() -> None:
                try:
                    bridge.restore_account("失敗的還原", str(source))
                except BaseException as exc:  # the bridge intentionally raises OperationFailed
                    errors.append(exc)
                finally:
                    finished.set()

            restore_thread = threading.Thread(target=restore, name="tdl-restore-test", daemon=True)
            with patch.object(backend, "_release_account_activity", side_effect=gated_release):
                restore_thread.start()
                try:
                    self.assertTrue(release_entered.wait(5), "restore did not reach final activity cleanup")
                    # Old backend ordering publishes done before this gate,
                    # allowing BackendBridge.restore_account to run its
                    # remove_account cleanup while the guard is still held.
                    self.assertFalse(finished.wait(0.1))
                finally:
                    allow_release.set()
                    restore_thread.join(5)

            self.assertFalse(restore_thread.is_alive(), "restore thread did not finish")
            self.assertEqual(release_calls, expected_release_calls)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], OperationFailed)
            self.assertEqual(bridge.store.list_accounts(), [])

    def test_bridge_restore_recover_failure_removes_new_account(self) -> None:
        self._assert_bridge_restore_failure_cleanup(
            {"recover": "import sys; sys.exit(1)"},
            expected_release_calls=1,
        )

    def test_bridge_restore_whoami_failure_removes_new_account(self) -> None:
        self._assert_bridge_restore_failure_cleanup(
            {
                "recover": "import sys; sys.exit(0)",
                "whoami": "import sys; sys.exit(1)",
            },
            expected_release_calls=2,
        )


if __name__ == "__main__":
    unittest.main()
