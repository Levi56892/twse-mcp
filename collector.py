"""
台股資料收集器（由 GitHub Actions 自動執行）
- 每天收盤後：抓當天全市場的股價、三大法人、融資融券、借券、外資持股，以及月營收、財報、集保股權分散
- 其餘時間：從新到舊回補歷史，直到 2015-01-01，抓過的日子不再重抓
資料存在本存放空間的 data 分支：
  daily/<代號>/<年>.csv   每日股價＋籌碼
  revenue/<代號>.csv      月營收
  financials/<代號>.csv   季報重點（當年度累計）
  holders/<代號>.csv      集保股權分散（每週）
  stocks.csv              代號、名稱、市場
  status.json             哪些日子抓過了
"""
import csv
import io
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

DATA = os.environ.get("DATA_DIR", "data")
START = date(2015, 1, 1)
TW = timezone(timedelta(hours=8))
T0 = time.time()
MODE = (os.environ.get("MODE") or "").strip()
SCHEDULE = (os.environ.get("SCHEDULE") or "").strip()
if not MODE:
    MODE = "daily" if SCHEDULE.startswith("30 13") else "backfill"
BUDGET = {"daily": 40, "backfill": 300, "probe": 10}.get(MODE, 60) * 60
GAP = 2.0
STOCK = re.compile(r"^[1-9]\d{3}$")
COLS = ["date", "open", "high", "low", "close", "volume_lots", "foreign_lots", "trust_lots", "dealer_lots",
        "total_lots", "margin_lots", "short_lots", "sbl_lots", "foreign_hold_lots", "foreign_hold_pct"]

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (twse-collector; +github actions)"
_locks = {"twse": threading.Lock(), "tpex": threading.Lock(), "other": threading.Lock()}
_last = {"twse": 0.0, "tpex": 0.0, "other": 0.0}


def log(*a):
    print(f"[{(time.time() - T0) / 60:6.1f}m]", *a, flush=True)


def time_left():
    return BUDGET - (time.time() - T0)


# ---------- 小工具 ----------
def num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").replace("X", "").strip()
    if s in ("", "-", "--", "---", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def lots(x):
    v = num(x)
    return round(v / 1000) if v is not None else None


def norm(k):
    return re.sub(r"[\s\-_()（）/、%．.]", "", str(k)).lower()


def f(d, *cands):
    nd = {norm(k): v for k, v in d.items()}
    for c in cands:
        v = nd.get(norm(c))
        if v not in (None, ""):
            return v
    return None


def ftok(d, *token_sets):
    for toks in token_sets:
        for k, v in d.items():
            nk = norm(k)
            if all(norm(t) in nk for t in toks) and v not in (None, ""):
                return v
    return None


def get(host, url, params=None, tries=3, as_text=False):
    err = None
    for i in range(tries):
        with _locks[host]:
            w = _last[host] + GAP - time.time()
            if w > 0:
                time.sleep(w)
            _last[host] = time.time()
            try:
                r = S.get(url, params=params, timeout=45)
                if r.status_code == 200:
                    if as_text:
                        return r
                    try:
                        return r.json()
                    except ValueError:
                        return None
                err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                err = str(e)
        time.sleep(6 * (i + 1))
    raise RuntimeError(f"{url} {params}: {err}")


def tables(j):
    out = []
    if isinstance(j, dict):
        if j.get("fields") and j.get("data") is not None:
            out.append((j["fields"], j["data"]))
        for t in j.get("tables") or []:
            if isinstance(t, dict) and t.get("fields") and t.get("data") is not None:
                out.append((t["fields"], t["data"]))
        for k, v in j.items():
            m = re.match(r"^fields(\d+)$", k)
            if m and j.get("data" + m.group(1)) is not None:
                out.append((v, j["data" + m.group(1)]))
        if j.get("aaData") is not None and j.get("fields"):
            out.append((j["fields"], j["aaData"]))
    return out


def rows_from(j):
    """取出含『代號』欄位的表；重複欄名加 _2、_3。回傳 {代號: row}。"""
    for fields, data in tables(j):
        if not any("代號" in str(x) for x in fields):
            continue
        names, seen = [], {}
        for x in fields:
            x = str(x).strip()
            seen[x] = seen.get(x, 0) + 1
            names.append(x if seen[x] == 1 else f"{x}_{seen[x]}")
        out = {}
        for row in data:
            if not isinstance(row, list):
                continue
            d = dict(zip(names, row))
            c = str(ftok(d, ("代號",)) or "").strip()
            if c:
                out[c] = d
        if out:
            return out
    return {}


# ---------- 每日全市場資料 ----------
def ymd(d):
    return d.strftime("%Y%m%d")


def slash(d):
    return d.strftime("%Y/%m/%d")


def src_twse_price(d):
    j = get("twse", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            {"date": ymd(d), "type": "ALLBUT0999", "response": "json"})
    out = {}
    for c, r in rows_from(j).items():
        out[c] = {"open": num(f(r, "開盤價")), "high": num(f(r, "最高價")), "low": num(f(r, "最低價")),
                  "close": num(f(r, "收盤價")), "volume_lots": lots(f(r, "成交股數")),
                  "_name": str(f(r, "證券名稱") or "").strip()}
    return out


def src_tpex_price(d):
    j = get("tpex", "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
            {"date": slash(d), "type": "EW", "response": "json"})
    out = {}
    for c, r in rows_from(j).items():
        vol = ftok(r, ("成交股數",), ("成交", "股數"))
        out[c] = {"open": num(ftok(r, ("開盤",))), "high": num(ftok(r, ("最高",))), "low": num(ftok(r, ("最低",))),
                  "close": num(ftok(r, ("收盤",))), "volume_lots": lots(vol),
                  "_name": str(ftok(r, ("名稱",)) or "").strip()}
    return out


def src_twse_inst(d):
    j = get("twse", "https://www.twse.com.tw/rwd/zh/fund/T86",
            {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"})
    out = {}
    for c, r in rows_from(j).items():
        a, b = num(f(r, "外陸資買賣超股數(不含外資自營商)")), num(f(r, "外資自營商買賣超股數"))
        foreign = None if a is None and b is None else (a or 0) + (b or 0)
        if foreign is None:
            foreign = num(f(r, "外資買賣超股數"))
        out[c] = {"foreign_lots": lots(foreign), "trust_lots": lots(f(r, "投信買賣超股數")),
                  "dealer_lots": lots(f(r, "自營商買賣超股數")), "total_lots": lots(f(r, "三大法人買賣超股數"))}
    return out


def src_tpex_inst(d):
    j = get("tpex", "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
            {"type": "Daily", "sect": "EW", "date": slash(d), "response": "json"})
    out = {}
    for c, r in rows_from(j).items():
        out[c] = {"foreign_lots": lots(ftok(r, ("外資及陸資", "買賣超"), ("外資", "買賣超"))),
                  "trust_lots": lots(ftok(r, ("投信", "買賣超"))),
                  "dealer_lots": lots(ftok(r, ("自營商", "買賣超"))),
                  "total_lots": lots(ftok(r, ("三大法人", "買賣超"), ("合計", "買賣超")))}
    return out


def _margin(r):
    m = num(f(r, "融資今日餘額") or f(r, "今日餘額") or ftok(r, ("資", "今日餘額"), ("資", "餘額")))
    s = num(f(r, "融券今日餘額") or f(r, "今日餘額_2") or ftok(r, ("券", "今日餘額"), ("券", "餘額")))
    return {"margin_lots": m, "short_lots": s}


def src_twse_margin(d):
    j = get("twse", "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
            {"date": ymd(d), "selectType": "ALL", "response": "json"})
    return {c: _margin(r) for c, r in rows_from(j).items()}


def src_tpex_margin(d):
    j = get("tpex", "https://www.tpex.org.tw/www/zh-tw/margin/balance", {"date": slash(d), "response": "json"})
    return {c: _margin(r) for c, r in rows_from(j).items()}


def _sbl(r):
    return {"sbl_lots": lots(f(r, "當日餘額") or ftok(r, ("借券", "餘額")))}


def src_twse_sbl(d):
    j = get("twse", "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U", {"date": ymd(d), "response": "json"})
    return {c: _sbl(r) for c, r in rows_from(j).items()}


def src_tpex_sbl(d):
    j = get("tpex", "https://www.tpex.org.tw/www/zh-tw/margin/sbl", {"date": slash(d), "response": "json"})
    return {c: _sbl(r) for c, r in rows_from(j).items()}


def _qfii(r):
    return {"foreign_hold_lots": lots(ftok(r, ("全體外資", "持有股數"), ("外資", "持有股數"), ("外資", "持股數"))),
            "foreign_hold_pct": num(ftok(r, ("全體外資", "持股比率"), ("外資", "持股比率"), ("外資", "持股比例")))}


def src_twse_qfii(d):
    j = get("twse", "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS",
            {"date": ymd(d), "selectType": "ALLBUT0999", "response": "json"})
    return {c: _qfii(r) for c, r in rows_from(j).items()}


def src_tpex_qfii(d):
    j = get("tpex", "https://www.tpex.org.tw/www/zh-tw/insti/qfii", {"date": slash(d), "response": "json"})
    return {c: _qfii(r) for c, r in rows_from(j).items()}


SOURCES = {
    "上市": [("price", src_twse_price), ("inst", src_twse_inst), ("margin", src_twse_margin),
            ("sbl", src_twse_sbl), ("qfii", src_twse_qfii)],
    "上櫃": [("price", src_tpex_price), ("inst", src_tpex_inst), ("margin", src_tpex_margin),
            ("sbl", src_tpex_sbl), ("qfii", src_tpex_qfii)],
}


# ---------- 儲存 ----------
def path(*p):
    full = os.path.join(DATA, *p)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def load_json(p, default):
    try:
        with open(os.path.join(DATA, p), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(p, obj):
    with open(path(p), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def merge_csv(p, key, cols, new_rows):
    """把 new_rows（{key: row}）合併進 CSV；同一個 key 以新資料覆蓋非空欄位。"""
    full = path(p)
    rows = {}
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows[r[key]] = r
    for k, r in new_rows.items():
        old = rows.get(k, {})
        merged = dict(old)
        for c in cols:
            v = r.get(c)
            if v is not None and v != "":
                merged[c] = v
        merged[key] = k
        rows[k] = merged
    with open(full, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows):
            w.writerow({c: _fmt(rows[k].get(c)) for c in cols})


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return "" if v is None else v


class Buffer:
    def __init__(self):
        self.daily = {}   # (code, year) -> {date: row}
        self.names = load_json("stocks.json", {})

    def add_day(self, d, market, parts):
        iso = d.isoformat()
        codes = set()
        for vals in parts.values():
            codes.update(vals.keys())
        price = parts.get("price", {})
        for c in codes:
            if not STOCK.match(c):
                continue
            row = {"date": iso}
            for vals in parts.values():
                row.update({k: v for k, v in (vals.get(c) or {}).items() if not k.startswith("_")})
            if c in price and price[c].get("_name"):
                self.names[c] = {"name": price[c]["_name"], "market": market}
            self.daily.setdefault((c, d.year), {})[iso] = row

    def flush(self):
        if not self.daily:
            return
        n = len(self.daily)
        for (c, y), rows in self.daily.items():
            merge_csv(f"daily/{c}/{y}.csv", "date", COLS, rows)
        self.daily = {}
        save_json("stocks.json", self.names)
        with open(path("stocks.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["code", "name", "market"])
            for c in sorted(self.names):
                w.writerow([c, self.names[c]["name"], self.names[c]["market"]])
        log(f"已寫入 {n} 個個股年度檔")


# ---------- 逐日收集 ----------
status = load_json("status.json", {"days": {}})
DAYS = status.setdefault("days", {})
broken = {}   # (market, kind) -> 連續失敗次數；本次執行中連續失敗太多就先停用


def day_done(iso):
    st = DAYS.get(iso)
    if not st:
        return False
    if st.get("上市:price") == 0 and st.get("上櫃:price") in (0, None):
        return True      # 休市
    return all(st.get(f"{m}:{k}") is not None for m in SOURCES for k, _ in SOURCES[m])


def collect_day(d, buf):
    iso = d.isoformat()
    st = DAYS.setdefault(iso, {})
    results = {}

    def run(market):
        parts = {}
        for kind, fn in SOURCES[market]:
            key = f"{market}:{kind}"
            if st.get(key) is not None:
                continue
            if kind != "price" and st.get(f"{market}:price") == 0:
                st[key] = 0
                continue
            if broken.get(key, 0) >= 8:
                continue
            try:
                vals = fn(d)
                parts[kind] = vals
                if vals:
                    st[key] = 1
                elif d < datetime.now(TW).date():
                    st[key] = 0          # 過去的日子沒資料＝休市或該資料尚未提供
                else:
                    break                # 今天還沒公布，下次再抓
                broken[key] = 0
            except Exception as e:  # noqa: BLE001
                broken[key] = broken.get(key, 0) + 1
                log(f"  {iso} {key} 失敗：{e}")
                if broken[key] == 8:
                    log(f"  !! {key} 連續失敗 8 次，本次先停用")
            if kind == "price" and st.get(key) == 0:
                break
        results[market] = parts

    threads = [threading.Thread(target=run, args=(m,)) for m in SOURCES]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for m, parts in results.items():
        if parts:
            buf.add_day(d, m, parts)


def weekdays_back(start, end):
    d = start
    while d >= end:
        if d.weekday() < 5:
            yield d
        d -= timedelta(days=1)


# ---------- 月營收、財報、集保 ----------
OPEN_TWSE = "https://openapi.twse.com.tw/v1"
OPEN_TPEX = "https://www.tpex.org.tw/openapi/v1"
IND = ["ci", "basi", "bd", "fh", "ins", "mim"]


def openapi(base, p):
    try:
        j = get("other", base + p, tries=2)
        return j if isinstance(j, list) else []
    except Exception as e:  # noqa: BLE001
        log(f"  {p} 失敗：{e}")
        return []


def code_of(r):
    return str(f(r, "公司代號", "SecuritiesCompanyCode", "Code", "證券代號") or "").strip()


def roc_month(s):
    s = str(s or "").strip()
    if len(s) >= 4 and s.isdigit():
        return f"{int(s[:-2]) + 1911:04d}-{int(s[-2:]):02d}"
    return None


REV_COLS = ["month", "revenue_k", "last_month_k", "last_year_k", "mom_pct", "yoy_pct", "ytd_k", "ytd_last_k", "ytd_yoy_pct"]


def rev_row(r):
    return {"revenue_k": num(f(r, "營業收入-當月營收")), "last_month_k": num(f(r, "營業收入-上月營收")),
            "last_year_k": num(f(r, "營業收入-去年當月營收")), "mom_pct": num(f(r, "營業收入-上月比較增減(%)")),
            "yoy_pct": num(f(r, "營業收入-去年同月增減(%)")), "ytd_k": num(f(r, "累計營業收入-當月累計營收")),
            "ytd_last_k": num(f(r, "累計營業收入-去年累計營收")),
            "ytd_yoy_pct": num(f(r, "累計營業收入-前期比較增減(%)"))}


def collect_revenue():
    by = {}
    for base, p in ((OPEN_TWSE, "/opendata/t187ap05_L"), (OPEN_TPEX, "/mopsfin_t187ap05_O")):
        for r in openapi(base, p):
            c, m = code_of(r), roc_month(f(r, "資料年月"))
            if STOCK.match(c) and m:
                by.setdefault(c, {})[m] = {"month": m, **rev_row(r)}
    for c, rows in by.items():
        merge_csv(f"revenue/{c}.csv", "month", REV_COLS, rows)
    log(f"月營收：{len(by)} 檔")


FIN_COLS = ["period", "revenue_k", "gross_profit_k", "operating_income_k", "pretax_income_k", "net_income_k", "eps",
            "total_assets_k", "total_liabilities_k", "equity_parent_k", "book_value_per_share"]


def collect_financials():
    by = {}
    for base, pre in ((OPEN_TWSE, "/opendata/t187ap06_L_"), (OPEN_TPEX, "/mopsfin_t187ap06_O_")):
        for x in IND:
            for r in openapi(base, pre + x):
                c = code_of(r)
                y, q = f(r, "年度", "Year"), f(r, "季別", "Season")
                if not (STOCK.match(c) and y and q):
                    continue
                per = f"{int(y) + 1911}Q{int(q)}"
                by.setdefault(c, {})[per] = {
                    "period": per,
                    "revenue_k": num(f(r, "營業收入", "收益", "淨收益", "利息淨收益")),
                    "gross_profit_k": num(f(r, "營業毛利(毛損)淨額", "營業毛利(毛損)")),
                    "operating_income_k": num(f(r, "營業利益(損失)", "營業利益")),
                    "pretax_income_k": num(f(r, "稅前淨利(淨損)", "繼續營業單位稅前淨利(淨損)")),
                    "net_income_k": num(f(r, "淨利(淨損)歸屬於母公司業主", "淨利(損)歸屬於母公司業主", "本期淨利(淨損)")),
                    "eps": num(f(r, "基本每股盈餘(元)", "基本每股盈餘", "每股盈餘"))}
    for base, pre in ((OPEN_TWSE, "/opendata/t187ap07_L_"), (OPEN_TPEX, "/mopsfin_t187ap07_O_")):
        for x in IND:
            for r in openapi(base, pre + x):
                c = code_of(r)
                y, q = f(r, "年度", "Year"), f(r, "季別", "Season")
                if not (STOCK.match(c) and y and q):
                    continue
                per = f"{int(y) + 1911}Q{int(q)}"
                by.setdefault(c, {}).setdefault(per, {"period": per}).update({
                    "total_assets_k": num(f(r, "資產總計", "資產總額")),
                    "total_liabilities_k": num(f(r, "負債總計", "負債總額")),
                    "equity_parent_k": num(f(r, "歸屬於母公司業主之權益合計", "權益總計", "權益總額")),
                    "book_value_per_share": num(f(r, "每股參考淨值"))})
    for c, rows in by.items():
        merge_csv(f"financials/{c}.csv", "period", FIN_COLS, rows)
    log(f"財報：{len(by)} 檔")


HOLD_COLS = ["date", "holders", "big400_pct", "big1000_pct", "retail_pct"] + [f"l{i}_pct" for i in range(1, 16)] + \
            [f"l{i}_holders" for i in range(1, 16)]


def collect_holders():
    try:
        r = get("other", "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", as_text=True)
    except Exception as e:  # noqa: BLE001
        log(f"集保失敗：{e}")
        return
    text = r.content.decode("utf-8-sig", errors="replace")
    by = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {str(k).strip(): v for k, v in row.items() if k}
        c = str(f(row, "證券代號") or "").strip()
        lv = num(f(row, "持股分級"))
        dt = str(f(row, "資料日期") or "").strip()
        if not (STOCK.match(c) and lv and dt) or not (1 <= lv <= 15):
            continue
        iso = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt
        rec = by.setdefault(c, {}).setdefault(iso, {"date": iso, "holders": 0})
        lv = int(lv)
        rec[f"l{lv}_pct"] = num(ftok(row, ("占集保庫存數比例",), ("比例",)))
        rec[f"l{lv}_holders"] = num(f(row, "人數"))
        rec["holders"] += rec[f"l{lv}_holders"] or 0
    for c, recs in by.items():
        for rec in recs.values():
            p = [rec.get(f"l{i}_pct") or 0 for i in range(1, 16)]
            rec["big400_pct"] = round(sum(p[11:15]), 2)
            rec["big1000_pct"] = round(p[14], 2)
            rec["retail_pct"] = round(sum(p[:11]), 2)
        merge_csv(f"holders/{c}.csv", "date", HOLD_COLS, recs)
    log(f"集保：{len(by)} 檔")


def backfill_revenue():
    """月營收歷史：公開資訊觀測站的月營收彙總表（盡力而為，抓不到就略過）。"""
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        return
    import pandas as pd
    done = status.setdefault("revenue_months", {})
    today = datetime.now(TW).date()
    y, m = START.year, START.month
    while (y, m) < (today.year, today.month) and time_left() > 600:
        key = f"{y:04d}-{m:02d}"
        for mk, typ in (("上市", "sii"), ("上櫃", "otc")):
            k2 = f"{key}:{typ}"
            if done.get(k2):
                continue
            url = f"https://mops.twse.com.tw/nas/t21/{typ}/t21sc03_{y - 1911}_{m}_0.html"
            try:
                r = get("other", url, as_text=True, tries=2)
                r.encoding = "big5"
                rows = {}
                for t in pd.read_html(io.StringIO(r.text)):
                    if isinstance(t.columns, pd.MultiIndex):
                        t.columns = [c[-1] for c in t.columns]
                    t.columns = [str(c) for c in t.columns]
                    if "公司代號" not in t.columns:
                        continue
                    for _, x in t.iterrows():
                        c = str(x["公司代號"]).strip()
                        if not STOCK.match(c):
                            continue
                        g = lambda *names: next((num(x[n]) for n in names if n in t.columns), None)  # noqa: E731
                        rows[c] = {"month": key, "revenue_k": g("當月營收"), "last_month_k": g("上月營收"),
                                   "last_year_k": g("去年當月營收"), "mom_pct": g("上月比較增減(%)"),
                                   "yoy_pct": g("去年同月增減(%)"), "ytd_k": g("當月累計營收"),
                                   "ytd_last_k": g("去年累計營收"), "ytd_yoy_pct": g("前期比較增減(%)")}
                for c, row in rows.items():
                    merge_csv(f"revenue/{c}.csv", "month", REV_COLS, {key: row})
                done[k2] = len(rows) or -1
                log(f"月營收歷史 {key} {mk}：{len(rows)} 檔")
            except Exception as e:  # noqa: BLE001
                log(f"月營收歷史 {key} {mk} 失敗：{e}")
                return   # 觀測站擋住時先停，下次再試
        m += 1
        if m == 13:
            y, m = y + 1, 1


# ---------- 主程式 ----------
def probe():
    d = datetime.now(TW).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    for _ in range(5):
        log(f"測試日期 {d}")
        for m in SOURCES:
            for kind, fn in SOURCES[m]:
                try:
                    vals = fn(d)
                    c = "2330" if m == "上市" else "6488"
                    log(f"  {m}:{kind} 共 {len(vals)} 檔；{c} = {vals.get(c)}")
                except Exception as e:  # noqa: BLE001
                    log(f"  {m}:{kind} 失敗：{e}")
        return


BUF = None


def main():
    global BUF
    log(f"模式：{MODE}，時間上限 {BUDGET / 60:.0f} 分鐘")
    if MODE == "probe":
        probe()
        return
    buf = BUF = Buffer()
    today = datetime.now(TW).date()
    if MODE == "daily":
        for d in weekdays_back(today, today - timedelta(days=14)):
            if not day_done(d.isoformat()):
                collect_day(d, buf)
        buf.flush()
        collect_revenue()
        collect_financials()
        collect_holders()
    else:
        count = 0
        for d in weekdays_back(today - timedelta(days=1), START):
            if time_left() < 900:
                log("時間快到了，先存檔，下次接著補")
                break
            if day_done(d.isoformat()):
                continue
            collect_day(d, buf)
            count += 1
            if count % 30 == 0:
                buf.flush()
                save_json("status.json", status)
                log(f"已回補到 {d}")
        buf.flush()
        backfill_revenue()
        if not os.path.exists(os.path.join(DATA, "revenue")):
            collect_revenue()
        if not os.path.exists(os.path.join(DATA, "financials")):
            collect_financials()
        if not os.path.exists(os.path.join(DATA, "holders")):
            collect_holders()
    remaining = sum(1 for d in weekdays_back(today - timedelta(days=1), START) if not day_done(d.isoformat()))
    status["remaining_days"] = remaining
    status["updated_at"] = datetime.now(TW).isoformat(timespec="minutes")
    save_json("status.json", status)
    log(f"完成。尚待回補的交易日（含假日）：{remaining}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"發生錯誤：{e}")
        if BUF is not None:
            BUF.flush()
        save_json("status.json", status)
        sys.exit(1)
