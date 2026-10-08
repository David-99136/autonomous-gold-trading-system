# Demo GOLD 唯讀採樣器

Status: resolved
Type: task

日期：2026-09-27。使用者已確認繼續實作；本規格不授權實際登入或開始採樣。
承接 `history-acceptance-recovery` 的前置研究；用途是蒐集證據，不是接受行情或恢復交易。

## 1. 已核對的工程缺口

- `history.download` 保存頁面、from/to 與接收時間，但沒有逐次 send-start、body-complete
  或時鐘估計綁定；它序列化解析後 JSON，不能把其 hash 稱為原始 HTTP bytes hash。
- `AsyncCapitalDemo._request` 已有固定 Demo URL、禁止 redirect、分段大小限制、
  總 timeout 與單 instance 排程，但未回傳完整時間回執或狀態碼。
- `clock_sync.synchronize` 取三次 `/time`，`BrokerClock` 有 offset／uncertainty 與有效期；
  必須保留每次時鐘採樣的來源，不能只有最後估計值。
- 單個 client 的鎖不是跨程序帳戶共用限流；不能宣稱目前已完成全帳戶協調。

以上是本機程式現況，不是新查證的券商契約。官方來源與未知項見
`docs/research/capital-history-finality-review-20260927.md`。

## 2. 範圍與交付順序

先實作單一採樣 Module 與離線 fake Adapter，透過同一 Interface 驗證計畫、
請求回執、落盤及停止條件；再補固定 Demo 唯讀 Adapter。避免另建交易執行管線。
開發階段只做 fake／MockTransport 測試，不讀 vault、不使用真實網路。

本批只採 REST 固定窗口，不擴充 WebSocket OHLC；既有 quote probe 不冒充完整 tick
或 OHLC finality 證據。之後若要同步串流採樣，另補計畫與驗收，不拼湊不完整報價。

```text
離線採樣計畫 → 採樣 Module → 受控唯讀 Adapter → 回執／白名單價格檔
                         ↘ 任一故障：停止＋保留已完成證據
```

唯一執行 Interface 建議為 `capture(plan, reader, archive, clock)`；
reader／archive／clock 在 Seam 注入，fake Adapter 與正式 Adapter 使用相同契約。
呼叫者不需要自行處理重試、節流或時間欄位。只回傳完成／失敗摘要，不回傳交易訊號。

## 3. 計畫與硬限制（專案提案，並非券商限制）

- plan 有版本、內容 hash、GOLD、Demo、明確起訖 UTC 及相對 monotonic deadline。
- 僅准 MINUTE／MINUTE_5／HOUR；每項明確指定 from/to、max 與執行時間，
  不動態使用省略 to 的「最新資料」。最多 240 次歷史查詢、窗口最多 999 分鐘。
- 全次最多 75 分鐘、900 次 HTTP 嘗試、64 MiB 落盤；失敗／登入／市場／時鐘請求
  都計入次數，單回應上限沿用傳輸的 1 MiB。任一預算不足便停止，不追加額度。
- 本批診斷 client 間隔至少 1 秒，串行送出，無自動 retry／重新登入。
  時鐘三次採樣及每 30 秒 refresh 同樣受此排程與總預算約束。
  [Codex | 2026-09-27] 排程修正：到期價格查詢優先，距下一筆查詢不超過 4 秒時，
  refresh 延至下一可用間隙；價格使用的估計仍不得超過既有 60 秒有效期。
- request 的排隊上限與 HTTP 執行 deadline 分開計算，避免沿用現有 2 秒總 timeout
  包含較長排隊時間而誤判；送出與等待均不得超過 run deadline。
- 提案預設排队／送出總時限最多 5 秒（登入最多 10 秒），不更動交易 client 的設定。
- 採樣時帳戶不得有其他已知 API 使用者／程序競爭；本工具持有本機帳戶診斷租約。
  租約不能證明其他主機不存在流量，不能聲稱跨主機強制限流；無法確認時不執行。

未達觀測目標不延長當次授權。超時後不得用補送把樣本偽裝成準時採樣。
具體邊界時刻、from/to 與計畫 hash 在實際連線前交使用者確認；不在此排程。

## 4. 每次回執契約

保存 run ID、request ID、序號、受控 endpoint 類別、允許的 query、plan hash；
記錄 queue-start、send-start、headers-received、body-complete、validation-complete
的 UTC 與 monotonic 時間，明確區分網路接收與本地解析完成。
若失敗發生於某阶段，後續時間為 null，不能用現在時間補成「成功接收」。

時鐘回執保留 estimate ID、三筆 `/time` request ID、offset／uncertainty、anchor 與
估計年齡；市場查詢綁定送出時有效的 estimate。`/time` 自身不要求尚未存在的估計。
沿用既有偏差＋不確定度限制及時钟跳變檢查，不藉研究用途放寬；失準則停止。

僅從 transport 層取得實際 HTTP status 與 body bytes hash；禁止從解析物件推回原始
位元組雜湊。非 2xx／無回應時依實際狀態記錄，不將錯誤 response body 或 headers 落盤。

成功價格回應只保存白名單 timestamp、雙側 OHLC 與必要市場狀態。數值以有界 Decimal
正規化，不修改值或補 ask。額外欄位不保存；不合法欄位只記 row index 與固定錯誤碼。
另保存「白名單檔 SHA-256」及正規化版本，與原始 body hash 清楚區分。
這不是原始 response 的完整備份；不足以重建未知欄位，報告必須揭露此限制。

## 5. 可追溯落盤與中断

只能使用 runtime 下的新診斷目錄，不接受既有交易 DB、任意輸出路徑或覆蓋。
開始先以排他方式保存 plan 和 run-start；每次送出前落盤 request-start。
每份白名單價格檔獨立編號／排他建立，flush 後 fsync，再保存完成回執與檔案 hash。
回執是完成證據的 commit marker；沒有它的檔案是孤立／未完成，不可當成可接受行情。
原始舊檔、失敗事件與價格版本不刪除；程序重啟不自動續跑／重送。

run 結束寫完成摘要或固定失敗碼；磁碟故障時可能連失敗摘要也寫不成，
此時 CLI 非零退出並明確說明完成狀態未知，不宣稱稽核已保存。
正常結束也始終輸出 `entries_enabled=false`、`closure_verified=false`、`qualified=false`。

## 6. 唯讀能力及停止條件

允許連線時僅由受控 Adapter 執行一次 Demo session 建立，然後呼叫 allowlist：
`GET /time`、`GET /markets/GOLD`、`GET /prices/GOLD`。session secrets 僅留記憶體。
不是將一般 client 的任意 `_request` 直接暴露給 plan；拒絕其他 path、method、
query 欄位、redirect、Live、訂單／資金／帳戶切換操作。

429、401、其他非 2xx、timeout、時鐘失效、解析錯誤、超限、寫入失敗、
使用者取消、未如期送出／程序中断均停止，不自動換 session 或補跑。
市場不可交易時不把空窗口當資料完整，也不繼續診斷；這不是策略的時段資格判斷。
品質異常需保留當次白名單證據並結束本次採樣，後續採樣須另行確認。
本診斷不接正式 Store，不能宣稱已封鎖其他執行器；也不解除任何鎖。

## 7. CLI 與驗收

預計 `history-sample-plan` 只生成／核對本地計畫；`demo-history-sample` 才能連線。
help、plan、測試均不讀憑證。執行需指定計畫檔、精確 plan hash、新輸出目錄及
明確 Demo-read-only 確認；該 CLI 確認不代替代理在每次實际連線前取得使用者授權。

離線驗收至少涵蓋：

1. 同一 Interface 下成功回執、UTC／monotonic 單調與時間阶段、不可偽造缺失時間。
2. 有界計畫、from/to 非整分／缺值／時區、商品／解析度／method／path 非法拒絕。
3. 次數／時長／大小／磁碟配額計入失败与時鐘流量；錯過排程不補送。
4. 429／401／timeout／取消無 retry 或重新登入；寫入失敗前後皆停止。
5. 雙側交叉、修訂、不合法價格、未知欄位／秘密 canary 不外洩。
6. plan hash 不符、輸出已存在、路徑逃逸、租約衝突、孤立檔／缺完成回執。
7. fake transport 證明所有送出都是 allowlist，無下單或 Live；核心無 vault 相依。
8. 所有結果無交易資格；不改既有 Store、風控或價格首次觀測基準。

## 8. 開發核准與連線邊界

已核准範圍是「實作以上 REST-only 採樣器與離線測試」，不含實際 75 分鐘
採樣。該時長只是硬上限，不能據此宣稱足以驗證 finality。實際 Demo 連線仍另問。
初稿時僅建立規格；後續實作結果見下方 Answer。先前 332 項測試結果不代表此功能驗收。

## Comments

2026-09-27：使用者回覆「繼續」，核准依本規格開發及離線測試；不授權實際連線。

## Answer

程式與離線驗收完成，見 `docs/history-sampler.md`。33 項新增測試，完整 365 項通過。
compileall、CLI help、離線 plan 核對與 diff 檢查通過；未讀使用者憑證或呼叫真實 API。
兩層租約涵蓋專案與同 OS 使用者／登入識別，並非券商端全帳戶或跨主機強制限流。
診斷排程容許 1 秒送出窗口，不適用於交易延遲資格。預設停止而非補送。
保存白名單檔與完整解碼 body 的不同雜湊，不宣稱保存完整原始 response。
resolved 只表示本規格的開發／離線驗收完成，不是正式 Demo 採樣、finality 或交易資格。
