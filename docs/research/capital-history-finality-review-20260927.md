# Capital.com 歷史行情與收棒證據核查

查閱日：2026-09-27。範圍：公開官方文件；未登入、未讀取憑證、未呼叫帳戶 API、未下單，客服問題僅為草稿。

## 結論

本次查閱不足以建立「券商保證已收棒且不再修訂」的資料契約。不得把讀取成功、時間已過、重抓相同，或下一根棒出現，直接升格為 `closure_verified=True`。這是本專案的證據判斷，不是宣稱 Capital.com 所有文件均無相關保證。

## 官方已記載與仍未知

下表來源為官方 [Public API 文件](https://open-api.capital.com/)，定位章節為 `Markets Info > Prices / Historical prices` 與 `WebSocket API / Subscribe to OHLC market data`。

| 項目 | 查閱結果 |
| --- | --- |
| REST `GET /api/v1/prices/{epic}` | 支援 M1、M5、H1 等解析度；`max` 預設 10、上限 1000。 |
| REST 時間篩選 | `from` / `to` 依 `snapshotTimeUTC` 篩選，格式 `YYYY-MM-DDTHH:MM:SS`；本段未明定兩端是否包含。 |
| REST 回傳 | 範例含時間及 bid/ask OHLC；本段未說明時間標籤代表開棒還是收棒。 |
| WebSocket OHLC | `OHLCMarketData.subscribe` 訂閱更新；`ohlc.event` 範例含 `t`、`priceType` 及 OHLC。 |
| WebSocket 時間語義 | 本段未定義 `t` 的開／收棒意義；數值外形不足以證明單位或事件時間契約。 |
| 收棒與修訂 | 所查兩段範例未呈現 closed/final 欄位，亦未提供最終不可修訂時間、修訂版本或延遲上界。這不是完整 schema 不可能含其他欄位的證明。 |

官方 FAQ 記載每使用者最多 10 requests/s，登入另限每 API key 1 request/s；REST 閒置有效期為 10 分鐘，WebSocket 須至少每 10 分鐘 ping，最多訂閱 40 商品。[官方 API FAQ 與 WebSocket 說明](https://open-api.capital.com/)

官方 Help 亦確認 10 requests/s 限制，並轉介其他限制；該短文未補充歷史棒最終性。[Do you have any limitations on your API?](https://help.capital.com/hc/en-us/articles/6630830103058-Do-you-have-any-limitations-on-your-API)

## 本專案推論與後續證據要求

- 價格一致只代表兩次觀測一致；不能推出永不修訂。接收時間必須保留，避免把較晚資料冒充當時已知。
- 本機離線工具採用的包含兩端時間窗，只是工具輸入契約，不能反推券商的 `from` / `to` 契約。
- 後續收集應限定 Demo、唯讀端點，將所有請求納入共享節流，保留重試與退避餘量；上限不是建議維持的負載。
- 在下列語義未獲確認與驗證前，不接入可開新倉的正式接受／恢復流程，也不自動解除資料品質封鎖。

## 客服問題草稿（未寄出）

1. 對 Demo GOLD 的 `MINUTE`、`MINUTE_5`、`HOUR`，`snapshotTimeUTC` 與 `ohlc.event.t` 是哪個時間點？`t` 的單位、UTC 定義與區間端點規則為何？
2. `from` / `to` 是否兩端包含？若筆數超過 `max`，保留最早還是最新資料？分頁是否有一致性或排序保證？
3. 如何識別仍形成中的棒與已收棒？有正式 final/closed 訊號、事件型別或延遲界線嗎？下一棒開始是否足以確認上一棒？
4. 已收棒的 bid/ask 是否仍可能補寫或修訂？最長修訂窗口、版本／通知機制為何？REST 與 WebSocket 是否共享同一最終資料？
5. bid/ask OHLC 是否同步發布？短暫價格交叉、斷線補送、無報價分鐘如何處理？Demo 與 Live 是否有不同保證？
6. 唯讀歷史收集是否另有每日配額、WebSocket 訊息限速或建議退避規則？

需取得可保存、附日期與適用環境的正式回覆，再設計接受條件與可重現的前向驗證；本研究不代替驗收。
