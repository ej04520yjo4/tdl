"""Regression tests for Store callbacks crossing back to the Qt GUI thread."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(QT_AVAILABLE, "Install desktop/requirements.txt for UI integration")
class StoreActionThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication([])
        from desktop.app import MainWindow

        cls.MainWindow = MainWindow

    def setUp(self) -> None:
        from PySide6.QtCore import QObject

        class Harness(QObject):
            def __init__(self) -> None:
                super().__init__()
                # MainWindow keeps these references until the corresponding
                # QThread has emitted finished.  Reuse the real methods here
                # without constructing a full QWidget tree for the callback
                # thread assertions.
                self._store_threads = []
                self._store_workers = []
                self._action_threads = []
                self._action_workers = []
                self._action_relays = []

            _start_store_action = self.MainWindow._start_store_action
            _forget_aux_thread = self.MainWindow._forget_aux_thread

        self.harness = Harness()

    def tearDown(self) -> None:
        self._drain(lambda: not self.harness._action_threads)
        self.harness.deleteLater()
        self.application.processEvents()

    def _drain(self, done, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not done() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertTrue(done(), "Qt worker did not finish before the test timeout")

    def test_success_callback_is_queued_to_gui_thread_and_relay_is_retained(self) -> None:
        main_thread = threading.get_ident()
        action_threads: list[int] = []
        callback_threads: list[int] = []
        values: list[object] = []
        callback_done = threading.Event()

        def action() -> str:
            action_threads.append(threading.get_ident())
            return "ok"

        def on_success(value: object) -> None:
            callback_threads.append(threading.get_ident())
            values.append(value)
            callback_done.set()

        self.harness._start_store_action(action, on_success=on_success)
        self._drain(callback_done.is_set)

        self.assertEqual(values, ["ok"])
        self.assertEqual(callback_threads, [main_thread])
        self.assertEqual(len(action_threads), 1)
        self.assertNotEqual(action_threads[0], main_thread)
        # The relay remains referenced while the action thread is finishing,
        # then is removed together with the worker and thread.
        self._drain(lambda: not self.harness._action_threads)
        self.assertFalse(self.harness._action_relays)

    def test_error_callback_is_queued_to_gui_thread(self) -> None:
        main_thread = threading.get_ident()
        action_threads: list[int] = []
        callback_threads: list[int] = []
        details: list[str] = []
        callback_done = threading.Event()

        def action() -> None:
            action_threads.append(threading.get_ident())
            raise RuntimeError("store fixture failure")

        def on_failure(detail: str) -> None:
            callback_threads.append(threading.get_ident())
            details.append(detail)
            callback_done.set()

        self.harness._start_store_action(action, on_failure=on_failure)
        self._drain(callback_done.is_set)

        self.assertEqual(callback_threads, [main_thread])
        self.assertIn("store fixture failure", details[0])
        self.assertEqual(len(action_threads), 1)
        self.assertNotEqual(action_threads[0], main_thread)
        self._drain(lambda: not self.harness._action_threads)
        self.assertFalse(self.harness._action_relays)

    def test_callback_exception_is_sanitized_and_still_cleans_up(self) -> None:
        callback_threads: list[int] = []
        details: list[str] = []
        callback_done = threading.Event()

        def on_success(_value: object) -> None:
            raise RuntimeError("token=clear-success-token")

        def on_failure(detail: str) -> None:
            callback_threads.append(threading.get_ident())
            details.append(detail)
            callback_done.set()
            raise RuntimeError("password=clear-failure-password")

        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            self.harness._start_store_action(
                lambda: "ok",
                on_success=on_success,
                on_failure=on_failure,
            )
            self._drain(callback_done.is_set)
            self._drain(lambda: not self.harness._action_threads)

        self.assertEqual(callback_threads, [threading.get_ident()])
        self.assertIn("token=***", details[0])
        self.assertNotIn("clear-success-token", details[0])
        self.assertNotIn("clear-failure-password", stderr.getvalue())
        self.assertFalse(self.harness._action_relays)

    def test_main_window_close_waits_for_store_action_relay(self) -> None:
        from PySide6.QtWidgets import QApplication

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                from desktop.app import BackendBridge, MainWindow

                bridge = BackendBridge()
                window = MainWindow(bridge)
                window.show()
                action_done = threading.Event()

                def action() -> str:
                    time.sleep(0.15)
                    return "close-safe"

                window._start_store_action(action, on_success=lambda _value: action_done.set())
                window.close()
                deadline = time.monotonic() + 5.0
                while not window.isHidden() and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.01)

                self.assertTrue(action_done.is_set())
                self.assertTrue(window.isHidden())
                self.assertFalse(window._action_threads)
                window.deleteLater()
                QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
