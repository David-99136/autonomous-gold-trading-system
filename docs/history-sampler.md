# GOLD 唯讀採樣器：使用與實作報告

日期：2026-09-27。依 `.scratch/history-sampler/spec.md` 完成程式與離線驗收。
本輪沒有讀取使用者憑證、連線券商、下單、解除資料鎖、啟動排程或推送 GitHub。

## 已完成

採樣器只能建立一次 Demo session，再讀取時間、GOLD 市場狀態與固定窗口價格。
計畫不能包含任意 URL、HTTP method、headers、其他商品或訂單端點。
成功僅表示計畫內觀測完成，`qualified`／`closure_verified`／`entries_enabled` 始終 false。

| 檔案 | 責任及關鍵區域 |
|---|---|
| `sampler_plan.py` | `validate_plan` 精確欄位／範圍檢查；`Archive.put` 排他寫入、fsync、64 MiB 配額；兩層租約 |
| `sampler_reader.py` | `DemoHistoryReader.send` 固定 Demo allowlist、單次登入、原始 body hash、接收階段、不重試 |
| `history_sampler.py` | `capture` 統一請求预算／排程／時鐘估計／品質檢查／回執；`clean_prices` 白名單資料與修訂偵測 |
| `sampler_cli.py` | 先檢查當次確認、plan hash、時間與輸出，再讀 vault；使用後清理記憶體內憑證 |
| `tests/test_history_sampler.py` | 可推進的假時鐘＋HTTPX MockTransport；測試走相同 capture 入口，不使用真實網路 |

沒有更改交易用 `AsyncCapitalDemo` 的 timeout 或限流設定。診斷 Adapter 重用其固定
Demo URL，刻意不繼承下單方法。時鐘採用既有三次估計、偏差加不確定度不超過
1 秒的演算法；另保留逐次回執，因為原本 `synchronize` 不提供這些來源紀錄。

## 先做離線計畫核對

```powershell
.\.venv\Scripts\python.exe -m gold_system history-sample-plan --plan examples/history-sample-plan.json
.\.venv\Scripts\python.exe -m gold_system demo-history-sample --help
```

上述指令均不讀憑證、不連網。範例計畫使用過去的固定時間，只供格式／合成測試；
不能直接拿來真實採樣。`PLAN_VALID` 僅表示結構正確，並不檢查現在是否適合開市採樣。
輸出的 `plan_sha256` 綁定 UTC 正規化後內容，改任何實質欄位就須重新核对。

計畫最上層固定 version=1、environment=DEMO、epic=GOLD，另有 start/end 與 queries。
每個 query 必填 at、from、to、resolution、max；from/to 為含 offset 的整分時間。
窗口最多 999 分鐘，max 為 1–1000，解析度限 MINUTE／MINUTE_5／HOUR。
這些是請求輸入限制，不是宣稱券商端點必定包含等號，也不自動生成收棒標記。

query 最早為 start+10 秒，最後不得到 end-5 秒，項目相隔至少 5 秒。
送出時刻允許最多 1 秒排程誤差，超過就停止；這是診斷排程窗口，不是交易延遲驗收。
程序只能在計畫 start 之後、第一個 query 之前明確啟動；不預先排程等待下一交易日。

## 真實執行的必要確認

`demo-history-sample` 需要 plan、精確 plan-sha256、全新 output-name，並要求
`--confirm-demo-read-only` 與 `--exclusive-account`。代理仍須每次取得使用者連線批准，
不能把 CLI 旗標當作永久授權。本輪只查看 help，沒有執行該指令。
帳戶不得與其他已知 API 程序競爭；此確認不能由程式自行推定。

兩層排他租約：專案 runtime，以及同一 OS 使用者暫存區中按登入識別雜湊分組的租約。
後者避免同一登入識別的不同工作副本同時採樣；它不控制其他 OS 使用者、主機、
其他程式或同一帳戶的其他登入別名，也不等於券商端帳戶限流鎖。
正常只移除自己持有的租約檔；崩潰留下的租約不自動刪除，需先核對持有程序。

## 硬限制與停止規則

- 全次最多 75 分鐘、240 次歷史查詢、900 次 HTTP 嘗試、64 MiB 輸出；不追加預算。
- 每次送出相隔至少 1 秒；登入、三次時鐘採樣及 refresh 都計入次數。
- 每 30 秒到期檢查時鐘估計並重採，市場／價格送出時估計不得超過 60 秒。
  [Codex | 2026-09-27] 已到期／4 秒內即將到期的價格請求優先於三次校時；
  refresh 在下一可用間隙進行，並未延長估計有效期或排程容忍窗口。
- 普通請求含排隊最多 5 秒，登入最多 10 秒，且不得超過 run deadline；HTTP 分段
  timeout 為 1.8 秒，單回應最多 1 MiB。這些是本專案診斷限制，不是券商保證。
- 401／429／其他非 2xx、redirect、timeout、時鐘跳變／失效、排程錯過、資料品質錯誤、
  配額耗盡、寫入失敗或取消均停止，不重試、不重新登入、不補送。
- 起始市場檢查須為 GOLD／TRADEABLE；後續價格結果仍核對，但不是持續市場狀態監控。

一般磁碟同步或程序強制中斷無法保證即時收尾；缺少完成回執就不能宣稱成功。
採樣在最後一個 query 完成後結束，不為了湊滿 75 分鐘持續呼叫。

## 證據格式與誠實邊界

runtime 新目錄保留 plan、run-start、每次 request-start、白名單 data、complete／failed、
clock estimate 與 run-result。每個回執都有 run/request ID、plan hash、允許的 query、
UTC／monotonic 的排隊／呼叫開始／headers／body／驗證完成階段、HTTP status 及時鐘綁定。
send-start 是呼叫 HTTP 傳輸前的本機時間，不是封包離開網卡的硬體時間戳。

`body_sha256` 來自 HTTPX 讀取的完整解碼後 body bytes，不是 TLS 封包或重序列化物件；
`data_sha256` 才是實際保存的白名單 JSON 檔案雜湊。非 2xx 不讀取／保存錯誤 body。
headers、帳戶 body、未知欄位、秘密字串不落盤；數值不合法只記列號與固定錯誤碼。
沒有保存完整原始 response，不能重建未知欄位或把雜湊當成券商簽章。

價格交叉／修訂會保留該次可安全記錄的兩側價格及 faults，再停止。首次指紋只在
本次 capture 內比較；跨採樣仍須配合既有離線品質核對，不宣稱已自動跨 run 合併。
from/to 邊界語義仍未驗證；不裁尾、不補值，亦不將缺頭／缺尾升格為完整行情。

data 檔 fsync 完成後，complete 回執才作為 commit marker；孤立檔／只有 start 記錄
代表未完成，不能供正式 feed 使用。complete 若標記 QUALITY_FAULT，也不是合格資料。
沒有自動續跑或孤立檔清理器；原有交易資料庫完全不接入。

正常 CLI 為 `COMPLETED_UNVERIFIED`／exit 0；操作失敗為 exit 1。
`completion_recorded=false` 表示最後回執未可靠落盤，不能用本機口頭摘要代替它。
參數解析錯誤由 argparse 回傳 exit 2。後續接線不得只依 exit 0 允許交易。

## 驗證結果與下一步

新增 33 項離線測試，完整 365 項測試通過；compileall、離線計畫指令、CLI help 及
diff 檢查通過。測試覆蓋 HTTP 錯誤／取消／timeout、不重試、clock、預算、價格修訂、
落盤失敗、缺 commit marker、來源 hash、敏感資料與租約。所有傳輸均為 MockTransport。

下一步需先指定真實採樣計畫與時段，確認沒有其他 API 流量，再取得當次 Demo 唯讀
授權並實測。這些尚未執行；正式收棒接受政策、WebSocket 同步、資料鎖恢復、
GOLD 策略／OOS／Demo 資格均未因此完成。
