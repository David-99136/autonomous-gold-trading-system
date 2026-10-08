# 710ba8c 審查缺口修補報告

[Author: Codex | Date: 2026-09-27]

## 結果與範圍

修補使用者核准的六項獨立缺口（兩軸審查的訊號 ID 問題重複列出一次）。
完整測試由 378 增至 **401 項，全部通過**；compileall 與 diff 空白檢查通過。
本次未讀取真實憑證、未連線券商、未下單、未解除交易鎖、未推送 GitHub。

## 架構與關鍵程式

1. **候選訊號**：`analysis.candidate_signals` 將完整 Signal 與 H1/M5/M1 歷史
   以固定欄位順序序列化，再計算 SHA-256。LEFT / RIGHT 的停損、目標、進場依據
   或歷史證據改變時，都不再沿用原 ID；相同 list / tuple 輸入可重播。
   `continuous` 檢查 UTC 收盤邊界、連續性、未來與過期資料；`frame_candidates`
   保留上游 `quality.data_complete=False` 的封鎖。
2. **新風險驗證**：`risk.validate_entry_contract` 要求 COMPLETE、區域及確認條件、
   正數失效價；`make_plan` 再用可退出的 bid / ask 檢查是否已觸及失效價。
   這些檢查不加入共用退出驗證器，避免壞的新訊號阻擋已持倉的硬停損。
3. **意圖與 Gateway**：`OrderJournal.prepare` 保存六個契約欄位，納入內容雜湊；
   `DemoEntryGuard` 從紀錄恢復原值，使用當前報價重驗。舊紀錄缺欄位會拒單，
   不自動填成 COMPLETE，也不重寫使用者現有資料庫。
4. **ETH 恢復平倉**：`close_eth_demo.close` 要求既有 Demo 稽核庫，先以帳戶指紋與
   deal ID 建立不可重送的持久化意圖、保存目標及新倉鎖，再送出一次 DELETE。
   返回 reference 後立即持久化；只有同帳戶、相符確認、指定 deal 消失、其他持倉
   ID／商品未增減且掛單為空，才標示 CLOSED。UNKNOWN／取消／錯誤持續鎖定。
   摘要以排他建立與原子替換保護，Store 紀錄才是恢復依據；成功也不自動解除新倉鎖。
5. **採樣排程**：`history_sampler.capture` 先處理到期價格請求。30 秒校時到期時，
   若距下一筆查詢不超過 4 秒，先採樣再於下一個可用間隙校時。市場／價格請求
   仍強制使用不超過 60 秒的時間估計、至少 1 秒限流及原授權截止，不延時補送。
   4 秒是三次串行校時加下一次限流的基本排程預留，不是網路延遲保證；真實延遲
   若使請求錯過 1 秒容忍窗口，仍會停止。

## 驗證證據

- ID 修訂碰撞、不完整／失效訊號、Journal 欄位遺失、採樣時段競爭先重現失敗再修正。
- LEFT 多／空反轉、缺棒、錯位收盤、未來／過期棒及上游品質封鎖。
- `CodexFrameAnalysis → Engine → PaperBroker` 實際跑過離線開倉與硬停損；
  模型回覆為模擬資料，風控、引擎、PaperBroker 不 mock。
- 恢復平倉的持倉未消失、逾時、取消、確認故障、帳戶切換、錯誤 reference、
  新增掛單、壞快照、缺稽核庫、輸出衝突及落盤失敗；只使用 HTTP MockTransport。
- 採樣在第 30～35 秒附近與每 5 秒密集排程均按時送出，沒有取消有效期限制。
- 舊的正向測試 fixture 已明確補齊合成契約；未放寬任何 production 風控門檻。
- 離線 synthetic replay 有 ENTRY / EXIT / EXIT，且仍回傳 `qualified=false`。

重跑：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q gold_system tests
git diff --check
```

## 使用與限制

恢復工具新增 `--database`（既有稽核庫）；未指定時使用專案的
`runtime/demo-audit.db`，不建立替代空庫。重複執行相同帳戶／deal 的意圖會拒絕，
改輸出檔名不能繞過同一稽核庫的去重。未知結果要先只讀對帳與人工恢復，
不要刪庫、換庫或清除意圖來重送；本批不提供解除鎖定或自動重試介面。

這仍是受限 ETH 恢復工具，不是工作包 5 的通用 Demo Order Gateway。
本次完成的是程式缺口與離線驗收；券商收棒語意、正式資料來源、GOLD 策略、
受控 Demo 執行、OOS、30 日資格期與 Live 解鎖均未因此完成。
**剩餘 10 個大工作包數量不變。**
