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
- 供需區與左右側訊號已有可重播原型；實際參數校準、最多一次加碼與反向切換仍待完成。
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
憑證不應直接交付代理；本次搭建沒有讀取任何已儲存憑證或建立券商 session。

## 本輪追加

加入 `gold_system/replay.py`，將完整 K 棒、左右側結構分析與交易引擎串接。
輸出逐時點權益與完整交易統計；不將半倉停利拆成兩筆獨立策略交易。
資料格式請見 [CSV 操作說明](csv-replay.md)。
專案 `.venv` 已建立，`keyring` 與 Windows 後端載入成功，未讀寫任何憑證。

## Demo 登入 401 診斷改善

使用者回報 `/session` HTTP 401。原錯誤處理隱藏全部回應，無法區分拒絕原因。
目前僅顯示格式受限且不含本次憑證的 `errorCode`；非 JSON 或不合格式的回應顯示 `UNAVAILABLE`。
已以假 HTTP 回應重現並測試遮罩行為，未讀取使用者憑證或執行真實登入。
帳戶 401 根因仍須使用者重跑 `demo-discover` 取得安全錯誤代碼才能判斷，不能視為已修復認證。
