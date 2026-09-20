# 本機交易稽核暫結

對應規格 §14。指令唯讀已存在的 SQLite `events`，不連券商、不呼叫模型、
不啟動交易。輸出固定模板 Markdown，不覆寫既有報告。

```powershell
.\run.ps1 daily-report --database runtime/demo-audit.db --start "2026-09-20T00:00:00+08:00" --end "2026-09-21T00:00:00+08:00" --mode DEMO --output runtime/demo-audit-summary-20260920.md
```

這只是明確區間的範例，**不是已核定的帳戶交易日邊界**。開始包含、結束不包含；
必須使用已有資料庫，輸出位置尚不能有同名檔案。回放紀錄改用 `--mode PAPER`。

## 架構與關鍵程式

`CLI → reporting.summarize（SQLite 唯讀）→ render（固定模板）→ 新 Markdown`

- `summarize`：以模式選用 Paper 的 `simulated_at` 或 Demo 的事件寫入時間；
  混合時基的略過筆數會列出。操作者選擇模式不是券商帳戶來源認證。
- `paper_recorded_exit_pnl`：僅 Paper EXIT 分段損益的加總；不是完整費用後淨損益。
  部分平倉缺完整交易 ID，不能據此計算完成交易數、勝率或 Profit Factor。
- Demo 記錄時間不是成交時間；完整帳務、餘倉／掛單、AI 費用等仍標示待核對。
- 原始 payload、未知事件自由文字不輸出；壞資料使命令失敗，不生成看似正常的報告。
- `write_report` 使用排他建立模式，不覆寫既有報告或來源資料庫。

這是手動執行的暫結，尚非正式日終對帳／每日通知服務。後續須接權威成交帳本、
交易日時區、通知狀態持久化及排程。完整 209 項測試通過，包含 10 項本輪測試；
未連券商實測，也未進行策略驗證。
