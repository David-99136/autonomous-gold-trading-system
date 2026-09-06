# 真實行情 CSV 重播

```powershell
.\.venv\Scripts\python.exe -m gold_system replay-csv --csv <行情.csv> --contract <規格.json> --output runtime/csv-run-001
```

CSV 必備欄位：

```text
timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,boundary,tradeable,liquid_window,spread_limit,extreme_volatility
```

- `timestamp`：一分鐘 K 棒收盤時間，ISO 8601，必須含 UTC offset 並對齊整分鐘。
- `boundary`：當日融資結算或提前休市兩者中較早的風險退出邊界，含 UTC offset。
- `tradeable`、`liquid_window`、`extreme_volatility`：只能是 `true` 或 `false`。
- `spread_limit`：當時已知的可接受價差；不能由未來 20 日資料反推。
- 缺棒不插值；重複或倒序時間與不合理價格拒絕整份輸入。

商品 JSON 的欄位為 `epic`、`value_per_point`、`minimum`、`increment`、`maximum`、`margin_rate`、`minimum_stop`。
金額應使用字串保存十進位精度。必須依實際 Demo 規格填入，沒有預填 Gold 真實交易條件。

所有訊號只用上根已完成 K 棒，下根開盤再進場。5m 與 1H 聚合要求完整且對齊 UTC 邊界。
持倉先重播不利極值，之後才重播有利極值；這是保守 OHLC 假設，不代表真實 tick 路徑。
結果保存輸入與商品檔案 SHA-256、事件、權益曲線、完整交易筆數、PF 與最大回撤。

目前 AI/新聞未接線，僅測試技術結構；趨勢低波動豁免尚未校準，因此 CSV 路徑預設不啟用。
所有結果固定 `qualified: false`，不可作為實盤解鎖證據。
