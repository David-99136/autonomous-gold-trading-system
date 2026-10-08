# 訊號候選管線與完整契約產出規格 (Signal Candidate Pipeline Spec)

Status: resolved
Type: task

日期：2026-09-27。工作包 4（分析代理與策略）核心接線。
承接 `Signal` 契約欄位擴充，建立確定性候選訊號生成器。

## 1. 目標

1. 將 `gold_system/analysis.py` 的 `analyze()` 升級，完整填入 spec §4 規範的 6 個契約欄位：
   `zone_id`, `confirmation_condition`, `invalidation_price`, `rule_score`, `confidence`, `data_completeness`。
2. 實作 `candidate_signals(h1, m5, m1, now, version=...)` 與 `frame_candidates(frame, version=...)`：
   - 確定性輸出 `LEFT_REVERSAL` 與 `RIGHT_CONTINUATION` 候選訊號元組。
   - 每個候選訊號具有唯一 `signal_id`、完整欄位與結構停損/目標 (R:R ≥ 1.5)。
   - 可直接作為 `CodexFrameAnalysis(..., candidates=frame_candidates, ...)` 的標準生產介面。
3. 嚴格遵守 fail-closed 不變條件：
   - K 棒未收盤、缺棒或不連續時，不生成候選 (`()`)。
   - 資料不完整時 `analyze()` 標記 `data_completeness="INSUFFICIENT"`。
   - 不修改風控參數，不產生浮點數。

## 2. 測試與驗收條件

- 單元測試：`analyze()` 各種情境（無資料、缺棒、趨勢延續、區域反轉）所有欄位型別與範圍正確。
- 候選測試：`frame_candidates` 在符合條件時產出正確的候選列表，不合條件時產出空元組。
- 整合測試：與 `CodexFrameAnalysis` 搭配運作，確認 Codex select 的候選可順利由 Trading Engine 執行。
- 全專案現有 370 項測試零迴歸。

## Answer

完成 `gold_system/analysis.py` 升級與 `tests/test_candidate_pipeline.py`，新增 8 項測試，全數通過（總計 378 項通過）。詳情見 `issues/01-candidate-generator.md`。

### 2026-09-27 審查後補驗收

[Codex | 2026-09-27] 原 378 項結果未涵蓋完整 Engine 接線；本次補上 LEFT、
資料完整性／未收盤邊界、ID 修訂，以及 Codex 選訊號 → Engine → PaperBroker
開倉／退出驗證。契約已接入新風險與 Journal 重驗；完整 401 項通過。
細節與限制見 [修補報告](../../docs/review-gap-repair-20260927.md)，不代表 Demo 或 Live 資格。
