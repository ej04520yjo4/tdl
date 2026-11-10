# 桌面版交付驗證

日期：2026-11-09。版本：1.6.2。基底：`master` 的 `6d36b6f`（1.6.1）。

## 本次範圍

暫停或關閉後只補下載缺少的 1 MB 分段；轉傳到論壇「一般」話題時不帶話題編號；進度存檔失敗不再中斷下載，Windows 取代檔案時等待短暫鎖定；DPAPI 加密失敗時不再改存明文；測試加上硬性時限；刪除 `BackendBridge.download` 不會執行到的分支。帳號、設定與資料格式不變。

## 原始碼測試

- 最終建置內完整 Python suite：**218 passed、30 subtests passed**，44.78 秒（1.6.1 為 211 項，新增 7 項）。
- Go 相關套件通過：`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/forward`、`app/migrate`、`pkg/tmessage`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`core/forwarder` 無測試。
- 新增測試涵蓋：只補缺少的分段、拒收長度不對的分段、暫停保留與取消清除、分段清單不符時重新下載、`pause` 指令、「一般」話題、一次存檔失敗不中斷下載、Windows 取代重試、DPAPI 失敗不寫明文且保留上一份加密資料、取消與清除工作時只刪除該工作的暫存檔。
- 故意卡住的測試會在時限到時印出所有執行緒堆疊並以錯誤結束。

## 最終 EXE 離線驗證

- `verification.json`：複製到獨立目錄、移除 Python／Go PATH，核心、繁簡搜尋、畫面繪製與原生 Windows 視窗啟閉通過。
- `restart-verification.json`：兩個 EXE 程序驗證關閉保存、原帳號同一筆下載恢復、已完成附件不重寫、群組資料夾恢復。
- `workflow-verification.json`、`batch-forward-verification.json`：頻道／話題下載、位置記憶與批次轉傳離線流程通過。

## 真實 Telegram 驗證（使用者同意，已記憶帳號的隔離副本）

- `channel-live-verification.json`（最終 EXE）：650 個對話、日期與影片篩選正確、實際下載公開 @telegram 影片 1 個；原始帳號檔案未改動。
- `resume-live-verification.json`（原始碼後端＋與 EXE 相同的內含核心）：8,505,533 bytes 的公開影片在 63% 暫停，保留 7 個分段；續傳從 7,456,957 bytes 接續，完成的檔案與直接下載的 SHA-256 相同；批次轉傳只試跑 3 則，未送出任何訊息；原始資料前後雜湊相同。
- **未驗證：**轉傳到「一般」話題的實際送出（使用者同意的範圍只有試跑）。

## 交付檔

Windows x64 GUI，版本 **1.6.2**，**54,719,965 bytes**。

EXE SHA-256：`8eafc359c59433a93ea4660380e1e2b31d01f9c6cd5e302c855fddbc36357dd1`。

內含核心 SHA-256：`d116a42defad542a96aae4b4d4d4d8734040f065ab21b265cf2dca86ee741aaf`。

`TDL下載助手.exe`、`TDL下載助手-1.6.2.exe`、`dist/TDL下載助手.exe` 與 `TDL-Desktop-1.6.2.exe` 發行別名內容相同。收據與畫面保存於 `desktop/verification/1.6.2/`。

## 已知限制

- 續傳只適用桌面版的「暫停」與正常關閉；強制結束、斷電或命令列 Ctrl+C 時該檔案從頭下載。暫停當下正在傳輸的分段會重抓。
- 1.6.1 以前暫停的工作沒有分段清單，接續時該檔案從頭下載。
- 第一次完整測試卡住的原因仍未找到；之後再發生會印出卡住位置。
- EXE 未簽章；上游完整 Go suite 既有 Windows `app/update/TestBinaryName` 差異未處理，只宣稱上述相關套件通過。
