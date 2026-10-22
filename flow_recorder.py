"""
盤中資金流記錄器（給「即時資金流」頁面用）
由 GitHub Actions 在交易日 08:35 啟動，記錄 09:00–13:30 各產業的主動買賣淨額，
每分鐘把當天結果存到 flow 分支的 today.json（只保留當天，隔天自動覆蓋）。

外／內盤判斷（證交所 mis 即時資料，每 15 秒一次）：
  成交價 >= 上一次的最佳賣價 → 外盤（主動買）
  成交價 <= 上一次的最佳買價 → 內盤（主動賣）
  介於中間 → 看價格升降；價格不變沿用上一次方向
開盤第一筆（集合競價）與 13:25 之後的收盤集合競價不計入。
若程式晚於開盤才啟動，已成交的部分用收盤位置（CLV）估算，並在檔案中標記 seeded。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TW = timezone(timedelta(hours=8))
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
POLL_SEC = 30
TOP_N = int(os.environ.get("TOP_N", "20"))   # 每個產業取近 20 日成交金額前 N 名
DATA_DIR = os.environ.get("DATA_DIR", "databr")  # data 分支（含每日成交量）
PUSH_SEC = 25   # 每輪抓完就存檔（約 30 秒一次）
SEED_K = 0.25
START = 9 * 60          # 09:00
AUCTION = 13 * 60 + 25  # 13:25 起收盤集合競價
END = 13 * 60 + 31      # 13:31 收工
OUT_DIR = os.environ.get("FLOW_DIR", "flowout")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")

# 備用清單：自動挑選失敗時才用（g：e 電子、t 傳產、f 金融）
SECTORS = [
    ("半導體", "e", "2330 2454 2303 3711 2379 3034 6415 3443 3661 2344 2408 5347 6488 3105 5483"),
    ("電子零組件", "e", "2308 3037 2327 2383 3044 2368 2059 3533 8046 2313"),
    ("電腦及週邊", "e", "2382 3231 2357 2376 2356 2353 6669 2301 2395 2324 3017"),
    ("光電", "e", "3008 2409 3481 6176 3406 3714"),
    ("通信網路", "e", "2412 3045 4904 2345 5388 6285 3596"),
    ("電子通路", "e", "3702 2347 3036 8112"),
    ("其他電子", "e", "2317 2360 6409 2404 3030 6139"),
    ("資訊服務", "e", "6214 2471 2480"),
    ("電機機械", "t", "1504 1519 1503 1513 2049 1514"),
    ("電器電纜", "t", "1605 1609 1608"),
    ("塑膠", "t", "1301 1303 1326 1304 1308"),
    ("化學工業", "t", "1722 1710 4763 1717"),
    ("生技醫療", "t", "6446 1795 4743 4147 6472 1760"),
    ("鋼鐵", "t", "2002 2027 2014 2006 2015"),
    ("航運", "t", "2603 2609 2615 2618 2610 2637"),
    ("汽車", "t", "2207 2201 1319 2204"),
    ("食品", "t", "1216 1227 1210 1229"),
    ("紡織纖維", "t", "1402 1476 1477 1440"),
    ("水泥", "t", "1101 1102"),
    ("建材營造", "t", "2542 2501 5522 2548 2545"),
    ("油電燃氣", "t", "6505 9908 8926"),
    ("貿易百貨", "t", "2912 5904 2903 8454"),
    ("玻璃陶瓷", "t", "1802 1806"),
    ("綠能環保", "t", "9930 3708 6443 6806"),
    ("觀光餐旅", "t", "2707 2727 2723"),
    ("橡膠", "t", "2105 2103 2106"),
    ("造紙", "t", "1907 1904"),
    ("金融保險", "f", "2881 2882 2891 2886 2884 2885 2892 5880 2880 2887"),
]
NAMES = [s[0] for s in SECTORS]
SEC_OF = {c: i for i, s in enumerate(SECTORS) for c in s[2].split()}
CODES = list(SEC_OF)
MARKET = {}   # code -> "tse" / "otc"（自動挑選時填入）
ALL_MARKET = {}   # 全部股票的市場別（stocks.csv）
IND = {}          # 全部股票的產業別（給熱力圖用）
EXTRA_SET = set()
THEMES = []   # [(族群名稱, [代號...])]，來自 themes.txt
EXTRA = []    # 只屬於族群、不在產業前 20 名裡的股票


def load_themes():
    """讀 themes.txt：每行「族群名稱: 代號 代號 ...」。"""
    global THEMES, EXTRA
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes.txt")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                name, codes = line.split(":", 1)
                codes = [c for c in codes.replace(",", " ").split() if c.isdigit()]
                if name.strip() and codes:
                    out.append((name.strip(), codes))
    except OSError as e:
        log("讀不到 themes.txt：", e)
    THEMES = out
    have = set(CODES)
    EXTRA = sorted({c for _, cs in THEMES for c in cs} - have)
    log(f"族群 {len(THEMES)} 個，另外加抓 {len(EXTRA)} 檔")
ELEC = {"半導體", "電子零組件", "電腦及週邊", "光電", "通信網路", "電子通路", "其他電子", "資訊服務"}
TWSE_API = "https://openapi.twse.com.tw/v1"
TPEX_API = "https://www.tpex.org.tw/openapi/v1"

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (twse-flow-recorder)"})


def log(*a):
    print(datetime.now(TW).strftime("%H:%M:%S"), *a, flush=True)


def now_min():
    n = datetime.now(TW)
    return n.hour * 60 + n.minute + n.second / 60


def num(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def first(s):
    return num(str(s).split("_")[0]) if s else None


def norm_industry(s):
    s = str(s or "").strip()
    special = {"電腦及週邊設備業": "電腦及週邊", "觀光事業": "觀光餐旅", "觀光餐旅業": "觀光餐旅",
               "金融業": "金融保險", "金融保險業": "金融保險"}
    if s in special:
        return special[s]
    if s.endswith("工業"):
        return s if s == "化學工業" else s[:-2]
    if s.endswith("業") and len(s) > 2:
        return s[:-1]
    return s or None


def avg_turnover(code, days=20):
    """data 分支的日資料：近 days 個交易日的平均成交金額（元）。"""
    year = datetime.now(TW).year
    rows = []
    for y in (year - 1, year):
        path = os.path.join(DATA_DIR, "daily", code, f"{y}.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows += f.read().strip().splitlines()[1:]
    vals = []
    for line in rows[-days:]:
        c = line.split(",")
        try:
            vals.append(float(c[5]) * 1000 * float(c[4]))   # volume_lots × close
        except (IndexError, ValueError):
            pass
    return sum(vals) / len(vals) if vals else 0.0


def build_universe():
    """全部上市櫃普通股依產業分組，每組取近 20 日平均成交金額前 TOP_N 名。失敗就用備用清單。"""
    global SECTORS, NAMES, SEC_OF, CODES
    try:
        ind = {}
        for base, path in ((TWSE_API, "/opendata/t187ap05_L"), (TPEX_API, "/mopsfin_t187ap05_O")):
            r = session.get(base + path, timeout=60)
            r.raise_for_status()
            for d in r.json():
                code, name = str(d.get("公司代號", "")).strip(), norm_industry(d.get("產業別"))
                if len(code) == 4 and code.isdigit() and name:
                    ind[code] = name
                    IND[code] = name
        market = {}
        with open(os.path.join(DATA_DIR, "stocks.csv"), encoding="utf-8") as f:
            for line in f.read().splitlines()[1:]:
                c = line.split(",")
                if len(c) >= 3:
                    market[c[0]] = "otc" if c[2].strip() == "上櫃" else "tse"
        ALL_MARKET.update(market)
        groups = {}
        for code, name in ind.items():
            if code not in market:
                continue
            t = avg_turnover(code)
            if t > 0:
                groups.setdefault(name, []).append((t, code))
        if len(groups) < 10:
            raise RuntimeError(f"產業太少（{len(groups)}）")
        secs = []
        for name, lst in groups.items():
            lst.sort(reverse=True)
            top = lst[:TOP_N]
            g = "f" if name.startswith("金融") else ("e" if name in ELEC else "t")
            secs.append((sum(t for t, _ in top), name, g, " ".join(c for _, c in top)))
        secs.sort(reverse=True)
        SECTORS = [(n, g, codes) for _, n, g, codes in secs]
        NAMES = [s[0] for s in SECTORS]
        SEC_OF = {c: i for i, s in enumerate(SECTORS) for c in s[2].split()}
        CODES = list(SEC_OF)
        MARKET.update({c: market[c] for c in CODES})
        log(f"自動挑選完成：{len(SECTORS)} 個產業、{len(CODES)} 檔")
    except Exception as e:  # noqa: BLE001
        log("自動挑選失敗，改用備用清單：", type(e).__name__, str(e)[:200])


def fetch(ex_list):
    """ex_list: ['tse_2330.tw', ...]；回傳有資料的 msgArray。"""
    if not session.cookies:
        try:
            session.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
        except requests.RequestException:
            pass
    out = []
    for i in range(0, len(ex_list), 50):
        part = "|".join(ex_list[i:i + 50])
        for attempt in range(2):
            try:
                r = session.get(MIS, params={"ex_ch": part, "json": 1, "delay": 0,
                                             "_": int(time.time() * 1000)}, timeout=10)
                r.raise_for_status()
                out += [m for m in r.json().get("msgArray", []) if m.get("c")]
                break
            except Exception as e:  # noqa: BLE001
                log("抓取失敗", type(e).__name__, str(e)[:120])
                time.sleep(3)
        time.sleep(1.2)
    return out


class Recorder:
    def __init__(self):
        self.nets = [0.0] * len(SECTORS)
        self.inten = [0.0] * len(SECTORS)
        self.st = {}           # code -> dict(v, p, a, b, dir)
        self.snet = {}         # code -> 個股累計主動買賣淨額（元）
        self.samt = {}         # code -> 個股累計成交金額（元，估算）
        self.sinfo = {}        # code -> (名稱, 昨收, 最新價)
        self.hist = []         # [{m, n:[億...]}]
        self.thist = []        # 族群：[{m, n:[億...]}]
        self.idx = {"t00": [], "o00": [], "tx": []}   # 加權、櫃買、台指期近月的盤中走勢 [[m, 價]]
        self.tx_sym = None
        self.ex = None         # 決定好的 ex_ch 清單
        self.seeded = False
        self.covered = 0
        self.last_t = None     # 最新成交時間（分鐘，從 0:00 起算）
        self.today = datetime.now(TW).strftime("%Y%m%d")
        self.open_today = False  # 有沒有看到今天日期的報價

    def resolve_markets(self):
        if MARKET:
            self.ex = [f"{MARKET[c]}_{c}.tw" for c in CODES] + \
                      [f"{ALL_MARKET[c]}_{c}.tw" for c in EXTRA if c in ALL_MARKET]
            log(f"使用 {len(self.ex)} 檔")
            return
        rows = fetch([f"{m}_{c}.tw" for c in CODES + EXTRA for m in ("tse", "otc")])
        found = {m["c"]: m.get("ex", "tse") for m in rows}
        self.ex = [f"{found[c]}_{c}.tw" for c in CODES + EXTRA if c in found]
        missing = [c for c in CODES if c not in found]
        log(f"找到 {len(self.ex)} 檔", "查無：" + ",".join(missing) if missing else "")

    def _pt(self, key, m, v):
        if m is None or not v:
            return
        m = round(max(0.0, min(m, END - START + 15)), 2)
        arr = self.idx[key]
        if arr and abs(arr[-1][0] - m) < 0.25:
            arr[-1] = [m, v]
        elif not arr or m > arr[-1][0]:
            arr.append([m, v])

    def step_index(self, rows):
        for m in rows:
            c = m.get("c")
            if c in ("t00", "o00") and m.get("d") == self.today:
                z = num(m.get("z")) or num(m.get("pz"))
                try:
                    hh, mm, ss = (int(x) for x in (m.get("t") or "").split(":"))
                    self._pt(c, hh * 60 + mm + ss / 60 - START, z)
                except ValueError:
                    pass
        # 台指期（期交所 MIS）：取成交量最大的近月合約
        try:
            body = {"MarketType": "0", "SymbolType": "F", "KindID": "1", "CID": "TXF", "ExpireMonth": "",
                    "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}
            r = session.post("https://mis.taifex.com.tw/futures/api/getQuoteList", json=body, timeout=10,
                             headers={"Referer": "https://mis.taifex.com.tw/futures/"})
            ql = ((r.json().get("RtData") or {}).get("QuoteList")) or []
            ql = [q for q in ql if str(q.get("SymbolID", "")).endswith("-F") and "/" not in str(q.get("SymbolID", ""))]
            if ql:
                q = max(ql, key=lambda q: num(q.get("CTotalVolume")) or 0)
                t = str(q.get("CTime") or "").zfill(6)
                self.tx_sym = q.get("SymbolID")
                self._pt("tx", int(t[:2]) * 60 + int(t[2:4]) + int(t[4:6]) / 60 - START, num(q.get("CLastPrice")))
        except Exception as e:  # noqa: BLE001
            log("台指期抓取失敗", type(e).__name__, str(e)[:100])

    def step(self):
        rows = fetch(self.ex + ["tse_t00.tw", "otc_o00.tw"])
        self.step_index(rows)
        tick = [0.0] * len(SECTORS)
        n = 0
        for m in rows:
            code = m.get("c")
            if code not in SEC_OF and code not in EXTRA_SET:
                continue
            si = SEC_OF.get(code)   # None＝只屬於族群，不計入產業
            v = num(m.get("v")) or 0.0
            a1, b1 = first(m.get("a")), first(m.get("b"))
            # 成交價：z 有時是 "-"，改用 pz、買賣中價或上一次價格，避免漏算成交量
            z = num(m.get("z")) or num(m.get("pz")) or ((a1 + b1) / 2 if a1 and b1 else None)
            # 漲跌停鎖住時，證交所常把成交價與一邊（或兩邊）報價都給 "-"：用當日最高／最低是否碰到漲跌停價判斷
            hi_now, lo_now, up, dn = num(m.get("h")), num(m.get("l")), num(m.get("u")), num(m.get("w"))
            lock = 0
            if not z and a1 is None and hi_now and up and hi_now >= up:
                z, lock = up, 1
            elif not z and b1 is None and lo_now and dn and lo_now <= dn:
                z, lock = dn, -1
            if m.get("d") != self.today:
                continue
            self.open_today = True
            t = m.get("t") or ""
            try:
                hh, mm, ss = (int(x) for x in t.split(":"))
                tmin = hh * 60 + mm + ss / 60
            except ValueError:
                tmin = None
            if tmin is not None and (self.last_t is None or tmin > self.last_t):
                self.last_t = tmin
            n += 1
            y = num(m.get("y"))
            self.sinfo[code] = (m.get("n") or code, y, z or (self.sinfo.get(code) or (0, 0, None))[2])
            st = self.st.get(code)
            if st is None:
                # 第一次看到：若已有成交，用 CLV 估算開啟前的部分
                if v > 0:
                    hi, lo = num(m.get("h")), num(m.get("l"))
                    p = z or ((hi + lo) / 2 if hi and lo else None) or num(m.get("y")) or 0
                    if hi and lo and hi > lo:
                        clv = max(-1.0, min(1.0, ((p - lo) - (hi - p)) / (hi - lo)))
                    else:
                        y = num(m.get("y")) or p
                        clv = (p > y) - (p < y)
                    typ = ((hi or p) + (lo or p) + p) / 3
                    seed = v * 1000 * typ * clv * SEED_K
                    if si is not None:
                        self.nets[si] += seed
                    self.snet[code] = self.snet.get(code, 0.0) + seed
                    self.samt[code] = v * 1000 * typ
                    if now_min() > START + 1:
                        self.seeded = True
                self.st[code] = {"v": v, "p": z, "a": a1, "b": b1, "dir": 1}
                continue
            dv = v - st["v"]
            if dv > 0 and (z or st["p"]):
                self.samt[code] = self.samt.get(code, 0.0) + dv * 1000 * (z or st["p"])   # 成交金額（含集合競價）
            opening = st["v"] == 0          # 今天第一筆成交＝開盤集合競價
            counting = tmin is None or tmin < AUCTION
            if dv > 0 and not z:
                z = st["p"]
            if dv > 0 and z and counting:
                if opening:
                    # 開盤集合競價：開高（高於昨收）算買、開低算賣、平盤不計
                    y0 = num(m.get("y"))
                    d = lock or (((z > y0) - (z < y0)) if y0 else 0)
                elif lock:
                    d = lock
                elif st["a"] is None and st["b"] and z >= st["b"]:
                    d = 1      # 沒有賣單（漲停鎖住）：成交都是買方追價
                elif st["b"] is None and st["a"] and z <= st["a"]:
                    d = -1     # 沒有買單（跌停鎖住）：成交都是賣方殺出
                elif st["a"] and z >= st["a"]:
                    d = 1
                elif st["b"] and z <= st["b"]:
                    d = -1
                elif st["p"] and z != st["p"]:
                    d = 1 if z > st["p"] else -1
                else:
                    d = st["dir"]
                amt = dv * 1000 * z
                if si is not None:
                    self.nets[si] += d * amt
                self.snet[code] = self.snet.get(code, 0.0) + d * amt
                if not d:
                    d = st["dir"]
                if si is not None:
                    tick[si] += amt
                st["dir"] = d
            if v > st["v"]:
                st["v"] = v
            if z:
                st["p"] = z
            st["a"], st["b"] = a1, b1   # 照實記錄，漲跌停時一邊會是空的
        self.covered = n
        self.inten = tick
        if self.last_t is not None:
            m = round(min(max(self.last_t - START, 0), END - START - 1), 2)
            snap = {"m": m, "n": [round(x / 1e8, 4) for x in self.nets]}
            if self.hist and int(self.hist[-1]["m"]) == int(m):
                self.hist[-1] = snap
            else:
                self.hist.append(snap)
            tsnap = {"m": m, "n": [round(sum(self.snet.get(c, 0.0) for c in cs) / 1e8, 4) for _, cs in THEMES]}
            if self.thist and int(self.thist[-1]["m"]) == int(m):
                self.thist[-1] = tsnap
            else:
                self.thist.append(tsnap)

    def payload(self):
        today = datetime.now(TW)
        return {
            "date": today.strftime("%Y-%m-%d"),
            "updated": today.strftime("%H:%M:%S"),
            "last_trade_min": None if self.last_t is None else round(self.last_t - START, 2),
            "covered": self.covered,
            "universe": len(CODES) + len(EXTRA),
            "seeded": self.seeded,
            "method": "bidask",
            "unit": "億元",
            "sectors": [{"name": s[0], "g": s[1]} for s in SECTORS],
            "intensity": [round(x / 1e8, 4) for x in self.inten],
            "hist": self.hist,
            # 個股最新累計：[代號, 名稱, 產業序號, 淨額(億), 漲跌%, 成交金額(億), 市場, 產業別]
            "stocks": [
                [c, self.sinfo[c][0], SEC_OF.get(c), round(self.snet.get(c, 0.0) / 1e8, 4),
                 round((self.sinfo[c][2] - self.sinfo[c][1]) / self.sinfo[c][1] * 100, 2)
                 if self.sinfo[c][1] and self.sinfo[c][2] else None,
                 round(self.samt.get(c, 0.0) / 1e8, 4),
                 "上櫃" if (MARKET.get(c) or ALL_MARKET.get(c)) == "otc" else "上市",
                 IND.get(c)]
                for c in CODES + EXTRA if c in self.sinfo
            ],
            # 族群（概念股）：成分股與累計淨額走勢
            "themes": [{"name": n, "codes": cs} for n, cs in THEMES],
            "thist": self.thist,
            # 大盤走勢：加權（t00）、櫃買（o00）、台指期近月（tx）
            "idx": {**self.idx, "tx_symbol": self.tx_sym},
        }


class Pusher:
    """把 today.json 推到 flow 分支（永遠只有一個 commit）。"""

    def __init__(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.ok = bool(REPO and TOKEN)
        self.first = True
        if not self.ok:
            log("沒有 GITHUB_REPOSITORY / GH_TOKEN，只寫本機檔案")
            return
        url = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"
        self.git("init", "-q", "-b", "flow")
        self.git("remote", "add", "origin", url)
        self.git("config", "user.name", "flow-recorder")
        self.git("config", "user.email", "flow-recorder@users.noreply.github.com")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=OUT_DIR, capture_output=True, text=True)

    def push(self, data):
        with open(os.path.join(OUT_DIR, "today.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        if not self.ok:
            return
        self.git("add", "today.json")
        msg = f"資金流 {data['date']} {data['updated']}"
        r = self.git("commit", "-q", "-m", msg) if self.first else self.git("commit", "-q", "--amend", "-m", msg)
        self.first = False
        r = self.git("push", "-q", "-f", "origin", "flow")
        if r.returncode != 0:
            log("推送失敗", r.stderr[-200:])


def main():
    if datetime.now(TW).weekday() >= 5:
        log("週末，不記錄")
        return
    if now_min() >= END - 1:
        log("已收盤，不重複記錄（避免覆蓋今天的檔案）")
        return
    build_universe()
    load_themes()
    EXTRA_SET.update(EXTRA)
    rec, pusher = Recorder(), Pusher()
    while now_min() < START - 1.5:
        time.sleep(20)
    rec.resolve_markets()
    if not rec.ex:
        log("抓不到任何報價，結束")
        sys.exit(1)
    last_push = 0
    while now_min() < END:
        t0 = time.time()
        try:
            rec.step()
        except Exception as e:  # noqa: BLE001
            log("計算錯誤", type(e).__name__, e)
        # 09:10 還沒有任何成交 → 休市日
        if now_min() > START + 10 and not rec.open_today:
            log("09:10 仍無成交，判斷為休市，結束")
            return
        if time.time() - last_push >= PUSH_SEC and rec.hist:
            pusher.push(rec.payload())
            last_push = time.time()
            log(f"已存檔 m={rec.hist[-1]['m']} 涵蓋 {rec.covered} 檔 全市場 {sum(rec.nets)/1e8:+.2f} 億")
        time.sleep(max(1, POLL_SEC - (time.time() - t0)))
    rec.step()
    pusher.push(rec.payload())
    log("收盤，完成")


if __name__ == "__main__":
    main()
