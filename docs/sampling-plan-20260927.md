# GOLD 採樣說明摘要 — 2026-09-27

## 1. 計畫目的

蒐集 2026-09-27 GOLD London session 的行情證據，供後續策略研究與採樣驗收使用。

採樣窗口涵蓋：

- **London session 開盤前 2 小時**（06:00–07:59 UTC）：兩個完整 60 分鐘 MINUTE 窗口
- **London session 開盤後 30 分鐘**（08:00–08:29 UTC）：兩個較短窗口（20 分鐘 + 10 分鐘）

> [!IMPORTANT]
> 本計畫為**唯讀診斷採樣**，`environment: DEMO`，不涉及開倉、平倉或任何訂單操作。
> `entries_enabled: false`、`qualified: false` 是硬性輸出，不因採樣完整而改變。

---

## 2. 計畫 SHA-256

```
de107bb5bc1123bc1ad943df134d590448cb82d667b7326998d27d299c99b67a
```

計畫檔：[`examples/gold-sample-plan-20260927.json`](../examples/gold-sample-plan-20260927.json)

驗證指令產生的完整輸出：

```json
{
  "status": "PLAN_VALID",
  "plan_sha256": "de107bb5bc1123bc1ad943df134d590448cb82d667b7326998d27d299c99b67a",
  "queries": 4,
  "start": "2026-09-27T08:00:00+00:00",
  "end": "2026-09-27T09:00:00+00:00",
  "entries_enabled": false,
  "qualified": false
}
```

---

## 3. 四個 Query 摘要

| # | 執行時間（UTC）       | 採樣區間（UTC）                       | 解析度   | max | 說明                        |
|---|----------------------|---------------------------------------|----------|-----|-----------------------------|
| 1 | 08:00:20Z            | 06:00:00 → 06:59:00（60 分鐘）        | MINUTE   | 60  | 開盤前第 2 小時完整行情     |
| 2 | 08:10:00Z            | 07:00:00 → 07:59:00（60 分鐘）        | MINUTE   | 60  | 開盤前第 1 小時完整行情     |
| 3 | 08:20:00Z            | 08:00:00 → 08:19:00（20 分鐘）        | MINUTE   | 20  | 開盤後最初 20 分鐘行情      |
| 4 | 08:30:00Z            | 08:20:00 → 08:29:00（10 分鐘）        | MINUTE   | 10  | 開盤後第 21–30 分鐘行情     |

**計畫執行窗口：** `2026-09-27T08:00:00Z` ～ `2026-09-27T09:00:00Z`（60 分鐘）

---

## 4. 執行前確認清單

在授權執行採樣之前，請確認以下所有事項：

- [ ] **Demo 帳戶無其他 API 程序在跑** — 確認同一 OS 使用者下無其他 Python 程序正在連線 Capital.com Demo API，避免租約衝突（`DIAGNOSTIC_LEASE_BUSY`）。
- [ ] **GOLD 在 08:00Z 左右為 TRADEABLE 狀態** — 可先以 `GET /markets/GOLD` 確認市場狀態；不可交易時採樣器會停止，不會把空窗口當資料完整。
- [ ] **取得使用者當次連線授權** — 本計畫 SHA-256 已確認，但每次實際連線前仍須使用者明確授權（逐次，不可累積）。

> [!CAUTION]
> 租約只能保護同一主機、同一 OS 使用者的程序互斥，**無法保證其他主機或其他 OS 使用者不存在 API 流量**。若無法確認，不得執行。

---

## 5. 執行指令

確認以上清單後，貼上以下完整 PowerShell 命令執行採樣：

```powershell
.venv\Scripts\python.exe -m gold_system demo-history-sample `
  --plan examples/gold-sample-plan-20260927.json `
  --plan-sha256 de107bb5bc1123bc1ad943df134d590448cb82d667b7326998d27d299c99b67a `
  --output runtime/history-sample-20260927 `
  --confirm demo-read-only
```

> [!NOTE]
> - `--output` 必須是**不存在的新目錄**，採樣器拒絕覆蓋既有路徑。
> - `--confirm demo-read-only` 是明確的 Demo 唯讀確認旗標，缺少時採樣器拒絕執行。
> - `--plan-sha256` 必須與上方 SHA-256 完全一致，防止計畫被竄改後執行。

---

## 6. 完成後預期輸出說明

採樣成功完成後，`runtime/history-sample-20260927/` 目錄下應包含：

| 檔案 | 說明 |
|------|------|
| `plan.json` | 採樣計畫副本（排他建立，run 開始時落盤） |
| `run-start.json` | Run 開始回執（含 run ID、plan hash、UTC 時間） |
| `request-<seq>.json` | 各次 HTTP 回執（含 queue-start、send-start、headers-received、body-complete、validation-complete） |
| `prices-<seq>.json` | 白名單價格檔（timestamp、bid/ask OHLC） |
| `run-summary.json` | Run 結束摘要（或失敗碼） |

> [!WARNING]
> 輸出摘要**必定**包含 `entries_enabled: false`、`closure_verified: false`、`qualified: false`。這是系統硬性設計，採樣完整並不改變此輸出，請勿據此判斷交易資格。

---

## 7. 硬限制提醒

| 限制項目 | 上限 |
|----------|------|
| 總執行時長 | **75 分鐘** |
| 歷史查詢次數 | **240 次**（含失敗、時鐘、市場狀態請求） |
| 落盤大小 | **64 MiB** |
| 單回應上限 | 1 MiB |
| Query 窗口 | 最多 999 分鐘 |
| Client 送出間隔 | 至少 1 秒 |
| 自動重試 | **無**（任一錯誤即停止） |

> [!IMPORTANT]
> 任一預算不足時採樣器立即停止，**不追加額度、不自動重試、不補送**。
> 超時後不得以補送方式把延遲樣本偽裝為準時採樣。
