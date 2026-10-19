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
