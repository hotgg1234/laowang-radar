#!/usr/bin/env python3
"""老王選股雷達：每日抓取台股收盤資料並計算訊號。

流程
1. 從今天往回補齊最近 TARGET_DAYS 個交易日的原始資料（data/raw/YYYYMMDD.json）
   - 證交所：每日收盤行情 MI_INDEX、三大法人 T86
   - 櫃買中心：上櫃收盤行情、三大法人
2. 讀取原始資料，對每一檔股票計算老王規則訊號
3. 輸出網站到 _site/：index.html、data/screener.json、data/s/<代號>.json

用法
  python scripts/update.py              # 正常每日執行
  python scripts/update.py --no-fetch   # 只用現有原始資料重建網站
"""
import argparse
import datetime as dt
import json
import math
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
HOLIDAYS = ROOT / "data" / "holidays.json"
SITE = ROOT / "_site"
TZ = ZoneInfo("Asia/Taipei")

TARGET_DAYS = 500      # 要保留的交易日數（約 2 年，供回測使用）
KEEP_DAYS = 520        # 超過就刪除最舊的原始檔
MAX_LOOKBACK = 760     # 往回最多找幾個日曆天
MAX_NEW_PER_RUN = 150  # 每次執行最多新抓幾個交易日，避免超過 GitHub Actions 時限；不足的下次再補
SLEEP = 4.0            # 每次請求間隔秒數，證交所每 5 秒超過 3 次會封鎖 IP
RETRY_WINDOW = 7       # 幾天內缺漏的資料會再補抓

UA = {"User-Agent": "Mozilla/5.0 (laowang-radar; daily close data)",
      "Accept": "application/json,text/plain,*/*"}


# ---------------------------------------------------------------- fetching
_session = None
_last = 0.0


def get_json(url):
    global _session, _last
    import requests
    if _session is None:
        _session = requests.Session()
        _session.headers.update(UA)
    for attempt in range(4):
        wait = SLEEP - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code}: {url}", file=sys.stderr)
        except Exception as e:  # network error or non-JSON (被封鎖時會回 HTML)
            print(f"  error {type(e).__name__}: {url}", file=sys.stderr)
        time.sleep(10 * (attempt + 1))
    return None


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("X", "").strip()
    if s in ("", "-", "--", "---", "----", "除權", "除息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def lots(shares):
    v = num(shares)
    return None if v is None else round(v / 1000)


def find(fields, *preds):
    """回傳第一個符合所有條件的欄位索引。"""
    for i, f in enumerate(fields):
        f = str(f).strip()
        if all(p(f) for p in preds):
            return i
    return -1


def has(s):
    return lambda f: s in f


def starts(s):
    return lambda f: f.startswith(s)


def tables_of(j):
    """證交所與櫃買新版 API 都是 {tables:[{title,fields,data}]}；舊版是 fields/data 或 fields9/data9。"""
    out = list(j.get("tables") or [])
    for k in ("", "9", "8"):
        if j.get("fields" + k) and j.get("data" + k):
            out.append({"fields": j["fields" + k], "data": j["data" + k]})
    return out


def twse_quotes(d):
    j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json")
    if not j:
        raise RuntimeError("證交所行情抓取失敗")
    if str(j.get("stat", "")).upper() != "OK":
        return None  # 非交易日
    for t in tables_of(j):
        f = [str(x).strip() for x in t.get("fields", [])]
        if "證券代號" in f and "收盤價" in f:
            ix = {k: f.index(k) for k in ("證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "成交股數")}
            out = {}
            for row in t.get("data", []):
                c = num(row[ix["收盤價"]])
                if c is None:
                    continue
                o, h, l = (num(row[ix[k]]) for k in ("開盤價", "最高價", "最低價"))
                out[str(row[ix["證券代號"]]).strip()] = {
                    "n": str(row[ix["證券名稱"]]).strip(), "m": "上市",
                    "o": o or c, "h": h or c, "l": l or c, "c": c, "v": lots(row[ix["成交股數"]]) or 0}
            return out
    raise RuntimeError("證交所行情格式改變，找不到收盤行情表")


def twse_chips(d):
    j = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json")
    if not j or str(j.get("stat", "")).upper() != "OK":
        return None
    for t in tables_of(j):
        f = [str(x).strip() for x in t.get("fields", [])]
        ic = find(f, has("證券代號"))
        fi = find(f, has("買賣超"), has("外陸資"), has("不含"))
        if fi < 0:
            fi = find(f, has("買賣超"), has("外資"), lambda x: "自營" not in x)
        ti = find(f, has("投信"), has("買賣超"))
        if ic >= 0 and fi >= 0 and ti >= 0:
            return {str(r[ic]).strip(): (lots(r[fi]), lots(r[ti])) for r in t.get("data", [])}
    return None


def tpex_quotes(d):
    j = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={d:%Y/%m/%d}&type=EW&response=json")
    if not j:
        return None
    for t in tables_of(j):
        f = [str(x).strip() for x in t.get("fields", [])]
        ic, inm = find(f, lambda x: x == "代號"), find(f, lambda x: x == "名稱")
        ix = {k: find(f, starts(k)) for k in ("收盤", "開盤", "最高", "最低", "成交股數")}
        if ic < 0 or inm < 0 or min(ix.values()) < 0:
            continue
        out = {}
        for row in t.get("data", []):
            c = num(row[ix["收盤"]])
            if c is None:
                continue
            o, h, l = (num(row[ix[k]]) for k in ("開盤", "最高", "最低"))
            out[str(row[ic]).strip()] = {"n": str(row[inm]).strip(), "m": "上櫃",
                                         "o": o or c, "h": h or c, "l": l or c, "c": c,
                                         "v": lots(row[ix["成交股數"]]) or 0}
        return out or None
    return None


def tpex_chips(d):
    j = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json")
    if not j:
        return None
    for t in tables_of(j):
        f = [str(x).strip() for x in t.get("fields", [])]
        ic = find(f, lambda x: x == "代號")
        fi = find(f, has("外資"), has("買賣超"), has("不含"))
        ti = find(f, has("投信"), has("買賣超"))
        if ic >= 0 and fi >= 0 and ti >= 0:
            return {str(r[ic]).strip(): (lots(r[fi]), lots(r[ti])) for r in t.get("data", [])}
    return None


def fetch_day(d, old=None):
    """抓一個交易日。old 是既有檔案（補抓缺漏時）。回傳 dict 或 None（非交易日）。"""
    missing = set(old.get("missing", [])) if old else {"twse", "twse_chips", "tpex", "tpex_chips"}
    stocks = dict(old["stocks"]) if old else {}
    if "twse" in missing:
        q = twse_quotes(d)
        if q is None:
            return None
        for k, v in q.items():
            stocks[k] = v
        missing.discard("twse")
    for key, fn, mkt in (("tpex", tpex_quotes, None), ("twse_chips", twse_chips, "上市"), ("tpex_chips", tpex_chips, "上櫃")):
        if key not in missing:
            continue
        res = fn(d)
        if res is None:
            print(f"  {d} {key} 暫無資料", file=sys.stderr)
            continue
        if mkt is None:
            for k, v in res.items():
                stocks[k] = v
        else:
            for k, (f, t) in res.items():
                if k in stocks and stocks[k]["m"] == mkt:
                    stocks[k]["f"], stocks[k]["t"] = f, t
        missing.discard(key)
    return {"date": d.isoformat(), "missing": sorted(missing), "stocks": stocks}


def load_json(p, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def update_raw():
    RAW.mkdir(parents=True, exist_ok=True)
    holidays = set(load_json(HOLIDAYS, []))
    today = dt.datetime.now(TZ).date()
    have = 0
    fetched = 0
    d = today
    for _ in range(MAX_LOOKBACK):
        if have >= TARGET_DAYS or fetched >= MAX_NEW_PER_RUN:
            break
        p = RAW / f"{d:%Y%m%d}.json"
        if d.weekday() >= 5 or d.isoformat() in holidays:
            d -= dt.timedelta(days=1)
            continue
        old = load_json(p, None) if p.exists() else None
        if old and (not old.get("missing") or (today - d).days > RETRY_WINDOW):
            have += 1
            d -= dt.timedelta(days=1)
            continue
        print(f"抓取 {d} …")
        rec = fetch_day(d, old)
        if rec is None:
            if d < today:  # 今天可能只是還沒公布，不記成假日
                holidays.add(d.isoformat())
            print(f"  {d} 非交易日或尚未公布")
        else:
            if d == today and "twse_chips" in rec["missing"]:
                print("  今日法人資料尚未公布，先存行情，下次執行會補抓")
            p.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")), "utf-8")
            have += 1
            fetched += 1
        d -= dt.timedelta(days=1)
    HOLIDAYS.write_text(json.dumps(sorted(holidays)), "utf-8")
    files = sorted(RAW.glob("*.json"))
    for p in files[:-KEEP_DAYS]:
        p.unlink()


# ---------------------------------------------------------------- signals
# 與網頁 index.html 內的 analyze() 規則一致，修改時兩邊要一起改。
def ma(arr, k, i):
    if i < k - 1:
        return math.nan
    return sum(arr[i - k + 1:i + 1]) / k


def gt(a, b):
    return not (math.isnan(a) or math.isnan(b)) and a > b


def analyze(rows, has_chips):
    """rows: [[date,o,h,l,c,v,f,t], ...] 依日期排序"""
    n = len(rows)
    L = n - 1
    C = [r[4] for r in rows]
    V = [r[5] for r in rows]
    M = {k: [ma(C, k, i) for i in range(n)] for k in (5, 10, 20, 60)}

    def above3(i):
        return all(gt(C[i], M[k][i]) for k in (5, 10, 20))

    def below3(i):
        return all(gt(M[k][i], C[i]) for k in (5, 10, 20))

    last = rows[-120:]
    lo, hi = min(r[3] for r in last), max(r[2] for r in last)

    def base_pos(v):
        return (v - lo) / (hi - lo) if hi > lo else 0.5

    big = None
    for i in range(L, max(20, n - 30) - 1, -1):
        avg = sum(V[i - 20:i]) / 20
        if avg > 0 and V[i] >= avg * 2:
            big = {"i": i, "date": rows[i][0], "high": rows[i][2], "low": rows[i][3],
                   "ratio": round(V[i] / avg, 2), "base": round(base_pos(C[i]), 3)}
            break
    seg = V[max(0, L - 20):L]
    v_avg = sum(seg) / max(1, min(20, L))
    sig, na = {}, {}
    have20, have60 = n >= 23, n >= 63
    if have20:
        sig["sanyang"] = above3(L) and any(not above3(i) for i in (L - 1, L - 2, L - 3))
        m = [M[k][L] for k in (5, 10, 20)]
        sig["tangle"] = (max(m) - min(m)) / C[L] < 0.02
        sig["sansheng"] = below3(L)
        sig["ride5"] = all(C[i] >= rows[i - 1][3] and not gt(M[5][i], C[i]) for i in (L, L - 1, L - 2))
    else:
        for k in ("sanyang", "tangle", "sansheng", "ride5"):
            na[k] = 1
    if have60:
        sig["season"] = all(gt(C[i], M[60][i]) for i in (L, L - 1, L - 2))
        sig["belowSeason"] = gt(M[60][L], C[L])
    else:
        na["season"] = na["belowSeason"] = 1
    if big:
        sig["bigBreak"] = C[L] > big["high"] and big["i"] < L
        sig["bigHold"] = C[L] >= big["low"]
        sig["bigBreakdown"] = C[L] < big["low"]
        sig["lowBaseVol"] = big["base"] <= 0.35
        sig["highBaseVol"] = big["base"] >= 0.75
    else:
        for k in ("bigBreak", "bigHold", "bigBreakdown", "lowBaseVol", "highBaseVol"):
            sig[k] = False
            na[k] = "近 30 日沒有大量 K 棒"
    f5 = t5 = 0
    if has_chips:
        for r in rows[-5:]:
            f5 += r[6] or 0
            t5 += r[7] or 0
        sig["chips"] = f5 > 0 and t5 > 0
    else:
        sig["chips"] = False
        na["chips"] = "沒有外資、投信資料"
    if big and C[L] >= big["low"]:
        stop = {"v": big["low"], "why": f"大量低點（{big['date'][5:]}）"}
    elif have20:
        stop = {"v": round(M[20][L], 2), "why": "20 日均線"}
    else:
        stop = None
    return {
        "sig": sig, "na": na, "big": big, "stop": stop,
        "close": C[L], "chg": round((C[L] / C[L - 1] - 1) * 100, 2) if n > 1 and C[L - 1] else 0,
        "vr": round(V[L] / v_avg, 2) if v_avg else 0, "base": round(base_pos(C[L]), 3),
        "f5": f5, "t5": t5, "hasChips": has_chips, "av": round(sum(V[-20:]) / min(20, n)),
    }


def write_page(src, dst):
    page = src.read_text("utf-8")
    if not page.lstrip().lower().startswith("<!doctype"):
        # 頁面檔只有內容；獨立網站需要補上編碼、viewport 與基本樣式
        page = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                '<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);'
                'padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0}img{max-width:100%}'
                '[hidden]{display:none!important}</style></head><body>' + page + '</body></html>')
    dst.write_text(page, "utf-8")


def build():
    files = sorted(RAW.glob("*.json"))[-KEEP_DAYS:]
    if not files:
        sys.exit("沒有原始資料，請先執行抓取。")
    days = [load_json(p, None) for p in files]
    days = [d for d in days if d]
    latest = days[-1]["date"]
    series, meta = {}, {}
    for day in days:
        for code, s in day["stocks"].items():
            series.setdefault(code, []).append([day["date"], s["o"], s["h"], s["l"], s["c"], s["v"], s.get("f"), s.get("t")])
            meta[code] = (s["n"], s["m"])
    out_s = SITE / "data" / "s"
    if SITE.exists():
        shutil.rmtree(SITE)
    out_s.mkdir(parents=True)
    for name in ("index.html", "backtest.html"):
        src = ROOT / name
        if src.exists():
            write_page(src, SITE / name)
    stocks = []
    for code, rows in series.items():
        # 只收一般股票：4 碼、非 0 開頭（排除 ETF、權證），且最新交易日有成交
        if len(code) != 4 or not code.isdigit() or code.startswith("0") or rows[-1][0] != latest or len(rows) < 2:
            continue
        has_chips = any(r[6] is not None for r in rows[-5:])
        a = analyze(rows, has_chips)
        if a["big"]:
            a["big"].pop("i")
        name, mkt = meta[code]
        stocks.append({"code": code, "name": name, "m": mkt, **a})
        (out_s / f"{code}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), "utf-8")
    doc = {"date": latest, "days": len(days), "generated": dt.datetime.now(TZ).isoformat(timespec="minutes"), "stocks": stocks}
    (SITE / "data" / "screener.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
    (SITE / ".nojekyll").write_text("")
    print(f"完成：{latest}，{len(stocks)} 檔，{len(days)} 個交易日")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="不抓資料，只重建網站")
    args = ap.parse_args()
    if not args.no_fetch:
        update_raw()
    build()
