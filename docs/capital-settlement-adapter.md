# Capital Demo 平倉帳務轉換：範圍與阻礙

日期：2026-09-20；主規格 §9–10，剩餘工作包第 1 階段。

## 已實作

`可信 Gateway 證據 → normalize_full_close → SettlementLedger.record_close → 通知待送`

`capital_settlement.py` 不連券商。確認請求 reference、部位 ID、商品、同帳戶 USD、
ACCEPTED/CLOSED 與唯一 FULLY_CLOSED；前快照 size 等於關閉數量、後快照不再有該部位。
明確時區時間、profit 與 currency 皆須存在；金額用 Decimal。
帳本只接資料指紋，未知額外費用不填零；驗證失敗不入帳、不建立通知。
來源真實性與快照完整／時序仍由可信 Gateway 負責，此函數不是 JSON 真偽驗證器。

## 真實證據仍不能直接入帳

### 後續唯讀核對（2026-09-20）

新增 `account_history_probe.py`，登入一次、同 session 讀取 00:09–00:11 UTC 的歷史，
前後核對同一 USD 帳戶。取得 2 筆 transactions、7 筆 activities，沒有下單。
本機證據：`runtime/eth-history-20260920.json`（Git 忽略）。

活動 dateUTC 能與最後全平 confirmation 的時間、部位、reference、商品、方向、數量、
價格唯一匹配，實測通過 `corroborate_close_time`，最後全平時間為
2026-09-20T00:09:37.757Z。reference 在多筆 POSITION 活動中重用，不能單獨當作成交鍵。
新轉換器允許這種嚴格 UTC 佐證；沒有佐證仍拒絕無時區 date。

兩筆 TRADE 的 size 都是字串 `0.0`，未提供精確 profit；不能把它當精確零損益。
活動含 openPrice、level、size，但未證實完整成本／費用語義，故不藉價格差補造權威盈虧。
這次只解除最後全平的 UTC 佐證缺口；減半精確損益、費用完整性及正式入帳仍未完成。

以下是加入 UTC 佐證前的歷史觀察：

既有 ETH Demo 紀錄中，REDUCE 為 OPEN、affectedDeals 空且沒有 profit；
最後全平才有 profit=-0.00146 USD，僅最後一段。confirmation date 沒有 offset。
本輪沒有把該時間自行補 Z，沒有推算第一段損益，也沒有寫入真實成交帳本。

[官方 API 文件](https://open-api.capital.com/) 說明確認用途；history/transactions 範例
有 dateUtc、reference、size、currency、status，但本輪未取得足夠依據把 size
解讀為平倉數量或已實現損益，因此不進行這種映射。

下一個證據工作：沿用／建立唯讀 Demo session 查交易與活動歷史，對照已知 ETH 分段，
確認唯一識別、UTC 时间及費用關聯；若仍無明確語義則向券商查證。
唯讀查詢不是重新執行 ETH 實驗，不需要為取得證據再下單。

上述唯讀工作已完成；後續需要具精確損益的帳戶報表／券商欄位定義與費用證據，
而不是重複查相同的 `size=0.0`。

## 測試與限制

新增 7 項離線測試：正確轉換、時區不明、淨額減倉拒絕、帳戶／識別／幣別衝突、
餘倉／數量衝突、壞金額／多 affectedDeals、入帳欄位不含秘密。
含時區的正例是合成測試資料，不是實際券商已驗證時間語義。
完整回歸測試 226 項通過，compileall 與 diff 檢查通過。
尚未支援部分平倉、自動停損成交補抓、完整帳務日報或通知實際發送。
