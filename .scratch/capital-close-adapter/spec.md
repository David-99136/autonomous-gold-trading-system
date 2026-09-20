# Capital Demo 全平分段轉換

Status: resolved
Type: task

§9–10，第 1 工作包。離線接受可信 Gateway 提供的同帳戶 session 前後快照、
已對應請求的 confirmation、前後持倉清單及預期交易識別，核對單一部位全平後
轉換至 SettlementLedger；不呼叫券商、不改交易規則。

僅處理 ACCEPTED/CLOSED、唯一 FULLY_CLOSED affected deal，USD profit 及 size 完整。
前快照部位數量須等於平倉 size，後快照無同部位。confirmation 時間必須明確含時區；
不假定無時區 date 是 UTC。部分平倉、淨額相抵及未知費用維持未完成。
此介面不能證明呼叫者傳入 JSON 的來源；只允許可信 Gateway 接線，不新增任意檔案入帳 CLI。

證據：2026-09-20 既有 ETH runtime 確認 CLOSE 有 profit，REDUCE 無 profit；
實測 date 無 offset，故此版本不能直接將該筆真實證據入帳。
官方來源：https://open-api.capital.com/ ，確認及 history/transactions 範例。
history 的 size 未在本輪確立為交易數量或已實現損益，禁止猜測映射。

## Answer

`capital_settlement.py` 完成嚴格全平轉換與 ledger 接口，新增 7 項離線測試。
真實 ETH 回應因 date 缺時區仍拒絕入帳；需第一方時間語義或可核對 dateUtc 的
權威帳務來源。部分減倉盈虧、完整費用及自動 Gateway 接線未完成。

後續補充：已唯讀取得真實 activities，dateUTC 與最後全平多欄位唯一匹配。
新增 `corroborate_close_time`，僅此種佐證可解除無 offset date 限制。
transactions 的兩筆 size=0.0 無法證明精確盈虧，未據此入帳。
