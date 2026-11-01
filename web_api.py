"""
給「網頁 APP」用的資料出口：一般網頁用 fetch 就能讀，不必經過 claude.ai 的連接器。

網址格式：GET https://twse-quotes.onrender.com/api/<工具名稱>?a=<JSON 參數>
  例：/api/get_quotes?a={"codes":["t00","o00"]}
回傳：工具原本的回傳內容（JSON）。出錯時回 {"error": "..."}。

只開放「唯讀」的查詢工具；start_recorder 這類會改變伺服器狀態的工具不開放。
允許哪些網站來讀：環境變數 WEB_ORIGINS（逗號分隔，例：https://gubao.pages.dev），沒設就全部允許。
"""
import json
import math
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _clean(x):
    """NaN／無限大換成 null（瀏覽器的 JSON 讀不了 NaN）；其他看不懂的型別轉成文字。"""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if x is None or isinstance(x, (str, int, bool)):
        return x
    if hasattr(x, "model_dump"):
        return _clean(x.model_dump())
    return str(x)

ALLOWED = {
    "get_quotes", "get_futures", "get_night_futures", "get_fund_flow", "get_index_intraday", "get_kline",
    "get_daily_close", "get_valuation", "get_emerging_quotes", "get_institutional", "get_monthly_revenue",
    "get_financials", "get_screener_universe", "get_stock_history", "get_revenue_history",
    "get_financials_history", "get_holder_history", "get_us_war_room", "get_us_quotes", "get_store_status",
}
ORIGINS = [o.strip() for o in os.environ.get("WEB_ORIGINS", "").split(",") if o.strip()]


def _cors(request):
    origin = request.headers.get("origin", "")
    allow = "*" if not ORIGINS else (origin if origin in ORIGINS else ORIGINS[0])
    return {"Access-Control-Allow-Origin": allow, "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*", "Access-Control-Max-Age": "86400",
            "Cache-Control": "no-store", "Vary": "Origin"}


def register(mcp, ns):
    """ns：server.py 的 globals()，用名稱找到各個工具函式（已經是丟到背景執行緒跑的版本）。"""

    @mcp.custom_route("/api/{tool}", methods=["GET", "OPTIONS"])
    async def api(request: Request):
        h = _cors(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=h)
        name = request.path_params.get("tool", "")
        if name == "health":
            return JSONResponse({"ok": True}, headers=h)
        fn = ns.get(name)
        if name not in ALLOWED or fn is None:
            return JSONResponse({"error": f"沒有這個工具：{name}"}, status_code=404, headers=h)
        try:
            args = json.loads(request.query_params.get("a") or "{}")
            if not isinstance(args, dict):
                raise ValueError("參數要是 JSON 物件")
        except ValueError as e:
            return JSONResponse({"error": f"參數格式錯誤：{e}"}, status_code=400, headers=h)
        try:
            out = fn(**args)
            if hasattr(out, "__await__"):
                out = await out
        except TypeError as e:
            return JSONResponse({"error": f"參數不對：{e}"}, status_code=400, headers=h)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500, headers=h)
        return JSONResponse(_clean(out), headers=h)

    print("網頁資料出口 /api/<工具> 已啟用", flush=True)
