"""Offline UI/backend integration using a tiny protocol-speaking test child."""
import base64
import importlib.util
import json
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
class BridgeIntegrationTests(unittest.TestCase):
    def test_login_dialog_renders_qr_and_masks_password(self):
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication, QLineEdit
        from desktop.app import BackendBridge, LoginDialog

        application = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                dialog = LoginDialog(BackendBridge())
                dialog.show()
                application.processEvents()
                self.assertTrue(dialog.phone_edit.isHidden())
                picture = QImage(32, 32, QImage.Format_RGB32)
                picture.fill(0xFFFFFFFF)
                data = QByteArray()
                buffer = QBuffer(data)
                buffer.open(QIODevice.WriteOnly)
                picture.save(buffer, "PNG")
                dialog.stack.setCurrentWidget(dialog.auth_page)
                dialog._handle_event({"event": "qr", "png_base64": base64.b64encode(bytes(data)).decode()})
                self.assertFalse(dialog.qr_image.pixmap().isNull())
                dialog._handle_event({"event": "prompt", "kind": "password", "message": "輸入密碼"})
                self.assertEqual(dialog.password_edit.echoMode(), QLineEdit.Password)
                self.assertFalse(dialog.password_edit.isHidden())
                dialog.close()

    def test_reopened_queue_has_retry_and_preserves_original_account(self):
        from PySide6.QtWidgets import QApplication
        from desktop.app import BackendBridge, MainWindow

        application = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                first = bridge.store.add_account("第一帳號")
                second = bridge.store.add_account("第二帳號")
                bridge.store.update_account(first.id, authenticated=True)
                bridge.store.update_account(second.id, authenticated=True)
                bridge.store.set_active_account(second.id)
                saved = bridge.store.create_download_job(first.id, ["https://t.me/telegram/193"])
                window = MainWindow(bridge, resume_downloads=False)
                window.show()
                deadline = time.monotonic() + 5
                while (not window.jobs or window.top_account.text() == "正在讀取帳號…") and time.monotonic() < deadline:
                    application.processEvents()
                    time.sleep(0.02)
                self.assertEqual(window.home_page.account_combo.currentData(), second.id)
                restored = next(iter(window.jobs.values()))
                self.assertEqual(restored.backend_job_id, saved.id)
                self.assertEqual(restored.status, "已暫停")
                with patch.object(window, "_start_job") as start:
                    window.retry_job(restored.id)
                    self.assertEqual(start.call_args.args[1]["account_id"], first.id)
                window.close()
                deadline = time.monotonic() + 5
                while not window.isHidden() and time.monotonic() < deadline:
                    application.processEvents()
                    time.sleep(0.02)
                self.assertTrue(window.isHidden())

    def test_file_only_download_reaches_file_flag_and_retry_reuses_saved_job(self):
        from desktop.app import BackendBridge, DownloadJob
        from desktop.backend import Runner

        class FixtureRunner(Runner):
            def _build_args(self, request, runtime):
                self.last_request = request
                self.last_args = super()._build_args(request, runtime)
                return [sys.executable, "-c", "print('@@TDL_GUI@@{\"event\":\"progress\",\"percent\":100}')"]

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                account = bridge.store.add_account("JSON 離線測試")
                bridge.runner = FixtureRunner(bridge.store, executable=sys.executable)
                source = Path(directory) / "export.json"
                source.write_text(json.dumps({"id": 123, "messages": [{"id": 1, "type": "message", "file": "clip.mp4"}]}), encoding="utf-8")
                job = DownloadJob("files-only", str(source), "JSON", directory, links=[], files=[str(source)], account_id=account.id)
                options = {"account_id": account.id, "output_dir": directory}
                result = bridge.download(job, options, threading.Event(), lambda event: None)
                self.assertTrue(result["success"])
                self.assertEqual(bridge.runner.last_request.title, "JSON")
                self.assertIn("--file", bridge.runner.last_args)
                self.assertNotIn("--url", bridge.runner.last_args)
                saved_id = job.backend_job_id
                self.assertTrue(saved_id)
                bridge.download(job, options, threading.Event(), lambda event: None)
                self.assertEqual(job.backend_job_id, saved_id)
                self.assertEqual(bridge.runner.last_request.title, "JSON")
                self.assertEqual(len(bridge.store.list_history()), 1)

    def test_login_drives_phone_code_password_and_saves_only_confirmed_account(self):
        from desktop.app import BackendBridge
        from desktop.backend import Runner

        script = r'''
import sys,json
def event(data): print('@@TDL_GUI@@'+json.dumps(data),flush=True)
for kind,wanted in [('phone','+886912345678'),('code','12345'),('password','test-password')]:
    event({'event':'prompt','kind':kind,'message':'test'})
    value=json.loads(sys.stdin.readline()).get('value')
    if value != wanted: sys.exit(9)
event({'event':'authenticated','id':'999','username':'test_fixture','name':'離線測試'})
'''

        class FixtureRunner(Runner):
            def _build_args(self, request, runtime):
                encoded = base64.b64encode(script.encode()).decode()
                return [sys.executable, "-c", "import base64;exec(base64.b64decode(%r))" % encoded]

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                self.assertTrue(bridge.available)
                bridge.runner = FixtureRunner(bridge.store)
                prompts = []

                def on_event(event):
                    if event.get("event") == "prompt":
                        prompts.append(event["kind"])
                        answer = {"phone": "+886912345678", "code": "12345", "password": "test-password"}[event["kind"]]
                        self.assertTrue(bridge.respond_login(answer))

                result = bridge.login(
                    {"label": "離線測試", "method": "code", "phone": "+886912345678"},
                    threading.Event(), on_event,
                )
                self.assertTrue(result["authenticated"])
                self.assertIn("code", prompts)
                self.assertIn("password", prompts)
                account = bridge.store.list_accounts()[0]
                self.assertTrue(account.authenticated)
                all_saved = b"".join(path.read_bytes() for path in Path(directory).glob("*.json"))
                self.assertNotIn(b"test-password", all_saved)
                self.assertNotIn(b"12345", all_saved)

    def test_main_window_uses_real_store_and_closes_without_running_threads(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from desktop.app import BackendBridge, MainWindow, STYLE

        application = QApplication.instance() or QApplication([])
        application.setStyleSheet(STYLE)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".build") as directory:
            with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
                bridge = BackendBridge()
                window = MainWindow(bridge)
                window.show()
                deadline = time.monotonic() + 5
                while window.top_account.text() == "正在讀取帳號…" and time.monotonic() < deadline:
                    application.processEvents()
                    time.sleep(0.02)
                self.assertEqual(window.minimumWidth(), 960)
                self.assertEqual(bridge.accounts(), [])
                self.assertIsNone(window.home_page.account_combo.currentData())
                self.assertEqual(window.top_account.text(), "未登入")
                window.close()
                deadline = time.monotonic() + 5
                while not window.isHidden() and time.monotonic() < deadline:
                    application.processEvents()
                    time.sleep(0.02)
                self.assertTrue(window.isHidden())


if __name__ == "__main__":
    unittest.main()
