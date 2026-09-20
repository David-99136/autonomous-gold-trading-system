# 帳務核對與每日熔斷協調核心

日期：2026-09-20；對應剩餘工作包 1、2（主規格 §7、10、12、14）。
**本機核心完成，不代表真實券商帳務、10% 自動減風險或每日排程已上線。**

## 執行關係

```text
可信 Gateway 已核對平倉分段 + 費用證據 + 權威期間成交清單／淨額
  → AccountingLedger：精確清單／金額核對，receipt 綁帳本摘要
  → DailyControl.observe：只讀有效 Demo receipt，日初權益不變
      未核對／資料過期 → 封鎖新進場（持倉管理仍可執行）
      已實現損失 3%   → 當日軟鎖，不重新登入
      已實現損失 10%  → 持久化硬鎖
        → 減風險、核對空倉／零掛單
        → Gateway 共用的 session 邊界內重新認證
        → 同帳戶新快照對帳
        → COMPLETE_HARD_LOCKED（仍不解鎖）
```

## 已完成的程式與關鍵行為

- `accounting.py`：`record_fee` 補登不含於平倉 profit 的額外費用證據。
  缺費用維持未知；明確 0 必須有證據，不把價差／滑價再扣一次。
- `reconcile_period`：模式、帳戶、期間、成交 hash 清單及 trade-only 淨額精確吻合才存 receipt。
  入金、出金、浮盈虧及 AI 成本不能冒充已實現交易盈虧。空帳本必須有權威零交易證據。
- `verified_period`：在一致的 SQLite 快照內重算摘要。晚到成交／費用補證使舊 receipt 失效。
  receipt 的來源指紋不是數位簽章；來源真實性與完整性仍由可信上游負責。
- `daily_control.py`：3%／10% 固定依已實現淨額及日初權益判斷，不接受浮虧欄位。
  截止時間須在兩秒內、不得倒退。日初權益不可因另一份報表調整；軟鎖保持到恢復流程，
  硬鎖跨日保持。不提供自動解除日鎖的方法，跨日正常恢復仍待控制面接線。
- `reconcile_hard_limit`：資料庫 compare-and-set 只讓一個 worker 取得一次性流程。
  中途重啟的 REDUCING／REAUTHENTICATING／RECONCILING 不重跑；必須由恢復器查證。
  取消、例外、錯帳戶、非零持倉／掛單或不新鮮快照進入 RECOVERY_REQUIRED。
  整體等待上限 30 秒，各回呼另有上限；這是失敗控制界線，不是真實延遲保證。
- `Store.entries_stopped()` 同時讀操作者停止鍵與 `daily_new_entries_blocked`。
  因此既有 Engine／DemoEntryGuard 會尊重日鎖，但不代表它們已自動餵入 DailyControl。
- `capital_settlement.py` 帳戶指紋改為 SHA-256(raw accountId)，與既有對帳／history probe 一致。
  本專案尚無使用舊格式的真實入帳資料；若外部已有舊帳本，須以可核對來源重新映射，
  不能混用兩套指紋或直接改 hash。

## 日報接線

`daily-report` 可加 `--account-hash <64位SHA-256>`，唯讀顯示該帳戶及期間的
`accounting.status`、已核對淨額、額外券商費用與分段數。
沒有 receipt、帳本已變或 schema 缺失時只顯示 UNVERIFIED，不洩漏錯誤原文。
原有 `event_counts` 仍是資料庫級統計，沒有假裝按帳戶隔離。
原有完整營運指標維持待核對；不能把這個帳務區塊當成完整費用後營運淨利。

報表輸出固定模板，不增加模型呼叫。通知時間仍為 Asia/Taipei 22:00，尚未啟動排程。

## 正式啟用前仍需完成

1. 取得 ETH 部分平倉的精確已實現損益、費用與唯一成交識別；既有 history size=0.0 不足。
2. 確定券商風控交易日邊界、日初權益來源、當日資料涵蓋範圍。22:00 通知不是日界線。
   目前期間介面最多 24 小時，不自行推論 DST 或跨週期區間。
3. 正式 Gateway 提供已核對清單與 receipt；不可讓模型或任意 JSON 自行宣告帳務已驗證。
4. 接線減風險／重認證／對帳回呼。必須與此帳戶送單共用 session 邊界，
   檢查全部未解決意圖及正在送出的訂單，不能只看空倉快照；不得盲目重送。
5. 完成 Recovery Required、重啟及跨日恢復的人工控制面／測試；沒有這些就維持新倉封鎖。

尚未發送任何券商詢問。可向券商確認：TRADE transaction.size 的幣別、單位與精度、
如何取得每個淨額減倉的精確 realized P&L、各費用是否已包含於 confirmation.profit，
以及帳戶交易日／日初權益的官方定義。

## 驗證

新增 25 項測試，完整 258 項通過，compileall／diff 檢查通過。
涵蓋費用缺漏／補證、清單／淨額不符、模式／帳戶／區間隔離、receipt 失效、
3%／10% 門檻、軟鎖保持、跨日硬鎖、並行 worker、重啟、取消、登入失敗與日報接線。
所有新增成交與 Gateway 回呼均為合成測試，沒有券商呼叫或下單。
