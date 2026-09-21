# 原始歷史品質防護：實作與限制

日期：2026-09-21。工作包 3；承接星期一 GOLD 原始 ask 異常，不改策略或風控數值。

## 這次改變

`HistoryQualityMonitor.observe()` 檢查可信讀取器提供的 GOLD／MINUTE 原始回應，
使用固定含端點窗口及接收時間，檢查整分 UTC、OHLC、bid/ask 交叉、缺欄、
重複／亂序／缺口與窗口覆蓋。單次最多 1000 根、窗口最多 999 分鐘。
缺頭／缺尾亦拒絕，不自行猜測休市或補價格。這是必要行情完整性，不是宏觀
選用證據的 40% 缺漏權重規則。

| 區域 | 責任 |
|---|---|
| `history_quality.py` | 原始資料檢查、首次價格指紋、修訂偵測、原子稽核與持久化鎖 |
| `Store.entries_stopped()` | 同時尊重操作者鎖、每日風控鎖與 `market_data_blocked` |
| Paper Engine／Demo EntryGuard | 既有新倉檢查讀取上述鎖；此次以離線測試驗證 |
| 未完成的正式行情 adapter | 未來須每次呼叫觀測器並處理例外；本輪没有網路接線 |

## 關鍵程式解析

`_row()` 只把原始標記解析為 UTC，不加一分鐘、不生成 `Bar` 或 `ClosedMinute`。
兩側 OHLC 以有界 Decimal 驗證及正規化；等價的 `100`／`100.00` 不誤判修訂，
量能或額外欄位變動亦不當作價格修訂。指紋不受全域 Decimal precision 影響。

每個 raw timestamp 首次見到的價格摘要與接收時間保留於 `history_price_first`。
它表示「首次觀測」，即使交叉也可能記錄，不表示通過或可交易；以後不覆寫，
因此能識別從壞快照變成好快照的修訂。`revised_rows` 是相對首次觀測的差異，
不是新增修訂事件數；同一後值再讀一次仍可能報告同樣的差異。

`BEGIN IMMEDIATE` 將價格指紋、觀測摘要、事件與新倉鎖放進同一交易。
寫入失敗全部回滾並拋例外，呼叫端必須停止資料處理；不得吞例外當作通過，
也不宣稱磁碟故障時仍能成功寫入一把鎖。回應中的秘密字串與任意額外欄位
不進稽核庫；保存的是白名單摘要，不是完整來源證明或原始 response 雜湊。

正常資料只會回傳 `OBSERVED_UNVERIFIED`，`entries_enabled` 與 `closure_verified`
永遠 false。異常寫入 `market_data_blocked=true`；正常後值、物件重建與程序重啟
都不會清除。沒有提供解鎖方法；不可藉換資料庫繞過恢復核對。
Paper 的新倉被阻擋，但既有部位的停損管理仍執行；沒有真實券商下單測試。

## 真實快照的離線重播

重播 `runtime/gold-monday-20260921-bars-0220/` 的四份資料：

| 快照 | 檢測結果 | 最終狀態 |
|---|---|---|
| recent-before | `HISTORY_BID_ASK_CROSSED` | RECOVERY_REQUIRED |
| recent-after | 相對首次觀測 3 根 `HISTORY_PRICE_REVISION` | RECOVERY_REQUIRED |
| fixed-repeat-0、1 | 同樣 3 根與首次快照不同；兩次後值彼此相同 | RECOVERY_REQUIRED |
| 關閉並重開診斷庫 | 持久化新倉鎖仍有效 | 保持封鎖 |

前两份原始查詢未指定 `to`；本輪重播明確以各回應的最後標記限制分析窗口，
不能把這當作當時已發出固定終點請求的證據。後兩份原始查詢確為固定窗口。
重播只建立 `runtime/gold-history-quality-replay-20260921.db`，
沒有改動既有交易資料庫。原始檔、診斷 DB 與憑證不推送。

新增 23 項測試：20 項品質觀測、2 項 Paper 新倉／停損、1 項 Demo EntryGuard。
完整 306 項測試、compileall 及 diff 檢查通過。

## 尚未完成，不能直接啟用交易

這是可供 adapter 呼叫的隔離核心，**正式行情讀取器尚未接入**；只有共用同一 Store
的交易 Guard 才會看到其鎖。沒有自動輪詢、回補、finality 認證或人工恢復控制面，
目前也不自動裁掉未收棒尾列；如輸入形成中資料而產生修訂，會保守封鎖。
收到較正常資料不證明券商暫時異常的根因已修復。

下一步需先明確訂定「何時可接受已收棒資料／如何恢復」的規格，再接正式 feed，
不能只加固定延遲、默默改為使用 bid 或刪除異常 ask。
本輪沒有券商／模型呼叫、沒有訂單、沒有 GitHub 推送。
