"""
台股報價 MCP 連接器（上市、上櫃、興櫃）
資料來源：
  - 盤中報價（上市、上櫃）：證交所「基本市況報導」查詢接口 mis.twse.com.tw（約數秒延遲，非官方 API）
  - 盤後資料（上市）：證交所 OpenAPI  openapi.twse.com.tw
  - 盤後資料（上櫃、興櫃）：櫃買中心 OpenAPI  www.tpex.org.tw/openapi
  - 美股：Finnhub 官方 API（FINNHUB_API_KEY），us_recorder.py 記錄的 us 分支
回傳格式一律是「列」(list of dict)，讓看板可以直接當表格使用。
"""
import csv
import io
import os
import re
import time
from datetime import datetime, timedelta, timezone
import requests
from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("PORT", "8000"))
mcp = FastMCP("twse-quotes", host="0.0.0.0", port=PORT, stateless_http=True)

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
TPEX_CLOSE = "/tpex_mainboard_daily_close_quotes"   # 上櫃 每日收盤行情
TPEX_PE = "/tpex_mainboard_peratio_analysis"         # 上櫃 本益比、殖利率、淨值比
TPEX_ESB = "/tpex_esb_latest_statistics"             # 興櫃 當日行情
UA = {"User-Agent": "Mozilla/5.0 (twse-quotes-mcp)"}
QUOTE_TTL = int(os.environ.get("QUOTE_TTL", "15"))
DAILY_TTL = int(os.environ.get("DAILY_TTL", "1800"))
MAX_CODES = 50

_cache: dict = {}
_session = requests.Session()
_session.headers.update(UA)
TW_TZ = timezone(timedelta(hours=8))


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "---", "N/A", "除息", "除權"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get(d, *names):
    """欄位名稱可能有不同寫法，依序找第一個有值的。"""
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _norm(k):
    return re.sub(r"[\s\-_()（）/、%．.]", "", str(k)).lower()


def _f(d, *cands):
    """欄位名稱寫法常有 '-'、'_'、括號 的差異；正規化後再比對。"""
    nd = {_norm(k): v for k, v in d.items()}
    for c in cands:
        v = nd.get(_norm(c))
        if v not in (None, ""):
            return v
    return None


def _ftok(d, *token_sets):
    """找第一個『包含所有關鍵字』的欄位，給名稱不確定的來源用。"""
    for toks in token_sets:
        for k, v in d.items():
            nk = _norm(k)
            if all(_norm(t) in nk for t in toks) and v not in (None, ""):
                return v
    return None


CODE_KEYS = ("Code", "SecuritiesCompanyCode", "CompanyCode", "StockCode", "證券代號", "公司代號", "股票代號", "代號")
NAME_KEYS = ("Name", "CompanyName", "CompanyAbbreviation", "證券名稱", "公司名稱", "公司簡稱", "名稱")


def _code(d):
    v = _f(d, *CODE_KEYS)
    return str(v).strip() if v is not None else None


def _clean_codes(codes):
    out = []
    for c in codes or []:
        c = str(c).strip()
        if c.isalnum() and c not in out:
            out.append(c)
    if not out:
        raise ValueError("請提供至少一個股票代號，例如 ['2330','6488']")
    if len(out) > MAX_CODES:
        raise ValueError(f"一次最多 {MAX_CODES} 檔")
    return out


def _fetch_json(base, path):
    def go():
        r = _session.get(base + path, timeout=30)
        r.raise_for_status()
        return r.json()
    return _cached(base + path, DAILY_TTL, go)


def _pick(data, codes):
    want = set(_clean_codes(codes))
    return [d for d in data if _code(d) in want]


# ---------- 盤中報價（上市、上櫃） ----------
def _fetch_mis(codes):
    ex_ch = "|".join(f"{m}_{c}.tw" for c in codes for m in ("tse", "otc"))
    if not _session.cookies:
        try:
            _session.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
        except requests.RequestException:
            pass
    r = _session.get(MIS_URL, params={"ex_ch": ex_ch, "json": 1, "delay": 0,
                                      "_": int(time.time() * 1000)}, timeout=10)
    r.raise_for_status()
    # 只留真的有資料的（查不到的那一邊會回空殼）
    return [m for m in r.json().get("msgArray", []) if m.get("c")]


def _first_level(s):
    return _num(str(s).split("_")[0]) if s else None


@mcp.tool()
def get_quotes(codes: list[str]) -> list[dict]:
    """取得台股盤中即時報價（上市、上櫃皆可），約數秒延遲。興櫃請改用 get_emerging_quotes。
    codes：股票代號清單，例如 ["2330", "6488"]，一次最多 50 檔。
    回傳每檔一列：ticker, name, market, price, prev_close, change, change_pct,
    open, high, low, volume_lots(累計成交張數), quote_time。"""
    codes = _clean_codes(codes)
    raw = _cached("mis:" + ",".join(sorted(codes)), QUOTE_TTL, lambda: _fetch_mis(codes))
    rows = []
    for m in raw:
        prev = _num(m.get("y"))
        price = _num(m.get("z")) or _first_level(m.get("b")) or _first_level(m.get("a")) or prev
        chg = (price - prev) if (price is not None and prev) else None
        rows.append({
            "ticker": m.get("c"), "name": m.get("n"),
            "market": "上市" if m.get("ex") == "tse" else "上櫃",
            "price": price, "prev_close": prev,
            "change": round(chg, 2) if chg is not None else None,
            "change_pct": round(chg / prev, 5) if chg is not None else None,
            "open": _num(m.get("o")), "high": _num(m.get("h")), "low": _num(m.get("l")),
            "volume_lots": _num(m.get("v")),
            "quote_time": f"{m.get('d', '')} {m.get('t', '')}".strip(),
        })
    order = {c: i for i, c in enumerate(codes)}
    rows.sort(key=lambda x: order.get(x["ticker"], 999))
    return rows


# ---------- 盤後收盤（上市 + 上櫃） ----------
@mcp.tool()
def get_daily_close(codes: list[str]) -> list[dict]:
    """取得最近一個交易日的盤後行情，上市與上櫃皆可（證交所、櫃買中心官方 OpenAPI）。
    回傳：ticker, name, market, date(民國年), open, high, low, close, change, volume_shares, shares_issued(僅上櫃)。"""
    rows = []
    for d in _pick(_fetch_json(TWSE, "/exchangeReport/STOCK_DAY_ALL"), codes):
        rows.append({
            "ticker": _code(d), "name": _f(d, *NAME_KEYS), "market": "上市", "date": d.get("Date"),
            "open": _num(d.get("OpeningPrice")), "high": _num(d.get("HighestPrice")),
            "low": _num(d.get("LowestPrice")), "close": _num(d.get("ClosingPrice")),
            "change": _num(d.get("Change")), "volume_shares": _num(d.get("TradeVolume")),
            "shares_issued": None,
        })
    for d in _pick(_fetch_json(TPEX, TPEX_CLOSE), codes):
        rows.append({
            "ticker": _code(d), "name": _f(d, *NAME_KEYS), "market": "上櫃",
            "date": _get(d, "Date", "資料日期"),
            "open": _num(_get(d, "Open", "OpeningPrice", "開盤")),
            "high": _num(_get(d, "High", "Highest", "HighestPrice", "最高")),
            "low": _num(_get(d, "Low", "Lowest", "LowestPrice", "最低")),
            "close": _num(_get(d, "Close", "ClosingPrice", "收盤")),
            "change": _num(_get(d, "Change", "漲跌")),
            "volume_shares": _num(_get(d, "TradingShares", "TradingVolume", "TradeVolume", "成交股數")),
            "shares_issued": _num(_get(d, "IssuedShares", "NumberOfSharesIssued", "Capitals", "發行股數")),
        })
    return rows


# ---------- 評價（上市 + 上櫃） ----------
@mcp.tool()
def get_valuation(codes: list[str]) -> list[dict]:
    """取得本益比、殖利率(%)、股價淨值比，上市與上櫃皆可（每日盤後更新）。
    回傳：ticker, name, market, date(民國年), pe, dividend_yield_pct, pb。"""
    rows = []
    for d in _pick(_fetch_json(TWSE, "/exchangeReport/BWIBBU_ALL"), codes):
        rows.append({"ticker": _code(d), "name": _f(d, *NAME_KEYS), "market": "上市", "date": d.get("Date"),
                     "pe": _num(d.get("PEratio")), "dividend_yield_pct": _num(d.get("DividendYield")),
                     "pb": _num(d.get("PBratio"))})
    for d in _pick(_fetch_json(TPEX, TPEX_PE), codes):
        rows.append({"ticker": _code(d), "name": _f(d, *NAME_KEYS), "market": "上櫃",
                     "date": _get(d, "Date", "資料日期"),
                     "pe": _num(_get(d, "PriceEarningRatio", "PEratio", "PERatio", "本益比")),
                     "dividend_yield_pct": _num(_get(d, "YieldRatio", "DividendYield", "殖利率")),
                     "pb": _num(_get(d, "PriceBookRatio", "PBratio", "PBRatio", "股價淨值比"))})
    return rows


# ---------- 興櫃 ----------
@mcp.tool()
def get_emerging_quotes(codes: list[str]) -> list[dict]:
    """取得興櫃股票的當日行情（櫃買中心官方 OpenAPI，每日更新，非盤中逐筆）。
    回傳：ticker, name, market, date, price(最後成交), prev_avg(前日均價), avg(當日均價),
    high, low, bid, ask, change_vs_prev_avg, volume_shares。"""
    rows = []
    for d in _pick(_fetch_json(TPEX, TPEX_ESB), codes):
        price = _num(_get(d, "LatestPrice", "LastPrice", "Close", "TransactionPrice", "成交", "最後成交價"))
        prev_avg = _num(_get(d, "PreviousAveragePrice", "PreviousDayAveragePrice", "前日均價"))
        rows.append({
            "ticker": _code(d), "name": _f(d, *NAME_KEYS), "market": "興櫃",
            "date": _get(d, "Date", "資料日期"),
            "price": price, "prev_avg": prev_avg,
            "avg": _num(_get(d, "Average", "DailyAveragePrice", "AveragePrice", "日均價")),
            "high": _num(_get(d, "Highest", "High", "最高")),
            "low": _num(_get(d, "Lowest", "Low", "最低")),
            "bid": _num(_get(d, "BuyingPrice", "LatestBidPrice", "BidPrice", "PurchasePrice", "買進價")),
            "ask": _num(_get(d, "SellingPrice", "LatestAskPrice", "LatesAskPrice", "AskPrice", "SellingPrice", "賣出價")),
            "change_vs_prev_avg": round(price / prev_avg - 1, 5) if (price and prev_avg) else None,
            "volume_shares": _num(_get(d, "TransactionVolume", "TradingVolume", "TradingShares", "成交量")),
        })
    return rows


# ---------- 三大法人、月營收、財報 ----------
TWSE_REV = ["/opendata/t187ap05_L"]
TPEX_REV = ["/mopsfin_t187ap05_O"]
TWSE_EPS = ["/opendata/t187ap14_L"]
TPEX_EPS = ["/mopsfin_t187ap14_O"]
IND = ["ci", "basi", "bd", "fh", "ins", "mim"]   # 一般業、銀行、證券期貨、金控、保險、異業
TWSE_IS = [f"/opendata/t187ap06_L_{x}" for x in IND]
TPEX_IS = [f"/mopsfin_t187ap06_O_{x}" for x in IND]
TWSE_BS = [f"/opendata/t187ap07_L_{x}" for x in IND]
TPEX_BS = [f"/mopsfin_t187ap07_O_{x}" for x in IND]
TPEX_INST = ["/tpex_3insti_daily_trading"]


def _multi(base, paths):
    """依序抓多個來源合併，抓不到的略過（例如某產業別沒有資料）。"""
    rows = []
    for p in paths:
        key = "fail:" + base + p
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < DAILY_TTL:
            continue   # 最近抓過失敗（例如這個產業別沒有資料），先跳過
        try:
            data = _fetch_json(base, p)
            if isinstance(data, list):
                rows.extend(data)
        except Exception:  # noqa: BLE001
            _cache[key] = (time.time(), None)
    return rows


def _index(rows):
    out = {}
    for d in rows:
        c = _code(d)
        if c:
            out[c] = d
    return out


def _lots(x):
    v = _num(x)
    return round(v / 1000) if v is not None else None


def _twse_t86():
    """證交所三大法人買賣超（個股），往前找最近一個有資料的交易日。"""
    def go():
        day = datetime.now(TW_TZ)
        for _ in range(10):
            ds = day.strftime("%Y%m%d")
            try:
                r = _session.get("https://www.twse.com.tw/rwd/zh/fund/T86",
                                 params={"date": ds, "selectType": "ALLBUT0999", "response": "json"}, timeout=30)
                j = r.json()
                if j.get("stat") == "OK" and j.get("data"):
                    f = j["fields"]
                    return [dict(zip(f, row), _date=ds) for row in j["data"]]
            except Exception:  # noqa: BLE001
                pass
            day -= timedelta(days=1)
        return []
    return _cached("t86", DAILY_TTL, go)


def _inst_row(d, market):
    if market == "上市":
        f1 = _num(_f(d, "外陸資買賣超股數(不含外資自營商)")) or 0
        f2 = _num(_f(d, "外資自營商買賣超股數")) or 0
        foreign = f1 + f2
        trust = _num(_f(d, "投信買賣超股數"))
        dealer = _num(_f(d, "自營商買賣超股數"))
        total = _num(_f(d, "三大法人買賣超股數"))
        date = d.get("_date")
    else:
        foreign = _num(_f(d, "ForeignInvestorsIncludeMainlandAreaInvestors-Difference", "外資及陸資-買賣超股數"))
        trust = _num(_f(d, "SecuritiesInvestmentTrustCompanies-Difference", "投信-買賣超股數"))
        dealer = _num(_f(d, "Dealers-Difference", "自營商-買賣超股數"))
        total = _num(_f(d, "TotalDifference", "三大法人買賣超股數合計"))
        date = _f(d, "Date", "資料日期")
    return {"foreign_lots": _lots(foreign), "trust_lots": _lots(trust),
            "dealer_lots": _lots(dealer), "total_lots": _lots(total), "inst_date": date}


@mcp.tool()
def get_institutional(codes: list[str]) -> list[dict]:
    """取得三大法人買賣超（最近一個交易日，單位：張），上市與上櫃皆可。
    回傳：ticker, name, market, inst_date, foreign_lots(外資), trust_lots(投信), dealer_lots(自營商), total_lots(合計)。
    證交所通常在下午 4 點半後公布當日資料。"""
    want = _clean_codes(codes)
    rows = []
    tw = _index(_twse_t86())
    tp = _index(_multi(TPEX, TPEX_INST))
    for c in want:
        if c in tw:
            rows.append({"ticker": c, "name": str(_f(tw[c], *NAME_KEYS) or "").strip(), "market": "上市", **_inst_row(tw[c], "上市")})
        elif c in tp:
            rows.append({"ticker": c, "name": _f(tp[c], *NAME_KEYS), "market": "上櫃", **_inst_row(tp[c], "上櫃")})
    return rows


def _rev_row(d):
    return {
        "industry": _f(d, "產業別"),
        "rev_month": _f(d, "資料年月"),
        "revenue_k": _num(_f(d, "營業收入-當月營收")),
        "rev_mom_pct": _num(_f(d, "營業收入-上月比較增減(%)", "營業收入-上月比較增減")),
        "rev_yoy_pct": _num(_f(d, "營業收入-去年同月增減(%)", "營業收入-去年同月增減")),
        "rev_ytd_k": _num(_f(d, "累計營業收入-當月累計營收")),
        "rev_ytd_yoy_pct": _num(_f(d, "累計營業收入-前期比較增減(%)", "累計營業收入-前期比較增減")),
    }


@mcp.tool()
def get_monthly_revenue(codes: list[str]) -> list[dict]:
    """取得最新一個月的月營收（上市、上櫃），金額單位：千元。
    回傳：ticker, name, market, industry, rev_month(民國年月), revenue_k, rev_mom_pct(月增%), rev_yoy_pct(年增%),
    rev_ytd_k(累計營收), rev_ytd_yoy_pct(累計年增%)。"""
    want = _clean_codes(codes)
    tw, tp = _index(_multi(TWSE, TWSE_REV)), _index(_multi(TPEX, TPEX_REV))
    rows = []
    for c in want:
        for src, mkt in ((tw, "上市"), (tp, "上櫃")):
            if c in src:
                rows.append({"ticker": c, "name": _f(src[c], *NAME_KEYS), "market": mkt, **_rev_row(src[c])})
                break
    return rows


def _is_row(d):
    return {
        "fin_year": _f(d, "年度", "Year"), "fin_quarter": _f(d, "季別", "Season"),
        "revenue_k": _num(_f(d, "營業收入", "收益", "淨收益", "利息淨收益")),
        "gross_profit_k": _num(_f(d, "營業毛利(毛損)淨額", "營業毛利(毛損)", "營業毛利")),
        "operating_income_k": _num(_f(d, "營業利益(損失)", "營業利益")),
        "pretax_income_k": _num(_f(d, "稅前淨利(淨損)", "繼續營業單位稅前淨利(淨損)", "稅前淨利")),
        "net_income_k": _num(_f(d, "淨利(淨損)歸屬於母公司業主", "淨利(損)歸屬於母公司業主", "本期淨利(淨損)", "本期稅後淨利(淨損)")),
        "eps": _num(_f(d, "基本每股盈餘(元)", "基本每股盈餘", "每股盈餘")),
    }


def _bs_row(d):
    return {
        "total_assets_k": _num(_f(d, "資產總額", "資產總計")),
        "total_liabilities_k": _num(_f(d, "負債總額", "負債總計")),
        "equity_parent_k": _num(_f(d, "歸屬於母公司業主之權益合計", "權益總額", "權益總計")),
        "book_value_per_share": _num(_f(d, "每股參考淨值")),
    }


@mcp.tool()
def get_financials(codes: list[str]) -> list[dict]:
    """取得最新一期季報（財報）重點，上市與上櫃皆可，金額單位：千元。
    損益數字為『當年度累計』：第 1 季=1-3 月、第 3 季=1-9 月、第 4 季=全年（即年報數字）。
    回傳：ticker, name, market, fin_year, fin_quarter, revenue_k, gross_profit_k, operating_income_k, pretax_income_k,
    net_income_k, eps, gross_margin, operating_margin, net_margin, total_assets_k, total_liabilities_k,
    equity_parent_k, debt_ratio, roe_ytd, book_value_per_share。"""
    want = _clean_codes(codes)
    srcs = (("上市", _index(_multi(TWSE, TWSE_IS)), _index(_multi(TWSE, TWSE_BS))),
            ("上櫃", _index(_multi(TPEX, TPEX_IS)), _index(_multi(TPEX, TPEX_BS))))
    rows = []
    for c in want:
        for mkt, inc, bal in srcs:
            if c in inc or c in bal:
                r = {"ticker": c, "name": _f((inc.get(c) or bal.get(c)), *NAME_KEYS), "market": mkt}
                r.update(_is_row(inc[c]) if c in inc else _is_row({}))
                r.update(_bs_row(bal[c]) if c in bal else _bs_row({}))
                rev, gp, op, ni = r["revenue_k"], r["gross_profit_k"], r["operating_income_k"], r["net_income_k"]
                ta, tl, eq = r["total_assets_k"], r["total_liabilities_k"], r["equity_parent_k"]
                r["gross_margin"] = round(gp / rev, 4) if (gp is not None and rev) else None
                r["operating_margin"] = round(op / rev, 4) if (op is not None and rev) else None
                r["net_margin"] = round(ni / rev, 4) if (ni is not None and rev) else None
                r["debt_ratio"] = round(tl / ta, 4) if (tl is not None and ta) else None
                r["roe_ytd"] = round(ni / eq, 4) if (ni is not None and eq) else None
                rows.append(r)
                break
    return rows


# ---------- 選股器用：全市場一張表 ----------
@mcp.tool()
def get_screener_universe(market: str = "all") -> list[dict]:
    """選股器用：一次取得全市場上市、上櫃普通股（不含 ETF、權證）的重點指標，一檔一列。
    market：all、上市、上櫃。
    回傳：ticker, name, market, industry, close, change_pct, pe, dividend_yield_pct, pb,
    rev_month, rev_yoy_pct, rev_mom_pct, rev_ytd_yoy_pct, fin_period, eps_ytd,
    foreign_lots, trust_lots, dealer_lots, total_lots。第一次呼叫要抓很多資料，可能需要一兩分鐘。"""
    stock = re.compile(r"^[1-9]\d{3}$")
    out = {}

    def base(c, name, mkt):
        return out.setdefault(c, {"ticker": c, "name": name, "market": mkt, "industry": None, "close": None,
                                  "change_pct": None, "pe": None, "dividend_yield_pct": None, "pb": None,
                                  "rev_month": None, "rev_yoy_pct": None, "rev_mom_pct": None, "rev_ytd_yoy_pct": None,
                                  "fin_period": None, "eps_ytd": None, "foreign_lots": None, "trust_lots": None,
                                  "dealer_lots": None, "total_lots": None})

    def px(r, close, chg):
        r["close"] = close
        if close is not None and chg is not None and close - chg:
            r["change_pct"] = round(chg / (close - chg), 5)

    if market in ("all", "上市"):
        for d in _fetch_json(TWSE, "/exchangeReport/STOCK_DAY_ALL"):
            c = _code(d)
            if c and stock.match(c):
                px(base(c, _f(d, *NAME_KEYS), "上市"), _num(d.get("ClosingPrice")), _num(d.get("Change")))
    if market in ("all", "上櫃"):
        for d in _fetch_json(TPEX, TPEX_CLOSE):
            c = _code(d)
            if c and stock.match(c):
                px(base(c, _f(d, *NAME_KEYS), "上櫃"), _num(_f(d, "Close")), _num(_f(d, "Change")))

    for d in _fetch_json(TWSE, "/exchangeReport/BWIBBU_ALL") + _fetch_json(TPEX, TPEX_PE):
        c = _code(d)
        if c in out:
            out[c].update(pe=_num(_f(d, "PEratio", "PriceEarningRatio")),
                          dividend_yield_pct=_num(_f(d, "DividendYield", "YieldRatio")),
                          pb=_num(_f(d, "PBratio", "PriceBookRatio")))

    for d in _multi(TWSE, TWSE_REV) + _multi(TPEX, TPEX_REV):
        c = _code(d)
        if c in out:
            rv = _rev_row(d)
            out[c].update(industry=rv["industry"], rev_month=rv["rev_month"], rev_yoy_pct=rv["rev_yoy_pct"],
                          rev_mom_pct=rv["rev_mom_pct"], rev_ytd_yoy_pct=rv["rev_ytd_yoy_pct"])

    for d in _multi(TWSE, TWSE_IS) + _multi(TPEX, TPEX_IS):
        c = _code(d)
        if c in out:
            ir = _is_row(d)
            if ir["eps"] is not None:
                out[c].update(eps_ytd=ir["eps"], fin_period=f"{ir['fin_year']}Q{ir['fin_quarter']}")

    for d in _twse_t86():
        c = _code(d)
        if c in out:
            out[c].update({k: v for k, v in _inst_row(d, "上市").items() if k != "inst_date"})
    for d in _multi(TPEX, TPEX_INST):
        c = _code(d)
        if c in out:
            out[c].update({k: v for k, v in _inst_row(d, "上櫃").items() if k != "inst_date"})

    return sorted(out.values(), key=lambda r: r["ticker"])


# ---------- 歷史資料（GitHub data 分支，由 collector.py 每天自動更新） ----------
DATA_BASE = os.environ.get("DATA_BASE", "https://raw.githubusercontent.com/Levi56892/twse-mcp/data").rstrip("/")


def _store_csv(p, ttl):
    def go():
        r = _session.get(f"{DATA_BASE}/{p}", timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        rows = []
        for row in csv.DictReader(io.StringIO(r.content.decode("utf-8"))):
            rows.append({k: (v if k in ("date", "month", "period") else _num(v)) for k, v in row.items()})
        return rows
    return _cached("store:" + p, ttl, go)


def _store_json(p, ttl):
    def go():
        r = _session.get(f"{DATA_BASE}/{p}", timeout=30)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    return _cached("store:" + p, ttl, go)


def _stock_info(code):
    info = _store_json("stocks.json", 6 * 3600).get(code) or {}
    return info.get("name"), info.get("market")


@mcp.tool()
def get_stock_history(code: str, start: str = "", end: str = "") -> list[dict]:
    """取得個股每日股價與籌碼歷史（2015 年起，每天收盤後自動更新，資料存在 GitHub）。
    code：股票代號；start、end：YYYY-MM-DD，可不填（不填＝全部）。每個交易日一列：
    ticker, name, market, date, open, high, low, close, volume_lots(成交張數),
    foreign_lots, trust_lots, dealer_lots, total_lots(三大法人買賣超，張),
    margin_lots(融資餘額，張), short_lots(融券餘額，張), short_margin_ratio(券資比),
    sbl_lots(借券賣出餘額，張), foreign_hold_lots(外資持股，張), foreign_hold_pct(外資持股比率 %)。"""
    code = _clean_codes([code])[0]
    name, market = _stock_info(code)
    this_year = datetime.now(TW_TZ).year
    y0 = int(start[:4]) if start[:4].isdigit() else 2015
    y1 = int(end[:4]) if end[:4].isdigit() else this_year
    rows = []
    for y in range(max(2015, y0), min(this_year, y1) + 1):
        rows.extend(_store_csv(f"daily/{code}/{y}.csv", 1800 if y == this_year else 7 * 86400))
    out = []
    for r in rows:
        if (start and r["date"] < start) or (end and r["date"] > end):
            continue
        m, s = r.get("margin_lots"), r.get("short_lots")
        out.append({"ticker": code, "name": name, "market": market, **r,
                    "short_margin_ratio": round(s / m, 5) if (m and s is not None) else None})
    out.sort(key=lambda r: r["date"])
    return out


@mcp.tool()
def get_revenue_history(code: str) -> list[dict]:
    """取得個股月營收歷史（金額單位：千元），每月一列：ticker, month(YYYY-MM), revenue_k, last_month_k,
    last_year_k, mom_pct(月增%), yoy_pct(年增%), ytd_k(累計營收), ytd_last_k, ytd_yoy_pct(累計年增%)。"""
    code = _clean_codes([code])[0]
    return [{"ticker": code, **r} for r in _store_csv(f"revenue/{code}.csv", 6 * 3600)]


@mcp.tool()
def get_financials_history(code: str) -> list[dict]:
    """取得個股季報歷史（金額單位：千元；損益為當年度累計，Q4＝全年），每季一列：ticker, period(如 2026Q2),
    revenue_k, gross_profit_k, operating_income_k, pretax_income_k, net_income_k, eps,
    total_assets_k, total_liabilities_k, equity_parent_k, book_value_per_share。"""
    code = _clean_codes([code])[0]
    return [{"ticker": code, **r} for r in _store_csv(f"financials/{code}.csv", 6 * 3600)]


@mcp.tool()
def get_holder_history(code: str) -> list[dict]:
    """取得個股集保股權分散歷史（每週一筆），每週一列：ticker, date, holders(總人數),
    big400_pct(400 張以上 %), big1000_pct(1000 張以上 %), retail_pct(400 張以下 %),
    l1_pct…l15_pct(各持股分級占比 %)、l1_holders…l15_holders(各分級人數)。
    分級：1=1-999股、2=1-5張、3=5-10張、4=10-15張、5=15-20張、6=20-30張、7=30-40張、8=40-50張、
    9=50-100張、10=100-200張、11=200-400張、12=400-600張、13=600-800張、14=800-1000張、15=1000張以上。"""
    code = _clean_codes([code])[0]
    return [{"ticker": code, **r} for r in _store_csv(f"holders/{code}.csv", 6 * 3600)]


@mcp.tool()
def get_store_status() -> dict:
    """查看歷史資料庫的狀態：最後更新時間、還有多少交易日待回補。"""
    st = _store_json("status.json", 600)
    days = st.get("days", {})
    filled = sorted(d for d, v in days.items() if any(x == 1 for x in v.values()))
    return {"updated_at": st.get("updated_at"), "remaining_days": st.get("remaining_days"),
            "earliest_day": filled[0] if filled else None, "latest_day": filled[-1] if filled else None,
            "days_with_data": len(filled)}


# ---------- 檢查用 ----------
PEEK = {
    "tpex_close": (TPEX, TPEX_CLOSE), "tpex_pe": (TPEX, TPEX_PE), "tpex_esb": (TPEX, TPEX_ESB),
    "twse_rev": (TWSE, TWSE_REV[0]), "tpex_rev": (TPEX, TPEX_REV[0]),
    "twse_is": (TWSE, TWSE_IS[0]), "tpex_is": (TPEX, TPEX_IS[0]),
    "twse_bs": (TWSE, TWSE_BS[0]), "tpex_bs": (TPEX, TPEX_BS[0]),
    "twse_eps": (TWSE, TWSE_EPS[0]), "tpex_eps": (TPEX, TPEX_EPS[0]),
    "tpex_inst": (TPEX, TPEX_INST[0]),
}


# ---------- 盤中資金流（flow_recorder.py 由 GitHub Actions 每分鐘更新） ----------
FLOW_REPO = os.environ.get("FLOW_REPO", "Levi56892/twse-mcp")
FLOW_TTL = int(os.environ.get("FLOW_TTL", "75"))   # 未登入的 GitHub API 每小時 60 次，75 秒快取剛好在額度內


def _fetch_flow():
    # 先走 GitHub API（沒有 CDN 快取，最即時）；被限流時改走 raw（最多約 5 分鐘延遲）
    headers = {"Accept": "application/vnd.github.raw+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = _session.get(f"https://api.github.com/repos/{FLOW_REPO}/contents/today.json",
                         params={"ref": "flow"}, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json(), "api"
    except requests.RequestException:
        pass
    r = _session.get(f"https://raw.githubusercontent.com/{FLOW_REPO}/flow/today.json", timeout=20)
    if r.status_code == 404:
        return None, "none"
    r.raise_for_status()
    return r.json(), "raw"


@mcp.tool()
def get_fund_flow() -> dict:
    """盤中各產業主動買賣淨額（外盤−內盤金額，單位億元），由 GitHub Actions 在交易日 09:00–13:30 每 15 秒計算、每分鐘存檔。
    回傳：date, updated(最後存檔時間), last_trade_min(距 09:00 的分鐘數), covered(涵蓋檔數),
    seeded(是否有開盤後才啟動而用 CLV 估算的部分), sectors:[{name,g}],
    intensity:[最近一次各產業成交金額], hist:[{m:距 09:00 分鐘, n:[各產業累計淨額，順序同 sectors]}], source。
    只保留當天；不是交易日或還沒開始記錄時，date 會是之前的日期或回傳 empty=true。"""
    data, src = _cached("flow", FLOW_TTL, _fetch_flow)
    if not data:
        return {"empty": True, "source": src}
    return {**data, "source": src}


# ---------- 期貨（期交所 MIS，台指期等） ----------
TAIFEX_MIS = "https://mis.taifex.com.tw/futures/api/getQuoteList"


def _taifex_quotes(cid, market_type):
    body = {"MarketType": market_type, "SymbolType": "F", "KindID": "1", "CID": cid,
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}
    r = _session.post(TAIFEX_MIS, json=body, timeout=10,
                      headers={"Content-Type": "application/json", "Referer": "https://mis.taifex.com.tw/futures/"})
    r.raise_for_status()
    d = r.json()
    return ((d.get("RtData") or {}).get("QuoteList")) or []


def _fut_row(q):
    g = lambda *k: next((q.get(x) for x in k if q.get(x) not in (None, "", "-")), None)
    price, ref = _num(g("CLastPrice")), _num(g("CRefPrice"))
    return {
        "symbol": g("SymbolID"), "name": g("DispCName", "DispEName"),
        "price": price, "ref": ref,
        "change": round(price - ref, 2) if price and ref else _num(g("CDiff")),
        "change_pct": round((price - ref) / ref, 5) if price and ref else None,
        "open": _num(g("COpenPrice")), "high": _num(g("CHighPrice")), "low": _num(g("CLowPrice")),
        "volume": _num(g("CTotalVolume")), "time": g("CTime"), "date": g("CDate"),
    }


@mcp.tool()
def get_futures(cid: str = "TXF", session: str = "auto") -> dict:
    """取得期貨即時報價（期交所 MIS，約數秒延遲）。cid：TXF 台指期、MXF 小台、TMF 微台等。
    session：day 日盤、night 夜盤、auto 依台灣時間自動判斷。
    回傳 near（成交量最大的近月合約）、contracts（各月份）、session。"""
    now = datetime.now(TW_TZ)
    hm = now.hour * 100 + now.minute
    if session == "auto":
        session = "day" if 830 <= hm <= 1350 else "night"
    mt = "0" if session == "day" else "1"

    def go():
        rows = _taifex_quotes(cid, mt)
        if not rows and mt == "1":
            rows = _taifex_quotes(cid, "0")
        return rows
    try:
        rows = _cached(f"fut:{cid}:{mt}", QUOTE_TTL, go)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    contracts = [_fut_row(q) for q in rows]
    # 日盤合約代號結尾是 -F，夜盤是 -M；排除價差合約（含 /）與現貨（-S）
    contracts = [c for c in contracts if c["symbol"] and c["symbol"][-2:] in ("-F", "-M") and "/" not in c["symbol"]
                 and c["price"]]
    if not contracts and session == "night":
        rows = _cached(f"fut:{cid}:0", QUOTE_TTL, lambda: _taifex_quotes(cid, "0"))
        contracts = [c for c in (_fut_row(q) for q in rows)
                     if c["symbol"] and c["symbol"][-2:] in ("-F", "-M") and "/" not in c["symbol"] and c["price"]]
        session = "day"
    near = max(contracts, key=lambda c: c["volume"] or 0) if contracts else None
    out = {"session": session, "near": near, "contracts": contracts[:6]}
    if not contracts:
        out["raw_sample"] = rows[:2]
    return out


# ---------- 加權指數盤中走勢（證交所每 5 秒指數統計） ----------
@mcp.tool()
def get_index_intraday(date: str = "") -> dict:
    """取得某交易日加權指數的盤中走勢（證交所「每 5 秒指數統計」，整理成每分鐘一點）。
    date：YYYYMMDD，不填為今天（台灣時間）。盤中約有數分鐘延遲，收盤後完整。
    回傳：date, t00:[[距 09:00 的分鐘數, 指數], ...]。"""
    d = date or datetime.now(TW_TZ).strftime("%Y%m%d")

    def go():
        r = _session.get("https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX",
                         params={"date": d, "response": "json"}, timeout=20)
        r.raise_for_status()
        return r.json()
    try:
        js = _cached("mi5:" + d, 60, go)
    except Exception as e:  # noqa: BLE001
        return {"date": d, "t00": [], "error": f"{type(e).__name__}: {e}"}
    fields = js.get("fields") or []
    col = next((i for i, f in enumerate(fields) if "加權" in str(f)), 1)
    pts = {}
    for row in js.get("data") or []:
        try:
            hh, mm, ss = (int(x) for x in str(row[0]).split(":"))
            v = _num(row[col])
        except (ValueError, IndexError):
            continue
        if v:
            m = hh * 60 + mm - 540
            pts[m] = v          # 同一分鐘取最後一筆
    return {"date": d, "t00": [[m, pts[m]] for m in sorted(pts)]}


# ---------- 台指期夜盤走勢（night_recorder.py 由 GitHub Actions 每分鐘更新） ----------
def _fetch_night():
    headers = {"Accept": "application/vnd.github.raw+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = _session.get(f"https://api.github.com/repos/{FLOW_REPO}/contents/night.json",
                     params={"ref": "night"}, headers=headers, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_night_futures() -> dict:
    """台指期夜盤（15:00～隔天 05:00）的盤中走勢，由 GitHub Actions 每 30 秒記錄、每分鐘存檔。
    回傳：session（夜盤所屬交易日）、symbol、ref、updated、
    tx:[[距該交易日 09:00 的分鐘數, 價格], ...]（15:00＝360，午夜＝900，05:00＝1200）。"""
    try:
        data = _cached("night", FLOW_TTL, _fetch_night)
    except Exception as e:  # noqa: BLE001
        return {"empty": True, "error": f"{type(e).__name__}: {e}"}
    return data or {"empty": True}


# ---------- 美股（us_recorder.py 由 GitHub Actions 每分鐘更新；即時報價走 Finnhub） ----------
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()


def _fetch_us():
    headers = {"Accept": "application/vnd.github.raw+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = _session.get(f"https://api.github.com/repos/{FLOW_REPO}/contents/us.json",
                         params={"ref": "us"}, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    r = _session.get(f"https://raw.githubusercontent.com/{FLOW_REPO}/us/us.json", timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_us_war_room() -> dict:
    """美股戰情室資料（由 GitHub Actions 在美股交易日紐約時間 08:00–16:15 每分鐘記錄，只保留最近一個交易日）。
    回傳：date(紐約日期), updated(紐約時間), updated_ts(UNIX 秒), phase(pre 盤前/reg 盤中/post 盤後), tz_offset(紐約對 UTC 時差),
    quotes:{代號:{p 現價, pc 昨收, o, h, l}}（指數／類股／債券美元原油黃金 ETF、80 檔大型股、BTC），
    series:{代號:[[m, 價格]...]}（m＝距紐約 09:30 的分鐘數，盤前為負），
    sectors:[板塊名稱], flow:[{m, n:[各板塊累計主動買賣淨額，百萬美元]}], intensity:[最近一分鐘各板塊成交金額],
    stocks:{代號:{p, buy, sell, net, dv(成交金額), h, l}}（百萬美元，正規時段 tick rule 估算），
    mcap:{代號:市值(百萬美元)}, names:{代號:中文名}, sector_of:{代號:板塊},
    macro:{FRED 序列:{name, date, v, prev, hist}}（VIX、10Y、2Y、美元廣義指數、WTI、美元兌台幣，每日資料）。"""
    try:
        data = _cached("us", FLOW_TTL, _fetch_us)
    except Exception as e:  # noqa: BLE001
        return {"empty": True, "error": f"{type(e).__name__}: {e}"}
    return data or {"empty": True}


@mcp.tool()
def get_us_quotes(symbols: list[str]) -> list[dict]:
    """美股即時報價（Finnhub 官方 API，需在伺服器設定 FINNHUB_API_KEY），一次最多 15 檔，快取 15 秒。
    symbols：美股代號，例如 ["NVDA", "TSM", "SPY"]。
    回傳每檔一列：symbol, price, prev_close, change, change_pct, open, high, low, time(UNIX 秒)。"""
    if not FINNHUB_KEY:
        raise ValueError("伺服器還沒設定 FINNHUB_API_KEY（Render → Environment）")
    syms = []
    for s in symbols or []:
        s = str(s).strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) and s not in syms:
            syms.append(s)
    if not syms:
        raise ValueError("請提供至少一個美股代號，例如 ['NVDA','TSM']")
    if len(syms) > 15:
        raise ValueError("一次最多 15 檔")
    rows = []
    for s in syms:
        def go(s=s):
            r = _session.get("https://finnhub.io/api/v1/quote", params={"symbol": s, "token": FINNHUB_KEY}, timeout=10)
            r.raise_for_status()
            return r.json()
        try:
            q = _cached("usq:" + s, QUOTE_TTL, go)
        except Exception as e:  # noqa: BLE001
            rows.append({"symbol": s, "error": f"{type(e).__name__}"})
            continue
        if not q or not q.get("c"):
            rows.append({"symbol": s, "error": "查無資料"})
            continue
        pc = q.get("pc")
        rows.append({"symbol": s, "price": q["c"], "prev_close": pc,
                     "change": q.get("d"), "change_pct": round(q["c"] / pc - 1, 5) if pc else None,
                     "open": q.get("o"), "high": q.get("h"), "low": q.get("l"), "time": q.get("t")})
    return rows


@mcp.tool()
def peek_source(source: str, code: str = "") -> dict:
    """檢查用：看某個資料來源的原始欄位。
    source 可填：tpex_close、tpex_pe、tpex_esb、twse_rev、tpex_rev、twse_is、tpex_is、
    twse_bs、tpex_bs、twse_eps、tpex_eps、tpex_inst、twse_inst。code 可指定代號，不填就回第一筆。"""
    try:
        if source == "twse_inst":
            data = _twse_t86()
        elif source in PEEK:
            data = _fetch_json(*PEEK[source])
        else:
            return {"error": "未知的 source"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list):
        return {"error": "回傳不是列表", "raw": str(data)[:500]}
    row = next((d for d in data if not code or _code(d) == code), data[0] if data else None)
    return {"rows": len(data), "sample": row}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
