# 桌面版交付驗證

日期：2026-11-17。版本：1.6.5。基底：1.6.4（`master` 的 `922d31c`）。

## 本次範圍

第三方授權只收內含核心實際連結的 Go 模組；Dependabot 設定改為本專案擁有者、每週、含桌面 pip 套件；清理本機建置快取。Go 核心與桌面程式的原始碼沒有修改，功能與 1.6.4 相同。

## 驗證

以下分為自動化測試、最終 EXE 離線驗證、真實 Telegram 帳號驗證三類，不能互相替代。

**自動化測試（原始碼）**

- 最終建置內完整 Python suite：**232 passed、36 subtests passed**（1.6.4 為 230 項，新增授權模組清單解析與快取目錄名稱 2 項）。
- Go：`build.ps1` 列出的 11 個有測試的套件全部通過（清除編譯快取後重新編譯執行）。

**最終 EXE 離線驗證（fixture，不連 Telegram）**

- `verification.json`、`restart-verification.json`、`workflow-verification.json`、`batch-forward-verification.json` 全部通過。
- 授權內容：`dist/licenses/go-modules` 由 495 個檔案降為 95 個，只含核心連結的 82 個模組；`THIRD-PARTY-LICENSES.zip` 約 0.9 MB → 0.2 MB。

**真實 Telegram 帳號驗證**

- **本版沒有執行。** 程式原始碼與 1.6.4 相同，只有隨附授權檔不同；使用者本次也沒有同意使用帳號。

**交付檔**

- 本機 `TDL下載助手.exe`、`TDL下載助手-1.6.5.exe` 與 `dist/TDL下載助手.exe` 相同：Windows x64 GUI，**1.6.5**，**56,511,474 bytes**（1.6.4 為 57,175,922 bytes）。
- EXE SHA-256：`89ef4901f661e7310795da3fbaac868456407ad80eb02b422a5b8656d4fadd7e`。
- 內含核心 SHA-256：`cba6eb75d16eb36875f69389c3dcfffc3df5556df2e3c8d7df42d52f6cc9976b`。
- 收據與 EXE 截圖：`desktop/verification/1.6.5/`。

## 已知限制

- Dependabot 版本更新是否已在 GitHub 上為這個 fork 啟用，本次沒有確認。
- 繼承 1.6.4：暫停按鈕的實際操作沒有在 EXE 上或用真實帳號驗證；CI 只跑測試；EXE 未簽章；上游完整 Go suite 既有 Windows `app/update/TestBinaryName` 差異未處理。
