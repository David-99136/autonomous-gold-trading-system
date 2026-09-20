# ETHUSD Demo 共用執行功能驗證：2026-09-20

使用者授權以以太幣先驗證黃金策略以外的子任務。本次使用 Capital.com Demo 的 ETHUSD CFD。
執行時間台灣時間約 08:09；最終新 session 核對於 08:10 完成。

| 規格／子任務 | 實際證據 | 判定 |
| --- | --- | --- |
| §6 即時行情 | 校準券商時鐘後，15 秒接收 57 筆有效 ETHUSD bid/ask | 此次路徑通過 |
| §6 時鐘檢查 | 原始報價比本機快約 0.3 秒；校準偏差加不確定度小於 1 秒 | 保留 2 秒新鮮度限制 |
| §8／§9 開倉 | BUY 0.002，成交價 2627.29，ACCEPTED 且持倉吻合 | 通過 |
| §8 停損 | 保證停損從 2605.59 修改至 2606.59，實際持倉仍為 guaranteedStop=true | 設定與收緊通過，未測觸發 |
| §8 部分平倉 | 淨額模式 SELL 0.001 後原 BUY 剩 0.001，原停損仍在 | 此次減倉通過 |
| §8 全平倉 | DELETE 本次 dealId，確認 CLOSED／FULLY_CLOSED，成交價 2625.83 | 通過 |
| §9 對帳 | 持倉 0、掛單 0；新登入再次讀到 SNAPSHOT_MATCHED | 通過，未測持倉中斷線恢復 |
| §9 延遲 | 四次寫入首次回應約 0.297、0.312、0.297、0.281 秒 | 樣本不足以驗收 p95 |
| §10 稽核 | 寫入前落盤，保留請求、確認、持倉快照及時間 | 本次實驗有證據鏈 |

最後 0.001 平倉的券商 profit 為 -0.00146 USD，只屬最後一段，不是整筆測試總損益。
部分平倉的帳務、費用及日損益歸戶尚待交易明細驗證。

## 重要介面發現

1. 減倉回覆 ACCEPTED／OPEN、affectedDeals 為空，但持倉快照顯示原部位確實減半。
   不能只用 status 或 affectedDeals 判斷減倉；需核對帳戶、商品、方向、數量及 stop。
2. 初始開倉確認沒有 stopLevel，但持倉快照包含保證停損。
3. 商品 metadata 的 marginFactor=50%，帳戶與實際部位 leverage=20。
   商品欄位不能單獨代表帳戶實際槓桿，亦不能替代 GOLD 契約核驗。
4. 首次串流為 STREAM_STALE_OR_FUTURE_QUOTE。時間及行情取樣顯示本機時鐘落後，
   新增短期 BrokerClock 後原命令成功接收行情。未修改 Windows 時鐘或券商時間戳。

## 程式與限制

`clock_sync.py` 取三次時間，選最低 RTT 樣本並保留 offset／半 RTT 不確定度。
合计超過 1000ms 即拒絕；估計以 monotonic 推進，60 秒到期。stream-probe 每 30 秒更新估計。
此估計仍受網路非對稱延遲影響。

唯讀行情 allowlist 擴為 GOLD、ETHUSD，預設 GOLD。正式 GOLD 訂單限制未放寬。
`eth_experiment.py` 為獨立、明確旗標啟動的實驗：初始 0.002、一次反向 0.001，
已有部位／掛單即拒絕，不更改模式、不重送未知寫入，只清理本次確認涉及的部位。
未知結果仍需人工對帳，不代表完整故障恢復已完成。

本地證據為 runtime/eth-stream-20260920-2.jsonl、eth-lifecycle-20260920-1.jsonl、
eth-postflight-20260920.db，均排除 Git；診斷時鐘脚本同樣只留 runtime。

未驗證：黃金策略、H1/M5/M1 收棒語義、宏觀資料、資金費、停損觸發、成本總帳、
極端行情、實盤或長期穩定性。不計入 GOLD 30 日及策略交易筆數驗收。

介面依據：[Capital.com Public API](https://open-api.capital.com/)。
淨額減倉效果為本次 Demo 實測，非官方文件對所有帳戶的保證。
