# 01: 候選訊號生成與完整契約產出

Status: resolved
Type: task

依據上層 `spec.md` 實作：
1. 更新 `gold_system/analysis.py`：
   - 擴充 `analyze()` 產出包含全部 6 個 spec §4 欄位的 `Signal`。
   - 新增 `candidate_signals(h1, m5, m1, now, version=...) -> tuple[Signal, ...]`。
   - 新增 `frame_candidates(frame, version=...) -> tuple[Signal, ...]` 方便 `PaperService` / `CodexFrameAnalysis` 介接。
2. 在 `tests/test_candidate_pipeline.py` 中增加單元與整合測試。
3. 驗證所有測試通過。

## Answer

已於 `gold_system/analysis.py` 實作：
- `analyze()`：在資料不完整、結構未確認、右側突破或左側反轉等全分支中，精確填寫 `zone_id`, `confirmation_condition`, `invalidation_price`, `rule_score`, `data_completeness`。
- `candidate_signals()`：同時對 H1/M5/M1 計算 `RIGHT_CONTINUATION` 與 `LEFT_REVERSAL` 候選，驗證 R:R ≥ 1.5 後輸出不可變 `Signal` 元組。
- `frame_candidates()`：提供標準 `MarketFrame` 接口供 `CodexFrameAnalysis` 直接掛載。
- 新增 `tests/test_candidate_pipeline.py` 包含 8 項測試，涵蓋完整契約驗證與 Codex bridge 整合。
- 完整測試套件 378 項測試（含 76 項子測試）100% 通過。

