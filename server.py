"""
台股報價 MCP 連接器
資料來源：
  - 盤中報價：證交所「基本市況報導」查詢接口（mis.twse.com.tw，約數秒延遲，非官方 API）
  - 盤後資料：證交所 OpenAPI（openapi.twse.com.tw，官方、免費）
回傳格式一律是「列」(list of dict)，讓看板可以直接當表格使用。
"""
import os
import time
import requests
from mcp.server.fastmcp import FastMCP

PORT = int(os.environ.get("PORT", "8000"))
mcp = FastMCP("twse-quotes", host="0.0.0.0", port=PORT, stateless_http=True)

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
OPENAPI = "https://openapi.twse.com.tw/v1"
UA = {"User-Agent": "Mozilla/5.0 (twse-quotes-mcp)"}
QUOTE_TTL = int(os.environ.get("QUOTE_TTL", "15"))      # 盤中報價快取秒數，避免打太兇
DAILY_TTL = int(os.environ.get("DAILY_TTL", "1800"))    # 盤後資料快取秒數
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
    """把 '1,234.5' / '-' / '' 轉成數字或 None。"""
    if x is None:
        return None
    s = str(x).replace(",", "").strip()
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean_codes(codes):
    out = []
    for c in codes:
        c = str(c).strip()
        if c.isalnum() and c not in out:
            out.append(c)
    if not out:
        raise ValueError("請提供至少一個股票代號，例如 ['2330','2317']")
    if len(out) > MAX_CODES:
        raise ValueError(f"一次最多 {MAX_CODES} 檔")
    return out


# ---------- 盤中報價 ----------
def _fetch_mis(codes):
    # 同時查上市(tse)與上櫃(otc)，查不到的那邊會被略過
    ex_ch = "|".join(f"{m}_{c}.tw" for c in codes for m in ("tse", "otc"))
    if not _session.cookies:
        try:  # 先拿一次首頁 cookie，部分時段沒有會被擋
            _session.get("https://mis.twse.com.tw/stock/index.jsp", timeout=10)
        except requests.RequestException:
            pass
    r = _session.get(MIS_URL, params={"ex_ch": ex_ch, "json": 1, "delay": 0,
                                      "_": int(time.time() * 1000)}, timeout=10)
    r.raise_for_status()
    return r.json().get("msgArray", [])


def _first_level(s):
    if not s:
        return None
    return _num(str(s).split("_")[0])


@mcp.tool()
def get_quotes(codes: list[str]) -> list[dict]:
    """取得台股盤中即時報價（上市、上櫃皆可），約數秒延遲。
    codes：股票代號清單，例如 ["2330", "2317", "3443"]，一次最多 50 檔。
    回傳每檔一列：ticker, name, market, price, prev_close, change, change_pct,
    open, high, low, volume_lots(累計成交張數), quote_time。
    price 在當下沒有成交時，會用最佳買價、再不行用昨收。"""
    codes = _clean_codes(codes)
    key = "mis:" + ",".join(sorted(codes))
    raw = _cached(key, QUOTE_TTL, lambda: _fetch_mis(codes))
    rows = []
    for m in raw:
        prev = _num(m.get("y"))
        price = _num(m.get("z")) or _first_level(m.get("b")) or _first_level(m.get("a")) or prev
        chg = (price - prev) if (price is not None and prev) else None
        rows.append({
            "ticker": m.get("c"),
            "name": m.get("n"),
            "market": "上市" if m.get("ex") == "tse" else "上櫃",
            "price": price,
            "prev_close": prev,
            "change": round(chg, 2) if chg is not None else None,
            "change_pct": round(chg / prev, 5) if chg is not None else None,
            "open": _num(m.get("o")),
            "high": _num(m.get("h")),
            "low": _num(m.get("l")),
            "volume_lots": _num(m.get("v")),
            "quote_time": f"{m.get('d', '')} {m.get('t', '')}".strip(),
        })
    order = {c: i for i, c in enumerate(codes)}
    rows.sort(key=lambda x: order.get(x["ticker"], 999))
    return rows


# ---------- 盤後資料（官方 OpenAPI） ----------
def _openapi(path):
    return _cached(path, DAILY_TTL,
                   lambda: _session.get(OPENAPI + path, timeout=30).json())


def _pick(data, codes):
    want = set(_clean_codes(codes)) if codes else None
    return [d for d in data if want is None or d.get("Code") in want]


@mcp.tool()
def get_daily_close(codes: list[str]) -> list[dict]:
    """取得上市股票最近一個交易日的盤後行情（證交所官方 OpenAPI）。
    回傳：ticker, name, date(民國年), open, high, low, close, change, volume_shares, value_ntd。"""
    rows = []
    for d in _pick(_openapi("/exchangeReport/STOCK_DAY_ALL"), codes):
        rows.append({
            "ticker": d.get("Code"), "name": d.get("Name"), "date": d.get("Date"),
            "open": _num(d.get("OpeningPrice")), "high": _num(d.get("HighestPrice")),
            "low": _num(d.get("LowestPrice")), "close": _num(d.get("ClosingPrice")),
            "change": _num(d.get("Change")), "volume_shares": _num(d.get("TradeVolume")),
            "value_ntd": _num(d.get("TradeValue")),
        })
    return rows


@mcp.tool()
def get_valuation(codes: list[str]) -> list[dict]:
    """取得上市股票的本益比、殖利率(%)、股價淨值比（證交所官方 OpenAPI，每日更新）。
    回傳：ticker, name, date(民國年), pe, dividend_yield_pct, pb。"""
    rows = []
    for d in _pick(_openapi("/exchangeReport/BWIBBU_ALL"), codes):
        rows.append({
            "ticker": d.get("Code"), "name": d.get("Name"), "date": d.get("Date"),
            "pe": _num(d.get("PEratio")), "dividend_yield_pct": _num(d.get("DividendYield")),
            "pb": _num(d.get("PBratio")),
        })
    return rows


if __name__ == "__main__":
    # 連接器網址會是  https://<你的主機>/mcp
    mcp.run(transport="streamable-http")
