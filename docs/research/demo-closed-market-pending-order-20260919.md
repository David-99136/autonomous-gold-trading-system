# Demo 休市掛單測試：2026-09-19

## 結果

台灣時間 08:53，實際連接固定的 Capital.com Demo API。GOLD 回報 CLOSED，測試前持倉與掛單均為 0。送出一筆 BUY LIMIT，券商確認 ACCEPTED / OPEN，且掛單清單出現相同 dealId；隨即撤單，確認 ACCEPTED / DELETED。最後掛單 0、持倉 0。沒有成交，也沒有操作 Live。

| 測試欄位 | 值 |
| --- | --- |
| 數量 | 0.01（當時最小數量） |
| 限價 | 4277.85 USD |
| 停損 | 4272.85 USD，非保證停損 |
| 停利 | 4287.85 USD |
| 有效期限 | 2026-09-19 01:03:34 UTC |

價格只用於遠離休市參考價的功能測試，不是分析代理產生的交易建議。

## 方法與邊界

一次性診斷 Order Gateway 腳本位於 `runtime/demo-preflight-20260919/pending_probe.py`，審計紀錄為同目錄 `pending-probe.jsonl`，均留在本機 runtime，不納入 GitHub。腳本先確認週六休市、帳戶無部位及掛單、固定 Demo URL 與 GOLD 最小數量，再將送單意圖寫入並同步磁碟。專屬紀錄檔以排他方式建立，防止重複執行；寫入請求不自動重試。正常策略進場路徑及 Live 鎖未修改。

僅證明此帳戶在此次休市時段能建立和取消這種掛單；未證明 STOP 掛單、到期自動撤單、開市成交、停損執行或自動交易策略有效。程式通過語法編譯，實際 POST、確認、GET 清單、DELETE、最終核對流程均完成；不代表正式掛單 Gateway 已具備完整故障恢復測試。

另觀察到本筆掛單回傳 leverage=100，與商品 metadata 的 marginFactor=5% 不一致。正式交易前須核對帳戶適用槓桿，不可只用商品 metadata 計算保證金。

官方介面依據：[Capital.com Public API](https://open-api.capital.com/) 的 Create working order、Delete working order 與 confirms；休市可掛單的結論來自本次 Demo 實测，而非文件保證。
