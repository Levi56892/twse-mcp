"""
台指期夜盤記錄器（給「台股戰情室」用）
夜盤 15:00 到隔天 05:00，共 14 小時；GitHub Actions 一次最多跑 6 小時，
所以排了三段（14:55、20:50、02:45 啟動），每段開始時先讀回 night 分支上已記錄的部分再接著記。
每 30 秒抓一次期交所夜盤報價，每分鐘存到 night 分支的 night.json（只保留最新一個夜盤）。

時間座標：距「夜盤所屬交易日 09:00」的分鐘數，15:00＝360，午夜＝900，05:00＝1200，
跟日盤的分鐘數同一把尺，頁面可以接在日盤後面畫。
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests

TW = timezone(timedelta(hours=8))
URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
POLL_SEC = 30
PUSH_SEC = 60
MAX_RUN_SEC = 5 * 3600 + 50 * 60      # 每段最多 5 小時 50 分
OUT_DIR = os.environ.get("NIGHT_DIR", "nightout")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (twse-night-recorder)", "Referer": "https://mis.taifex.com.tw/futures/"})


def log(*a):
    print(datetime.now(TW).strftime("%H:%M:%S"), *a, flush=True)


def num(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def session_date(now):
    """夜盤屬於開盤那天：15:00 以後算今天，凌晨算前一天。"""
    return (now if now.hour >= 12 else now - timedelta(days=1)).strftime("%Y-%m-%d")


def in_night(now):
    hm = now.hour * 100 + now.minute
    return hm >= 1450 or hm <= 505


def unified_min(base_date, hhmmss, now):
    """期交所時間 HHMMSS → 距 base_date 09:00 的分鐘數。"""
    t = str(hhmmss or "").zfill(6)
    try:
        h, m, s = int(t[:2]), int(t[2:4]), int(t[4:6])
    except ValueError:
        return None
    mins = h * 60 + m + s / 60 - 540
    if h < 12:                       # 過了午夜
        mins += 24 * 60
    return round(mins, 2)


def fetch_night():
    body = {"MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "TXF", "ExpireMonth": "",
            "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}
    r = session.post(URL, json=body, timeout=10)
    r.raise_for_status()
    ql = ((r.json().get("RtData") or {}).get("QuoteList")) or []
    ql = [q for q in ql if str(q.get("SymbolID", ""))[-2:] in ("-M", "-F") and "/" not in str(q.get("SymbolID", ""))
          and num(q.get("CLastPrice"))]
    if not ql:
        return None
    return max(ql, key=lambda q: num(q.get("CTotalVolume")) or 0)


class Store:
    def __init__(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        self.ok = bool(REPO and TOKEN)
        url = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"
        self.path = os.path.join(OUT_DIR, "night.json")
        if self.ok:
            # 讀回 night 分支已有的紀錄（同一個夜盤的前一段）
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "night", url, OUT_DIR + "_old"],
                               capture_output=True, text=True)
            old = os.path.join(OUT_DIR + "_old", "night.json")
            self.prev = json.load(open(old, encoding="utf-8")) if r.returncode == 0 and os.path.exists(old) else None
            self.git("init", "-q", "-b", "night")
            self.git("remote", "add", "origin", url)
            self.git("config", "user.name", "night-recorder")
            self.git("config", "user.email", "night-recorder@users.noreply.github.com")
        else:
            self.prev = None
        self.first = True

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=OUT_DIR, capture_output=True, text=True)

    def save(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        if not self.ok:
            return
        self.git("add", "night.json")
        msg = f"夜盤 {data['session']} {data['updated']}"
        self.git("commit", "-q", "-m", msg) if self.first else self.git("commit", "-q", "--amend", "-m", msg)
        self.first = False
        r = self.git("push", "-q", "-f", "origin", "night")
        if r.returncode != 0:
            log("推送失敗", r.stderr[-200:])


def main():
    now = datetime.now(TW)
    if not in_night(now):
        log("不在夜盤時段，結束")
        return
    sdate = session_date(now)
    store = Store()
    data = store.prev if (store.prev and store.prev.get("session") == sdate) else \
        {"session": sdate, "symbol": None, "ref": None, "tx": [], "updated": ""}
    log(f"夜盤 {sdate}，已有 {len(data['tx'])} 點")
    start, last_push, misses = time.time(), 0, 0
    while time.time() - start < MAX_RUN_SEC:
        now = datetime.now(TW)
        if not in_night(now):
            break
        try:
            q = fetch_night()
        except Exception as e:  # noqa: BLE001
            log("抓取失敗", type(e).__name__, str(e)[:100])
            q = None
        if q and str(q.get("CDate", "")).replace("-", "")[:8] >= sdate.replace("-", ""):
            misses = 0
            m, p = unified_min(sdate, q.get("CTime"), now), num(q.get("CLastPrice"))
            m_now = unified_min(sdate, now.strftime("%H%M%S"), now)
            # 只收時間跟現在差 20 分鐘以內的報價（避免 15:00 前讀到上一個夜盤的最後一筆）
            if m is not None and p and m >= 355 and abs(m - m_now) <= 20:
                arr = data["tx"]
                vol = num(q.get("CTotalVolume"))
                if not data.get("first") and vol:
                    vals = [x for x in (num(q.get(k)) for k in ("COpenPrice", "CHighPrice", "CLowPrice", "CLastPrice")) if x]
                    data["first"] = {"vol": vol, "typ": round(sum(vals) / len(vals), 2) if vals else p}
                pt = [m, p, vol] if vol else [m, p]
                if arr and abs(arr[-1][0] - m) < 0.4:
                    arr[-1] = pt
                elif not arr or m > arr[-1][0]:
                    arr.append(pt)
                data.update(symbol=q.get("SymbolID"), ref=num(q.get("CRefPrice")),
                            high=num(q.get("CHighPrice")), low=num(q.get("CLowPrice")), volume=num(q.get("CTotalVolume")))
        else:
            misses += 1
            if misses * POLL_SEC > 40 * 60 and not data["tx"]:
                log("40 分鐘都沒有夜盤資料（可能休市），結束")
                return
        if time.time() - last_push >= PUSH_SEC and data["tx"]:
            data["updated"] = now.strftime("%H:%M:%S")
            store.save(data)
            last_push = time.time()
        time.sleep(POLL_SEC)
    if data["tx"]:
        data["updated"] = datetime.now(TW).strftime("%H:%M:%S")
        store.save(data)
    log(f"這段結束，共 {len(data['tx'])} 點")


if __name__ == "__main__":
    main()
