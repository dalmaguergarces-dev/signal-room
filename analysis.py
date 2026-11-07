"""Atlas's analysis, server side.

A line-for-line port of analyze() / zones() / choppiness() in index.html, so the
server can call trades (and push phone alerts) while nobody has the page open.
If you change the scoring in one place, change it in the other.
"""
import math


def ema(arr, n):
    k = 2 / (n + 1)
    out, p = [], None
    for i, x in enumerate(arr):
        p = x if i == 0 else x * k + p * (1 - k)
        out.append(p)
    return out


def rsi(c, n=14):
    if len(c) < n + 1:
        return 50.0
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def atr(cs, n=14):
    tr = []
    for i, x in enumerate(cs):
        if i == 0:
            tr.append(x["h"] - x["l"])
        else:
            pc = cs[i - 1]["c"]
            tr.append(max(x["h"] - x["l"], abs(x["h"] - pc), abs(x["l"] - pc)))
    a = sum(tr[:n]) / n
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
    return a


def zones(cs, A):
    fvg, ob = [], []
    vols = [x["v"] for x in cs]

    def avg_v(i):
        s = vols[max(0, i - 20):i]
        return sum(s) / len(s) if s else 0

    for i in range(1, len(cs) - 1):
        p, n = cs[i - 1], cs[i + 1]
        if n["l"] > p["h"] and n["l"] - p["h"] >= .15 * A:
            fvg.append({"i": i, "type": 1, "bot": p["h"], "top": n["l"]})
        if n["h"] < p["l"] and p["l"] - n["h"] >= .15 * A:
            fvg.append({"i": i, "type": -1, "bot": n["h"], "top": p["l"]})
        c = cs[i]
        body, av = abs(c["c"] - c["o"]), avg_v(i)
        if body > 1.5 * A and av > 0 and c["v"] > 1.5 * av:
            d = 1 if c["c"] > c["o"] else -1
            for j in range(i - 1, max(0, i - 3) - 1, -1):
                o = cs[j]
                if (1 if o["c"] > o["o"] else -1) == -d:
                    ob.append({"i": j, "type": d, "bot": o["l"], "top": o["h"]})
                    break
    for z in fvg + ob:
        z["filled"] = False
        for k in range(z["i"] + 2, len(cs)):
            if (cs[k]["l"] <= z["bot"]) if z["type"] == 1 else (cs[k]["h"] >= z["top"]):
                z["filled"] = True
                break
    return fvg, ob


def choppiness(cs, n=14):
    s = cs[-n - 1:]
    tr = 0.0
    for i in range(1, len(s)):
        tr += max(s[i]["h"] - s[i]["l"], abs(s[i]["h"] - s[i - 1]["c"]), abs(s[i]["l"] - s[i - 1]["c"]))
    rng = max(x["h"] for x in s[1:]) - min(x["l"] for x in s[1:])
    return 100 * math.log10(tr / rng) / math.log10(n) if rng > 0 and tr > 0 else 50.0


def analyze(meta, depth=None):
    """meta: the /api/candles payload. Returns a dict like the page's analyze()."""
    cs = meta.get("candles") or []
    if len(cs) < 60:
        return None
    closes = [x["c"] for x in cs]
    price = closes[-1]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    e200 = ema(closes, min(200, len(closes) - 1))
    m12, m26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b for a, b in zip(m12, m26)]
    sig = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig)]
    R, A = rsi(closes), atr(cs)
    score = 0.0

    up = price > e50[-1] and e20[-1] > e50[-1]
    dn = price < e50[-1] and e20[-1] < e50[-1]
    score += 2 if up else -2 if dn else 0
    score += 1 if price > e200[-1] else -1
    if R < 30:
        score += 1.5
    elif R > 70:
        score -= 1.5
    elif up and 50 < R < 65:
        score += .5
    elif dn and 35 < R < 50:
        score -= .5
    h1, h0 = hist[-1], hist[-2]
    score += 1 if (h1 > 0 and h1 > h0) else -1 if (h1 < 0 and h1 < h0) else 0

    vols = [x["v"] for x in cs]
    lc = cs[-2]
    prev20 = vols[-22:-2]
    avg_v = sum(prev20) / 20 if len(prev20) == 20 else 0
    vr = lc["v"] / avg_v if avg_v > 0 else 0
    if vr > 1.5:
        score += 1 if lc["c"] > lc["o"] else -1

    buy = tot = 0.0
    for x in cs[-10:]:
        if meta.get("flow") == "taker":
            buy += x.get("tb", 0)
            tot += x["v"]
        else:
            rng = x["h"] - x["l"]
            buy += x["v"] * (x["c"] - x["l"]) / rng if rng > 0 else x["v"] / 2
            tot += x["v"]
    fr = buy / tot if tot > 0 else .5
    score += 1 if fr > .55 else -1 if fr < .45 else 0

    max_pts = 8.5
    if depth and depth.get("bids"):
        lo, hi = price * .995, price * 1.005
        b = sum(q for p, q in depth["bids"] if p >= lo)
        a = sum(q for p, q in depth["asks"] if p <= hi)
        im = b / (b + a) if b + a > 0 else .5
        score += 1 if im > .6 else -1 if im < .4 else 0
        max_pts = 9.5

    w = cs[-50:]
    pv = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in w)
    vv = sum(x["v"] for x in w)
    if vv > 0:
        score += .5 if price > pv / vv else -.5

    fvg, ob = zones(cs, A)
    allz = fvg + ob
    in_bull = any(not z["filled"] and z["type"] == 1 and z["bot"] <= price <= z["top"] * 1.002 for z in allz)
    in_bear = any(not z["filled"] and z["type"] == -1 and z["bot"] * .998 <= price <= z["top"] for z in allz)
    score += .5 if in_bull else -.5 if in_bear else 0
    chop = choppiness(cs, 14)

    conf = min(100, round(abs(score) / max_pts * 100))
    # A+ setups only: strong score AND trend (EMA 20/50) and bias (EMA 200) agree.
    quality = .6
    above200 = price > e200[-1]
    if score >= max_pts * quality and up and above200:
        signal = "LONG"
    elif score <= -max_pts * quality and dn and not above200:
        signal = "SHORT"
    else:
        signal = "WAIT"
    if chop > 61.8 and signal != "WAIT":
        signal = "WAIT"
    if meta.get("kind") != "crypto" and not meta.get("open"):
        signal = "CLOSED"
    d = -1 if signal == "SHORT" else 1
    # Swing targets: stop 2 ATR away; Target 1 at 3R, Target 2 at 6R.
    risk, rr1, rr2 = 2 * A, 3, 6
    stop, tp1, tp2 = price - d * risk, price + d * rr1 * risk, price + d * rr2 * risk
    rr_block = None
    if signal in ("LONG", "SHORT"):
        levels = [(z["bot"] if d == 1 else z["top"]) for z in allz if not z["filled"] and z["type"] == -d]
        if d == 1:
            levels = [lv for lv in levels if price * 1.0005 < lv < price + rr1 * risk]
        else:
            levels = [lv for lv in levels if price - rr1 * risk < lv < price * .9995]
        if levels:
            rr_block = min(levels, key=lambda lv: abs(lv - price))
            signal = "WAIT"
    return {"price": price, "score": score, "conf": conf, "signal": signal, "A": A,
            "stop": stop, "tp1": tp1, "tp2": tp2, "dir": d, "chop": chop, "rr_block": rr_block}


def replay(meta, warmup=210):
    """Past results of these exact rules on this market's history: walk forward candle
    by candle, and for every new call see what was hit first. The plan being scored:
    half off at Target 1 with the stop moved to entry, the rest to Target 2.
    (No past order book exists, so that one factor is left out here.)"""
    cs = meta.get("candles") or []
    out = {"calls": 0, "stop": 0, "t1_be": 0, "t2": 0, "open": 0}
    prev = None
    for i in range(warmup, len(cs) - 1):
        a = analyze({**meta, "candles": cs[:i + 1], "open": True})
        sig = a["signal"] if a else None
        if sig in ("LONG", "SHORT") and sig != prev:
            out["calls"] += 1
            d, entry, stop, t1, t2 = a["dir"], a["price"], a["stop"], a["tp1"], a["tp2"]
            hit1, res = False, "open"
            for c in cs[i + 1:]:
                guard = entry if hit1 else stop
                if (c["l"] <= guard) if d == 1 else (c["h"] >= guard):
                    res = "t1_be" if hit1 else "stop"
                    break
                if (c["h"] >= t1) if d == 1 else (c["l"] <= t1):
                    hit1 = True
                if (c["h"] >= t2) if d == 1 else (c["l"] <= t2):
                    res = "t2"
                    break
            out[res] += 1
        prev = sig
    closed = out["calls"] - out["open"]
    reached1 = out["t1_be"] + out["t2"]
    out.update(
        closed=closed,
        t1_rate=round(100 * reached1 / closed) if closed else None,
        t2_rate=round(100 * out["t2"] / closed) if closed else None,
        stop_rate=round(100 * out["stop"] / closed) if closed else None,
        result_r=round(4.5 * out["t2"] + 1.5 * out["t1_be"] - out["stop"], 1),
        bars=len(cs) - warmup,
    )
    return out
