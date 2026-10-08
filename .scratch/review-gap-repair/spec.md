# 710ba8c 審查缺口修補

[Author: Codex | Date: 2026-09-27]

Status: resolved
Type: task

使用者已要求「補齊缺口」。範圍限 2869f73 → 710ba8c 審查發現，
不連線券商、不解除交易鎖、不更改風控額度、不推送 GitHub。

## 驗收

1. ETH Demo 恢復平倉：送出前留下持久化意圖與新倉鎖；未知結果不重送，
   只有確認接受、指定持倉已不存在且掛單／持倉快照有效時才回報 CLOSED。
2. 候選 ID 包含完整候選與歷史輸入；相同輸入可重播，風險參數／證據改變須改 ID。
3. 新風險拒絕不完整或已失效訊號，Journal 保留契約欄位；保護性退出不受新增門檻阻擋。
4. 採樣校時不得優先占用已到期的價格查詢；維持時間估計有效期、限流及授權截止。
5. 補上 LEFT、缺棒、未收盤與 Codex 選訊號 → Paper Trading Engine 整合測試。

## 測試邊界

- 恢復平倉入口及持久化紀錄（券商 HTTP 模擬）。
- candidate_signals / frame_candidates / make_plan / DemoEntryGuard。
- capture 的假時鐘／HTTP 模擬排程。
- CodexFrameAnalysis → Engine → PaperBroker（非券商交易）。

完整 unittest、git diff --check；離線測試不代表策略獲利、Demo 資格或 Live 就緒。

## Answer

使用者已確認上述四條測試邊界。六項獨立缺口已修補，401 項 unittest 通過。
實作、相容性限制與重跑方式見 [修補報告](../../docs/review-gap-repair-20260927.md)。
