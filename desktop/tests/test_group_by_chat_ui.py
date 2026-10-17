"""The GUI keeps a job's folder layout stable when it is restored."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from desktop.app import BackendBridge, DownloadJob, MainWindow


def test_new_gui_job_persists_group_folder_default() -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=root / ".build") as directory:
        with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            account = bridge.store.add_account("離線資料夾驗證")
            target = str(Path(directory) / "downloads")
            job = DownloadJob("ui", "https://t.me/telegram/193", "測試頻道", target)
            saved = bridge.prepare_download(job, {"account_id": account.id})
            assert saved.group_by_chat is True
            assert saved.output_dir == str(Path(target).resolve())
            restored = MainWindow._restore_job(saved, 0)
            assert restored.options["group_by_chat"] is True
            assert restored.output_dir == saved.output_dir
            assert bridge.prepare_download(restored, restored.options).id == saved.id
            assert len(bridge.store.list_queue()) == 1


def test_legacy_gui_job_keeps_flat_directory_when_prepared_again() -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=root / ".build") as directory:
        with patch.dict(os.environ, {"TDL_DESKTOP_DATA_DIR": directory}):
            bridge = BackendBridge()
            account = bridge.store.add_account("舊工作驗證")
            target = str(Path(directory) / "downloads")
            legacy = {
                "id": "legacy", "account_id": account.id,
                "links": ["https://t.me/telegram/193"],
                "output_dir": target, "status": "paused",
            }
            restored = MainWindow._restore_job(legacy, 0)
            assert restored.options["group_by_chat"] is False
            # Exercise the adapter's creation path as well as reading an
            # existing durable record: it must pass false explicitly.
            restored.backend_job_id = ""
            saved = bridge.prepare_download(restored, restored.options)
            assert saved.group_by_chat is False
            assert saved.output_dir == str(Path(target).resolve())
