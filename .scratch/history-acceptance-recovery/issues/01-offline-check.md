# A 批：離線歷史品質核對入口

Status: resolved
Type: task

依本目錄上層 spec.md §5 A、§7 實作；使用者於 2026-09-27 核准。
不得提供網路、訂單、已收棒認證或解鎖能力。B 批不在本票範圍。

## Comments

2026-09-27：完成入口、Store 既有庫保護及合成測試，正執行完整回歸。

## Answer

新增 `history_import.audit_file`、`history-check` CLI、Store 必须存在模式，
品質觀測回傳可追溯 ID／完成時間；保留首次價格與全部既有鎖。
23 項入口測試加 3 項 Paper／Demo Guard 整合測試，完整 332 項皆通過。
compileall、CLI help 與 diff 檢查通過；沒有真實帳戶、資料庫或訂單操作。
使用方式、架構與限制見 `docs/history-offline-check.md`；B 批未納入。
