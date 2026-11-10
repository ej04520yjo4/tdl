# 桌面版交付驗證

日期：2026-11-10。版本：1.6.3。基底：1.6.2（`c0b0418`，已由 PR #8 合併為 `master` 的 `5d10caf`）。

## 本次範圍

桌面版下載失敗時也保留已完成的分段，重試從中斷處接續（使用者取消仍刪除；命令列不變）；新增 Windows CI workflow；gotd `v0.163.0`、x/net、x/crypto、klauspost/compress、otel、brotli 升級並以 Go 1.27.3 建置；PySide6 6.12.0、PyInstaller 6.22.3、pytest 9.2.0；`build.ps1` 加上 UTF-8 BOM；修正一個會隨機失敗的帳號名稱測試。帳號、設定與資料格式不變。

## 原始碼測試

- 最終建置內完整 Python suite：**221 passed、30 subtests passed**，40.78 秒（1.6.2 為 218 項；新增 CI 清單一致 2 項、失敗建議文字 1 項）。
- 建置後加入建置腳本編碼測試，完整 suite 再執行：**222 passed、30 subtests passed**，41.46 秒。
- Go：`build.ps1` 列出的套件全部通過（`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/forward`、`app/migrate`、`pkg/tmessage`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`core/forwarder` 無測試）；`core` 模組全部測試、`go vet` 通過。
- 新增 Go 測試：桌面版因網路錯誤失敗、或被另一個失敗檔案停止時保留分段且重試可讀回；只有仍在執行的桌面工作會保留，命令列與使用者取消不保留。
- `govulncheck`：升級前程式實際呼叫到 3 個漏洞（gotd GO-2026-6138、klauspost/compress GO-2026-6865、`crypto/x509` GO-2026-6809），升級後 **0**。
- 隨機失敗的測試：`test_phone_number_names_are_replaced_on_start` 檢查帳號檔不得含「912」，但隨機帳號 ID 可能含這三個數字（本次第一次建置遇到 `…-b912-…`）。改為排除 ID 欄位後再檢查，並以強制含「912」的 ID 驗證新寫法通過。

## 最終 EXE 離線驗證

- `verification.json`：複製到獨立目錄、移除 Python／Go PATH，核心、繁簡搜尋、畫面繪製與原生 Windows 視窗啟閉通過。
- `restart-verification.json`：兩個 EXE 程序驗證關閉保存、同一筆下載恢復、已完成附件不重寫、群組資料夾恢復。
- `workflow-verification.json`、`batch-forward-verification.json`：頻道／話題下載、位置記憶與批次轉傳離線流程通過。

## 真實 Telegram 驗證（使用者當次同意，只下載；已記憶帳號的隔離副本）

- `channel-live-verification.json`（最終 EXE，新版 gotd）：取得 650 個對話、包含起迄日的日期篩選與影片篩選正確、實際下載公開 @telegram 影片 1 個（1,842,304 bytes）；原始帳號檔案未改動。
- `resume-live-verification.json`（原始碼後端＋與 EXE 相同的內含核心，SHA-256 `39dcea96…6d61`）：公開 @telegram 影片 5,697,716 bytes，在 18.4% 暫停，工作狀態「已暫停」並保留 1 個分段與分段清單；續傳的第一個進度為 2,097,152 bytes（保留的 1 MB 加上新下載的 1 MB），完成檔與直接下載的 SHA-256 相同（`59fc15a8…55a1`）；原始資料前後雜湊相同。依同意範圍以 `--skip-batch-dry-run` 略過批次轉傳試跑，沒有任何轉傳呼叫。
- 續傳驗證的實際過程（如實記錄）：
  1. 預設大小 20–200 MB 找不到公開影片，改用 `TDL_PROBE_MIN_MB=5`。
  2. 預設 40% 暫停的一次回報「暫停後找不到暫存檔」。當時沒有診斷輸出，原因未確認；推測是 5.4 MB 影片在暫停送達前已下載完成。之後以同設定再跑 3 次，都保留分段且完成檔相同，沒有重現。
  3. 一次在暫停時 6 個分段都已寫完，驗證程式把每段都算成 1 MiB（最後一段其實較短）而誤判失敗；已修正計算。
  4. 為確保續傳時還有分段要補，最終收據以 `TDL_PROBE_PAUSE_AT=0.15` 執行並通過。
- **未驗證：**下載途中真的斷線後按「重試」接續（只有自動化測試）；上傳、轉傳實際送出與 QR 登入沒有在新版 gotd 上用真實帳號操作。

## 交付檔

Windows x64 GUI，版本 **1.6.3**，**56,865,598 bytes**（1.6.2 為 54,719,965 bytes；主要差異為 PySide6 6.12 與新版 gotd）。

EXE SHA-256：`edf22cc1a665074041939b26c29bf69bb2791b6d9702ff757a206fa54b540c75`。

內含核心 SHA-256：`39dcea966369761921ad801554ee7f62d5d8663aba91db402aa257ce17896d61`。

`TDL下載助手.exe`、`TDL下載助手-1.6.3.exe`、`dist/TDL下載助手.exe` 與 `TDL-Desktop-1.6.3.exe` 發行別名內容相同。收據與畫面保存於 `desktop/verification/1.6.3/`。

建置後才修改的檔案（`desktop/build.ps1` 加 BOM、`.editorconfig`、`desktop/tests/test_ci_workflow.py`、`desktop/resume_live_probe.py`、文件）都不會打包進 EXE；修改後完整 Python suite 另行執行通過。

## 已知限制

- 續傳只在桌面版的暫停、正常關閉與失敗重試時接續；強制結束、斷電或命令列 Ctrl+C 時該檔案從頭下載。暫停或失敗當下正在傳輸的分段會重抓。
- 失敗後保留的暫存檔在取消工作、清除已結束工作或刪除紀錄時刪除；工作一直留著不處理時，暫存檔也會留在下載資料夾。
- CI 只跑測試，不建置 EXE、不做真實 Telegram 驗證。
- 第一次完整測試卡住的原因（1.6.2 記錄）仍未找到；之後再發生會印出卡住位置。
- EXE 未簽章；上游完整 Go suite 既有 Windows `app/update/TestBinaryName` 差異未處理，只宣稱上述相關套件通過。
