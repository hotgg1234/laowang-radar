# 台股選股雷達

每天收盤後自動掃描上市櫃股票與 ETF，依「基本面 × 技術面 × 歷史回測」三項加權評分，給出 BUY／WATCH／SKIP 與停損、停利參考價。另有策略回測頁，可以調整參數看單檔或多檔的歷史表現。

策略規則移植自開源專案 [kevin801221/stock-strategies-only](https://github.com/kevin801221/stock-strategies-only)（台股每日選股機器人 V3.3 的參數化策略）。

- 每個交易日台灣時間 18:20 自動執行
- 資料來源：臺灣證券交易所、證券櫃檯買賣中心的公開收盤行情、三大法人買賣超、本益比與加權指數
- 網站架在你自己的 GitHub Pages，免費、不用租主機，也不需要任何 API 金鑰

## 評分方式

| 項目 | 分數 | 怎麼算 |
|---|---|---|
| 基本面 | 40 或 100 | 近四季 EPS 與 ROE 都高於門檻得 100 分。EPS = 收盤價 ÷ 本益比，ROE ≈ 股價淨值比 ÷ 本益比。ETF 不看基本面。 |
| 技術面 | 0～100 | 均線多頭、布林下軌反彈、KD 黃金交叉、MACD 多頭四訊號平分 100 分，再加減量價型態分數（倍量 +10、梯量 +8、縮量 +5、低量 +8、放量滯漲 −20）。 |
| 回測 | 0～100 | 過去每一天技術分達門檻就假設隔天開盤買進，持有 N 天內碰停利算贏、碰停損算輸，否則看第 N 天收盤。分數 = 勝率 × 100，沒樣本給 50。 |

總分與技術分過門檻、且基本面通過才是 BUY；總分 ≥ 50 是 WATCH；其他是 SKIP。加權指數跌破均線時，BUY 一律降為 WATCH（大盤濾鏡）。

### 與原專案的差異

- 原專案用 FinMind 的「最近兩年年度 EPS、ROE」，這裡改用證交所、櫃買中心每日公布的本益比換算「近四季」數值，不需要註冊 API。
- 原專案回測 3～5 年價格；這裡用網站保存的交易日（最多約 2 年，第一次安裝會慢慢補齊）。
- 原專案只處理自選清單；這裡掃全部上市櫃股票與 ETF。ETF 沒有 EPS、ROE，基本面權重改由技術與回測分攤。
- 夜盤盤前快報、Telegram、Google Sheet、AI 生策略沒有移植。

## 策略檔

策略放在 `strategies/`，一個策略一個 JSON 檔，欄位與原專案相同（說明見原專案的 `strategies/SCHEMA.md`）。新增或修改後，網站會自動列在策略選單裡。內建三個：

| 檔案 | 策略 |
|---|---|
| `default.json` | 預設策略：EPS>5、ROE>15，權重 0.3／0.3／0.4，停損 −8%、停利 +10%，持有 20 日 |
| `conservative.json` | 保守型：EPS>8、ROE>20，總分 ≥75 才 BUY，停損 −5%、停利 +8%，持有 15 日 |
| `value-hold.json` | 長期價值存股：只看均線與 MACD，持有 90 日、停利 +15% |

網頁左邊可以即時調整基本面門檻、權重、BUY 門檻與大盤濾鏡（存在瀏覽器裡）。技術訊號開關、持有天數、停利停損會影響歷史勝率，要改就改策略檔，或到回測頁試。

## 安裝（約 10 分鐘，只需要一個 GitHub 帳號）

1. 登入 GitHub，按右上角「+」→「New repository」，名稱例如 `laowang-radar`，選 **Public**，建立。
2. 在新的 repository 頁面按「uploading an existing file」，把這個資料夾裡的所有檔案拖進去（包含 `.github` 資料夾），按「Commit changes」。
3. 到「Settings」→「Pages」，在「Build and deployment」的 Source 選 **GitHub Actions**。
4. 到「Actions」分頁，如果看到提示，按「I understand my workflows, go ahead and enable them」。
5. 左邊點「每日更新台股資料」→ 右邊「Run workflow」，手動跑第一次。第一次會往回補抓資料，需要較久；之後每天幾分鐘。
6. 跑完後，網址會是 `https://你的帳號.github.io/laowang-radar/`。

## 檔案說明

| 路徑 | 用途 |
|---|---|
| `index.html` | 選股雷達 |
| `backtest.html` | 策略回測 |
| `engine.js` | 網頁用的策略引擎 |
| `scripts/strategy.py` | 抓資料程式用的策略引擎（與 `engine.js` 同一套規則，修改時兩邊要一起改） |
| `scripts/update.py` | 抓資料、計算評分、產生網站 |
| `strategies/` | 策略參數 |
| `.github/workflows/daily.yml` | 每日排程 |
| `.github/workflows/rebuild.yml` | 只重建網站（不抓資料，約 1 分鐘） |
| `data/raw/` | 每個交易日的原始資料（自動產生，保留最近約 2 年） |
| `data/fund.json` | 最新的本益比、股價淨值比、殖利率 |
| `data/taiex.json` | 加權指數每日收盤 |

## 常見問題

**Actions 執行失敗，紀錄出現 HTTP 403 或「格式改變」**
證交所偶爾會擋國外雲端主機的連線，或調整 API 格式。可以先隔天再試；如果一直失敗，改在自己電腦執行：

```
pip install requests
python scripts/update.py
```

再把 `data/` 推上 GitHub，Actions 會用現有資料重建網站。

**基本面都顯示「—」**
本益比資料抓取失敗時會沿用上次的資料；從來沒抓到過就沒有基本面，所有股票基本面都算未通過。看 Actions 紀錄裡「本益比資料」那一行。

**股價有除權息缺口**
資料是未還原的收盤價，除權息當天會出現跳空，可能讓均線、布林通道與回測短暫失真。

**60 天沒動 repository，排程停了**
GitHub 會暫停長期沒有活動的排程。每天自動提交資料通常就算活動；如果真的停了，到 Actions 分頁重新啟用即可。

## 免責聲明

分數只是篩選工具，回測只代表過去，不構成任何買賣建議。
