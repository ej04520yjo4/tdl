# 桌面版交付驗證

日期：2026-11-02。版本：1.6.1。基底：`codex/group-batch-forward` 的 `bdd223d`（1.6.0）。

## 本次範圍

效能與 JSON 匯入修正：逐檔下載進度改存各工作的 `progress.json`，共用的佇列與紀錄檔只留摘要；佇列每個工作最多列出 20 個檔案；Go 核心讀取匯出檔開頭時讀到 `messages` 就停，並修正提早離開時背景解碼與重讀互搶、只讀到部分訊息的問題。功能與操作流程不變。

## 原始碼測試

- 最終建置內完整 Python suite：**211 passed、30 subtests passed**，43.06 秒（1.6.0 為 207 項，新增 4 項）。
- Go 相關套件通過：`pkg/desktopio`、`app/chat`、`app/dl`、`app/up`、`app/forward`、`app/migrate`、`pkg/tmessage`、`cmd`、`core/downloader`、`core/uploader`、`core/util/tutil`；`pkg/key`、`core/forwarder` 無測試。
- `pkg/tmessage` 首次加入測試：tdl 匯出、官方匯出（無 `peer_type`）與字串 id 只讀開頭；`messages` 在 `id` 前面時逐項略過；非物件與截斷檔回報錯誤；20,000 則訊息先讀開頭、回到檔首後完整收集。
- 後端：逐檔進度不出現在 `queue.json`／`downloads.json`、舊版逐檔紀錄啟動時搬移、200 筆連續失敗寫入次數少於 20。
- 介面：5,000 檔工作最多 20 列、失敗列保留、新下載接用已完成列、其餘只計數。

## 效能量測（原始碼，非 EXE）

| 項目 | 1.6.0 | 1.6.1 |
| --- | --- | --- |
| 每次進度存檔（另有 10 個各 2,000 檔失敗工作，目前工作 500–5,000 檔） | 0.47–0.69 秒 | 29–86 毫秒 |
| `queue.json`／`downloads.json` 大小（同上） | 6.6–8 MB | 不到 0.1 MB |
| 還原 5,000 檔工作的佇列列 | 0.89 秒、20,011 個元件、高 125,101 px | 0.03 秒、92 個元件、高 624 px |

JSON 匯入漏讀的離線重現（舊做法）：20,000 則訊息、兩次讀取間隔 0–5 毫秒時只讀到 115 則；200,000 則、間隔 50 毫秒時只讀到 58 則。新做法不留背景解碼，測試讀到全部 20,000 則。

## 最終 EXE 離線驗證

- `verification.json`：複製到獨立目錄、移除 Python／Go PATH，核心、繁簡搜尋、畫面繪製與原生 Windows 視窗啟閉通過。
- `restart-verification.json`：兩個 EXE 程序驗證關閉保存、原帳號同一筆下載恢復、已完成附件不重寫、群組資料夾恢復，以及重開後逐檔列仍顯示。
- `workflow-verification.json`：既有頻道／話題下載、取消選擇資料夾保留掃描、不同工作維持原路徑、跨頁同步與重開位置記憶通過。
- `batch-forward-verification.json`：批次轉傳離線流程全部通過。
- 收據與畫面保存於 `desktop/verification/1.6.1/`。`queue-5000-files-source-render.png` 為原始碼繪製，不是 EXE 截圖。

## 交付檔

Windows x64 GUI，版本 **1.6.1**，**54,696,790 bytes**。

EXE SHA-256：`3d8d94a4cd4ae977b1d27304d4e0c86e16c7cca143715a7b98cc86c371a66583`。

內含核心 SHA-256：`404712480d24d038c5cf39a9eed8fc01783056d63959ac8bdf88d852ddb90fe6`。

`TDL下載助手.exe`、`TDL下載助手-1.6.1.exe`、`dist/TDL下載助手.exe` 與 `TDL-Desktop-1.6.1.exe` 發行別名內容相同。

## 已知限制

- **未執行本版真實 Telegram 登入、下載或傳送測試**；JSON 匯入漏讀以離線測試重現及驗證。
- 截斷或格式錯誤的 JSON 匯入檔現在整筆失敗，不再只下載前段。
- 退回 1.6.0 時，暫停工作仍能接續，但不會顯示 1.6.1 期間保存的逐檔列。
- 既有下載重開接續仍以附件為單位；未完成的單一附件可能重下。EXE 未簽章。
- 上游完整 Go suite 既有 Windows `app/update/TestBinaryName` 差異未在本次處理，只宣稱上述相關套件通過。
