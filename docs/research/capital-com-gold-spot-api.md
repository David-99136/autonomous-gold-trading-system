# Capital.com Gold Spot CFD 自動交易介面研究

- 查證截止：2026-09-06（Asia/Taipei）
- 來源範圍：只使用 Capital.com 官方網站、官方 Public API 文件，以及 Capital.com 官方 GitHub 組織 `capital-com-sv`
- 研究目的：確認 Gold Spot CFD 的程式交易介面能力與限制；不代表已取得帳戶層級的實測結果，也不構成法律或投資意見

## 結論摘要

1. Capital.com 的 Gold Spot 是以保證金交易的 **CFD**，不是 COMEX 期貨，也不持有實體黃金；官網顯示 ticker 為 `Gold`。Public API 實際下單使用的是帳戶可見市場的 `epic`，必須登入後以 `GET /markets?searchTerm=Gold` 找到，再以 `GET /markets/{epic}` 驗證，不能只根據網頁 ticker 或範例把 `GOLD` 寫死。[Gold Spot 官方商品頁](https://capital.com/en-int/markets/commodities/gold-spot-commodity)；[Public API：Symbology、Markets](https://open-api.capital.com/)（查證：2026-09-06）
2. 官方 Public API 提供 Live/Demo REST、WebSocket 報價與 OHLC、歷史價格、持倉、限價／停止委託、停損停利、修改／撤單、成交確認與帳戶歷史。這足以建立 Demo 交易閘道，但「部分平倉／部分成交語義、Gold 的實際 dealing rules、交易時段與費率」仍必須用使用者 Demo 帳戶實測。[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）
3. `POST /positions` 回傳 HTTP 200 或 `dealReference` **不代表已成交**；官方要求以 `GET /confirms/{dealReference}` 查證。API 沒有在建立部位 payload 中公開文件化的客戶端冪等鍵，因此逾時後不可盲目重送，必須先查 confirmation、positions、working orders 與 activity。[Capital.com Public API：Orders and positions](https://open-api.capital.com/)（查證：2026-09-06）
4. 官方 MCP 並非全自動交易介面：交易預設停用，採 preview → confirm → execute，官方 FAQ 明確說每張訂單都需要使用者確認、AI 不能自主代客下單。若系統目標是無人值守自主交易，不能把官方 MCP 當作實盤執行層。[Capital.com AI Integration](https://capital.com/en-int/trading-platforms/ai-integration)；[官方 Capital.com MCP repository](https://github.com/capital-com-sv/capital-mcp)（查證：2026-09-06）
5. 官方 MCP／Public API 免責文字另寫明：客戶不得讓第三方對帳戶行使自主裁量控制，且 API 使用須符合帳戶所屬實體條款與所在地法律。使用者所提「分析代理＋自主交易代理」是否落入該限制，官方公開資料不足以判定；**Live 上線前必須由 Capital.com 依該帳戶實體書面確認**。[官方 Capital.com MCP repository：Prohibited Use](https://github.com/capital-com-sv/capital-mcp)（查證：2026-09-06）

## 1. 商品身分與 ticker／epic

### 官方可確認

- Gold Spot 在 Capital.com 是商品 CFD；可做多或做空，但不取得實體黃金所有權。官網列出的 ticker 是 `Gold`，計價幣別為 USD。[Gold Spot 官方商品頁](https://capital.com/en-int/markets/commodities/gold-spot-commodity)（查證：2026-09-06）
- `XAU` 是現貨黃金的貨幣代碼，但這不等於 Capital.com API 的 `epic`。Capital.com 說明 Gold Spot CFD 反映現貨黃金市場價格，而 Gold Futures CFD 是另一種追蹤期貨合約價格的商品。[Capital.com 官方 Gold trading guide](https://capital.com/en-int/learn/market-guides/what-is-gold-trading)（查證：2026-09-06）
- Public API 將 `epic` 定義為市場識別名稱，官方指定以 `GET /markets?searchTerm=...` 搜尋，再使用回應中的 `epic`。`GET /markets/{epic}` 可回傳商品規格、opening hours、overnight fee、dealing rules、bid/offer 與 market status。[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）

### 實作要求

```text
建立 Demo session
  -> GET /api/v1/markets?searchTerm=Gold
  -> 篩選 name/symbol/expiry/type，拒絕 Gold Future
  -> 取得候選 epic
  -> GET /api/v1/markets/{epic}
  -> 驗證 type、currency、expiry、marketStatus、dealingRules
  -> 將該帳戶／環境的已驗證 epic 固定於 allowlist
```

`GOLD` 出現在官方 MCP 的 allowlist 範例中，但範例不能取代登入後的市場查詢；不同帳戶實體、Demo/Live 或商品調整後，實際可用 `epic` 必須重新驗證。[官方 Capital.com MCP repository](https://github.com/capital-com-sv/capital-mcp)；[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）

## 2. REST base URL、認證與 session

| 項目 | 官方文件內容 | 系統規格含義 |
|---|---|---|
| Live REST base | `https://api-capital.backend-capital.com/` | v1 端點位於 `/api/v1/...` |
| Demo REST base | `https://demo-api-capital.backend-capital.com/` | Demo 與 Live 必須是獨立設定，不由代理自行切換 |
| 建立 session | `POST /api/v1/session` | header 使用 `X-CAP-API-KEY`；body 傳 `identifier`、API key 的 custom password、`encryptedPassword` |
| 後續認證 | 回應 header 的 `CST` 與 `X-SECURITY-TOKEN` | 兩者要放入後續 REST 請求；`X-SECURITY-TOKEN` 也識別目前交易帳戶 |
| session 效期 | 最後使用後 10 分鐘 | 需以 authenticated ping 維持，逾期重新認證；不可假設永久 token |
| 帳戶切換 | `PUT /session` | 切換後 WebSocket streaming 會中斷，必須重訂閱並重新對帳 |

來源：[Capital.com Public API](https://open-api.capital.com/)；[官方 CST / X-SECURITY-TOKEN 說明](https://help.capital.com/hc/en-us/articles/5595698273298-How-can-I-get-CST-and-X-SECURITY-TOKEN-parameters)（查證：2026-09-06）

官方也提供「加密密碼」登入流程：先以 `GET /session/encryptionKey` 取 key/timestamp，再向 `POST /session` 傳 `encryptedPassword=true`。但同一份官方文件的文字稱 AES、程式範例實際呈現 RSA/PKCS#1 操作，存在文件表述不一致；實作前應以 Demo 與最新官方範例驗證，不自行推測演算法。[Capital.com Public API：Authentication](https://open-api.capital.com/)（查證：2026-09-06）

## 3. REST／WebSocket 市場資料

### REST 歷史 K 線

- `GET /api/v1/prices/{epic}` 支援 `MINUTE`、`MINUTE_5`、`MINUTE_15`、`MINUTE_30`、`HOUR`、`HOUR_4`、`DAY`、`WEEK`；本系統所需的 1m、5m、1h 都在官方列舉中。[Capital.com Public API：Historical prices](https://open-api.capital.com/)（查證：2026-09-06）
- `max` 預設 10、單次最大 1,000；可用 `from`、`to` 依 `snapshotTimeUTC` 過濾。官方頁未列出 Gold 可追溯到多久以前，不能把「每次 1,000 筆」誤解為「完整歷史都可取得」。[Capital.com Public API：Historical prices](https://open-api.capital.com/)（查證：2026-09-06）
- K 線回應包含 bid/ask 的 open、close、high、low，以及 `lastTradedVolume`；官方文件只列出欄位，沒有把 `lastTradedVolume` 定義成全球現貨黃金或交易所總成交量，因此只能標為語義未確認的輔助資料。[Capital.com Public API：Historical prices response](https://open-api.capital.com/)（查證：2026-09-06）

### WebSocket

- 官方連線端點是 `wss://api-streaming-capital.backend-capital.com/connect`；訊息使用 REST session 的 `CST` 與 `X-SECURITY-TOKEN`。至少每 10 分鐘 ping 一次以維持連線。[Capital.com Public API：WebSocket API](https://open-api.capital.com/)（查證：2026-09-06）
- `marketData.subscribe` 提供即時 bid/offer 與 timestamp；`OHLCMarketData.subscribe` 提供 K 線更新，可指定 `MINUTE`、`MINUTE_5`、`HOUR` 等解析度及 classic/heikin-ashi。[Capital.com Public API：WebSocket subscriptions](https://open-api.capital.com/)（查證：2026-09-06）
- WebSocket 最多同時訂閱 40 個 epics。官方公開頁只列一個 WebSocket URL，未另列 Demo URL；Demo session 實際回傳的 `streamEndpoint` 與可訂閱行為應由帳戶實測。[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）
- WebSocket OHLC 範例只展示 `priceType: bid`；公開文件沒有明確保證同時提供 ask OHLC。若策略需要可重播的 bid/ask K 線，須由 REST 補齊並在 Demo 驗證。[Capital.com Public API：OHLC subscription](https://open-api.capital.com/)（查證：2026-09-06）
- 官方 Public API 文件目前明確呈現價格與 OHLC 訂閱，沒有同樣清楚地文件化「訂單／成交事件」WebSocket destination；訂單確認仍應以 REST confirmation 與對帳端點為準，不能假設有交易事件串流。[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）

## 4. Rate limits 與 session limits

| 限制 | 官方值 |
|---|---:|
| 一般 API | 每位使用者最多 10 requests/second |
| 開倉／建立 working order | 每位使用者每 0.1 秒最多 1 request；超過會被拒絕 |
| `POST /session` | 每 API key 每秒最多 1 request |
| Demo `POST /positions`、`POST /workingorders` | 各／合計語義未說明；官方文字為每小時 1,000 requests |
| REST session | 最後活動後 10 分鐘 |
| WebSocket session | 10 分鐘，需至少每 10 分鐘 ping |
| WebSocket instruments | 最多 40 epics |
| `GET /markets?epics=...` | 一次最多 50 epics |
| API key 生成嘗試 | 24 小時最多 100 次成功生成嘗試（依官方原文） |

來源：[Capital.com Public API：FAQ limitations](https://open-api.capital.com/)；[官方 API limitation 說明](https://help.capital.com/hc/en-us/articles/6630830103058-Do-you-have-any-limitations-on-your-API)（查證：2026-09-06）

限流應在單一帳戶層集中管理；分析、行情補抓、健康檢查與交易閘道不能各自以為擁有完整的 10 req/s 配額。

## 5. 下單、停損、修改與成交確認

### 官方明確支援

| 能力 | REST endpoint / 欄位 | 官方可確認內容 |
|---|---|---|
| 建立立即部位 | `POST /positions` | `direction`、`epic`、`size`；可附 guaranteed/trailing stop、`stopLevel`/`stopDistance`/`stopAmount`、take-profit 對應欄位 |
| 查全部／單一持倉 | `GET /positions`、`GET /positions/{dealId}` | 回傳 active account 的開放持倉 |
| 修改持倉風控 | `PUT /positions/{dealId}` | 可更新 stop/take-profit 與相關欄位 |
| 關閉持倉 | `DELETE /positions/{dealId}` | 回傳新的 `dealReference`；仍需確認 |
| 建立掛單 | `POST /workingorders` | `LIMIT` 或 `STOP`，含價格、size、有效期與 stop/take-profit 欄位 |
| 查／改／撤掛單 | `GET /workingorders`、`PUT /workingorders/{dealId}`、`DELETE /workingorders/{dealId}` | 管理 active account 的 working orders |
| 查交易結果 | `GET /confirms/{dealReference}` | 回傳 `dealStatus`、`status`、`dealId`、`affectedDeals`、level、size 等 |
| 查稽核歷史 | `GET /history/activity`、`GET /history/transactions` | activity 可含 `UNKNOWN`、`REJECTED`、`MODIFY_REJECT`、`CANCEL_REJECT` 等狀態；transactions 可查 TRADE、SWAP、commission 等類型 |

來源：[Capital.com Public API](https://open-api.capital.com/)（查證：2026-09-06）

### 不確定回覆的必要處理

官方明確警告：`POST /positions` 的成功 response 不一定表示部位已成功開啟，必須用回傳的 `dealReference` 呼叫 `GET /confirms/{dealReference}`；`affectedDeals` 可能包含多個實際 deal。[Capital.com Public API：Orders and positions](https://open-api.capital.com/)（查證：2026-09-06）

因此交易閘道的安全流程應為：

```text
POST 成功或網路結果不明
  -> 以 dealReference 查 GET /confirms/{dealReference}
  -> 同步 GET /positions 與 GET /workingorders
  -> 必要時查 GET /history/activity?dealId=...
  -> 只有證明原意圖沒有建立任何 deal，才允許新的替代意圖
```

這是從官方確認機制導出的安全設計；不是 Capital.com 提供了 client idempotency key。公開 `POST /positions` 與 `POST /workingorders` body 文件未列 client-supplied idempotency key。

### 部分成交／部分平倉：官方資料不足

- `affectedDeals` 可揭示一個 request 影響多個 deals，但官方沒有將它定義為「部分成交」。[Capital.com Public API：Position/Order confirmation](https://open-api.capital.com/)（查證：2026-09-06）
- Capital.com 官方 App／Web 操作說明確認平台 UI 可輸入「Amount to close」進行部分平倉；但公開 REST 文件的 `DELETE /positions/{dealId}` 沒有 size body，因此沒有確認 API 如何把既有部位只平掉 50%。在 non-hedging mode 送反向較小部位是否淨額沖銷、會產生何種 deal/已實現損益，以及 hedging mode 下行為，必須在 Demo 以最小規模驗證。[官方平台部分平倉說明](https://help.capital.com/hc/en-us/articles/4403989686418-How-to-close-a-position-partially)；[Capital.com Public API：Close position、Account preferences](https://open-api.capital.com/)（查證：2026-09-06）
- 官方文件沒有清楚列出 partial-fill 狀態、剩餘數量欄位或 fill event subscription。不能虛構「一定全成」或「一定支援部分成交事件」。

## 6. Market status、交易時間、overnight funding 與費用

### API 可取得的動態資訊

`GET /markets/{epic}` 官方 response schema/範例含：

- `openingHours`（逐日區間與 `zone`）
- `overnightFee.longRate`、`shortRate`、`swapChargeTimestamp`、`swapChargeInterval`
- `dealingRules`：min/max deal size、size increment、最小/最大 stop/profit distance、market order/trailing stop preference
- `snapshot.marketStatus`、bid、offer、update time、`marketModes`

這些值可能隨商品、帳戶實體、Demo/Live 與日期改變；交易引擎應每次啟動與定期刷新，不可把範例中的 Silver 數值或某次官網快照套到 Gold。[Capital.com Public API：Single market details](https://open-api.capital.com/)（查證：2026-09-06）

Capital.com 另有 `close-only` 模式：價格仍可更新且既有部位可關閉，但禁止新開部位；它不同於完全停用或 view-only。若 market mode/status 表示 close-only，交易閘道必須只准減少曝險。[官方 close-only 說明](https://help.capital.com/hc/en-us/articles/27732492955026-Why-do-some-markets-in-a-close-only-mode)（查證：2026-09-06）

### Gold Spot 官網的當期快照

Capital.com 國際站 Gold Spot 頁面在 2026-09-04 20:45:47 UTC 的頁面快照顯示：CFD、ticker `Gold`、USD、min traded quantity 0.01、margin 1.00%、commission 0%，交易成本說明為 spread；另列 guaranteed-stop premium 0.03%，overnight funding adjustment time 21:00 UTC，long/short funding rate 分別顯示當時值。[Gold Spot 官方商品頁](https://capital.com/en-int/markets/commodities/gold-spot-commodity)（查證：2026-09-06）

這些是**公開國際站的時點快照，不是使用者帳戶的合約保證值**。系統的結算前 30 分鐘停新倉／前 10 分鐘強平規則，必須以該日 `swapChargeTimestamp` 和實際 `openingHours` 計算；不可永久寫死 21:00 UTC。

官方 Gold guide 列出的基礎黃金市場時間為：夏令時間 Sunday 22:00–Friday 21:00 UTC（每日 21:00–22:00 break），冬令時間 Sunday 23:00–Friday 22:00 UTC（每日 22:00–23:00 break）；但同頁把「our hours via CFDs」導回商品頁，故帳戶可交易時間仍以 API market details 為準。[Capital.com 官方 Gold trading guide](https://capital.com/en-int/learn/market-guides/what-is-gold-trading)（查證：2026-09-06）

## 7. API key 安全與權限

- API key 只能從網頁版 `Settings > API integrations` 產生；若未開啟 2FA，平台會要求先啟用。可設定 key label、獨立 custom password 與到期日；預設有效期一年。key 只在建立時完整顯示一次。[官方 API key 產生說明](https://help.capital.com/hc/en-us/articles/4415179146386-How-to-generate-an-API-key)（查證：2026-09-06）
- key 可在 API integrations 介面 pause/play，而不必刪除重建。[官方 pause/launch 說明](https://help.capital.com/hc/en-us/articles/8148272474642-How-can-I-pause-or-launch-an-API-key)（查證：2026-09-06）
- 官方目前只有「可交易」的一種 API key privilege，**不能產生 read-only key**。任何取得 key 的元件都具有交易風險，因此資料分析代理不應直接持有 key；憑證只應交給隔離的 broker gateway。[官方 API key privileges 說明](https://help.capital.com/hc/en-us/articles/4415179156754-Which-kind-of-API-Key-privileges-can-I-have)（查證：2026-09-06）
- 官方 MCP 說明稱憑證留在本機，MCP 本身只在記憶體處理 session data、不寫入磁碟；但 AI client 是否記錄內容受其自身政策控制。[Capital.com AI Integration](https://capital.com/en-int/trading-platforms/ai-integration)；[官方 Capital.com MCP repository](https://github.com/capital-com-sv/capital-mcp)（查證：2026-09-06）

## 8. 官方 MCP／自動化邊界

官方 MCP repository 提供的預設防線包括：

- `CAP_ENV=demo` 為預設環境；交易預設關閉 (`CAP_ALLOW_TRADING=false`)
- EPIC allowlist、最大部位／掛單 size、最大同時持倉、每日下單上限
- 所有 side-effect 操作使用 preview → execute，並可要求 `confirm=true`
- broker rules 與本機 risk policy 在執行時重查，之後輪詢 broker confirmation

來源：[官方 Capital.com MCP repository](https://github.com/capital-com-sv/capital-mcp)（查證：2026-09-06）

但官方對 MCP 的產品定位非常明確：

- 每張交易都要由使用者明確確認；AI 不能自主代為交易。
- market order 在確認時執行；working order 在建立時確認一次，之後觸價可自動執行。
- 使用者對第三方 AI、延遲、設定與自動化活動負完全責任。

來源：[Capital.com AI Integration FAQ](https://capital.com/en-int/trading-platforms/ai-integration)（查證：2026-09-06）

另外，官方文件存在需要釐清的表述衝突：Public API REST reference 明確提供 `POST /workingorders` 的 `LIMIT`／`STOP`；AI Integration 的免責段落卻寫「all orders placed through the Public API are executed as market orders」，同頁 FAQ 又說 working orders 會在觸價後執行。系統不能自行選擇有利解讀；應以 Demo 實測並向帳戶實體確認 working-order 與滑價語義。[Capital.com Public API](https://open-api.capital.com/)；[Capital.com AI Integration](https://capital.com/en-int/trading-platforms/ai-integration)（查證：2026-09-06）

## 9. 台灣帳戶、法律實體與可用性

### 官方公開資料能確認到的程度

Capital.com 於 2026-08-18 更新的「不可服務國家」清單沒有列出台灣；這只能證明台灣不在該頁當時列舉的排除名單，不能推導為保證可新開戶、保證可用 Live API，或構成台灣法規意見。[官方國家可用性清單](https://help.capital.com/hc/en-us/articles/27040372144274-Is-Capital-com-available-in-my-country)（查證：2026-09-06）

Capital.com 繁中首頁顯示其國際站平台由巴哈馬證券委員會（SCB）授權監管；然而網頁 footer/locale 仍不能證明使用者現有 Demo 或未來 Live 帳戶的實際 contracting entity。[Capital.com 繁中官方首頁](https://capital.com/zh-hant)（查證：2026-09-06）

Capital.com 2026-06-08 的官方 MCP 新聞稿只明確說 MCP 當時提供給 Capital Com Mena Securities Trading LLC 的 eligible clients。公開資料未確認台灣／Bahamas 帳戶是否能使用同一官方 MCP。[Capital.com 官方 MCP 新聞稿](https://capital.com/en-ae/press/we-enable-mcp-server-plugin-giving-traders-real-time-account-access-from-within-their-ai-research-environments)（查證：2026-09-06）

### 狀態

下列項目一律標為 `UNAVAILABLE_FROM_PUBLIC_OFFICIAL_SOURCES`，不得推測：

- 使用者 Demo 與未來 Live 帳戶的實際 contracting entity／監管實體
- 台灣居住者在該實體下能否合法使用完全自主、無逐筆人工確認的 AI 交易代理
- 該帳戶是否可用官方 MCP，以及 MCP 工具／風控設定是否與公開 repository 相同
- Demo 與 Live 的 Gold epic、槓桿、保證金、min size、GSL、trailing stop、交易時段與 overnight rate 是否完全一致

## 10. 必須用使用者帳戶實測／書面確認的未知項

以下項目未完成前，不應解鎖 Live：

1. 以 `GET /accounts`、`GET /session` 及帳戶法律文件確認 contracting entity、account ID、currency 與 Demo/Live 分離方式。
2. Demo 執行 `GET /markets?searchTerm=Gold`，確認正確 Gold Spot epic；排除 futures、spread-bet 或其他同名商品。
3. 保存 Gold 的 `GET /markets/{epic}` 完整回應，驗證 min/max size、increment、margin、stop distance、GSL/trailing availability、market-order preference、opening hours、market status、overnight timestamp/rates。
4. 確認 Demo/Live 是否共用同一 WebSocket URL，session response 的 `streamEndpoint` 是否一致，斷線重連與 token 更新後是否必須重新訂閱。
5. 實測歷史 `MINUTE`、`MINUTE_5`、`HOUR` 的最早可得日期、分頁邊界、缺棒、UTC 邊界、DST 與 bid/ask OHLC 一致性。
6. 實測 `lastTradedVolume` 的行為；在 Capital.com 未提供明確定義前，不將它當成全球黃金成交量。
7. 用最小部位測試 create → confirm → position reconcile；涵蓋 200 但 rejected、confirmation 404 暫態、timeout、斷線、重複查詢與 activity 的 `UNKNOWN`。
8. 確認一般 stop、guaranteed stop、trailing stop 是否可用於該 Gold epic，以及停損修改只能收緊時 API 的實際拒絕代碼。
9. 驗證 working order 的 LIMIT/STOP 觸發、有效期、取消與 gap/slippage 行為；釐清官方「working orders」與「all Public API orders are market orders」文字衝突。
10. 以最小部位驗證「+1R 平 50%」的可行做法、netting/hedging mode 差異、實際已實現損益與 `affectedDeals`；官方 REST 文件未提供 size-based close。
11. 驗證是否出現 partial fill、如何表示 remaining size、是否有可用的交易事件串流；若無，將 REST 對帳設為唯一真相來源。
12. 測量 Capital.com endpoint 的 p50/p95/p99 latency、confirmation lag、rate-limit headers／429、維護時段與 market status 轉換；官方未承諾「1 秒內首次回覆」。
13. 由 Capital.com support 或帳戶所屬實體以書面確認：無人值守的分析代理＋交易代理是否符合 Client Agreement / Electronic Trading Terms，是否構成被禁止的 third-party discretionary control。
14. 在 Live 憑證產生前確認 key 是否可限制來源 IP、商品、讀寫能力或子帳戶。公開文件只確認無 read-only key，未記載更細粒度 server-side scope。

## 11. 對系統規格的直接約束

```text
Public API / account truth
        |
        +--> Gold epic 與 dealing rules 每次登入後驗證
        +--> marketStatus / openingHours / swap timestamp 動態刷新
        +--> POST 的 dealReference 必須走 confirmation + reconciliation
        +--> 未證實不存在原 deal 前，不得重送
        +--> 無 read-only key：分析代理與憑證完全隔離
        +--> MCP 不能用於無人值守實盤下單
        +--> Live 前需取得自主交易條款的帳戶實體確認
```

在上述未知項未經 Demo 測試或書面確認之前，系統狀態應保持 `DEMO_ONLY` / `LIVE_LOCKED`。
