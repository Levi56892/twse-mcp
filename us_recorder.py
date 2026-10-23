"""
美股記錄器（給「美股戰情室」用）
由 GitHub Actions 在美股交易日啟動，記錄紐約時間 08:00（盤前）到 19:30（盤後），
每分鐘把當天結果存到 us 分支的 us.json（只保留當天，隔天自動覆蓋）。
GitHub Actions 一次最多 6 小時，所以分兩段接力，第二段會讀回第一段的紀錄繼續記。

資料來源（都是官方提供、允許程式存取的公開 API）：
  - Finnhub（需免費金鑰，環境變數 FINNHUB_API_KEY）
      WebSocket：50 檔大型股與 SPY、QQQ 的逐筆成交 → 用 tick rule 估算主動買賣金額
      REST /quote：指數 ETF、類股 ETF、債券／美元／原油／黃金 ETF、熱力圖其餘個股（每分鐘最多約 45 次，免費額度 60 次）
      REST /stock/profile2：市值（熱力圖方塊大小，一天抓一次）
  - Coinbase Exchange 公開 API：比特幣報價（免金鑰）
  - FRED（聖路易聯準銀行）：VIX、10 年／2 年期美債殖利率、美元廣義指數、WTI 原油、美元兌台幣（每日資料，免金鑰）

主動買賣（tick rule）：成交價高於上一筆 → 主動買；低於上一筆 → 主動賣；相同 → 沿用上一筆方向。
只統計正規交易時段（09:30–16:00 ET），金額單位：百萬美元。
時間座標 m：距紐約時間 09:30 的分鐘數（盤前為負，16:00＝390）。
"""
import csv
import io
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import websocket   # pip install websocket-client

ET = ZoneInfo("America/New_York")
KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
OUT_DIR = os.environ.get("US_DIR", "usout")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")
START_M = -90            # 08:00 ET 開始
END_M = 600              # 19:30 ET 收工（含盤後交易）
MAX_RUN_SEC = 5 * 3600 + 50 * 60
REST_PER_MIN = 45        # 留 15 次給「台股報價」連接器的 get_us_quotes

# 50 檔 WebSocket（免費版上限 50）：(代號, 中文名, 板塊)
WS = [
    ("NVDA", "輝達", "半導體"), ("AVGO", "博通", "半導體"), ("AMD", "超微", "半導體"), ("TSM", "台積電ADR", "半導體"),
    ("MU", "美光", "半導體"), ("INTC", "英特爾", "半導體"), ("QCOM", "高通", "半導體"), ("AMAT", "應材", "半導體"),
    ("ARM", "安謀", "半導體"), ("MRVL", "邁威爾", "半導體"),
    ("MSFT", "微軟", "軟體雲端"), ("ORCL", "甲骨文", "軟體雲端"), ("CRM", "Salesforce", "軟體雲端"),
    ("ADBE", "Adobe", "軟體雲端"), ("PLTR", "Palantir", "軟體雲端"),
    ("AAPL", "蘋果", "硬體網通"), ("SMCI", "美超微", "硬體網通"), ("ANET", "Arista", "硬體網通"), ("DELL", "戴爾", "硬體網通"),
    ("GOOGL", "Alphabet", "通訊媒體"), ("META", "Meta", "通訊媒體"), ("NFLX", "Netflix", "通訊媒體"),
    ("AMZN", "亞馬遜", "非必需消費"), ("TSLA", "特斯拉", "非必需消費"), ("HD", "家得寶", "非必需消費"), ("MCD", "麥當勞", "非必需消費"),
    ("JPM", "摩根大通", "金融"), ("BAC", "美國銀行", "金融"), ("GS", "高盛", "金融"), ("V", "Visa", "金融"),
    ("MA", "萬事達", "金融"), ("BRK.B", "波克夏", "金融"),
    ("LLY", "禮來", "醫療保健"), ("UNH", "聯合健康", "醫療保健"), ("JNJ", "嬌生", "醫療保健"), ("ABBV", "艾伯維", "醫療保健"),
    ("GE", "奇異航太", "工業"), ("CAT", "開拓重工", "工業"), ("BA", "波音", "工業"),
    ("XOM", "埃克森美孚", "能源"), ("CVX", "雪佛龍", "能源"),
    ("WMT", "沃爾瑪", "必需消費"), ("COST", "好市多", "必需消費"), ("PG", "寶僑", "必需消費"),
    ("NEE", "新紀元能源", "公用事業"), ("VST", "Vistra", "公用事業"),
    ("LIN", "林德", "原物料地產"), ("PLD", "安博", "原物料地產"),
    ("SPY", "S&P 500 ETF", None), ("QQQ", "那斯達克100 ETF", None),
]
# 熱力圖其餘個股（REST 輪流更新）
EXTRA = [
    ("TXN", "德儀", "半導體"), ("LRCX", "科林研發", "半導體"), ("KLAC", "科磊", "半導體"), ("ADI", "亞德諾", "半導體"),
    ("NOW", "ServiceNow", "軟體雲端"), ("INTU", "Intuit", "軟體雲端"), ("IBM", "IBM", "軟體雲端"),
    ("CSCO", "思科", "硬體網通"),
    ("DIS", "迪士尼", "通訊媒體"), ("T", "AT&T", "通訊媒體"), ("VZ", "威訊", "通訊媒體"), ("TMUS", "T-Mobile", "通訊媒體"),
    ("UBER", "Uber", "非必需消費"), ("ABNB", "Airbnb", "非必需消費"),
    ("WFC", "富國銀行", "金融"), ("MS", "摩根士丹利", "金融"), ("C", "花旗", "金融"), ("AXP", "美國運通", "金融"),
    ("MRK", "默克", "醫療保健"), ("PFE", "輝瑞", "醫療保健"), ("TMO", "賽默飛", "醫療保健"), ("ISRG", "直覺手術", "醫療保健"),
    ("HON", "漢威聯合", "工業"), ("UNP", "聯合太平洋", "工業"), ("RTX", "雷神", "工業"), ("LMT", "洛克希德", "工業"),
    ("COP", "康菲石油", "能源"), ("SLB", "斯倫貝謝", "能源"),
    ("KO", "可口可樂", "必需消費"), ("PEP", "百事", "必需消費"),
]
# 每分鐘更新的 ETF：(代號, 中文名, 分組)
ETF = [
    ("SPY", "S&P 500", "idx"), ("QQQ", "那斯達克100", "idx"), ("DIA", "道瓊", "idx"),
    ("IWM", "羅素2000", "idx"), ("SOXX", "費城半導體", "idx"),
    ("XLK", "科技", "sec"), ("XLC", "通訊服務", "sec"), ("XLY", "非必需消費", "sec"), ("XLF", "金融", "sec"),
    ("XLV", "醫療保健", "sec"), ("XLI", "工業", "sec"), ("XLE", "能源", "sec"), ("XLP", "必需消費", "sec"),
    ("XLU", "公用事業", "sec"), ("XLB", "原物料", "sec"), ("XLRE", "房地產", "sec"),
    ("TLT", "20年美債", "mac"), ("UUP", "美元", "mac"), ("USO", "原油", "mac"), ("GLD", "黃金", "mac"),
]
SERIES = ["SPY", "QQQ", "DIA", "IWM", "SOXX", "TLT", "UUP", "USO", "GLD", "TSM", "NVDA", "BTC"]
FRED = [("VIXCLS", "VIX 恐慌指數"), ("DGS10", "10年期美債殖利率"), ("DGS2", "2年期美債殖利率"),
        ("DTWEXBGS", "美元廣義指數"), ("DCOILWTICO", "WTI 原油"), ("DEXTAUS", "美元兌台幣")]

SECTORS = []
for _, _, g in WS + EXTRA:
    if g and g not in SECTORS:
        SECTORS.append(g)
INFO = {s: (n, g) for s, n, g in WS + EXTRA}
INFO.update({s: (n, None) for s, n, _ in ETF if s not in INFO})
WS_SYMS = [s for s, _, _ in WS]
FLOW_SYMS = [s for s, _, g in WS if g]

http = requests.Session()
http.headers.update({"User-Agent": "twse-mcp us-recorder (personal dashboard)"})


def log(*a):
    print(datetime.now(ET).strftime("%H:%M:%S ET"), *a, flush=True)


def et_min(dt=None):
    dt = dt or datetime.now(ET)
    return dt.hour * 60 + dt.minute + dt.second / 60 - 570


def r2(x, n=2):
    return None if x is None else round(x, n)


# ---------- Finnhub REST（自帶節流） ----------
class Finnhub:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        while True:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) < REST_PER_MIN:
                break
            time.sleep(61 - (now - self.calls[0]))
        self.calls.append(time.time())
        r = http.get(f"https://finnhub.io/api/v1{path}", params={**params, "token": KEY}, timeout=15)
        if r.status_code == 429:
            log("Finnhub 限流，休息 20 秒")
            time.sleep(20)
            return None
        r.raise_for_status()
        return r.json()

    def quote(self, sym):
        try:
            q = self.get("/quote", symbol=sym)
        except Exception as e:  # noqa: BLE001
            log("quote 失敗", sym, type(e).__name__, str(e)[:80])
            return None
        if not q or not q.get("c"):
            return None
        return {"p": q["c"], "pc": q.get("pc"), "o": q.get("o"), "h": q.get("h"), "l": q.get("l"), "t": q.get("t")}

    def mcap(self, sym):
        try:
            j = self.get("/stock/profile2", symbol=sym)
        except Exception:  # noqa: BLE001
            return None
        return (j or {}).get("marketCapitalization")


# ---------- WebSocket 逐筆成交 ----------
class Trades:
    def __init__(self, prev_stocks):
        self.lock = threading.Lock()
        self.st = {}
        for s in WS_SYMS:
            p = (prev_stocks or {}).get(s) or {}
            self.st[s] = {"last": p.get("p"), "dir": 0, "buy": (p.get("buy") or 0) * 1e6, "sell": (p.get("sell") or 0) * 1e6,
                          "dv": (p.get("dv") or 0) * 1e6, "h": p.get("h"), "l": p.get("l"), "t": 0, "n": 0,
                          "rc": p.get("rc"), "ext": p.get("x")}
        self.minute_dv = {g: 0.0 for g in SECTORS}
        self.count = 0
        self.last_trade = time.time()
        self.stop = False

    def on_trade(self, s, p, v, t):
        st = self.st.get(s)
        if st is None or not p or not v:
            return
        reg = 0 <= et_min() < 390
        with self.lock:
            if st["last"] is not None:
                if p > st["last"]:
                    st["dir"] = 1
                elif p < st["last"]:
                    st["dir"] = -1
            st["last"], st["t"] = p, t
            if reg:
                st["rc"] = p          # 正規時段最新價（16:00 後就是收盤價）
            else:
                st["ext"] = p         # 盤前／盤後最新價
            self.count += 1
            self.last_trade = time.time()
            if reg:
                dv = p * v
                st["dv"] += dv
                st["n"] += 1
                if st["dir"] > 0:
                    st["buy"] += dv
                elif st["dir"] < 0:
                    st["sell"] += dv
                st["h"] = p if st["h"] is None else max(st["h"], p)
                st["l"] = p if st["l"] is None else min(st["l"], p)
                g = INFO[s][1]
                if g:
                    self.minute_dv[g] += dv

    def run(self):
        def on_open(ws):
            for s in WS_SYMS:
                ws.send(json.dumps({"type": "subscribe", "symbol": s}))
            log(f"WebSocket 已連線，訂閱 {len(WS_SYMS)} 檔")

        def on_message(ws, msg):
            try:
                j = json.loads(msg)
            except ValueError:
                return
            if j.get("type") == "trade":
                for d in j.get("data") or []:
                    self.on_trade(d.get("s"), d.get("p"), d.get("v"), d.get("t"))
            elif j.get("type") == "error":
                log("WebSocket 錯誤訊息", str(j)[:200])

        while not self.stop:
            ws = websocket.WebSocketApp(f"wss://ws.finnhub.io?token={KEY}", on_open=on_open, on_message=on_message,
                                        on_error=lambda ws, e: log("WebSocket 錯誤", str(e)[:120]))
            ws.run_forever(ping_interval=30, ping_timeout=10)
            if not self.stop:
                log("WebSocket 中斷，5 秒後重連")
                time.sleep(5)


# ---------- 其他來源 ----------
def fetch_btc():
    try:
        r = http.get("https://api.exchange.coinbase.com/products/BTC-USD/stats", timeout=10)
        r.raise_for_status()
        j = r.json()
        last, op = float(j["last"]), float(j["open"])
        return {"p": last, "pc": op, "h": float(j["high"]), "l": float(j["low"]), "note": "24小時"}
    except Exception as e:  # noqa: BLE001
        log("BTC 失敗", type(e).__name__, str(e)[:80])
        return None


def fetch_fred():
    out = {}
    since = (datetime.now(ET) - timedelta(days=45)).strftime("%Y-%m-%d")
    for sid, name in FRED:
        try:
            r = http.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid, "cosd": since}, timeout=20)
            r.raise_for_status()
            rows = []
            for row in csv.reader(io.StringIO(r.text)):
                if len(row) >= 2:
                    try:
                        rows.append((row[0], float(row[1])))
                    except ValueError:
                        pass
            if rows:
                out[sid] = {"name": name, "date": rows[-1][0], "v": rows[-1][1],
                            "prev": rows[-2][1] if len(rows) > 1 else None,
                            "hist": [[d, v] for d, v in rows[-30:]]}
        except Exception as e:  # noqa: BLE001
            log("FRED 失敗", sid, type(e).__name__, str(e)[:80])
    return out


# ---------- 存到 us 分支 ----------
class Store:
    def __init__(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.ok = bool(REPO and TOKEN)
        self.path = os.path.join(OUT_DIR, "us.json")
        self.prev = None
        if self.ok:
            url = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "us", url, OUT_DIR + "_old"],
                               capture_output=True, text=True)
            old = os.path.join(OUT_DIR + "_old", "us.json")
            if r.returncode == 0 and os.path.exists(old):
                self.prev = json.load(open(old, encoding="utf-8"))
            self.git("init", "-q", "-b", "us")
            self.git("remote", "add", "origin", url)
            self.git("config", "user.name", "us-recorder")
            self.git("config", "user.email", "us-recorder@users.noreply.github.com")
        self.first = True

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=OUT_DIR, capture_output=True, text=True)

    def save(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        if not self.ok:
            return
        self.git("add", "us.json")
        msg = f"美股 {data['date']} {data['updated']}"
        self.git("commit", "-q", "-m", msg) if self.first else self.git("commit", "-q", "--amend", "-m", msg)
        self.first = False
        r = self.git("push", "-q", "-f", "origin", "us")
        if r.returncode != 0:
            log("推送失敗", r.stderr[-200:])


def push_pt(arr, m, v):
    if v is None:
        return
    m = round(m, 2)
    if arr and abs(arr[-1][0] - m) < 0.5:
        arr[-1] = [m, r2(v, 4)]
    elif not arr or m > arr[-1][0]:
        arr.append([m, r2(v, 4)])


def main():
    if not KEY:
        log("還沒設定 FINNHUB_API_KEY：到 Settings → Secrets and variables → Actions 新增")
        raise SystemExit(1)
    now = datetime.now(ET)
    m0 = et_min(now)
    if now.weekday() >= 5:
        log("週末，結束")
        return
    if m0 < START_M - 20 or m0 > END_M:
        log(f"不在記錄時段（現在距 09:30 {m0:.0f} 分鐘），結束")
        return
    date = now.strftime("%Y-%m-%d")
    store = Store()
    prev = store.prev if (store.prev and store.prev.get("date") == date) else None
    data = prev or {"date": date, "series": {k: [] for k in SERIES}, "flow": [], "quotes": {}, "mcap": {},
                    "sectors": SECTORS, "stocks": {}, "intensity": [], "macro": {}}
    data["sectors"] = SECTORS
    for k in SERIES:
        data["series"].setdefault(k, [])
    log(f"美股 {date}，{'接續前一段' if prev else '新的一天'}")

    fh = Finnhub()
    trades = Trades(data.get("stocks"))
    threading.Thread(target=trades.run, daemon=True).start()

    data["macro"] = fetch_fred() or data.get("macro", {})
    last_fred = time.time()
    quotes = data["quotes"]
    pool = [s for s, _, _ in WS if INFO[s][1]] + [s for s, _, _ in EXTRA]
    # 先把 WebSocket 個股的昨收補齊（熱力圖要用）
    for s in pool[:len(FLOW_SYMS)]:
        if s not in quotes or not quotes[s].get("pc"):
            q = fh.quote(s)
            if q:
                quotes[s] = q
    rot = 0
    start = time.time()
    idle_since = None
    while time.time() - start < MAX_RUN_SEC:
        loop_t = time.time()
        now = datetime.now(ET)
        m = et_min(now)
        if m > END_M:
            break
        # 1) ETF 每分鐘
        for s, _, _ in ETF:
            q = fh.quote(s)
            if q:
                quotes[s] = q
        # 2) 個股輪流（每分鐘 15 檔）＋市值（每分鐘 3 檔，直到補齊）
        for _ in range(15):
            s = pool[rot % len(pool)]
            rot += 1
            q = fh.quote(s)
            if q:
                quotes[s] = {**quotes.get(s, {}), **q}
        miss = [s for s in pool if s not in data["mcap"]][:3]
        for s in miss:
            mc = fh.mcap(s)
            data["mcap"][s] = r2(mc, 0) if mc else 0
        # 3) 比特幣、FRED（每小時）
        btc = fetch_btc()
        if btc:
            quotes["BTC"] = btc
        if time.time() - last_fred > 3600:
            data["macro"] = fetch_fred() or data["macro"]
            last_fred = time.time()

        # 4) 整理成一分鐘快照
        m = et_min()
        with trades.lock:
            for s, st in trades.st.items():
                q = quotes.setdefault(s, {})
                # 漲跌一律用正規時段價格算；盤前、盤後價另外放在 xp
                if st["rc"]:
                    q["p"] = st["rc"]
                if st["ext"] and (m < 0 or m >= 390):
                    q["xp"] = st["ext"]
                    if 0 <= m < 390 and st["h"] is not None:
                        q["h"], q["l"] = st["h"], st["l"]
            stocks = {s: {"p": r2(st["last"], 3), "buy": r2(st["buy"] / 1e6), "sell": r2(st["sell"] / 1e6),
                          "net": r2((st["buy"] - st["sell"]) / 1e6), "dv": r2(st["dv"] / 1e6),
                          "h": st["h"], "l": st["l"], "rc": r2(st["rc"], 3),
                          "x": r2(st["ext"], 3) if (m < 0 or m >= 390) else None}
                      for s, st in trades.st.items() if INFO[s][1]}
            lasts = {s: st["last"] for s, st in trades.st.items() if st["last"]}
            inten = {g: r2(v / 1e6) for g, v in trades.minute_dv.items()}
            trades.minute_dv = {g: 0.0 for g in SECTORS}
            ws_count = trades.count
        data["stocks"] = stocks
        for k in SERIES:
            q = quotes.get(k)
            v = lasts.get(k) or (q or {}).get("p")      # 有逐筆成交的用最新成交價（含盤前盤後）
            if v:
                push_pt(data["series"][k], m, v)
        # 個股日內走勢（每 2 分鐘一點，給「盤面焦點」的小走勢圖）
        sp = data.setdefault("spark", {})
        b = int(m // 2) * 2
        for s2, v in lasts.items():
            if INFO[s2][1]:
                arr = sp.setdefault(s2, [])
                if arr and arr[-1][0] == b:
                    arr[-1] = [b, r2(v, 2)]
                elif not arr or b > arr[-1][0]:
                    arr.append([b, r2(v, 2)])
        if 0 <= m <= 391:
            nets = [0.0] * len(SECTORS)
            for s, v in stocks.items():
                nets[SECTORS.index(INFO[s][1])] += v["net"] or 0
            fl = data["flow"]
            pt = {"m": round(m, 1), "n": [round(x, 1) for x in nets]}
            if fl and abs(fl[-1]["m"] - pt["m"]) < 0.5:
                fl[-1] = pt
            else:
                fl.append(pt)
            data["intensity"] = [inten[g] for g in SECTORS]
        data["names"] = {s: INFO[s][0] for s in INFO}
        data["sector_of"] = {s: INFO[s][1] for s in INFO if INFO[s][1]}
        data["ws_symbols"] = WS_SYMS
        data["phase"] = "pre" if m < 0 else ("reg" if m < 390 else "post")
        data["tz_offset"] = int(now.utcoffset().total_seconds() // 3600)
        data["updated"] = now.strftime("%H:%M:%S")
        data["updated_ts"] = int(time.time())
        data["trades_seen"] = ws_count
        store.save(data)
        log(f"m={m:.1f} 逐筆 {ws_count} 筆，報價 {len(quotes)} 檔")

        # 休市判斷：正規時段 30 分鐘都沒有成交 → 結束
        if 0 <= m < 390 and time.time() - trades.last_trade > 120:
            idle_since = idle_since or time.time()
            if time.time() - idle_since > 30 * 60:
                log("正規時段 30 分鐘沒有成交（可能休市），結束")
                break
        else:
            idle_since = None
        time.sleep(max(1, 60 - (time.time() - loop_t)))
    trades.stop = True
    log("這段結束")


if __name__ == "__main__":
    main()
