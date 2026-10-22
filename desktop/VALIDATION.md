# 桌面版交付驗證

日期：2026-10-22。版本：1.5.0。基底：master `64c1638`。

## 本次範圍

人性化介面 19 項：右上角單一帳號選單與登入提示、首次啟動直接登入、不以電話號碼命名帳號、佇列置頂、失敗改為提示列、完成通知與工作列進度、直接開啟群組資料夾、設定自動儲存與代理檢查、頻道頁狀態精簡、隱藏內部識別碼、用語改寫、下載紀錄整理、拖放與剪貼簿連結、側邊欄圖示、清單省略、停滯提示、錯誤建議與複製、記住視窗。保留帳號、重開接續、繁簡搜尋與群組資料夾格式。

## 驗證結果與交付

自動化 fixture、最終 EXE 離線驗證、原生 Windows 檢查及真實 Telegram 分開列出，不能互相替代。

**自動化 fixture（原始碼）**

- Python 完整桌面 suite：**174 passed、22 subtests passed**，40.39 秒。
- Go 相關套件通過：`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/migrate`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`pkg/tmessage` 無測試。
- `qa_capture.py` 隔離 fixture 畫面 12 張已檢視。

**最終 EXE 離線驗證**

- 獨立資料夾、移除 Python／Go PATH：內含核心、繁簡搜尋、畫面繪製、原生 Windows 視窗啟閉通過。
- 離線頻道流程：清單、掃描、加入後留在頻道頁、查看佇列、返回、換頻道使舊結果失效、論壇話題掃描通過；GUI 執行緒違規 0；1180×800、960×680 共 4 組版面無水平裁切，8 項按鈕捲動可見性通過。
- 兩個 EXE 程序的重開接續：保存暫停狀態、原帳號與工作、已完成附件不重寫、群組設定恢復。

**原生 Windows 檢查（原始碼，非 EXE）**

- 工作列進度（一般、失敗未讀時紅色、清除）、系統匣圖示建立與釋放、`ui-state.json` 寫入。

**真實 Telegram**

- 未執行本版真實帳號或附件傳輸測試，不沿用舊版連線驗證收據。

EXE：Windows x64 GUI，**1.5.0**，**54,629,456 bytes**。

SHA-256：`5fbc3f70cafd42ed4918deb05bbe3af1252642a8724b8aaaa524a7f8352c8152`。

內含 Go 核心 SHA-256：`6538a1cf473fb0fbaeb67b5e0b757968f90a0f72077c71777cd97c8203f9ebea`。

根目錄 `TDL下載助手.exe`、`TDL下載助手-1.5.0.exe` 與 `dist/TDL下載助手.exe` 為相同內容。收據與離線 fixture 畫面位於 `desktop/verification/1.5.0/`。

## 限制

- 日期按訊息進入目前群組／頻道的日期；轉傳訊息不採原始發布日。
- 接續下載以檔案為單位，未完成的單一附件可能重新下載。
- 系統匣通知與工作列進度只在一般 Windows 視窗啟用；剪貼簿只在程式回到前景時讀取一次。
- 停滯提示只是說明，不會中止或重啟工作。
- 上游完整 Go suite 有既有 Windows `app/update/TestBinaryName` 差異，本次只宣稱相關套件通過。
- Windows EXE 未簽章。
