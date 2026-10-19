"""
台股報價 MCP 連接器（上市、上櫃、興櫃）
資料來源：
  - 盤中報價（上市、上櫃）：證交所「基本市況報導」查詢接口 mis.twse.com.tw（約數秒延遲，非官方 API）
  - 盤後資料（上市）：證交所 OpenAPI  openapi.twse.com.tw
  - 盤後資料（上櫃、興櫃）：櫃買中心 OpenAPI  www.tpex.org.tw/openapi
回傳格式一律是「列」(list of dict)，讓看板可以直接當表格使用。
"""
import os
import time
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


CODE_KEYS = ("Code", "SecuritiesCompanyCode", "CompanyCode", "StockCode", "證券代號", "公司代號", "代號")
NAME_KEYS = ("Name", "CompanyName", "CompanyAbbreviation", "證券名稱", "公司名稱", "名稱")


def _code(d):
    v = _get(d, *CODE_KEYS)
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
            "ticker": _code(d), "name": _get(d, *NAME_KEYS), "market": "上市", "date": d.get("Date"),
            "open": _num(d.get("OpeningPrice")), "high": _num(d.get("HighestPrice")),
            "low": _num(d.get("LowestPrice")), "close": _num(d.get("ClosingPrice")),
            "change": _num(d.get("Change")), "volume_shares": _num(d.get("TradeVolume")),
            "shares_issued": None,
        })
    for d in _pick(_fetch_json(TPEX, TPEX_CLOSE), codes):
        rows.append({
            "ticker": _code(d), "name": _get(d, *NAME_KEYS), "market": "上櫃",
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
        rows.append({"ticker": _code(d), "name": _get(d, *NAME_KEYS), "market": "上市", "date": d.get("Date"),
                     "pe": _num(d.get("PEratio")), "dividend_yield_pct": _num(d.get("DividendYield")),
                     "pb": _num(d.get("PBratio"))})
    for d in _pick(_fetch_json(TPEX, TPEX_PE), codes):
        rows.append({"ticker": _code(d), "name": _get(d, *NAME_KEYS), "market": "上櫃",
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
            "ticker": _code(d), "name": _get(d, *NAME_KEYS), "market": "興櫃",
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


# ---------- 檢查用 ----------
@mcp.tool()
def peek_source(source: str, code: str = "") -> dict:
    """檢查用：看某個資料來源的原始欄位。source 填 tpex_close、tpex_pe 或 tpex_esb；
    code 可指定代號，不填就回第一筆。"""
    path = {"tpex_close": TPEX_CLOSE, "tpex_pe": TPEX_PE, "tpex_esb": TPEX_ESB}.get(source)
    if not path:
        return {"error": "source 只能是 tpex_close、tpex_pe、tpex_esb"}
    try:
        data = _fetch_json(TPEX, path)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    row = next((d for d in data if not code or _code(d) == code), data[0] if data else None)
    return {"rows": len(data), "sample": row}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
