# GOLD 離線原始資料核對入口

日期：2026-09-27。對應工作包 3、`history-acceptance-recovery` A 批。

## 本次改變

新增 `history-check`：讀本機 raw JSON、核對固定窗口、沿用首次價格指紋與持久化
資料鎖。沒有網路請求、憑證讀取、訂單、收棒認證或解鎖參數。
輸入上限 2 MiB、1–1000 根、含端點窗口最多 999 分鐘；不補棒、不裁尾。
拒絕 URL／UNC／裝置路徑；請使用真正本機儲存，不使用網路映射磁碟。

## 架構與關鍵程式

`本機 raw JSON → history_import.audit_file → HistoryQualityMonitor → 同一 SQLite Store`

| 區域 | 責任 |
|---|---|
| `history_import.py` | 有界讀檔、嚴格 JSON／時區檢查、caller-supplied 來源標記、匯入事件與固定錯誤碼 |
| `history_quality.py` | 既有價格品質／修訂核對；新增回傳 observation ID 與核對完成時間 |
| `Store(..., must_exist=True)` | SQLite `mode=rw` 禁止悄悄建立空庫，先確認基本 Store schema |
| `cli.py` | 顯式資料庫／窗口／接收時間參數，輸出 JSON 與非零失敗退出碼 |

`_persist_failure()` 將錯誤摘要及 `market_data_blocked=true` 放進同一交易。
若資料庫無法寫入，回報 `lock_persisted=null`，不能把它當成 false 或封鎖成功。
正常品質觀測與匯入完成摘要為分開提交；STARTED 沒有 FINISHED 是未完成匯入，
不是接受證據。本批不提供背景程序崩潰監管或正式 feed 接線。

摘要保留輸入 SHA-256、正規化版本、import ID、observation ID、指定窗口、
呼叫者提供的接收時間、匯入／核對／完成時間；不記錄來源路徑或任意原始欄位。
SHA-256 是內容對照，不證明資料真實，也不能證明文件內沒有敏感資料。
商品／Demo 來源和接收時間均未獨立認證，不得作為 point-in-time 可交易證據。

## 使用方式

先準備本機 raw response JSON：最上層必須是 `{"prices": [...]}`，每列保留
`snapshotTimeUTC` 與 open/high/low/closePrice 的 bid、ask。
含 `response` 外層的研究封裝檔不能直接使用；本指令不猜測或自動抽取任意外層。
下列路徑與時間是用法示例，請替換成待核對檔案的實際值。

新建診斷庫（父目錄須已存在，檔案必須尚未存在）：

```powershell
.\.venv\Scripts\python.exe -m gold_system history-check --input runtime/local-gold-response.json --database runtime/history-check-diagnostic.db --create-diagnostic --start 2026-09-21T02:15:00Z --end 2026-09-21T02:17:00Z --received-at 2026-09-21T02:18:33.522558Z
```

再次核對同一庫時移除 `--create-diagnostic`；新快照需更新實際 input 與 received-at。
診斷庫身份持久保存為 `diagnostic`。既有未標記 Store 顯示 `existing_unverified`，
不會因此被認定為正式交易庫。原始檔／DB 放在已排除 Git 的 `runtime/`。

若明確指定既有交易 Store，異常會影響共用它的新倉 Guard；不要任意指定無關 DB。
新建另一個診斷庫不會解除原本的鎖，也不能拿其正常結果代替正式恢復。

| Exit code | 意義 |
|---|---|
| 0 | `OBSERVED_UNVERIFIED`；只是完成觀測，不是可交易 |
| 2 | `RECOVERY_REQUIRED`；品質異常或既有資料鎖（CLI 參數錯誤亦由 argparse 回傳 2） |
| 1 | `FAILED`；輸入／時間／儲存等操作失敗，停止呼叫鏈並核對 lock_persisted |

所有結果均為 `entries_enabled=false`、`closure_verified=false`。
同一 Store 中的操作者鎖與每日風控鎖不會被清除。即使 code=0，其他鎖仍可能存在。
目前 `run.ps1` 不轉送這組參數，請使用上面的 Python 模組指令。

## 驗證與限制

合成測試包含正常／重複、交叉／修訂／缺棒／亂序、JSON 重複 key、非有限數、
深層／編碼錯誤、大小限制、缺檔、時間錯誤、DB 不存在／非 Store、防覆蓋、
持久化失敗、秘密不外洩、拒絕網路路徑與新倉／既有停損隔離。
真正 CLI 子程序亦核對錯誤退出碼；mock 檢查無網路 connect 或憑證讀取。
新增 26 項測試；完整 332 項測試、compileall、CLI help 與 diff 檢查通過。
本輪未重播使用者原始快照、未存取券商或真實交易 DB；驗證只使用暫存合成資料。

下一批 B 仍需時間語義、finality 接受政策、正式 adapter 與人工恢復專用審核。
本次工程完成不算工作包 3 完整驗收，也不等於 GOLD 策略或 Demo 資格通過。
