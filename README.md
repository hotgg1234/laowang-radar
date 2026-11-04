# 老王選股雷達

依王倚隆（老王）常用的技術面與籌碼面規則，每天收盤後自動掃描上市櫃股票、評分排序的網站。

- 每個交易日台灣時間 18:20 自動執行
- 資料來源：臺灣證券交易所、證券櫃檯買賣中心的公開收盤行情與三大法人買賣超
- 網站架在你自己的 GitHub Pages，免費、不用租主機

## 安裝（約 10 分鐘，只需要一個 GitHub 帳號）

1. 登入 GitHub，按右上角「+」→「New repository」，名稱例如 `laowang-radar`，選 **Public**，建立。
2. 在新的 repository 頁面按「uploading an existing file」，把這個資料夾裡的所有檔案拖進去（包含 `.github` 資料夾），按「Commit changes」。
   - 如果拖曳時 `.github` 資料夾沒有被帶上去，可以按「Add file」→「Create new file」，檔名輸入 `.github/workflows/daily.yml`，再把 `daily.yml` 的內容貼上。
3. 到「Settings」→「Pages」，在「Build and deployment」的 Source 選 **GitHub Actions**。
4. 到「Actions」分頁，如果看到提示，按「I understand my workflows, go ahead and enable them」。
5. 左邊點「每日更新台股資料」→ 右邊「Run workflow」→「Run workflow」，手動跑第一次。
   - 第一次會往回補抓約 130 個交易日的資料，大約需要 40～60 分鐘（為了不被證交所封鎖，每次請求間隔 4 秒）。
   - 之後每天只抓當天，幾分鐘就完成。
6. 跑完後，網址會是 `https://你的帳號.github.io/laowang-radar/`（也會顯示在 Settings → Pages）。

## 檔案說明

| 路徑 | 用途 |
|---|---|
| `index.html` | 網站本體 |
| `scripts/update.py` | 抓資料、計算訊號、產生網站 |
| `.github/workflows/daily.yml` | 每日排程 |
| `data/raw/` | 每個交易日的原始資料（自動產生，保留最近 160 個交易日） |

## 修改選股規則

規則同時寫在兩個地方，改的時候兩邊要一致：

- `scripts/update.py` 的 `analyze()`：決定表格裡的訊號與分數
- `index.html` 的 `analyze()`：決定個股詳細頁的圖與檢查清單

網頁左邊的勾選框只改變加減分，不需要改程式。

## 常見問題

**Actions 執行失敗，紀錄出現 HTTP 403 或「格式改變」**
證交所偶爾會擋國外雲端主機的連線，或調整 API 格式。可以先隔天再試；如果一直失敗，改在自己電腦執行：

```
pip install requests
python scripts/update.py
```

再把 `data/raw/` 推上 GitHub，Actions 會用現有資料重建網站。

**股價有除權息缺口**
資料是未還原的收盤價，除權息當天會出現跳空，可能讓均線與大量訊號短暫失真。

**60 天沒動 repository，排程停了**
GitHub 會暫停長期沒有活動的排程。每天自動提交資料通常就算活動；如果真的停了，到 Actions 分頁重新啟用即可。

## 免責聲明

條件是根據老王公開的節目、文章與網友整理歸納的近似規則，不是老王本人或浦惠投顧的官方公式。分數只是篩選工具，不構成任何買賣建議。
