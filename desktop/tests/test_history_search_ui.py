"""Offline download-history search regression tests."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from desktop.app import HistoryInfo, HistoryPage


def test_history_search_matches_simplified_text_and_keeps_visible_name() -> None:
    application = QApplication.instance() or QApplication([])
    page = HistoryPage()
    page.set_history(
        [
            HistoryInfo(
                id="1",
                title="台灣攝影影片",
                status="已完成",
                path="D:/下載/台灣攝影影片",
            ),
            HistoryInfo(
                id="2",
                title="器材交流群組",
                status="失敗",
                path="D:/下載/器材交流群組",
            ),
        ]
    )

    page.search.setText("台湾摄影影片")
    application.processEvents()

    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "台灣攝影影片"

    page.search.setText("群组")
    application.processEvents()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "器材交流群組"

    page.deleteLater()
