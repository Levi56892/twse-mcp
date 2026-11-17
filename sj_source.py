"""
永豐金 Shioaji 報價來源（取代證交所 MIS，避免 Render 被證交所封鎖）
- 金鑰放 Render Environment：SJ_API_KEY、SJ_SECRET_KEY（只開「行情／資料＋正式環境」，不開交易）
- 用 api.snapshots() 查快照：每次最多 500 檔，全市場約 4 次；永豐金限制 50 次／10 秒，我們每分鐘只查約 5 次
- 把快照轉成跟證交所 MIS 一樣的欄位（c, n, z, v, a, b, y, h, l, u, w, d, t, ex），記錄器其他邏輯不用改
"""
import os
import threading
import time
from datetime import datetime, timezone

API_KEY = os.environ.get("SJ_API_KEY", "")
SECRET = os.environ.get("SJ_SECRET_KEY", "")
ENABLED = bool(API_KEY and SECRET) and os.environ.get("FLOW_SOURCE", "sj") != "mis"

_lock = threading.Lock()
_api = None
_last_call = [0.0]
GAP = 1.0          # 兩次快照請求之間至少 1 秒（上限是 10 秒 50 次，我們遠低於此）
CHUNK = 500        # 每次最多 500 檔（永豐金上限）
INDEX = {"t00": ("TSE", "001"), "o00": ("OTC", "101")}   # 加權指數、櫃買指數


def _log(*a):
    try:
        import flow_recorder
        flow_recorder.log("[永豐]", *a)
    except Exception:  # noqa: BLE001
        print("[永豐]", *a, flush=True)


def api():
    """登入一次，之後共用同一條連線（永豐金每人最多 5 條連線、每天最多登入 1000 次）。"""
    global _api
    with _lock:
        if _api is not None:
            return _api
        import shioaji as sj
        a = sj.Shioaji(simulation=False)
        a.login(api_key=API_KEY, secret_key=SECRET, subscribe_trade=False)
        _api = a
        _log("登入成功", getattr(sj, "__version__", ""))
        return _api


def reset():
    """連線出問題時：登出，下次再重新登入。"""
    global _api
    with _lock:
        if _api is not None:
            try:
                _api.logout()
            except Exception:  # noqa: BLE001
                pass
        _api = None


def usage_mb():
    """今天已用流量（MB）與上限（MB）。"""
    try:
        u = api().usage()
        return round(u.bytes / 1048576, 1), round(u.limit_bytes / 1048576, 1)
    except Exception:  # noqa: BLE001
        return None, None


def _wait():
    d = _last_call[0] + GAP - time.time()
    if d > 0:
        time.sleep(d)
    _last_call[0] = time.time()


def _contract(key):
    """key：'2330'、或 't00'／'o00'（指數）。找不到回傳 None。"""
    a = api()
    try:
        if key in INDEX:
            ex, code = INDEX[key]
            return getattr(a.Contracts.Indexs, ex)[code]
        return a.Contracts.Stocks[key]
    except Exception:  # noqa: BLE001
        return None


def _p(x):
    try:
        x = float(x)
        return f"{x:.4f}" if x > 0 else "-"
    except (TypeError, ValueError):
        return "-"


def _to_mis(s, c, key):
    """一筆永豐快照 → 證交所 MIS 格式的 dict。"""
    ts = datetime.fromtimestamp(s.ts / 1e9, tz=timezone.utc)   # 永豐的 ts 是「台灣時間當成 UTC」
    close = float(s.close or 0)
    ref = float(getattr(c, "reference", 0) or 0)
    y = ref or (close - float(s.change_price or 0) if close else 0)
    is_idx = key in INDEX
    return {
        "c": key,
        "n": getattr(c, "name", key),
        "ex": "otc" if str(getattr(c, "exchange", "")).upper().endswith("OTC") else "tse",
        "z": _p(close) if (s.total_volume or is_idx) else "-",
        "v": str(int(s.total_volume or 0)),            # 累計成交量（張）
        "a": _p(s.sell_price) + "_",
        "b": _p(s.buy_price) + "_",
        "y": _p(y),
        "h": _p(s.high), "l": _p(s.low), "o": _p(s.open),
        "u": _p(getattr(c, "limit_up", 0)), "w": _p(getattr(c, "limit_down", 0)),
        "d": ts.strftime("%Y%m%d"),
        "t": ts.strftime("%H:%M:%S"),
    }


def fetch(ex_list):
    """ex_list：['tse_2330.tw', 'otc_o00.tw', ...]（跟 MIS 一樣的寫法）→ MIS 格式的 rows；整批失敗回傳 None。"""
    keys = [e.split("_", 1)[1].rsplit(".", 1)[0] for e in ex_list]
    pairs = [(k, _contract(k)) for k in keys]
    pairs = [(k, c) for k, c in pairs if c is not None]
    out, ok, fail = [], 0, 0
    for i in range(0, len(pairs), CHUNK):
        part = pairs[i:i + CHUNK]
        _wait()
        try:
            snaps = api().snapshots([c for _, c in part])
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            _log("快照失敗", type(e).__name__, str(e)[:120])
            if fail >= 2:
                reset()
                break
            continue
        by = {s.code: s for s in snaps}
        for k, c in part:
            s = by.get(getattr(c, "code", k))
            if s is not None:
                try:
                    out.append(_to_mis(s, c, k))
                except Exception as e:  # noqa: BLE001
                    _log("轉換失敗", k, type(e).__name__, e)
    return out, ok, fail
