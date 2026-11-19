"""大盤融資維持率（上市、上櫃，扣除／含 ETF；融資金額每天用官方公布數字校正）

做法跟融資維持率頁的個股估算一樣：
  每檔普通股每天新買進的融資，以當天均價（高＋低＋收 ÷3）記成本；賣出與償還按比例從所有部位扣掉，
  得到每天「還留著的融資」平均成本。
  個股融資金額 ≈ 餘額張數 × 1000 × 平均成本 × 融資成數（上市、上櫃皆 60%）
  大盤維持率 = Σ(餘額張數 × 1000 × 收盤價) ÷ Σ(融資金額)
資料庫只收普通股（4 位數代號），所以自然排除 ETF。
「含 ETF」版本另外抓 ETF（00 開頭）每天的收盤與融資，存 DATA_DIR/etf/<日期>.json（抓過不重抓），用同一套方法加總。
輸出 DATA_DIR/margin_market.json：{"上市": [[日期, 維持率, 融資市值億, 融資金額億, 融資張數], ...], "上櫃": [...],
  "上市含ETF": [...], "上櫃含ETF": [...]}（含 ETF 要等 ETF 資料回補夠多天才會出現）
GitHub Actions 每次收集資料後執行（約數十秒）。
"""
import csv
import json
import math
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 讓 import collector 找得到

DATA = os.environ.get("DATA_DIR", "data")
WARMUP_YEARS = 3          # 用前幾年資料把成本推估暖機
KEEP_DAYS = 500           # 輸出最近幾個交易日
RATIO = {"上市": 0.6, "上櫃": 0.6}
RANK_N = 30               # 個股排行各取幾檔
RANK_MIN_LOAN = 5.0       # 維持率排行只看融資金額 5 億以上（太小的維持率雜訊大）
ETF_WARMUP = 250         # ETF 成本暖機要多抓的交易日
ETF_MIN_WARM = 120        # ETF 連續資料滿這麼多天後才輸出「含 ETF」
ETF_BUDGET = float(os.environ.get("ETF_BUDGET_MIN", "25")) * 60   # 每次最多花幾秒回補 ETF
OFF_BUDGET = float(os.environ.get("OFFICIAL_BUDGET_MIN", "15")) * 60   # 每次最多花幾秒回補官方融資金額
OFF_FILE = "margin_official.json"   # {日期: [上市融資金額元, 上櫃融資金額元]}（官方公布，含 ETF）


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def model(rows):
    """回傳 {date: (餘額張, 收盤, 平均成本)}"""
    out = {}
    layers = {}
    prev = None

    def add(p, q):
        if q > 0 and p > 0:
            k = round(math.log(p) / 0.004)
            c = layers.get(k)
            if c:
                c[0] += p * q
                c[1] += q
            else:
                layers[k] = [p * q, q]

    def tot():
        q = pq = 0.0
        for v in layers.values():
            pq += v[0]
            q += v[1]
        return q, pq

    def scale(f):
        for v in layers.values():
            v[0] *= f
            v[1] *= f

    for r in rows:
        bal, close = num(r.get("margin_lots")), num(r.get("close"))
        if bal is None or not close:
            continue
        vals = [x for x in (num(r.get("high")), num(r.get("low")), close) if x and x > 0]
        typ = sum(vals) / len(vals)
        if prev is None:
            add(typ, bal)
        else:
            buy = num(r.get("margin_buy_lots"))
            if buy is not None:
                inc, red = buy, prev + buy - bal
            else:
                d = bal - prev
                inc, red = max(0.0, d), max(0.0, -d)
            if red < 0:
                inc, red = inc - red, 0.0
            q, _ = tot()
            if red > 0 and q > 0:
                scale(max(0.0, 1 - red / q))
            add(typ, inc)
            q, _ = tot()
            if q > 0 and bal > 0:
                scale(bal / q)
            elif bal == 0:
                layers.clear()
            for k in [k for k, v in layers.items() if v[1] < 0.01]:
                del layers[k]
        prev = bal
        q, pq = tot()
        out[r["date"]] = (bal, close, pq / q if q > 0 else None)
    return out


def add_model(A, m, r):
    for d, (bal, close, avg) in m.items():
        if not bal or not avg:
            continue
        a = A.setdefault(d, [0.0, 0.0, 0.0])
        a[0] += bal * 1000 * close
        a[1] += bal * 1000 * avg * r
        a[2] += bal


def row(d, a):
    return [d, round(a[0] / a[1], 5), round(a[0] / 1e8, 2), round(a[1] / 1e8, 2), int(a[2])]


def fetch_etf(days):
    """把缺的交易日 ETF 收盤與融資抓下來存 etf/<日期>.json（新的先抓，有時間上限，下次接著補）。"""
    root = os.path.join(DATA, "etf")
    os.makedirs(root, exist_ok=True)
    miss = [d for d in reversed(days) if not os.path.exists(os.path.join(root, f"{d}.json"))]
    if not miss:
        return
    try:
        import collector as C          # 共用 collector.py 的抓取與欄位解析（含節流）
    except Exception as e:  # noqa: BLE001
        print("載入 collector 失敗，略過 ETF：", e)
        return
    t0, n = time.time(), 0
    for d in miss:
        if time.time() - t0 > ETF_BUDGET:
            break
        dt = date.fromisoformat(d)
        out = {}
        try:
            for mkt, price_fn, margin_fn in (("上市", C.src_twse_price, C.src_twse_margin),
                                             ("上櫃", C.src_tpex_price, C.src_tpex_margin)):
                px, mg = price_fn(dt), margin_fn(dt)
                if not px or not mg:
                    raise RuntimeError(f"{mkt} 沒資料")
                out[mkt] = {c: [px[c]["high"], px[c]["low"], px[c]["close"], m["margin_lots"], m["margin_buy_lots"]]
                            for c, m in mg.items()
                            if c.startswith("00") and c in px and px[c].get("close") and m.get("margin_lots") is not None}
        except Exception as e:  # noqa: BLE001
            print(f"ETF {d} 抓取失敗：{e}")
            continue
        with open(os.path.join(root, f"{d}.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
        n += 1
        if n % 20 == 0:
            print(f"ETF 回補中：已補 {n} 天（到 {d}），已花 {(time.time() - t0) / 60:.1f} 分鐘", flush=True)
    print(f"ETF 本次補了 {n} 天，還缺 {len(miss) - n} 天", flush=True)


def _amt(s):
    try:
        return float(str(s).replace(",", "")) * 1000   # 仟元 → 元
    except ValueError:
        return None


def official_total(C, d):
    """證交所／櫃買中心公布的市場融資金額（今日餘額，含 ETF），回傳 [上市元, 上櫃元]。"""
    j = C.get("twse", "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
              {"date": C.ymd(d), "selectType": "MS", "response": "json"})
    tw = None
    for t in (j or {}).get("tables") or []:
        for r in (t or {}).get("data") or []:
            if r and str(r[0]).startswith("融資金額"):
                tw = _amt(r[-1])
    k = C.get("tpex", "https://www.tpex.org.tw/www/zh-tw/margin/balance",
              {"date": C.slash(d), "response": "json"})
    ot = None
    for t in (k or {}).get("tables") or []:
        for r in (t or {}).get("summary") or []:
            if len(r) > 6 and str(r[1]).startswith("融資金"):
                ot = _amt(r[6])
    return [tw, ot]


def fetch_official(days):
    """把缺的交易日官方融資金額抓下來（新的先抓，有時間上限，下次接著補）。"""
    p = os.path.join(DATA, OFF_FILE)
    try:
        with open(p, encoding="utf-8") as fh:
            have = json.load(fh)
    except (OSError, ValueError):
        have = {}
    miss = [d for d in reversed(days) if not (have.get(d) and None not in have[d])]
    if miss:
        try:
            import collector as C
        except Exception as e:  # noqa: BLE001
            print("載入 collector 失敗，略過官方融資金額：", e)
            return have
        t0, n = time.time(), 0
        for d in miss:
            if time.time() - t0 > OFF_BUDGET:
                break
            try:
                v = official_total(C, date.fromisoformat(d))
            except Exception as e:  # noqa: BLE001
                print(f"官方融資金額 {d} 抓取失敗：{e}")
                continue
            if v[0] or v[1]:
                have[d] = v
                n += 1
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(have, fh, separators=(",", ":"))
        print(f"官方融資金額本次補了 {n} 天，還缺 {len(miss) - n} 天", flush=True)
    return have


def calib(days, off, A, E, i):
    """每天的校正係數 k = 官方融資金額 ÷ 推估融資金額（含 ETF）；沒有官方數字的日子用最近一天的 k。"""
    ks = {}
    for d in days:
        o = (off.get(d) or [None, None])[i]
        est = A[d][1] + (E[d][1] if d in E else 0)
        if o and est > 0 and d in E:
            ks[d] = o / est
    out, last = {}, None
    for d in days:
        last = ks.get(d, last)
        out[d] = last
    nxt = None
    for d in reversed(days):       # 最前面還沒抓到的，用後面第一個
        if out[d] is None:
            out[d] = nxt
        else:
            nxt = out[d]
    return out, ks


def main():
    try:
        with open(os.path.join(DATA, "stocks.json"), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        info = {}
    y1 = date.today().year
    years = range(y1 - WARMUP_YEARS, y1 + 1)
    agg = {"上市": {}, "上櫃": {}}
    last_stock = {}
    root = os.path.join(DATA, "daily")
    for code in sorted(os.listdir(root)):
        if not (len(code) == 4 and code.isdigit() and code[0] != "0"):
            continue
        mkt = (info.get(code) or {}).get("market")
        if mkt not in agg:
            continue
        rows = []
        for y in years:
            p = os.path.join(root, code, f"{y}.csv")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    rows.extend(csv.DictReader(fh))
        rows.sort(key=lambda r: r["date"])
        r = RATIO[mkt]
        A = agg[mkt]
        m = model(rows)
        add_model(A, m, r)
        # 個股最新一天（給排行用）
        ds = sorted(m)
        if ds:
            bal, close, avg = m[ds[-1]]
            if bal and avg:
                back = lambda n: bal - m[ds[-1 - n]][0] if len(ds) > n else None  # noqa: E731
                last_stock[code] = [ds[-1], code, (info.get(code) or {}).get("name") or code, mkt, close, int(bal),
                                    back(1), back(5), round(avg, 2), round(close / (avg * r), 4),
                                    round(bal * 1000 * avg * r / 1e8, 2),
                                    round(close / m[ds[-2]][1] - 1, 5) if len(ds) > 1 and m[ds[-2]][1] else None,
                                    [round(b * 1000 * a * r / 1e8, 2) if a else 0 for b, _, a in (m[x] for x in ds[-11:])]]

    # ---- ETF（含 ETF 版本用）：每天一個檔，抓過不重抓 ----
    days_all = sorted(set(agg["上市"]) | set(agg["上櫃"]))
    want = days_all[-(KEEP_DAYS + ETF_WARMUP):]
    fetch_etf(want)
    etf_rows = {"上市": {}, "上櫃": {}}       # {市場: {代號: [row,...]}}
    have = []
    for d in want:
        p = os.path.join(DATA, "etf", f"{d}.json")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            day = json.load(fh)
        have.append(d)
        for mkt in ("上市", "上櫃"):
            for code, v in (day.get(mkt) or {}).items():
                hi, lo, cl, bal, buy = v
                etf_rows[mkt].setdefault(code, []).append(
                    {"date": d, "high": hi, "low": lo, "close": cl, "margin_lots": bal, "margin_buy_lots": buy})
    # 只用「到今天為止連續有 ETF 資料」的那一段；暖機 ETF_MIN_WARM 天之後才輸出
    run = []
    for d in reversed(want):
        if d in have:
            run.append(d)
        else:
            break
    run = sorted(run)
    etf_agg = {"上市": {}, "上櫃": {}}
    if len(run) > ETF_MIN_WARM:
        start = run[0]
        for mkt in etf_agg:
            for code, rows in etf_rows[mkt].items():
                add_model(etf_agg[mkt], model([x for x in rows if x["date"] >= start]), RATIO[mkt])
        emit_from = run[ETF_MIN_WARM]
    else:
        emit_from = None
    print(f"ETF 資料：{len(have)} 天，連續到今天 {len(run)} 天" + (f"，含 ETF 從 {emit_from} 起" if emit_from else "（還在回補）"))

    # ---- 用官方公布的市場融資金額（含 ETF）校正推估的融資金額 ----
    # 推估成本會隨行情偏離（2024 年約高 3%、2026 年高到 15%），融資市值則是準的；
    # 所以每天算 k = 官方融資金額 ÷ 推估融資金額（含 ETF），含 ETF 與扣 ETF 的融資金額都乘上 k。
    off = fetch_official(sorted(days_all)[-KEEP_DAYS:])
    out = {"校正": []}   # 用 list，伺服器切片才不會出錯
    for i, (mkt, A) in enumerate(agg.items()):
        days = [d for d in sorted(A)[-KEEP_DAYS:] if A[d][1] > 0]
        E = {d: v for d, v in etf_agg[mkt].items() if emit_from and d >= emit_from}
        K, ks = calib(days, off, A, E, i) if E else ({}, {})

        def adj(a, d, K=K):
            k = K.get(d)
            return [a[0], a[1] * k, a[2]] if k else a
        out[mkt] = [row(d, adj(A[d], d)) for d in days]
        if E:
            out[mkt + "含ETF"] = [row(d, adj([A[d][j] + E[d][j] for j in range(3)], d)) for d in days if d in E]
        if ks:
            last = max(ks)
            out["校正"].append([mkt, last, round(ks[last], 4), len(ks)])
            print(f"{mkt} 校正係數 {last}：{ks[last]:.4f}（有官方數字 {len(ks)} 天）")
    # 全市場個股排行（只算最新交易日有資料的股票）
    # 每列 [日期, 代號, 名稱, 市場, 收盤, 融資餘額張, 日增減, 5日增減, 融資成本, 維持率, 融資金額億, 漲跌幅, 近11日融資金額億[]（算 10 日變化）]
    latest = max((v[0] for v in last_stock.values()), default=None)
    pool = [v for v in last_stock.values() if v[0] == latest]
    out["排行金額"] = sorted(pool, key=lambda v: -v[10])[:RANK_N]
    dl = lambda v: v[12][-1] - v[12][-2] if len(v[12]) > 1 else 0  # noqa: E731  融資金額日變動
    half = RANK_N // 2   # 融資金額增加前 15 ＋ 減少前 15
    out["排行金額變化"] = (sorted([v for v in pool if dl(v) > 0], key=lambda v: -dl(v))[:half]
                       + sorted([v for v in pool if dl(v) < 0], key=dl)[:half])
    out["排行維持率"] = sorted([v for v in pool if v[10] >= RANK_MIN_LOAN], key=lambda v: v[9])[:RANK_N]
    with open(os.path.join(DATA, "margin_market.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    for mkt, rows in out.items():
        if rows and not mkt.startswith(("排行", "校正")):
            print(f"{mkt} 融資維持率 {rows[-1][0]}：{rows[-1][1] * 100:.2f}%（融資金額 {rows[-1][3]:,.0f} 億）")


if __name__ == "__main__":
    main()
