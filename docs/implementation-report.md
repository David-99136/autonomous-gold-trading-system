# 第一階段實作報告

日期：2026-09-06。版本：0.1。範圍：可執行離線原型與 Demo 資料探測。

## 已實作

| 模組 | 責任 |
|---|---|
| `gold_system/core.py` | Decimal 價格、商品規格、訊號與不可變風控設定 |
| `gold_system/risk.py` | 新倉前驗證時效、價差、波動、時間窗、成本後 1:1.5、0.25% 風險與 20% 保證金；向下取整 |
| `gold_system/engine.py` | 串列處理意圖、3%/10% 已實現損益熔斷、停損、+1R 半倉退出與追蹤、結算前 10 分鐘退出 |
| `gold_system/broker.py` | 帶不利滑價的 bid/ask 離線成交與單一部位；停損不可放寬 |
| `gold_system/store.py` | SQLite WAL、持久化意圖去重、熔斷保存、事件及敏感欄位遮罩 |
| `gold_system/analysis.py` | 完整 K 棒連續性、1H/5m/1m continuation 價格原型、新聞欄位加權缺漏檢查 |
| `gold_system/capital.py` | Demo session、Gold 搜尋、規格與歷史行情查詢、Windows 憑證庫，阻擋 HTTP redirects |
| `gold_system/validation.py` | PF、盤中權益回撤、樣本門檻與營運費用計算 |
| `gold_system/cli.py` | 合成資料展示、JSON/Markdown 報告與 Demo 探測命令 |

## 關鍵程式解析

`make_plan` 先確認結構停損有效，再依風險金額與保證金可用額取較小下單量。
部位向下取整至兩倍步進，確保 50% 停利仍符合最小交易量；無法符合時拒單，不改近停損。

`Engine.tick` 先管理持倉，再評估新倉。新聞服務或預算不可用只會阻擋新風險，
不妨礙已持有部位退出。意圖先落入 SQLite，送出結果不明後鎖定，不會因重試造成雙重下單。

`CapitalDemo` 只有 Demo session 與行情查詢，固定 endpoint 且不跟隨重新導向。
尚未完成遠端訂單確認與部分平倉實測，所以程式未暴露遠端下單功能。

## 規格澄清

由實際 bid/ask 成交價計算的已實現損益已包含價差與滑價，不再重複扣除。
只有未入帳平台費、AI、行情與主機成本另扣。
此版 UTC 日界線只是離線測試設定；正式帳戶的交易日、夏令時間及 funding timestamp 待確認。

## 尚未完成／禁止冒充已完成

- 即時 AI 主模型、授權新聞、跨市場來源、月預算實際金額；沒有產生付費呼叫。
- 供需區與左右側訊號已有可重播原型；反向切換與最多一次盈利加碼已加入離線引擎，實際參數校準與遠端部位操作仍待完成。
- 20 日價差分布、動態高波動／交易時窗的資料收集與計算。現有 Quality 是明確測試輸入，預設全部 fail-closed。
- 已加入使用者 CSV bid/ask OHLC 重播；真實 tick 重播、walk-forward 訓練及 Monte Carlo 尚未完成。
- Demo 實際送單、broker confirmation、部分平倉、完整持倉對帳與斷線恢復。
- WebSocket 長時間服務、人工控制台、SMTP 告警、雲端 PostgreSQL 與 Live 解鎖。
- 300 筆 OOS、30 日／100 筆 Demo 等驗收尚無資料，全數未通過。

## 驗證

測試涵蓋風險取整、多空、價差／波動／時效／日界限制、部分停利、停損收緊、
已實現熔斷持久化、重複意圖、送出後逾時、敏感資料遮罩、Demo 認證與重新導向阻擋、
缺棒／未收盤資料、缺漏 40% 門檻與成本防重複計算。
合成展示會生成獨立的報告與事件庫，明確標示不具策略驗收資格。

## 接續步驟

在本機憑證庫完成 Demo 設定後執行 `demo-discover`，用回傳商品規格完成 sizing／session adapter，
再驗證遠端部分平倉與確認流程。模型與新聞來源尚待選定與量測。
憑證不應直接交付代理。初次離線搭建未建立券商 session；後續授權 Demo 探測已由程式透過 Windows 憑證庫登入，沒有將憑證輸出到聊天。

## 本輪追加

加入 `gold_system/replay.py`，將完整 K 棒、左右側結構分析與交易引擎串接。
輸出逐時點權益與完整交易統計；不將半倉停利拆成兩筆獨立策略交易。
資料格式請見 [CSV 操作說明](csv-replay.md)。
專案 `.venv` 已建立，`keyring` 與 Windows 後端載入成功，未讀寫任何憑證。

## Demo 登入 401 診斷改善

使用者回報 `/session` HTTP 401。原錯誤處理隱藏全部回應，無法區分拒絕原因。
目前僅顯示格式受限且不含本次憑證的 `errorCode`；非 JSON 或不合格式的回應顯示 `UNAVAILABLE`。
已以假 HTTP 回應重現並測試遮罩行為，未讀取使用者憑證或執行真實登入。
後續使用者回報手動重輸 API key 後登入成功，Demo 查詢也已驗證成功。
這支持原先輸入內容有差異，但沒有證據能指定是哪個字元或剪貼簿行為造成錯誤。

## 2026-09-06：商品、行情與唯讀對帳進展

- `market.py`：使用 Demo 商品回應轉換百分比停損距離與保證金、跨午夜時段及 funding 邊界。
  已讀取的 GOLD 規格為 USD、最小量／步進 0.01、marginFactor 5%；這是當次快照，不是永久設定。
  每點損益乘數仍要求獨立驗證，不能只由 lotSize 推定。
- `history.py`：分頁下載並保存來源雜湊，重疊資料去重，衝突拒絕。
  本機 `runtime/history-20260904/manifest.json` 記錄 1,258 根，內部缺少 12:31 UTC 的資料；未補造。
- `reconciliation.py`：前後帳戶一致性、USD／淨額模式、完整持倉集合、停損不可放寬、未知掛單／意圖檢查。
  `verify_open_confirmation` 要求 reference、affectedDeals、方向、實際數量／價格與持倉快照共同吻合。
  多筆 affectedDeals、部分成交、只有 HTTP 成功編號或持倉消失均不能視為已確認開倉。
- `capital.py` 新增 GET-only 查詢；`demo-reconcile` 可直接使用，沒有任何遠端訂單寫入路徑。

架構：`Demo REST 唯讀資料 → 記憶體 Snapshot → 嚴格集合比對 → 去識別化 SQLite 摘要`。
成交檢查函數不會自行清除 UNKNOWN 意圖，也不會解除鎖定；未接入尚待實作的遠端送單服務。

2026-09-06 05:04:49 UTC 實測：0 持倉、0 掛單，查詢集合耗時 4.688 秒。
由於超過本模組保守的 2 秒快照跨度，結果為 `RECOVERY_LOCKED / SNAPSHOT_STALE_OR_SLOW`。
這不是券商故障，也不等於規格中的首次訂單回應延遲；兩者必須分開量測。
之後需改善收集流程並驗證一致性，不能為取得通過而直接放寬門檻。

本輪共 51 項單元／整合測試通過。新增 16 項對帳測試包括帳戶切換、資料過期、未知意圖、
缺停損、部分數量、多空停損方向、錯誤 reference、多筆成交、查詢失敗與私密例外不落盤。
成交測試使用假回應；真實 Demo 下單、部分平倉、重連、AI 分析與策略驗收依然未完成。
因此整體「完成實作」目標維持進行中，不能宣稱系統已可自主交易。

API 語意依據：[Capital.com Public API](https://open-api.capital.com/)，查證日期 2026-09-06。

## 2026-09-06：費用帳本與分析呼叫閘門

新增 `costs.py`，把規格第 12、15 節的費用控制落實成可測試模組。

| 元件 | 責任與限制 |
|---|---|
| `CostLedger` | SQLite 原子預留、USD 實際入帳、凍結月額及 AI/DATA/HOSTING 共用上限 |
| `budgeted_analysis` | 預留成功才呼叫 provider；重複 request ID 不重呼；逾時／取消保留費用 |
| `AnalysisReceipt` | 分離分析訊號與實際費用；仍需 provider 的可追溯用量與凍結費率，不能造估計帳單 |
| `Engine.cost_ledger` | 接入時在新倉前檢查帳本；費用耗盡／帳本故障不干擾先執行的硬停損與時間退出 |
| `cost-status` | 本機狀態查詢，不設定額度、不讀券商憑證、不發付費 API 請求 |

關鍵程式：`reserve` 在 `BEGIN IMMEDIATE` 交易內完成額度檢查及寫入，
因此兩個連線不能同時取得同一份剩餘預算。`settle` 與帳務事件同一交易落盤，
重複相同入帳不會重複扣款，不同金額則拒絕。未知結果沒有自動釋放或重試路徑。
若 provider 的實際費用高於預留額，保留真實費用並記錄 `OVERRUN`，不假裝硬上限能約束服務商帳單。
真正的請求成本上界仍須 adapter 以輸入／輸出限制與已查證費率實作。

帳本依 UTC 月份歸屬請求。跨月補入帳仍留在原請求月份；新月份沒有自動授權新額度。
這是目前保守的本地設定介面，不是已完成的預算管理 UI。

本次本地 `cost-status` 顯示 2026-09 月額未設定、帳本已記費用與預留均為 0，付費分析不可用。
這不代表整個 Codex 開發過程或服務商帳單沒有費用；本帳本尚未接入那些資料來源。

驗證：67 項測試全部通過，新增 14 項費用测试及 2 項引擎整合測試。
涵蓋兩連線競爭額度、程序重啟、跨月、逾時、取消、超額、重複請求、服務商例外遮罩、
以及費用故障時仍能管理已有部位。此輪沒有券商或付費模型網路請求。

仍待完成：實際 provider 選擇／用量計價、使用者核准月額、獨立服務中的強制帳本接線，
以及營運費用與真實成交報告整合。離線 replay 仍使用明確的合成品質輸入，不能當成付費服務安全驗收。

## 2026-09-06：離線反向訊號生命週期

`risk.validate_signal` 現在供進場與反向退出共用，驗證版本、型別、時效與價格欄位。
引擎另外要求資料／分析可用、時鐘符合限制，且當前價格尚未越過反向訊號的失效價格或目標。

執行順序為：`硬熔斷／停損／時間退出 → 有效反向平倉 → +1R 半倉／追蹤 → 新倉檢查`。
反向平倉前先寫入獨立 `reverse:<signal_id>` 意圖，成功後記入已實現損益。
同一次 tick 不反手；下一次 tick 必須再次通過完整的新倉風控。軟熔斷或預算耗盡可退出，但不能重新進場。
平倉結果不明時保存 UNKNOWN 並进入 RECOVERY_LOCKED，不重送反向退出，也不猜測成交損益。
停損與時間退出仍是獨立的風險減量路徑。

新增 11 項測試，整套 78 項通過。涵蓋多空反向、過期／錯版／失效訊號、資料故障、
預算耗盡、軟／硬熔斷、停損及時間退出優先、平倉前後逾時、重複反向意圖不妨礙部分停利。
SQLite 重複意圖失敗時增加 rollback，避免未結束的交易影響後續帳本操作。

限制：目前成交仍由 PaperBroker 同步執行。遠端 journal／成交／帳務原子恢復、
券商確認後的反向進場、最多一次盈利加碼與實際部分平倉仍待完成；本輪未建立券商 session 或送單。

## 2026-09-06：盈利加碼與完整滑價風險預算

`make_add_plan` 已加入單一部位的加碼風控：原部位有淨浮盈、券商模擬停損已到淨保本、
新收盤 5m 棒支持相同方向及策略模式，且仍通過所有新倉品質與預算檢查，才能至多加碼一次。
新停損不能放寬，部位大小以「原始風險餘額、20% 總保證金餘額、商品總量上限」共同向下限制。
已鎖定的盈利不會被當成額外可損失預算；最低量不符合即拒绝。

`Position` 保存原始風險、初始成交價、實際進場時間與加碼次數。
`PaperBroker.add` 更新加權成本但不改寫原始 1R 基準。尚未半倉退出時，加碼量保留可對半的步進；
已部分停利後則使用一般最小步進。CSV replay 已將分析所用的最新已收盤 M5 棒傳入加碼檢查。
這仍使用未校準的價格策略；M5 同向條件不是已驗證的盈利保證。

核對實際離線成交模型時，也修正原先只預留一次滑價的不足：
進場與出場各一次不利滑價均計入風險及成本後 RR；bid/ask 價差仍不重複扣除。
1R 觸發價亦扣除預期退出滑價，模擬券商與風控的滑價假設不一致時拒絕啟動。

新增 9 項測試，累計 87 項通過；包括多空實際模擬停損損失等於預算、
合併部位風險與保證金、不得攤平／二次加碼／放寬停損、M5 時效與方向、
部分停利後加碼的引擎與事件整合、部分停利前的可分割量及滑價假設一致性。
正式驗收仍需真實 Demo 加碼／部分平倉／滑價樣本、OOS 與 30 日 Demo 成績。

## 2026-09-06：持久化訂單送出核心

新增 `order_journal.py`，為尚待接線的 Demo Order Gateway 提供 journal 與非同步 dispatch 核心。
它不持有憑證、不提供解鎖、不直接呼叫 Capital.com，不能取代風控核准或啟用條件。

狀態順序：`PREPARED → SUBMITTING → ACKNOWLEDGED → CONFIRMED / REJECTED`；
送出或對帳逾時則保留 `UNKNOWN`。PREPARED 是已保存但未開始送出；SUBMITTING 必須在外部呼叫前提交到 SQLite。
相同 intent ID 內容變更會拒絕，帳戶使用雜湊綁定。收到 reference 不等於成交，
reference 尚未知時即使對帳器回報拒絕也不採信為終態。例外原文與自由文字理由不寫入 journal。

同一帳戶只容許一個進行中／已開倉初始意圖，CONFIRMED 不等於平倉。
此版尚無「確認平倉後釋放曝險占用」或加碼 journal 動作，因此不能拿來啟動完整交易循環。
`dispatch_once` 的 send／reconcile 目前為測試注入；正式 transport 必須支援非阻塞、可取消等待，
不能假定 urllib 執行緒會因 asyncio timeout 而停止網路請求。
每段等待上限設定至 2 秒，但不將此單元測試門檻冒充真實券商延遲保證。

新增 14 項測試，累計 101 項通過。涵蓋外部呼叫前可跨連線讀到 SUBMITTING、
重啟不重送、並行重複送出攔截、真實非同步等待逾時後進入對帳、取消、內容／reference 衝突、
過期意圖與未知狀態阻擋後續開倉。測試對帳器是 mock，不是實際成交證據。

官方 API 已重新核對：[Capital.com Public API](https://open-api.capital.com/)，2026-09-06。
尚待工作仍包括風控／市場帳戶快照接線、真實 Demo transport、成交風險再驗證、
平倉／改停損／取消／加碼的動作 journal、完整恢復服務與實測；本轮未實際下單。

## 2026-09-06：Demo 非同步 HTTP 傳輸

新增 `async_capital.py`，使用 `httpx==0.28.1` 的 AsyncClient／連線池，
已安裝於專案 `.venv` 並列入 `.[demo]` 依賴。
傳輸層固定 Demo URL、停用環境代理與重新導向、重試次數為 0，
以 async deadline 包住完整請求，另設 socket timeout 與一般／登入限流。
登入成功後只保留 session headers，不保留 API key 或自訂密碼。
HTTP 錯誤不輸出 body，傳輸例外不輸出原文，取消登入會清除 session。

`post_prepared_open` 現已能組合官方初始 position payload，但預設 write_guard=None 即拒絕。
每次要有 durable SUBMITTING、獨立 guard 核准、帳戶雜湊與 USD 一致、未過期且狀態未改變，
才允許送出一次。Decimal 價格與数量直接編碼為 JSON number，沒有 float 往返或字串數字猜測。
即使網路逾時，該意圖仍保留已嘗試送出標記；取消等待不能被當作券商取消訂單。

新增 13 項測試，累計 114 項通過；透過 HTTPX MockTransport 實際走編碼、路由、
headers、取消與 journal 接線，未發送任何真實券商請求。
測試涵蓋預設禁單、風控拒絕、帳戶切換、狀態變更、POST 逾時不重送、redirect 阻擋與敏感例外遮罩。
這比純 callback mock 多驗證一層 HTTP 請求，但仍不是真實 Demo 成交驗收。

正式 guard 尚未完成，沒有任何 CLI 可以開啟下單。風控／成本／市場品質的獨立程序隔離、
契約每點損益驗證、真實成交後風險檢查、其餘訂單動作及長期驗收仍未完成。
因此不能把此傳輸層視為已可自主交易的整套系統。

依據：[HTTPX 非同步支援](https://www.python-httpx.org/async/)、
[HTTPX timeout 語意](https://www.python-httpx.org/advanced/timeouts/)、
[Capital.com Public API](https://open-api.capital.com/)，查證日期 2026-09-06。

## 2026-09-06：初始成交對帳接線與非同步 Demo 實測

`collect_async` 使用與同步版相同的前後帳戶、持倉、掛單、偏好與活動檢查。
`reconcile_open_async` 已接到 journal 原始意圖與現有確認驗證器：
除了查成交編號，還比對实际大小、方向、填單價、券商停損、帳戶雜湊，
並重新計算剩餘硬停損風險與保證金是否超過原核准值。
不利滑價超出假設、大小不一致或缺停損皆回 UNKNOWN；不是把實際成交否認成未成交，
而是要求恢復處理，禁止直接解鎖新風險。

查不到確認時仍嘗試讀取持倉／掛單／活動。收到 REJECTED 也必須確認帳戶空倉且一致，
否則仍 UNKNOWN。尚未取得 reference 的逾時訂單不會因空倉就被視為確定失敗。
contract 的每點損益仍必須由獨立 gateway 提供已驗證值，不由分析代理猜測。

新增 9 項測試，累計 123 項通過。涵蓋 journal dispatch 接入實際對帳函數、
多空滑價與風險、部分成交、停損缺失／放寬、錯誤乘數、拒單但仍有曝險與查詢失敗。
上述成交資料皆為測試回應，不代表真實送單驗證。

另在取得本次權限後執行唯讀命令 `demo-reconcile --async-http`：
2026-09-06 11:15:51 UTC，0 持倉、0 掛單，快照收集耗時 **1.750 秒**，
狀態為 `SNAPSHOT_MATCHED` 且 `entries_enabled=false`。
相較之前同步探測的 4.688 秒，這次單筆觀測已在既有 2 秒門檻內，沒有放寬限制。
這不證明 p95 或首次訂單回應延遲；亦不代表已有背景交易服務運行。
證據保存於 `runtime/demo-audit.db` 的 BROKER_RECONCILIATION 事件。

本輪只有真實 Demo 唯讀請求，沒有送單。正式風控 guard、完整訂單生命週期、
AI／資料品質服務、獨立程序隔離、控制台／通知、統計驗收與 Live 授權仍未完成。

## 2026-09-06：完整交易權益路徑與重抽樣壓力測試

`replay.py` 現在記錄每筆完整交易的起始權益、方向、策略、開平倉時間、淨損益與盤中累積報酬路徑。
部分停利及加碼維持同一筆交易，不把獲利部分拆成額外勝場。
進場後立即按 bid/ask 標記權益，最終退出則保留實際模擬成交成本，避免漏記早期浮損或最後滑價。

新增 `stress.py` 與 `stress-replay` 命令。每次重抽連續交易區塊，逐筆套用整段盤中權益路徑，
輸出 nearest-rank 第 95 百分位最大回撤、最壞回撤、破產次數、種子與路徑來源 SHA-256。
若權益曾低於或等於 0，該次模擬不允許靠後續盈利「復活」。
額外成本僅扣一次／筆，並明確分離於路徑原有成本，避免重扣價差／滑價。

新增 7 項測試，累計 130 項通過。涵蓋盈利平倉仍保留盤中 20% 浮損、
連續虧損複利、同種子可重現、成本只扣一次、破產不復活、缺失／非有限路徑拒絕，
以及重播輸出完整路徑與淨損益一致。
另提供 `examples/stress-synthetic.json` 供命令列煙霧測試，清楚標記合成資料。

限制：這是以已觀察交易路徑為條件的重抽樣工具，不是市場生成模型，
也未代替 walk-forward、新聞 point-in-time、斷線／延遲成交等完整操作壓力測試。
區塊長度、成本壓力情境與模擬次数仍須研究固定；輸出不會自動通過任何 Demo／Live 驗收。
目前沒有足夠真實 OOS 樣本，因此規格第 13 節仍未完成。

## 2026-09-06：Walk-forward 時間隔離與凍結記錄

新增 `walk_forward.py`，將資料依固定 train／purge／test 長度切成連續且測試區間不重疊的 fold，
不使用隨機訓練測試拆分。fit 只接收訓練資料；evaluate 只接收設定副本、當時已知 warmup 與當前測試窗，
不傳入後續樣本。不足完整測試窗的尾端樣本明確列於報告，不擴寫資料。

每個 fold 在 evaluate 前將訓練、測試及設定 SHA-256、策略／模型／prompt 版本写入 prepared 檔。
測試後再次核對設定未變動；若被修改，沒有成功 result 或整體 summary。
策略結果必須包含每根測試 K 棒的盤中標記時點及視窗終點，交易不可跨窗或重疊，
完整淨損益必須與測試權益變動相等。現版不允許未歸屬的入金提款或費用混入樣本外收益。
成功時保留結果雜湊、每窗績效及完整結果，既有目錄不可覆寫。

新增 7 項測試，累計 137 項通過，驗證 fit 看不到測試資料、prepare 先於 evaluate、
設定變更不能產生成功結果、缺盤中標記／跨窗交易／帳務差異拒絕、
同資料設定輸出可重現，以及重複來源或樣本不足不得產生驗收結論。

此功能是研究協調介面，不是已接通的真實策略訓練器。
Python callback 並非安全沙箱，不能用此測試取代策略外部 I/O 與 point-in-time 特徵審查；
跨 fold 資金曲線拼接、分策略／方向／regime／session 統計與完整成本仍待接線。
因此報告保持 qualified=false，不會因介面測試通過而解鎖 Demo 或 Live。

## 2026-09-07：初始開倉獨立風控與傳輸對帳串接

新增 `entry_guard.py` 的 `EntryContext` 與 `DemoEntryGuard`。Gateway 在送出
初始開倉前，從自己的資料來源重新核對帳戶指紋、空倉／掛單／未解決意圖、
當日已實現損益、資料時間、契約驗證、Demo 啟用狀態與實際費用庫。
使用最新權益、報價及原訊號重新計算可接受部位，不能沿用過期核准量；
亦檢查價格跳動單位、最大距離及原風險／保證金上限。來源故障預設拒絕，
一般稽核日誌不保存來源原始例外。

架構為持久化意圖 → 獨立風控 → 固定 Demo HTTP adapter → 成交確認與帳戶對帳。
新增的三項串接測試使用真實 guard、adapter 及 reconciler，只有 HTTP 遠端為
MockTransport：吻合成交可以確認；缺少券商停損保留 UNKNOWN；軟鎖定阻止 POST。
這不是實際券商成交測試，亦未證明實際延遲或券商行為。

本次完整測試 149 項通過，`git diff --check` 通過（僅有 Windows 換行提示）。
權威 EntryContext 資料生產器、策略資格證据與操作者啟用流程仍未接線；
正常傳輸建構仍預設禁止寫入。未設定使用者月度費用額度、未下 Demo 或 Live 訂單。

## 2026-09-07：持久化停止新倉控制

新增 `stop-new --database` 本機命令，要求明確指定既有資料庫。
Store 使用同一 SQLite 交易提交操作者停止狀態與稽核事件；提交失敗不回報成功。
Engine 先處理既有部位，再於新增風險前讀取鎖；DemoEntryGuard 也獨立讀取。
鎖的非布林資料或讀取失敗均視為停止，重啟／另一個資料庫連線不會清除鎖。

新增 5 項測試：跨連線持久化與封鎖新倉、停止後仍執行硬停損、
壞資料預設封鎖、稽核失敗回滾，以及權威來源核准不能覆蓋操作者停止。
完整 154 項測試通過。

這只是操作控制的一部分：沒有解除、平倉、取消掛單或 Demo 切換介面，
也不是阻斷所有在途請求的原子屏障。指令只控制指定資料庫，不會發出券商請求。
未對使用者執行中的資料庫實際設定停止，亦未啟動交易服務。

## 2026-09-07：Walk-forward 連續資金與分組損益

`run_walk_forward` 可明確指定 Decimal `initial_equity`，並將各窗前實際承接資金
先寫入 prepared 記錄，再以 `initial_equity` 關鍵字傳給 evaluator。
評估器須用這筆資金重新執行策略／定倉；工具不會事後縮放既有交易來模擬複利。
每窗首筆權益必須吻合前窗期末值，盤中觀測保留在全段 OOS 曲線，相同邊界時點只保留一次。
任何非正盤中權益要求另行建立清算模型，不容許下一窗重置資金掩蓋破產。

全段報告提供 MTM 最大回撤、PF、淨損益、交易數，以及策略模式 × 多空方向的
交易數、勝負／損益兩平數、淨損益與 PF。沒有虧損樣本時 PF 為 null，不宣稱無限獲利能力。
未傳 initial_equity 的既有獨立視窗模式仍保留 NO_GLOBAL_EQUITY_STITCHING 限制。
版本標記更新為 TIME_ORDERED_WALK_FORWARD_V2，輸出仍 qualified=false。

新增 4 項測試：資金逐窗承接及盤中回撤、重設資金拒絕、盤中破產拒絕、
初始資金輸入檢查。完整 158 項測試通過。
仍缺真實策略 evaluator、分組 MTM 歸因、regime／session 統計與實際完整成本；
本次合成測試不構成策略或實盤驗收證據。

## 2026-09-07：價格重播核心接入 Walk-forward

抽出共用 `replay_candles`，原 `replay-csv` 與新的 walk-forward adapter 都執行
相同 Analysis 原型、Engine、PaperBroker 與風控。warmup 僅建立已收盤指標，
資料缺口清空歷史，不能在暖機段產生交易或 OOS 損益。
輸出新增帶時間的完整權益路徑與平倉邊界；窗首初始資金與同時開倉價差用
合成的 1 微秒順序標記分開保存，這不是實際 tick／延遲證據。

`replay_evaluator` 只接受目前固定價格版本與 NOT_USED 模型／prompt，未知參數拒絕，
不會悄悄忽略使用者參數。`walk-forward-csv` 可直接執行此固定基準並保存每窗重播檔案。
目前沒有參數訓練或 AI；train-size 在此 adapter 的作用為歷史指標暖機長度。

新增完整串接測試，以連續上升的合成 bid/ask K 棒讓真實原型策略產生交易，
檢查交易範圍、資金承接、固定設定與報告標記。完整 159 項測試通過，
命令列 help 可正常顯示。尚未以已驗證 Capital.com 資料執行 OOS 獲利驗收。

## 2026-09-07：原始歷史稽核與時間語義研究

依 research 技能以背景研究核對官方 REST／OHLC 文件，結果存於
`docs/research/capital-history-semantics.md`。公開文件未足以證明 GOLD 時間戳為
開棒或收棒標記，因此保留原始時間，不自行加一分鐘或宣稱完成棒已確認。

新增 `history-audit` 離線命令，由原始分頁重建 prices.json，檢查頁面 SHA-256、
固定檔名及目錄邊界、查詢區間、分鐘對齊、衝突資料、合併內容及 manifest 覆蓋資訊。
命令不登入券商、不修改原始下載，也不認證內容的來源真實性或 OHLC 品質。

已實際核對 `runtime/history-20260904`：2 頁、1,258 筆內部一致，12:30 至 12:32 UTC
間仍有未分類缺口；不補值、不轉成已驗證回測資料。新增篡改／路徑／覆蓋測試，
完整 160 項測試通過。研究後續需經批准的 Demo 唯讀邊界觀測及必要的官方確認。

## 2026-09-07：量化發布的 Point-in-time 證據庫

新增 `evidence.py`：量化證據明確保存 series、vintage、來源 HTTPS URL、觀測／發布／
接收時間、Decimal actual、單位與原始資料 SHA-256。EvidenceArchive 只能追加版本；
相同 ID 不同內容拒絕，修訂須另建 ID，重啟不會重寫接收時間。

`as_of` 同時排除截止時間後的發布及接收；各 series 有效期由呼叫者明確提供，
不把慢頻背景資料的新鮮度套用於即時報價。相同發布時間的衝突值不任意挑選。
`context` 以當時可用的證據計算版本化完整性：任一必填 series 缺失、或選填加權
缺漏達 40% 即不合格。通過證據完整性不等於交易啟用，回應保留 entries_enabled=false。

新增 8 項測試涵蓋接收延遲、修訂不倒灌、ID 不可覆寫、版本衝突、時間／數值／
來源檢查、資料庫篡改、重啟及 40% 邊界。完整 168 項測試通過。

此模組尚未接入供應商或 AI；HTTPS／雜湊不證明來源權威或內容真實，
實際 provider allowlist、原始檔保存、series 觀測期對應及有效期仍需研究固定。
既有 analysis.evidence_allowed 仍為舊版欄位展示，不可當作生產時間驗證；
生產資料管線須接入新 archive context，質化新聞仍維持研究門檻。

## 2026-09-19：Codex 分析接入與非同步 Paper 服務

使用者確認分析來源希望採用 Codex。已查證官方非互動 CLI 文件與本機 help，
並在一般使用者環境確認 ChatGPT 登入；未讀取或搬移 auth／券商憑證。
新增 `codex_analysis.py` 與 `codex-check`，完成一次實際合成 NO_TRADE 請求：
11065 input tokens、35 output tokens，費用未知。模型版本尚未固定，未呼叫券商或下單。

新增 `service.py`，持倉管理不等待分析；分析請求單工、只保留最新待處理行情，
過期／故障前分析不得重新打開新倉。`CodexFrameAnalysis` 將證據、價格候選、
Codex 結構決策、呼叫上限與 Engine 的 Signal 介面串接，仍限定 PaperBroker。

新增服務與 Codex 介面測試：分析卡住仍停損、取消回收、遲到結果失效、錯誤遮蔽、
候選價格不可修改、呼叫上限、缺證據不呼叫、token 不偽裝美元費用、
子程序參數／環境變數過濾、逾時終止，以及模擬模型經 bridge／service 到 Paper 成交。
詳見 docs/codex-analysis-setup.md；供應商資料、全域配額、固定模型及真實交易驗證仍未完成。

本輪最終驗證：183 項 unittest 全部通過；compileall 與 git diff --check 通過。
真實 Codex 呼叫與模擬模型的 Paper 串接為兩項不同驗證，未宣稱已完成真實行情端到端交易。

## 2026-09-19：即時報價接收與行情中斷處理

目前實作依據 `.scratch/autonomous-gold-cfd-trading/spec.md` 的下列位置：

| 規格位置 | 本輪工作 | 狀態 |
| --- | --- | --- |
| §6 Market Quality and timing，第 129 行 | GOLD WebSocket、券商時間戳、兩秒新鮮度及順序驗證 | 程式測試通過；開市報價待驗證 |
| §10 Data and auditability，第 219 行 | JSONL 保存接收時間、券商時間、bid/ask、固定事件碼 | 診斷紀錄完成；完整決策證據鏈未完成 |
| §11 Technology and security constraints，第 236 行 | 非同步訂閱、Demo session、固定主機、拒絕重新導向 | 接收器完成；常駐部署及程序權限隔離未完成 |
| §12 Failure behavior，第 249 行 | 無行情逾時、清除進場訊號、取消分析工作 | 測試通過；券商恢復協調器未完成 |

### 架構及關鍵程式

- `AsyncCapitalDemo.quotes()` 在 Gateway 內使用 Demo session 訂閱，分析介面只接收 Quote。
- `streaming.py` 的 `parse_quote()` 保留券商毫秒時間戳並轉為 UTC，拒絕過期、未來、重複／倒序、非法價格及錯誤商品；接收時間不會取代券商時間。
- `stream_probe.py`／`demo-stream` 提供有期限的唯讀測試，JSONL 排他建立防止覆寫，僅保存允許的行情欄位與固定錯誤碼。
- `PaperService.run()` 等待下一個 MarketFrame 最多兩秒。行情永久等待時，會記錄 FEED_SILENT、使進場訊號失效、取消分析並停止服務；不虛構平倉成交。
- 新增 demo 依賴 `websockets==15.0.1`。使用 [Capital.com 官方 WebSocket 介面](https://open-api.capital.com/) 與 Demo REST 登入取得的 session，固定主機、停用代理並拒絕重新導向。

### 驗證

真實執行 `demo-stream --seconds 15 --output runtime/stream-probe-20260919-1.jsonl`：
subscribed=true，但兩秒內無報價，回傳 STREAM_QUOTE_TIMEOUT、quote_count=0、entries_enabled=false。
沒有下單。這證明訂閱通道可用，尚未證明開市報價與自動交易可用。

新增 9 項測試涵蓋訂閱、拒絕、無行情、錯誤遮蔽、非法價格／商品、過期／未來／重複報價、
非法訊息、拒絕重新導向及 Paper 行情中斷取消分析。
完整 192 項 unittest、compileall、git diff --check 通過。

下一步是開市報價驗證、H1/M5/M1 時間語義與品質接線，以及帳戶槓桿／每點价值核對。
尚未完成 §8 券商部分平倉及修改停損、§9 正式訂單管理、§13 策略驗收。
斷線後不自行重連或解除進場鎖；正式恢復流程仍需實作。

## 2026-09-20：以 ETHUSD Demo 驗證共用交易功能

依使用者授權，以以太幣驗證黃金策略以外的子任務，對應規格 §6、§8–10 及部分 §12。
詳細計畫為 `.scratch/eth-demo-validation/spec.md`，實測證據摘要見
[ETHUSD Demo 驗證](research/eth-demo-validation-20260920.md)。

完成實際 BUY 0.002、收緊保證停損、SELL 0.001 淨額減倉及 DELETE 剩餘部位。
每步核對确认與持倉；結束持倉／掛單均 0，再登入核對為 SNAPSHOT_MATCHED。
減倉的 affectedDeals 為空，但持倉由 0.002 降至 0.001，已將此真實回應模式加入回歸測試。

為定位串流故障，按 diagnosing-bugs 的取樣流程比對本機／券商時間及報價時間戳，
確認本機落後造成合法報價被視為未來報價。新增 BrokerClock 三次取樣及誤差界線後，
重新執行原診斷命令，在 15 秒收到 57 筆有效行情；不更改 OS 時間、不放寬兩秒門檻。

程式新增 `clock_sync.py`、獨立 `eth_experiment.py`，唯讀串流擴充 ETHUSD allowlist。
正式 GOLD 進場商品限制仍不變；實驗需明確旗標，固定微量、空帳戶前置條件，
送單意圖先落盤，寫入不重試，最後僅清理本次部位。

新增 7 項測試，完整 199 項 unittest 通過，compileall 與 diff --check 通過。
未宣稱驗證黃金策略、黃金契約、停損實際觸發、部分平倉成本總帳或持倉中斷線恢復。
本輪不是持續自動交易，亦不累計 GOLD 的 30 日驗收；修改尚未推送 GitHub。

## 2026-09-20：登入頻率與日終對帳需求修訂

使用者要求取消逐筆交易後重新登入，改在每日交易限制到達後重新登入對帳。
檢查目前程式後確認：ETH 實驗只在啟動時登入一次，逐筆確認／持倉查詢均沿用 session；
上次全平後的新登入是另外執行的單次驗證命令，不是自動交易迴圈中的既有行為。

已將正常交易沿用 session、逐筆即時核對、日限制後新 session 對帳的規則寫入規格 §10。
使用者隨後明確確認為 10% 已實現日虧損硬熔斷；3% 軟熔斷停止新進場，但不觸發例行重新登入。
認證失效／程序重啟等必要恢復不受日終登入時機限制；換 session 不應先於緊急減風險。
本輪只更新文件，尚未實作日終協調器；無交易、未切换分析供應商。

本機編輯器檢查：VS Code 列出 Python、Pylance、debugpy、Python Environments、PowerShell
及 rainbow-csv，未見 Gemini／Code Assist 外掛；目前 PATH 亦未找到 gemini 指令。
另在 AppData/Local/Programs/antigravity 找到 Antigravity.exe，可能是使用者所指的 Gemini 工具。
此檢查只證明程式存在，未驗證它目前選用的模型、登入或可用額度，未讀取認證檔。
目前模型 adapter 為 Codex CLI；新增 Skill 不能自動把該 adapter 改成 Gemini。
