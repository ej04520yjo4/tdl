# 桌面版交付驗證

日期：2026-10-25。版本：1.5.1。基底：1.5.0 `2fa8c1e`。

## 本次範圍

下載前選擇電腦儲存位置、記住上次位置、頻道下載／下載中心／設定頁同步、自動詢問開關。取消時保留掃描結果；原有工作、重試及重開接續保留原路徑。加入選擇器返回後的來源檢查與未登入帳號先行檢查。

## 原始碼測試

- 最終建置內完整 Python suite：**188 passed、22 subtests passed**，44.49 秒。
- Go 相關套件通過：`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/migrate`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`pkg/tmessage` 無測試。最終建置使用有效快取。
- 首次測試為 183 passed、2 setup errors（系統 pytest 暫存目錄無法建立）；改用專案內 `.build/pytest-destination-final-1.5.1` 後全部通過。兩個錯誤都發生於校驗檔測試執行前。
- 原始碼 Qt 設定頁截圖已檢視：儲存位置開關及預設資料夾在一般設定中可見，未裁切。

## 最終 EXE 離線驗證

- `verification.json`：複製到獨立資料夾、移除 Python／Go PATH 後，核心、繁簡搜尋、畫面繪製與原生 Windows 視窗啟閉通過。
- `workflow-verification.json`：取消不建工作且保留掃描；選 A 同步三頁並保存；選 B 後舊工作維持 A、新工作使用 B；重新建立 MainWindow 與 Store 恢復 B 及詢問開關。原生資料夾選擇器的**回傳值以隔離 fixture 提供**，不是人工點選 Windows 選擇器。
- 原有頻道／話題流程通過；GUI 執行緒違規 **0**，兩個尺寸共 **4** 組版面與 **8** 項按鈕捲動檢查通過。
- `restart-verification.json`：兩個 EXE 程序驗證關閉保存、原帳號同一筆工作恢復、已完成附件不重寫及群組資料夾設定恢復。
- 最終 EXE 畫面與收據保存於 `desktop/verification/1.5.1/`。`settings.png` 為原始碼 fixture，其餘截圖來自 EXE。

## 交付檔

Windows x64 GUI，版本 **1.5.1**，**54,638,808 bytes**。

EXE SHA-256：`7a355091d8c2862949bc7c0aa3cbd7b1549717662f56d168e0b01eab7eb12454`。

內含核心 SHA-256：`4d2d6025d0194815b6bbcded9ee2e27fcab882b47e415fd3f94b6713e2db771e`。

`TDL下載助手.exe`、`TDL下載助手-1.5.1.exe`、`dist/TDL下載助手.exe` 與發行別名內容相同。

## 已知限制

- **未執行本版真實 Telegram 登入或附件傳輸測試**，也未沿用舊版連線收據。
- 磁碟無權限、斷線、空間不足仍由下載工作回報；測試未覆蓋每種網路磁碟。
- 日期依目前群組／頻道訊息日期；話題檔案放入所屬群組資料夾。
- 接續以附件為單位，未完成的單一附件可能重新下載；EXE 未簽章。
- 上游完整 Go suite 既有 Windows `app/update/TestBinaryName` 差異未在本次處理，只宣稱相關套件通過。
