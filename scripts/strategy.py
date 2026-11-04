"""選股策略引擎（移植自 github.com/kevin801221/stock-strategies-only 的 V3.3 策略）。

流程與原專案相同：
  基本面門檻（EPS、ROE）→ 技術四訊號評分 + 量價型態加減分 → 歷史訊號回測勝率
  → 三項加權得總分 → BUY / WATCH / SKIP → 大盤跌破均線時 BUY 降為 WATCH

這份檔案與網頁的 engine.js 是同一套規則，修改時兩邊要一起改。
只用 Python 標準函式庫，不需要 pandas。

rows 格式：[[date, open, high, low, close, volume, foreign, trust], ...]，依日期排序。
"""
import math

NAN = float("nan")


def _isnan(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def rnd(x):
    """四捨五入到整數（與 JS Math.round 相同，不用 Python 的銀行家捨入）。"""
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- indicators
def _sma(a, k):
    # 每個視窗重新加總，避免累加誤差讓「收盤剛好等於均線」的判斷翻轉
    out = [NAN] * len(a)
    for i in range(k - 1, len(a)):
        out[i] = sum(a[i - k + 1:i + 1]) / k
    return out


def _std(a, k, mean):
    """樣本標準差（ddof=1，與 pandas rolling().std() 相同）。"""
    out = [NAN] * len(a)
    for i in range(k - 1, len(a)):
        m = mean[i]
        out[i] = math.sqrt(sum((a[j] - m) ** 2 for j in range(i - k + 1, i + 1)) / (k - 1))
    return out


def _ewm_adjust(a, com):
    """pandas ewm(com=…, adjust=True).mean()，從第一個有值的位置開始。"""
    beta = 1 - 1 / (1 + com)
    out, num, den = [NAN] * len(a), 0.0, 0.0
    started = False
    for i, v in enumerate(a):
        if _isnan(v):
            if started:
                num, den = num * beta, den * beta
                out[i] = num / den
            continue
        started = True
        num, den = v + beta * num, 1 + beta * den
        out[i] = num / den
    return out


def _ewm_span(a, span):
    """pandas ewm(span=…, adjust=False).mean()"""
    alpha = 2 / (span + 1)
    out, e = [NAN] * len(a), None
    for i, v in enumerate(a):
        e = v if e is None else alpha * v + (1 - alpha) * e
        out[i] = e
    return out


def indicators(rows):
    H = [r[2] for r in rows]
    L = [r[3] for r in rows]
    C = [r[4] for r in rows]
    n = len(rows)
    ma20 = _sma(C, 20)
    sd = _std(C, 20, ma20)
    rsv = [NAN] * n
    for i in range(8, n):
        lo, hi = min(L[i - 8:i + 1]), max(H[i - 8:i + 1])
        # 9 日內最高等於最低（停牌或鎖死）時 pandas 會得到 NaN，這裡當作 50 處理
        rsv[i] = (C[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0
    k = _ewm_adjust(rsv, 2)
    d = _ewm_adjust(k, 2)
    e12, e26 = _ewm_span(C, 12), _ewm_span(C, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ewm_span(dif, 9)
    return {
        "close": C, "ma5": _sma(C, 5), "ma20": ma20, "ma60": _sma(C, 60),
        "bbU": [m + 2 * s for m, s in zip(ma20, sd)], "bbL": [m - 2 * s for m, s in zip(ma20, sd)],
        "k": k, "d": d, "dif": dif, "dea": dea, "hist": [a - b for a, b in zip(dif, dea)],
    }


# ---------------------------------------------------------------- technical score
SWITCHES = ("use_ma_alignment", "use_bollinger_bounce", "use_kd_golden_cross", "use_macd_bullish")
SIG_NAMES = ("均線多頭", "布林下軌反彈", "KD黃金交叉", "MACD多頭")
PARTIAL = (0.48, 0.4, 0.4, 0.4)


def tech_state(ind, i):
    """四個技術訊號在第 i 天的狀態：2 = 完全成立、1 = 部分成立、0 = 不成立、None = 資料不足。"""
    c = ind["close"][i]
    ma20, ma60 = ind["ma20"][i], ind["ma60"][i]
    st = [None, None, None, None]
    if not _isnan(ma20) and not _isnan(ma60):
        st[0] = 2 if c > ma20 > ma60 else (1 if c > ma20 else 0)
    bl, bm = ind["bbL"][i], ind["ma20"][i]
    if not _isnan(bl) and not _isnan(bm):
        dist = (c - bl) / bl if bl else NAN
        st[1] = 2 if (not _isnan(dist) and 0 < dist < 0.03) else (1 if c < bm else 0)
    k, d = ind["k"][i], ind["d"][i]
    if not _isnan(k) and not _isnan(d):
        st[2] = 2 if (k > d and k < 80) else (1 if k > d else 0)
    h = ind["hist"][i]
    if not _isnan(h):
        st[3] = 2 if (h > 0 and ind["dif"][i] > ind["dea"][i]) else (1 if h > 0 else 0)
    return st


def tech_score(state, p):
    on = [bool(p.get(s, True)) for s in SWITCHES]
    per = 100 / (sum(on) or 1)
    score, sigs = 0.0, []
    for j in range(4):
        if not on[j] or state[j] is None:
            continue
        if state[j] == 2:
            score += per
            sigs.append(SIG_NAMES[j])
        elif state[j] == 1:
            score += per * PARTIAL[j]
    return rnd(score), sigs


# ---------------------------------------------------------------- volume patterns
def volume_patterns(rows, i=None):
    """量價型態（倍量柱、梯量柱、縮量柱、低量柱、平量柱、放量滯漲）。回傳 (型態清單, 加減分)。"""
    n = len(rows)
    if n < 21:
        return [], 0
    if i is None:
        i = n - 1
    V = [r[5] for r in rows]
    C = [r[4] for r in rows]
    pats, bonus = [], 0
    vt, vy = V[i], V[i - 1]
    if vy > 0 and vt >= 2 * vy:
        pats.append("倍量柱")
        bonus += 10
    v3, v2, v1 = V[i], V[i - 1], V[i - 2]
    if v3 > v2 > v1 and C[i] > C[i - 2]:
        pats.append("梯量柱")
        bonus += 8
    if v3 < v2 < v1 and C[i] < C[i - 2]:
        pats.append("縮量柱")
        bonus += 5
    seg = V[max(0, i - 20):i]
    v20 = sum(seg) / len(seg) if seg else 0
    if v20 > 0 and vt < 0.6 * v20:
        pats.append("低量柱")
        bonus += 8
    if v1 > 0 and v2 > 0 and v3 > 0 and (max(v1, v2, v3) - min(v1, v2, v3)) / max(v1, v2, v3) < 0.15:
        pats.append("平量柱")
    seg5 = V[max(0, i - 5):i]
    v5 = sum(seg5) / len(seg5) if seg5 else 0
    if v5 > 0 and vt >= 1.5 * v5:
        o, h, c, pc = rows[i][1], rows[i][2], C[i], C[i - 1]
        body = abs(c - o)
        upper = h - max(c, o)
        chg = (c - pc) / pc if pc > 0 else 0
        if c < o or (body > 0 and upper > body * 2) or (0 < chg < 0.01):
            pats.append("放量滯漲")
            bonus -= 20
    return pats, bonus


# ---------------------------------------------------------------- backtest (signal win rate)
def signal_backtest(rows, states, p):
    """對歷史上所有技術分達門檻的日子做持有 N 日結算（原專案 backtest()）。

    第 i 天收盤出訊號 → 第 i+1 天開盤進場 → 看第 i+2 ~ i+1+N 天：
    只碰到停利算贏、碰到停損算輸（同時碰到算輸），都沒碰到就看第 N 天收盤。
    """
    hold = int(p["hold_days"])
    tgt, stp = float(p["target_return"]), float(p["stop_loss"])
    min_s = int(p["min_tech_score_for_signal"])
    n = len(rows)
    wins = losses = 0
    rets = []
    for i in range(60, n - hold - 1):
        if tech_score(states[i], p)[0] < min_s:
            continue
        entry = rows[i + 1][1]
        if not entry or entry <= 0:
            continue
        fut = rows[i + 2:i + 2 + hold]
        if len(fut) < hold:
            continue
        hi = max(r[2] for r in fut)
        lo = min(r[3] for r in fut)
        hit_t, hit_s = hi >= entry * (1 + tgt), lo <= entry * (1 - stp)
        if hit_t and not hit_s:
            wins += 1
            rets.append(tgt)
        elif hit_s:
            losses += 1
            rets.append(-stp)
        else:
            r = (fut[-1][4] - entry) / entry
            rets.append(r)
            if r > 0:
                wins += 1
            else:
                losses += 1
    total = wins + losses
    if not total:
        return None, 0, None
    return rnd(wins / total * 1000) / 1000, total, rnd(sum(rets) / len(rets) * 1e4) / 1e4


# ---------------------------------------------------------------- evaluate (同 engine.js evaluate)
def evaluate(s, p, market_ok=True):
    """s：screener.json 的單檔資料；p：策略參數。回傳 action、總分等。網頁端用同樣的算法。"""
    if s.get("short"):
        return {"action": "SKIP", "score": None}
    st = s["ts"]
    ts, sigs = tech_score(st, p)
    bonus = s["vb"] if p.get("use_volume_patterns", True) else 0
    tech = max(0, min(100, ts + bonus))
    wr = s["bt"][p["id"]][0] if p.get("id") in s.get("bt", {}) else None
    bt_score = (wr if wr is not None else 0.5) * 100
    is_etf = s.get("k") == "ETF"
    if is_etf:
        fund_pass = None
    else:
        eps, roe = s.get("eps"), s.get("roe")
        fund_pass = eps is not None and roe is not None and eps > p["eps_threshold"] and roe > p["roe_threshold"]
    wf, wt, wb = p["weight_fundamental"], p["weight_technical"], p["weight_backtest"]
    if is_etf:
        wf = 0  # ETF 沒有 EPS／ROE，只看技術與回測
    tot = wf + wt + wb
    if tot <= 0:
        wf, wt, wb, tot = 0, 1, 0, 1
    fund_score = 100 if fund_pass else 40
    score = rnd((wf * fund_score + wt * tech + wb * bt_score) / tot * 10) / 10
    gate = is_etf or (not p.get("fundamental_pass_required", True)) or fund_pass
    if score >= p["min_total_score_for_buy"] and gate and tech >= p["min_tech_score_for_buy"]:
        action = "BUY"
    elif score >= 50:
        action = "WATCH"
    else:
        action = "SKIP"
    if action == "BUY" and p.get("market_filter_enabled", True) and not market_ok:
        action = "WATCH"
    return {"action": action, "score": score, "tech": tech, "fund_pass": fund_pass, "signals": sigs}
