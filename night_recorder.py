"""
台指期夜盤記錄器（給「台股戰情室」用）
夜盤 15:00 到隔天 05:00，共 14 小時。由 Render 伺服器在交易日 14:58 啟動、一路記到 05:00
（GitHub Actions 排程常延遲幾十分鐘，只留手動備援；Actions 一次最多 6 小時，手動時會分段接力）。
每次啟動先讀回 night 分支上已記錄的部分再接著記。
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
OUT_DIR = os.environ.get("NIGHT_DIR", "nightout")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")
API_TOKEN = os.environ.get("GH_WRITE_TOKEN", "")        # Render 用：可寫入 repo 的金鑰（走 GitHub API，不需要 git）
API_REPO = os.environ.get("FLOW_REPO", REPO or "Levi56892/twse-mcp")
# GitHub Actions 每段最多 5 小時 50 分；Render 沒有限制，一路記到 05:00
MAX_RUN_SEC = 15 * 3600 if API_TOKEN else 5 * 3600 + 50 * 60

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


class ApiStore:
    """Render 模式：用 GitHub API 讀寫 night 分支的 night.json。"""

    def __init__(self):
        self.url = f"https://api.github.com/repos/{API_REPO}/contents/night.json"
        self.h = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/vnd.github+json"}
        self.prev, self.sha = None, None
        self.load()
        log("用 GitHub API 存檔（Render 模式）")

    def load(self):
        import base64
        try:
            r = session.get(self.url, params={"ref": "night"}, headers=self.h, timeout=20)
            if r.status_code == 200:
                js = r.json()
                self.sha = js.get("sha")
                self.prev = json.loads(base64.b64decode(js["content"]).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log("讀取 night.json 失敗", type(e).__name__, str(e)[:100])

    def save(self, data):
        import base64
        body = {"message": f"夜盤 {data['session']} {data['updated']}", "branch": "night",
                "content": base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode()}
        for _ in range(2):
            if self.sha:
                body["sha"] = self.sha
            try:
                r = session.put(self.url, json=body, headers=self.h, timeout=30)
            except requests.RequestException as e:
                log("推送失敗", type(e).__name__)
                return
            if r.status_code in (200, 201):
                self.sha = r.json().get("content", {}).get("sha")
                return
            log("推送失敗", r.status_code, r.text[:150])
            self.sha = None
            self.load()   # sha 過期：重新取得再試一次（只更新 sha，不動已記錄的資料）


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
    store = ApiStore() if API_TOKEN else Store()
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
