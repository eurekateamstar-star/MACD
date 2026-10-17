# 我的台股學習站

每個交易日收盤後，GitHub 會自動抓台股資料、計算指標，產生可以用手機看的互動報告。

> 僅供學習參考，不構成投資建議。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `stocks.txt` | 要追蹤的股票清單，**想換股票就改這個** |
| `tw_stock_report.py` | 抓資料、計算指標、產生報告的 Python 程式 |
| `requirements.txt` | 程式需要的套件 |
| `.github/workflows/update.yml` | 告訴 GitHub 什麼時候自動執行 |

## 怎麼新增或刪除股票

1. 在 GitHub 網頁上點 `stocks.txt`
2. 點右上角的鉛筆圖示（Edit）
3. 一行一檔，格式是 `代號 名稱`，例如 `2603 長榮`
4. 按「Commit changes」存檔，約 1～2 分鐘後網站就會更新

## 手動更新

到 **Actions** 分頁 → 左邊點「更新台股報告」→ 右邊按 **Run workflow**。

## 報告內容

- 白話分析：趨勢、股價位置、RSI、KD、成交量、風險
- K 線圖 + 週線 / 月線 / 季線 + 成交量
- RSI 與 KD 圖
- 小回測：均線策略 vs 買進持有
- 名詞小教室

## 在自己電腦上執行（選用）

```
pip install -r requirements.txt
python tw_stock_report.py
```

報告會產生在 `site/` 資料夾，並自動用瀏覽器打開首頁。
