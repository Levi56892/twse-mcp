# 台股報價 MCP 連接器

把證交所的公開報價包成一個 MCP 連接器，讓 Claude 和「台股大型股研究看板」可以直接查到最新股價。

## 提供的工具

| 工具 | 內容 | 來源 | 更新頻率 |
|---|---|---|---|
| `get_quotes` | 盤中即時報價（上市、上櫃） | mis.twse.com.tw（非官方接口） | 約數秒延遲，伺服器快取 15 秒 |
| `get_daily_close` | 最近一日盤後行情（上市） | openapi.twse.com.tw（官方） | 每個交易日盤後 |
| `get_valuation` | 本益比、殖利率、股價淨值比（上市） | openapi.twse.com.tw（官方） | 每個交易日盤後 |

## 一、先在自己電腦試跑

```bash
pip install -r requirements.txt
python server.py
```

看到伺服器啟動後，連接器網址就是 `http://localhost:8000/mcp`。
想先手動測試工具，可以用官方的 MCP Inspector：

```bash
npx @modelcontextprotocol/inspector
```

在 Inspector 裡選擇 Streamable HTTP、填入上面的網址，呼叫 `get_quotes`，參數填 `["2330","2317"]`。

## 二、部署到雲端

Claude 的網頁版連不到你電腦上的 localhost，所以要放到一台有公開 HTTPS 網址的主機。以下兩個選一個就好。

**Google Cloud Run（推薦，用量小幾乎免費）**

```bash
gcloud run deploy twse-quotes --source . --region asia-east1 --allow-unauthenticated
```

部署完成會拿到 `https://twse-quotes-xxxxx.a.run.app`，連接器網址就是後面加上 `/mcp`。

**Render**

把這個資料夾推上 GitHub，在 Render 建立一個 Web Service、選 Docker，完成後一樣是 `https://你的服務.onrender.com/mcp`。免費方案閒置會休眠，第一次開看板會慢個幾十秒。

如果公司有自己的伺服器，可以直接跑 Docker，前面再放一層 HTTPS 反向代理即可。

## 三、加進 Claude

在 Claude 的設定中找到連接器，新增一個自訂連接器，名稱填「台股報價」，網址填上面的 `https://…/mcp`。加好之後，回到原本的對話告訴我，我會把看板的股價、漲跌和本益比改成即時查詢。

## 注意事項

- **非官方接口**：`get_quotes` 用的是證交所網站背後的查詢接口，並非正式 API，格式可能變動，請求太頻繁也可能被暫時封鎖。程式已經內建 15 秒快取，請不要把它調得太低。
- **公開存取**：目前的設定是不需要登入就能呼叫。資料本身是公開的，但如果擔心被別人濫用，可以請 IT 在前面加上公司 VPN 或 OAuth 驗證。
- **SSL 錯誤**：證交所 OpenAPI 的憑證偶爾會讓 Python 驗證失敗。如果遇到這種情況，先更新系統的 CA 憑證（`pip install -U certifi`），不要直接關掉驗證。
- **上櫃股票**：`get_quotes` 已經支援上櫃。盤後的兩個工具目前只有上市資料，櫃買中心的 OpenAPI 之後可以再加。
- **市值與歷史報酬**：這兩項不在這個連接器裡。市值需要股本資料，7 日與 1 年報酬需要歷史股價，這些可以繼續從 CMoney 匯出。

## 歷史資料庫（GitHub Actions）

`collector.py` 由 GitHub Actions（`.github/workflows/collect.yml`）自動執行，把資料存在本存放空間的 **data 分支**：

| 時間 | 做什麼 |
|---|---|
| 週一到週五 21:30（台灣時間） | 抓當天全市場的股價、三大法人、融資融券、借券、外資持股，以及最新月營收、財報、集保股權分散 |
| 每 6 小時 | 從新到舊回補 2015 年以來的歷史；抓過的日子不再重抓，補完後會很快結束 |

手動執行：存放空間的 **Actions** 分頁 → 「收集台股資料」→ **Run workflow**，可選：
- `probe`：只測試各資料來源抓不抓得到，不存檔
- `daily`：馬上抓最新資料
- `backfill`：馬上開始回補

連接器（server.py）的 `get_stock_history`、`get_revenue_history`、`get_financials_history`、`get_holder_history` 會直接讀 data 分支上的檔案。
若存放空間不在 `Levi56892/twse-mcp`，請在 Render 的 Environment 設定 `DATA_BASE=https://raw.githubusercontent.com/<帳號>/<存放空間>/data`。

## 美股戰情室（us_recorder.py）

`us_recorder.py` 由 GitHub Actions（`.github/workflows/us.yml`）在美股交易日紐約時間 08:00–16:15 執行，每分鐘把結果存到 **us 分支**的 `us.json`；連接器的 `get_us_war_room` 讀這個檔案，`get_us_quotes` 直接查 Finnhub 即時報價。

需要一組 Finnhub 免費金鑰，設定在兩個地方：
- GitHub：Settings → Secrets and variables → Actions → New repository secret，名稱 `FINNHUB_API_KEY`
- Render：服務的 Environment，新增 `FINNHUB_API_KEY`

資料來源：Finnhub 官方 API（美股逐筆成交、報價、市值）、Coinbase 公開 API（比特幣）、FRED（VIX、美債殖利率、美元指數、原油、美元兌台幣，每日）。
