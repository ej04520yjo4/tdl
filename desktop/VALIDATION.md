# 桌面版交付驗證

日期：2026-10-13。版本：1.3.2。基底：1.3.1 的 `c04aa5f`。

## 本次範圍

1. 頻道／群組、論壇話題與下載紀錄共用繁簡搜尋；保留原文，支援 emoji、全形英數、大小寫及 `@username`。
2. 新工作預設依群組／頻道名稱存放附件。名稱對應在同帳號與下載根目錄中保存，重開、群組改名及同名群組都保持穩定路徑。
3. 舊工作缺少 `group_by_chat` 時維持原本平面路徑；不搬移過去的檔案。

## 測試結果與證據層級

- Python 完整桌面 suite：**134 passed、22 subtests passed**，30.65 秒。
- Go 相關套件全部通過：`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/migrate`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`pkg/tmessage` 無測試。
- 搜尋使用 Windows NLS 的真實字元轉換，並以隔離 UI 資料驗證繁簡比對與原始名稱顯示；不連線 Telegram。
- Go iterator 使用假 peer／訊息及本機檔案，驗證子目錄、安全名稱、同名／大小寫碰撞、skip-same；名稱對應測試包含重新建立 iterator、群組改名、已完成群組被略過、不同輸出根目錄、讀寫失敗及損壞資料。不是網路下載測試。
- 最終單檔 EXE：複製至獨立目錄，移除 Python／Go PATH；內含核心、繁簡搜尋、畫面繪製、原生 Windows 視窗啟閉均通過。
- 兩個最終 EXE 程序搭配離線模擬核心：關閉保存、原工作／帳號、分組設定保存及恢復、已完成檔案不重寫、剩餘檔案寫入及每檔進度恢復通過。這項驗證檢查 GUI 傳遞分組旗標；Go 真正資料夾處理由 iterator 測試驗證。
- 解出最終 EXE 的真實 Go 核心，確認與測試核心雜湊相同；全部桌面參數、備份／還原及鎖住目的備份保護等 **3 個離線核心測試**通過。
- 本次沒有真實 Telegram 帳號或傳輸驗證，也未實際上傳／轉傳。不沿用舊版真實驗證收據。

## 交付檔案

EXE：Windows x64 GUI，**1.3.2**，**54,538,262 bytes**。

EXE SHA-256：`c9a4bfb2cc7c0e5dcecdf41f8ed4e5f9d25b25727bc959c36636f5d383b09f3c`。

內含 Go 核心 SHA-256：`b97f155321765807638d8e3efbb33b6b4e90d51e7cc07865ddfc9b21bb9c9e34`。

收據：`desktop/verification/1.3.2/verification.json`、`restart-verification.json`，皆綁定上述 EXE 雜湊。

`desktop/build.ps1` 的 Go、Python、打包與最終 EXE 驗證通過。最後覆蓋根目錄舊 EXE 因 1.3.1 仍執行而受 Windows 檔案鎖阻擋，因此另交付 `TDL下載助手-1.3.2.exe`；GitHub 附件由已驗證的 dist EXE 產生並逐一核對雜湊。2026-10-17 確認根目錄 `TDL下載助手.exe` 已與上述 EXE 同雜湊，詳見 1.3.2 交接清冊的「交接確認」。

## 限制

- 繁簡字元轉換不等於同義詞或語意搜尋；Windows 外的開發環境僅提供 Unicode／大小寫正規化。
- 手動移動／改名下載資料夾不會自動更新群組對應。不同帳號的群組對應各自保存。
- 下載接續以檔案為單位；尚未完成的單一附件可能重新下載。
- 完整上游 Go suite 有既有 Windows `app/update/TestBinaryName` 差異，這次只宣稱相關套件通過。
- EXE 未簽章。
