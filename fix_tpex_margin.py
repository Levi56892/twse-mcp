"""一次性修正：上櫃股票的融資／融券餘額落後一天

原因：collector.py 以前把櫃買中心的「前資餘額」「前券餘額」（前一天）當成今天的餘額。
修法（只動上櫃股票的 margin_lots、short_lots 兩欄）：
  融資：今天餘額 = 前資餘額（原本存的）＋ 資買 − 資賣 − 現償
        沒有買賣明細的那幾天，改用「下一個交易日存的前資餘額」
  融券：今天餘額 = 下一個交易日存的前券餘額
  最後一天：直接向櫃買中心重抓一次（用修好的 collector）
做完會留下 DATA_DIR/fixes/tpex_margin_v1.done，之後再跑會直接跳過。
GitHub Actions 在「收集資料」之前執行。
"""
import csv
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA = os.environ.get("DATA_DIR", "data")
MARK = os.path.join(DATA, "fixes", "tpex_margin_v1.done")


def num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def fmt(v):
    if v is None:
        return ""
    return str(int(v)) if float(v) == int(v) else f"{v:.4f}".rstrip("0").rstrip(".")


def main():
    if os.path.exists(MARK):
        print("上櫃融資餘額已修正過，跳過")
        return
    with open(os.path.join(DATA, "stocks.json"), encoding="utf-8") as fh:
        info = json.load(fh)
    root = os.path.join(DATA, "daily")
    codes = [c for c, v in info.items() if (v or {}).get("market") == "上櫃" and os.path.isdir(os.path.join(root, c))]

    # 最後一天：用修好的 collector 重抓
    last_day = None
    for c in codes[:50]:
        fs = sorted(x for x in os.listdir(os.path.join(root, c)) if x.endswith(".csv"))
        if fs:
            with open(os.path.join(root, c, fs[-1]), encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if rows:
                last_day = max(last_day or "", rows[-1]["date"])
    fresh = {}
    if last_day:
        try:
            import collector as C
            fresh = C.src_tpex_margin(date.fromisoformat(last_day))
            print(f"重抓 {last_day} 上櫃融資券：{len(fresh)} 檔")
        except Exception as e:  # noqa: BLE001
            print("重抓最後一天失敗（最後一天的融券先維持原值）：", e)

    n_rows = n_stock = 0
    for c in codes:
        files = sorted(x for x in os.listdir(os.path.join(root, c)) if x.endswith(".csv"))
        per = []          # [(檔名, 欄位, rows)]
        allrows = []
        for fn in files:
            with open(os.path.join(root, c, fn), encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                rows = list(rd)
            per.append((fn, rd.fieldnames, rows))
            allrows.extend(rows)
        allrows.sort(key=lambda r: r["date"])
        old_m = [num(r.get("margin_lots")) for r in allrows]
        old_s = [num(r.get("short_lots")) for r in allrows]
        for i, r in enumerate(allrows):
            nxt = i + 1 < len(allrows)
            b, s_, p = num(r.get("margin_buy_lots")), num(r.get("margin_sell_lots")), num(r.get("margin_repay_lots"))
            if old_m[i] is not None and None not in (b, s_, p):
                m = old_m[i] + b - s_ - p
            elif nxt:
                m = old_m[i + 1]
            else:
                m = (fresh.get(c) or {}).get("margin_lots", old_m[i])
            if nxt:
                s = old_s[i + 1]
            else:
                s = (fresh.get(c) or {}).get("short_lots", old_s[i])
            if not nxt and c in fresh and fresh[c].get("margin_lots") is not None:
                m = fresh[c]["margin_lots"]
            r["margin_lots"], r["short_lots"] = fmt(m), fmt(s)
            n_rows += 1
        for fn, fields, rows in per:
            with open(os.path.join(root, c, fn), "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        n_stock += 1
    os.makedirs(os.path.dirname(MARK), exist_ok=True)
    with open(MARK, "w", encoding="utf-8") as fh:
        fh.write(f"fixed {n_stock} stocks, {n_rows} rows, last_day={last_day}\n")
    print(f"上櫃融資券餘額修正完成：{n_stock} 檔、{n_rows} 列")


if __name__ == "__main__":
    main()
