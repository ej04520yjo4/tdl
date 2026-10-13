# 桌面版交付驗證

日期：2026-10-13。版本：1.2.0。基底修訂：`70c561c0a6d44d8d7fffdea14988fc4104bc56d2`。

## 測試範圍

- 下載佇列固定高度、有獨立垂直捲軸；進度及新增項目保留捲動位置。每個檔案依穩定識別碼固定顯示，長檔名不改變行高。
- 真實 Qt worker、Store、Runner 與離線模擬核心測試：關閉保存暫停；同一筆工作與原帳號在重開時自動接續；已完成檔案不重寫；手動取消不自動啟動；儲存清單途中關閉或取消不會啟動核心。
- JSON 清單先保存工作副本；刪除原始清單後仍可接續。完成、失敗、取消及暫停狀態區分；各檔案進度保存並恢復。
- 論壇清單保留 forum 標記；選擇群組後才呼叫 `chat topics --chat`。必須選定話題才可掃描，搜尋隱藏原話題會清除舊選擇；明確選全部才掃描整個群組。
- 話題 ID 限制在 MTProto int32 正整數範圍。General（ID 1）使用 history 再排除其他話題；其他 ID 使用 getReplies。整組相簿不會擴大日期、類型與話題清單。
- 保留先前回歸：台灣日期整日範圍、媒體分類、typed peer、匯出筆數／大小一致、部分清單警示、帳號切換、Qt GUI 執行緒更新、QR 登入、取消及程序環境隔離。
- 核心下載失敗須傳入 OnDone，不得將部分檔案標為完成；完成 checkpoint 與每檔 completed 事件在成功後處理後才提交。

話題語意參照 [Telegram 官方論壇說明](https://core.telegram.org/api/forum)及 [messages.getReplies](https://core.telegram.org/method/messages.getReplies)。

## 離線與真實帳號驗證的區別

`desktop/verify_release.py` 在移除 Python、Go 的 PATH 下啟動獨立 EXE，驗證核心定位、離線畫面及原生 Windows 視窗正常關閉。

`desktop/verify_restart_release.py` 使用離線模擬核心，分兩次啟動最終 EXE，驗證保存、重開、自動恢復、同一工作、原帳號及完成檔案不重寫。此項不代表真實 Telegram 中斷下載的全情境驗證。

`desktop/verify_channel_ui_release.py` 使用記憶帳號的加密副本，實際操作最終 EXE 的更新清單按鈕兩次、搜尋及選取；帳號具有論壇時，再查詢並選擇一個話題、清除選擇，確認掃描按鈕狀態。只查詢中繼資料，不掃描或下載私人附件；報告只有數量及結果，不含名稱或 ID。

`desktop/verify_channel_release.py` 使用同樣的隔離方式，取得清單、掃描公開 `@telegram` 的影片與台灣日期範圍，並下載一個小於 2 MiB 的公開影片。

`desktop/verify_qr_release.py` 驗證最終 EXE 的登入按鈕可取得並顯示 QR Code，於掃碼登入前取消，不保存 QR 或登入憑證。

真實帳號驗證後比對原始 accounts、preferences、加密 session 的 SHA-256；原資料必須完全未變。測試副本與附件用完移除。

## 本版本最終結果

- Python 桌面完整 suite：**73 passed、10 subtests passed**。
- Go 相關套件及核心 downloader 測試通過：`pkg/desktopio`、`app/chat`、`app/dl`、`pkg/tmessage`（無測試）、`cmd`、`core/downloader`。
- 原生視窗、獨立 EXE、離線畫面，以及兩次 EXE 啟動的關閉／恢復流程全部通過。
- 最終 EXE 真實頻道介面：兩次更新、**618 個對話**、所選論壇 **13 個話題**，選取／清除話題與掃描按鈕狀態正確；GUI thread violations **0**。耗時 13.438 秒。
- 最終 EXE 真實公開影片下載：日期與影片類型篩選通過，下載 **1 個檔案、1,842,304 bytes**，耗時 22.109 秒。
- QR Code 實際顯示通過，耗時 4.859 秒，掃碼登入前取消。
- 真實帳號來源檔案雜湊不變，隔離副本已清除。
- 九張離線 fixture 畫面涵蓋 1180×800、960×680、頻道及話題選取；另檢視實際 EXE 的獨立檔案進度畫面。

EXE：Windows x64 GUI，版本 **1.2.0**，大小 **54,445,824 bytes**。

SHA-256：`3ef03c924cf6fa01570318edaf328a115cba558341271035590b6fe715b7b351`。

可稽核收據保存在 `desktop/verification/1.2.0/`，發行資料夾也保留同名 JSON。五份收據皆綁定相同的最終 EXE 雜湊。

初次話題介面驗證在串流清單事件送達、但背景工作尚未清理時提前選取，隨後完成事件重設了測試選擇，因此未列為通過。驗證程式現等待控制項可操作及背景執行緒結束，再進行選取；來源版及最終 EXE 已重跑通過。


## 已知限制

- 完成附件可略過；未完成的單一檔案可能從頭重抓，不宣稱位元組續傳。
- 私人權限、已刪除訊息、磁碟空間、網路及大檔長時間下載依實際情況而定。真實下載驗證僅使用公開小型影片。
- 原始專案完整 Go suite 有既有 Windows `app/update/TestBinaryName`：預期 `tdl`、實際 `tdl.exe` 不一致。本次不宣稱原始專案全套通過。
- EXE 是未簽章的 Windows x64 GUI 程式，首次開啟需解壓內含元件。
