"""Regression tests for the bounded, stable download queue viewport."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from desktop.app import DownloadJob, DownloadPage


@unittest.skipUnless(QApplication, "PySide6 is required")
class QueueScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _jobs(self, count: int) -> list[DownloadJob]:
        return [
            DownloadJob(
                id=f"queue-{index}",
                url=f"https://t.me/example/{index}/" + ("x" * 180),
                title=f"第 {index} 個下載項目 " + ("超長檔名前綴 " * 8),
                output_dir="C:/Downloads",
            )
            for index in range(count)
        ]

    def _settle(self, page: DownloadPage) -> None:
        page.show()
        self.app.processEvents()
        page.queue_scroll.widget().adjustSize()
        page.queue_scroll.updateGeometry()
        self.app.processEvents()

    def test_queue_has_independent_bounded_scroll_at_supported_window_sizes(self):
        page = DownloadPage()
        page.add_jobs(self._jobs(40))
        for width, height in ((960, 680), (1180, 800)):
            page.resize(width, height)
            self._settle(page)
            self.assertEqual(page.queue_scroll.height(), 280)
            self.assertGreater(page.queue_scroll.verticalScrollBar().maximum(), 0)
            self.assertEqual(page.queue_scroll.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            row_heights = {row.height() for row in page.rows.values()}
            self.assertEqual(len(row_heights), 1)
            self.assertGreaterEqual(next(iter(row_heights)), 96)
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_progress_updates_and_new_jobs_preserve_queue_position(self):
        page = DownloadPage()
        jobs = self._jobs(36)
        page.add_jobs(jobs)
        self._settle(page)
        scrollbar = page.queue_scroll.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(scrollbar.maximum() // 2)
        before = scrollbar.value()
        row_heights = {job_id: row.height() for job_id, row in page.rows.items()}

        for index in range(80):
            job = jobs[index % len(jobs)]
            job.status = "下載中"
            job.progress = index % 101
            job.speed = "目前檔案：" + ("非常長的影片檔名_" * 40) + f" · {index} MB/s"
            page.update_job(job)
            self.app.processEvents()

        self.assertEqual(scrollbar.value(), before)
        self.assertEqual(row_heights, {job_id: row.height() for job_id, row in page.rows.items()})
        self.assertEqual(page.rows[jobs[0].id].percent.width(), page.rows[jobs[-1].id].percent.width())

        page.add_jobs(self._jobs(8))
        self.app.processEvents()
        self.assertEqual(scrollbar.value(), before)
        page.close()
        page.deleteLater()
        self.app.processEvents()

    def test_concurrent_file_progress_has_stable_file_rows_and_summary(self):
        page = DownloadPage()
        jobs = self._jobs(18)
        page.add_jobs(jobs)
        self._settle(page)
        scrollbar = page.queue_scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum() // 2)
        before = scrollbar.value()
        job = jobs[0]
        job.status = "下載中"
        job.file_progress = {
            "file-b": {
                "filename": "第二個檔案_" + ("很長的名稱" * 20),
                "percent": 26,
                "state": "downloading",
            },
            "file-a": {"filename": "第一個檔案.mp4", "percent": 100, "state": "completed"},
        }
        page.update_job(job)
        self.app.processEvents()
        row = page.rows[job.id]
        self.assertTrue(row._has_file_progress)
        self.assertFalse(row.progress.isVisible())
        self.assertEqual(row._file_order, ["file-b", "file-a"])
        self.assertIn("檔案 1/2", row.percent.text())
        self.assertIn("進行中 1", row.percent.text())
        first_height = row.height()
        self.assertEqual(scrollbar.value(), before)

        # Reordered callbacks update existing rows in place; they must not
        # reorder the visible list or change its geometry.
        job.file_progress = {
            "file-a": {"filename": "第一個檔案.mp4", "percent": 100, "state": "completed"},
            "file-b": {
                "filename": "第二個檔案_" + ("很長的名稱" * 20),
                "percent": 55,
                "state": "downloading",
            },
        }
        for _ in range(30):
            job.file_progress["file-b"]["percent"] = 40 + (_ % 60)
            page.update_job(job)
            self.app.processEvents()
        self.assertEqual(row._file_order, ["file-b", "file-a"])
        self.assertEqual(row.height(), first_height)
        self.assertEqual(scrollbar.value(), before)
        self.assertEqual(row._file_rows["file-a"][2].text(), "已完成")
        self.assertTrue(row._file_rows["file-b"][2].text().endswith("%"))
        job.status = "已暫停"
        page.update_job(job)
        self.assertTrue(row.retry_button.isVisible())
        self.assertEqual(row.retry_button.text(), "繼續下載")
        page.close()
        page.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
