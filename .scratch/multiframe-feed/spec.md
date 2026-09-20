# 已收棒 M1 → M5/H1 行情框架

Status: resolved
Type: task

承接工作包 3，不變更策略。輸入為可信 adapter 已證實收盤時間／finality 的
Capital Demo GOLD bid/ask M1 棒，附收到時間與證據指紋。原始 REST snapshotTimeUTC
尚未證實端點，不可直接轉換成此輸入。

依決策時點篩選可用資料，逐邊聚合 M5/H1。只產生 UTC 邊界對齊且完全連續的棒；
缺漏不插值、不以 mid/固定價差補 ask。晚到資料只影響接收後決策。
重複相同棒不改首次收到時間，衝突版本鎖定 buffer，待明確恢復。
保留有限歷史，輸出資料新鮮度／缺口理由並接 PaperService.MarketFrame。
來源／finality 未證實、串流回補、交易時段等門檻不由此模組解鎖。

## Answer

`gold_system/timeframes.py` 完成此有界資料層；新增 14 項測試，完整 272 項通過。
可信原始資料 adapter、真實時間端點／收棒證據與自動回補網路接線尚未完成。
星期一開市驗證不因本模組完成而省略。
