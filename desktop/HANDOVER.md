# 桌面版交接清冊

每次更新都要新增版本清冊、驗證交付 EXE，並同步至 GitHub。維護規則見專案根目錄 `AGENTS.md`。

| 版本 | 交接清冊 | 範圍 |
| --- | --- | --- |
| 1.4.0 | [1.4.0](handovers/1.4.0.md) | 明確顯示頻道／話題與掃描來源、連續選頻道下載、佇列往返入口、過期結果與重複加入保護 |
| 1.3.2 | [1.3.2](handovers/1.3.2.md) | 繁簡共用搜尋、依群組／頻道名稱存放下載、新舊工作接續相容 |
| 1.3.1 | [1.3.1](handovers/1.3.1.md) | 備份失敗保留舊檔、副檔名篩選不分大小寫、還原失敗確實清理新帳號 |
| 1.3.0 | [1.3.0](handovers/1.3.0.md) | 進度管線效能、上傳與轉傳、帳號備份還原、進階下載設定、佇列整體進度與清除、頻道清單快取；修正上傳失敗仍刪除本機檔案 |
| 1.2.0 | [1.2.0](handovers/1.2.0.md) | 穩定下載佇列、重開接續、群組話題選擇；首次同步完整桌面版來源與發行檔 |

目前版本的測試記錄見 `VALIDATION.md`，執行檔 SHA-256 見 Release 的 `build-info.json`。

2026-10-17：依使用者指示，以 [ej04520yjo4/tdl#1](https://github.com/ej04520yjo4/tdl/pull/1) 將 `codex/traditional-chinese-desktop` 以合併提交併入 `master`，保留分支與各版本的提交紀錄。各版本清冊中「`master` 尚未合併」是當時的狀態。

2026-10-22：依使用者指示，以 [PR #2](https://github.com/ej04520yjo4/tdl/pull/2) 將 `codex/channel-workflow` 的 1.4.0 更新合併至 `master`，並同步本機主分支；EXE 沿用原發布版本，詳見 [1.4.0 主分支同步紀錄](handovers/1.4.0.md)。

## 尚未發行的修正

以下修正已在原始碼中，尚未隨版本發布。下一版建立交接清冊時，把它們列入該版清冊並從這裡移除。

### 2026-10-22：發行校驗檔改用 LF 換行

- 需求：使用者核對 desktop-v1.5.0 附件時發現 `SHA256SUMS.txt` 是 CRLF 換行，`sha256sum -c`（GNU coreutils，例如 Git Bash、Linux）把每個檔名讀成結尾多一個 `\r`，每一項都回報「No such file or directory」；去除 `\r` 後雜湊值正確。
- 原因：`package_release.py` 以 `Path.write_text` 寫檔，Windows 會把 `\n` 轉成 `\r\n`；`build-info.json` 也一樣。
- 修改：新增 `write_release_text`（一律寫 LF）與 `write_checksums`，`SHA256SUMS.txt` 與 `build-info.json` 都改由它們寫出。`package_release.py` 改用與 `app.py` 相同的匯入方式，讓測試能匯入 `desktop.package_release`；建置仍以指令碼執行。新增 `desktop/tests/test_package_release.py`，檢查輸出沒有 `\r` 且內容格式正確。
- 自動化 fixture：以 `master`（1.4.0）為基底，Python 完整桌面 suite **147 passed、22 subtests passed**；暫時與 `claude/humane-ui-1.5.0` 合併時沒有衝突，**176 passed、22 subtests passed**。暫時拿掉 LF 設定時，新測試確實失敗。
- 實際指令碼：在暫存資料夾以真正的 `package_release.py` 產生附件後執行 `sha256sum -c SHA256SUMS.txt`。修正前 7 項全部找不到檔案；修正後 7 項全部 OK，`SHA256SUMS.txt` 與 `build-info.json` 都沒有 `\r`。
- 未重新建置或驗證 EXE，也未執行真實 Telegram 測試；本修正只改變發行附件的寫出方式。
- 既有 Release：版本號未變更，既有 Release 未重新發布或修改。依附件大小（每行剛好多 1 byte）判斷，desktop-v1.2.0 至 v1.5.0 的 `SHA256SUMS.txt` 都是 CRLF。驗證這些版本時先去除 `\r`：`tr -d '\r' < SHA256SUMS.txt | sha256sum -c`。
- 限制：驗證收據（`verification.json` 等）由各 `verify_*.py` 寫出，仍是 CRLF；這不影響 `sha256sum -c`，本次未修改。
- 維護位置：`desktop/package_release.py` 的 `write_release_text`、`write_checksums`；`desktop/tests/test_package_release.py`。
