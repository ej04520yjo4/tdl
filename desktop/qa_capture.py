"""Render real Qt widgets with isolated fixture data; never contact Telegram."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
os.environ["TDL_DESKTOP_DATA_DIR"] = str(ROOT / ".build" / "qa-fixtures")

from PySide6.QtCore import QLocale, QTranslator, QLibraryInfo, QEventLoop, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from desktop.app import BackendBridge, MainWindow, LoginDialog, STYLE


def settle(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def main():
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    for name in ("msjh.ttc", "msjhbd.ttc"):
        QFontDatabase.addApplicationFont(str(Path(os.environ["QT_QPA_FONTDIR"]) / name))
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    QLocale.setDefault(QLocale("zh_TW"))
    translator = QTranslator(app)
    if translator.load("qtbase_zh_TW", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)
    bridge = BackendBridge()
    fixture_accounts = bridge.store.list_accounts()
    if not fixture_accounts:
        fixture = bridge.store.add_account("介面測試帳號")
        bridge.store.update_account(fixture.id, authenticated=True)
        bridge.store.set_active_account(fixture.id)
    window = MainWindow(bridge)
    window.show()
    settle(900)
    channel_page = window.channel_page
    channel_page._operation_succeeded(channel_page._generation, "list", {"chats": [
        {"id": "10001", "ref": "channel:10001", "type": "channel", "title": "Telegram 功能與教學", "username": "example_updates"},
        {"id": "10002", "ref": "channel:10002", "type": "channel", "title": "旅行影片收藏", "username": ""},
        {"id": "10003", "ref": "channel:10003", "type": "group", "title": "攝影與剪輯交流", "username": ""},
        {"id": "10004", "ref": "channel:10004", "type": "channel", "title": "每週學習筆記", "username": "example_learning"},
    ]})
    channel_page.chat_list.setCurrentRow(0)
    channel_page._operation_succeeded(channel_page._generation, "export", {"path": str(ROOT / ".build" / "qa-fixture-export.json"), "count": 12})
    target = ROOT / "desktop" / "qa"
    target.mkdir(exist_ok=True)
    for index, name in enumerate(("channels", "home", "accounts", "history", "settings")):
        window._show_page(index)
        settle(200)
        window.grab().save(str(target / f"{name}.png"))
    window._show_page(0)
    window.resize(960, 680)
    settle(200)
    window.grab().save(str(target / "channels-compact.png"))
    # Embedded topic fixtures exercise the actual selection layout without
    # giving the page a live network runner.
    real_runner = channel_page._runner
    channel_page._runner = SimpleNamespace()
    channel_page._operation_succeeded(channel_page._generation, "list", {"chats": [
        {"id": "10005", "ref": "channel:10005", "type": "group", "forum": True,
         "title": "攝影與剪輯交流", "topics": [
             {"id": 1, "title": "一般"}, {"id": 45, "title": "拍攝教學影片"},
             {"id": 82, "title": "作品分享"}, {"id": 107, "title": "剪輯素材與範例"},
         ]},
    ]})
    channel_page.chat_list.setCurrentRow(0)
    channel_page.topic_list.setCurrentRow(2)
    for width, height, name in ((1180, 800, "topics"), (960, 680, "topics-compact")):
        window.resize(width, height)
        settle(200)
        window.grab().save(str(target / f"{name}.png"))
    channel_page._runner = real_runner
    dialog = LoginDialog(bridge, window)
    dialog.show()
    settle(200)
    dialog.grab().save(str(target / "login.png"))
    dialog.close()
    window.close()
    settle(200)
    print("Captured nine offline widget screenshots, including channel and forum topic selection.")


if __name__ == "__main__":
    main()
