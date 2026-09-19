# Capital.com 歷史 K 線：時間標記與收棒語義

查證：2026-09-07（Asia/Taipei）。僅閱讀官方公開文件，未登入帳戶、讀取憑證或執行交易。遵循 ADR 0003 的券商原生 bid/ask 驗證原則。

## 已證實與未知

官方 `GET /api/v1/prices/{epic}` 支援 MINUTE、MINUTE_5、HOUR；`from` / `to` 以 `snapshotTimeUTC` 過濾，單次最多 1,000 筆。回應範例的 openPrice、highPrice、lowPrice、closePrice 都分別提供 bid 與 ask。這是一般商品範例，不是 GOLD 實測。[官方 Historical prices 文件](https://open-api.capital.com/)

該章節**沒有明確定義** `snapshotTimeUTC` 是開棒或收棒時間，也未保證尾棒已收完、歷史永不修訂，或 `from` / `to` 邊界是否包含等號。不能因為名稱含 snapshot、時間整分或範例相隔一分鐘，就判定已收棒。[官方 Historical prices 文件](https://open-api.capital.com/)

WebSocket OHLC 文件稱其提供 K 線更新，範例含 `t`、`priceType: bid` 與 OHLC，未顯示 final/closed 旗標或定義 `t` 的區間端點；不能將收到事件等同收棒，也不能由範例保證 ask 串流。[官方 OHLC market data 文件](https://open-api.capital.com/)

## 對匯入器的工程約束（本專案設計，不是券商承諾）

- 預設 `timestamp_semantics=UNVERIFIED`；原始下載可保存，不能因此輸出已合格回測或允許交易。
- 明確記錄來源時間、接收時間、解析度、原始回應雜湊、驗證證據及其適用環境；不要直接把原始時間改名為 close time。
- 待證實開棒標記時，以「原始時間 + 解析度」計算 nominal close；待證實收棒標記時才直接使用。完整性另行判斷，不與時間轉換混為一談。
- 收棒後安全延遲、重讀一致性與尾棒排除是防護，不是券商 finality 證明。僅刪除最後一列不足以處理資料延遲、缺棒或多根未完成棒。
- bid/ask 必須分別保存及驗證；缺失不得由固定價差、mid 或前值補造。歷史後來取得的完成棒不代表當時已可用，point-in-time 可用性需另有證據。

## 安全驗證計畫（尚未執行）

1. 使用者另行批准 Demo 唯讀連線後，在正常活躍交易時段，使用已驗證 GOLD epic；記錄 UTC 時鐘誤差及每次請求前後時間，不保存認證 headers。
2. 同時收集原生報價與 classic OHLC 更新；在數個 1m、5m、1h 邊界前後取得相同歷史範圍。為每根候選棒記錄首次出現及後續修訂。遵守帳戶共用限流，不另啟獨立高頻輪詢。
3. 將可完整觀測區間的報價 OHLC 與 REST 比對，測試標記對應 `[t,t+Δ)` 或前一區間的假說。若串流有缺口、延遲或聚合差異，保留不確定性，不硬選較吻合者。
4. 對已完成範圍稍後再次讀取，另外測試相鄰查詢的等號邊界、重複與缺漏。單次不再變動並不能證明永久不可修訂。
5. 將觀測結果連同環境、日期與局限保存；向 Capital.com support 詢問標記端點、當前棒是否包含、最終化與修訂規則。未取得足夠一致證據前，維持未驗證／禁止以此解鎖交易。

本次可得结論是「公開文件不足以證明 GOLD 開／收棒語義」，不是「官方保證為開棒」或「尾棒一定未完成」。
