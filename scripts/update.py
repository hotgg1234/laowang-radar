#!/usr/bin/env python3
"""台股選股雷達：每日抓取台股收盤資料，依 strategies/ 內的策略評分。

流程
1. 從今天往回補齊最近 TARGET_DAYS 個交易日的原始資料（data/raw/YYYYMMDD.json）
   - 證交所：每日收盤行情 MI_INDEX、三大法人 T86
   - 櫃買中心：上櫃收盤行情、三大法人
2. 抓最新的本益比、股價淨值比（data/fund.json），換算 EPS 與 ROE
3. 抓加權指數收盤（data/taiex.json），給大盤濾鏡用
4. 用 scripts/strategy.py 計算技術分、量價型態、各策略的歷史訊號勝率
5. 輸出網站到 _site/：index.html、backtest.html、engine.js、data/screener.json、data/s/<代號>.json

用法
  python scripts/update.py              # 正常每日執行
  python scripts/update.py --no-fetch   # 只用現有原始資料重建網站
"""
import argparse
import datetime as dt
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import strategy as S

ROOT = Path(__file__).resolve().parent.parent
STRATS = ROOT / "strategies"
FUND = ROOT / "data" / "fund.json"
TAIEX = ROOT / "data" / "taiex.json"
RAW = ROOT / "data" / "raw"
HOLIDAYS = ROOT / "data" / "holidays.json"
SITE = ROOT / "_site"
TZ = ZoneInfo("Asia/Taipei")
STOCK_RE = re.compile(r"^[1-9]\d{3}$")
ETF_RE = re.compile(r"^00\d{2,4}[A-Z]?$")

TARGET_DAYS = 500      # 要保留的交易日數（約 2 年，供回測使用）
KEEP_DAYS = 520        # 超過就刪除最舊的原始檔
MAX_LOOKBACK = 760     # 往回最多找幾個日曆天
MAX_NEW_PER_RUN = 150  # 每次執行最多新抓幾個交易日，避免超過 GitHub Actions 時限；不足的下次再補
SLEEP = 4.0            # 每次請求間隔秒數，證交所每 5 秒超過 3 次會封鎖 IP
RETRY_WINDOW = 7       # 幾天內缺漏的資料會再補抓
FETCH_BUDGET = 95 * 60 # 抓資料最多花幾秒；時間到就先存檔、建網站，剩下的下次再補
_t0 = time.time()

UA = {"User-Agent": "Mozilla/5.0 (tw-stock-radar; daily close data)",
      "Accept": "application/json,text/plain,*/*"}


# ---------------------------------------------------------------- fetching
_session = None
_last = 0.0


def get_json(url, tries=4):
    global _session, _last
    import requests
    if _session is None:
        _session = requests.Session()
        _session.headers.update(UA)
    for attempt in range(tries):
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
        if attempt < tries - 1:
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
    j = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json", tries=2)
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
    j = get_json(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={d:%Y/%m/%d}&type=EW&response=json", tries=2)
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
    j = get_json(f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json", tries=2)
    if not j:
        return None
    for t in tables_of(j):
        f = [str(x).strip() for x in t.get("fields", [])]
        ic = find(f, lambda x: x == "代號")
        fi = find(f, has("外資"), has("買賣超"), has("不含"))
        ti = find(f, has("投信"), has("買賣超"))
        if (fi < 0 or ti < 0) and ic == 0 and len(f) >= 24 and f[4] == f[13] == "買賣超股數":
            # 新版欄位只寫「買進/賣出/買賣超股數」重複 7 組，順序固定：
            # 外資及陸資(不含外資自營商)、外資自營商、外資合計、投信、自營商自行買賣、自營商避險、自營商合計
            fi, ti = 4, 13
        if ic >= 0 and fi >= 0 and ti >= 0:
            return {str(r[ic]).strip(): (lots(r[fi]), lots(r[ti])) for r in t.get("data", [])}
    debug_once("tpex_chips", j)
    return None


_debugged = set()


def debug_once(name, j):
    """資料格式對不上時，印出一次回應結構，方便從 Actions 紀錄修正解析。"""
    if name in _debugged:
        return
    _debugged.add(name)
    info = {"keys": list(j.keys()), "stat": j.get("stat")}
    for i, t in enumerate(tables_of(j)[:4]):
        info[f"table{i}"] = {"title": t.get("title"), "fields": t.get("fields"),
                             "row0": (t.get("data") or [None])[0]}
    print(f"[debug] {name} 格式：" + json.dumps(info, ensure_ascii=False)[:3000])


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
        if time.time() - _t0 > FETCH_BUDGET:
            print("已達本次抓取時間上限，剩下的下次再補")
            break
        p = RAW / f"{d:%Y%m%d}.json"
        if d.weekday() >= 5 or d.isoformat() in holidays:
            d -= dt.timedelta(days=1)
            continue
        old = load_json(p, None) if p.exists() else None
        # 上櫃法人資料早期解析失敗，舊日期也補抓一次
        chips_fix = old and old.get("missing") == ["tpex_chips"] and not old.get("chips_retried")
        if old and not chips_fix and (not old.get("missing") or (today - d).days > RETRY_WINDOW):
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
            if (today - d).days > RETRY_WINDOW:
                rec["chips_retried"] = True
            p.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")), "utf-8")
            have += 1
            fetched += 0 if chips_fix else 1
        d -= dt.timedelta(days=1)
    HOLIDAYS.write_text(json.dumps(sorted(holidays)), "utf-8")
    files = sorted(RAW.glob("*.json"))
    for p in files[:-KEEP_DAYS]:
        p.unlink()


# ---------------------------------------------------------------- fundamentals
def _pick(obj, *keys):
    """在 dict 裡找第一個鍵名包含任一關鍵字的值。"""
    for k, v in obj.items():
        if any(x.lower() in k.lower() for x in keys):
            return v
    return None


def update_fund():
    """最新一日的本益比、股價淨值比、殖利率。
    EPS（近四季）= 收盤價 ÷ 本益比；ROE ≈ 股價淨值比 ÷ 本益比。本益比空白代表近四季虧損。"""
    out, srcs = {}, []
    j = get_json("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", tries=2)
    if isinstance(j, list) and j:
        for r in j:
            code = str(_pick(r, "Code") or "").strip()
            if code:
                out[code] = [num(_pick(r, "PEratio")), num(_pick(r, "PBratio")), num(_pick(r, "DividendYield"))]
        srcs.append("上市")
    else:
        print("  證交所本益比資料抓取失敗", file=sys.stderr)
    j = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", tries=2)
    n0 = len(out)
    if isinstance(j, list) and j:
        for r in j:
            code = str(_pick(r, "SecuritiesCompanyCode", "Code") or "").strip()
            if code:
                out[code] = [num(_pick(r, "PriceEarningRatio", "PEratio")), num(_pick(r, "PriceBookRatio", "PBratio")),
                             num(_pick(r, "YieldRatio", "DividendYield"))]
    if len(out) > n0:
        srcs.append("上櫃")
    else:
        print("  櫃買本益比資料抓取失敗或格式改變", file=sys.stderr)
        if isinstance(j, list) and j:
            print("[debug] tpex pe 欄位：" + json.dumps(j[0], ensure_ascii=False)[:500])
    if out:
        old = load_json(FUND, {})
        if "上櫃" not in srcs and old.get("stocks"):  # 這次上櫃沒抓到，沿用上次的
            for k, v in old["stocks"].items():
                out.setdefault(k, v)
        FUND.write_text(json.dumps({"date": dt.datetime.now(TZ).date().isoformat(), "stocks": out},
                                   ensure_ascii=False, separators=(",", ":")), "utf-8")
        print(f"本益比資料：{len(out)} 檔（{'、'.join(srcs) or '沿用舊資料'}）")


def update_taiex():
    """加權指數每日收盤（證交所 FMTQIK，一次一個月）。"""
    have = load_json(TAIEX, {})
    files = sorted(RAW.glob("*.json"))
    if not files:
        return
    first = dt.date.fromisoformat(f"{files[0].stem[:4]}-{files[0].stem[4:6]}-01")
    today = dt.datetime.now(TZ).date()
    months, m = [], first
    while m <= today:
        months.append(m)
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    recent = {months[-1], months[-2] if len(months) > 1 else months[-1]}
    for m in months:
        key = f"{m:%Y-%m}"
        if m not in recent and any(d.startswith(key) for d in have):
            continue
        j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={m:%Y%m}01&response=json", tries=2)
        if not j or str(j.get("stat", "")).upper() != "OK":
            continue
        f = [str(x).strip() for x in j.get("fields", [])]
        idt, ic = find(f, has("日期")), find(f, has("加權股價指數"))
        if idt < 0 or ic < 0:
            debug_once("taiex", j)
            continue
        for row in j.get("data", []):
            y, mo, d = str(row[idt]).strip().split("/")
            v = num(row[ic])
            if v:
                have[f"{int(y) + 1911}-{int(mo):02d}-{int(d):02d}"] = v
    TAIEX.write_text(json.dumps(dict(sorted(have.items())), separators=(",", ":")), "utf-8")


# ---------------------------------------------------------------- build site
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


def load_strategies():
    out = []
    for p in sorted(STRATS.glob("*.json")):
        j = load_json(p, None)
        if j and isinstance(j.get("params"), dict):
            out.append({"id": j.get("id", p.stem), "name": j.get("name", p.stem),
                        "description": j.get("description", ""), "params": j["params"]})
    out.sort(key=lambda s: (s["id"] != "default", s["id"]))
    if not out:
        sys.exit("strategies/ 裡沒有策略檔")
    return out


def r2(v, d=2):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, d)


def analyze(rows, strategies, fund):
    """一檔股票的所有策略共用資料。"""
    n = len(rows)
    L = n - 1
    C = [r[4] for r in rows]
    V = [r[5] for r in rows]
    c = C[L]
    base = {"close": c, "chg": r2((c / C[L - 1] - 1) * 100) if n > 1 and C[L - 1] else 0,
            "av": round(sum(V[-20:]) / min(20, n))}
    pe, pb, dy = fund or (None, None, None)
    base["pe"], base["pb"], base["dy"] = pe, pb, dy
    if fund:
        # 本益比空白＝近四季虧損，EPS 記為 0 以下（不會通過門檻）
        base["eps"] = r2(c / pe) if pe else -1
        base["roe"] = r2(pb / pe * 100, 1) if pe and pb else (-1 if pb is not None else None)
    else:
        base["eps"] = base["roe"] = None
    if n < 100:  # 與原專案相同：價格資料不足 100 天就 SKIP
        base["short"] = 1
        return base
    ind = S.indicators(rows)
    states = [S.tech_state(ind, i) for i in range(n)]
    pats, bonus = S.volume_patterns(rows)
    hi252 = max(r[2] for r in rows[-252:])
    base.update({
        "ts": states[L], "vb": bonus, "vp": pats,
        "bt": {s["id"]: list(S.signal_backtest(rows, states, s["params"])) for s in strategies},
        "c5": r2((c / C[-6] - 1) * 100), "c20": r2((c / C[-21] - 1) * 100),
        "vr": r2(sum(V[-5:]) / 5 / (sum(V[-20:]) / 20), 2) if sum(V[-20:]) else 1,
        "fh": r2((c / hi252 - 1) * 100, 1),
        "a20": int(c > ind["ma20"][L]), "a60": int(c > ind["ma60"][L]),
        "bbHi": int(c > ind["bbU"][L]),
    })
    return base


def build():
    files = sorted(RAW.glob("*.json"))[-KEEP_DAYS:]
    if not files:
        sys.exit("沒有原始資料，請先執行抓取。")
    days = [load_json(p, None) for p in files]
    days = [d for d in days if d]
    latest = days[-1]["date"]
    strategies = load_strategies()
    fund_doc = load_json(FUND, {})
    fund = fund_doc.get("stocks", {})
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
    shutil.copy(ROOT / "engine.js", SITE / "engine.js")
    # 大盤：加權指數；抓不到就用 0050 收盤代替
    taiex = load_json(TAIEX, {})
    dates = [d["date"] for d in days]
    mkt = [taiex.get(d) for d in dates]
    mkt_src = "加權指數"
    if sum(v is not None for v in mkt) < len(dates) * 0.9 and "0050" in series:
        px = {r[0]: r[4] for r in series["0050"]}
        mkt, mkt_src = [px.get(d) for d in dates], "元大台灣50（0050）收盤，加權指數資料不足時的替代"
    (SITE / "data" / "market.json").write_text(json.dumps({"src": mkt_src, "d": dates, "c": mkt}, separators=(",", ":")), "utf-8")
    stocks = []
    t1 = time.time()
    for code, rows in series.items():
        # 收一般股票（4 碼、非 0 開頭）與 ETF（00 開頭，可帶 L/R/B 等字尾）；排除權證、牛熊證
        kind = "ETF" if ETF_RE.match(code) else ("股票" if STOCK_RE.match(code) else None)
        if not kind or rows[-1][0] != latest or len(rows) < 2:
            continue
        a = analyze(rows, strategies, None if kind == "ETF" else fund.get(code))
        name, m = meta[code]
        stocks.append({"code": code, "name": name, "m": m, "k": kind,
                       "hasChips": int(any(r[6] is not None for r in rows[-5:])), **a})
        (out_s / f"{code}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), "utf-8")
    doc = {"date": latest, "days": len(days), "generated": dt.datetime.now(TZ).isoformat(timespec="minutes"),
           "fundDate": fund_doc.get("date"), "strategies": strategies, "stocks": stocks}
    (SITE / "data" / "screener.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
    (SITE / ".nojekyll").write_text("")
    print(f"完成：{latest}，{len(stocks)} 檔，{len(days)} 個交易日，{len(strategies)} 個策略，計算 {time.time() - t1:.0f} 秒")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="不抓資料，只重建網站")
    args = ap.parse_args()
    if not args.no_fetch:
        update_raw()
        for fn in (update_fund, update_taiex):
            try:
                fn()
            except Exception as e:  # 輔助資料失敗不影響主流程
                print(f"  {fn.__name__} 失敗：{type(e).__name__} {e}", file=sys.stderr)
    build()
