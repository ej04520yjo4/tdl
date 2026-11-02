"""Batch transfers share one operation/account boundary with legacy transfers."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from desktop.app import BackendBridge, MainWindow
from desktop.transfer_page import TransferPage


class BatchTransferIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def pump(self, predicate) -> None:
        deadline = time.monotonic() + 6
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_batch_is_default_and_busy_keeps_cancel_page_enabled(self) -> None:
        page = TransferPage()
        self.addCleanup(page.deleteLater)
        self.assertIs(page.stack.currentWidget(), page.batch_page)
        with patch.object(page.batch_page, "is_busy", return_value=True):
            page._batch_busy_changed(True)
            self.assertTrue(page.is_busy())
            self.assertTrue(page.batch_page.isEnabled())
            self.assertFalse(page.upload_button.isEnabled())
            self.assertFalse(page.forward_button.isEnabled())
            self.assertFalse(page.upload_tab.isEnabled())
            with patch.object(page, "_runner") as runner:
                page._start("forward", "start_forward", ("account", []), {})
                runner.start_forward.assert_not_called()
        page._batch_busy_changed(False)
        self.assertTrue(page.upload_button.isEnabled())

    def test_legacy_work_locks_batch_and_cancel_reaches_both(self) -> None:
        page = TransferPage()
        self.addCleanup(page.deleteLater)
        with patch.object(page, "_legacy_busy", return_value=True):
            page._set_controls_enabled(False)
            self.assertFalse(page.batch_page.isEnabled())
        with patch.object(page.batch_page, "cancel_operations") as cancel:
            page.cancel_operations()
            cancel.assert_called_once()

    def test_top_account_cannot_change_during_batch_operation(self) -> None:
        build = Path(__file__).resolve().parents[2] / ".build"
        with tempfile.TemporaryDirectory(dir=build) as directory, patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            first = bridge.store.add_account("離線帳號甲")
            second = bridge.store.add_account("離線帳號乙")
            for account in (first, second):
                bridge.store.update_account(account.id, authenticated=True)
            bridge.store.set_active_account(first.id)
            window = MainWindow(bridge, resume_downloads=False)
            try:
                self.pump(lambda: window._accounts_loaded and window._queue_loaded)
                with patch.object(window.transfer_page.batch_page, "is_busy", return_value=True), patch("desktop.app.show_error") as error:
                    window._set_active_account(second.id)
                    self.assertEqual(window.home_page.account_combo.currentData(), first.id)
                    self.assertEqual(window.transfer_page.active_account_id(), first.id)
                    error.assert_called_once()
            finally:
                window.close()
                self.pump(lambda: not window._background_work_running())
                window.deleteLater()
                self.app.processEvents()
