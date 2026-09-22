import math
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://fapi.binance.com"
INTERVAL = "15m"

CURRENT_BREAKOUT_WINDOW = 0
RECENT_BREAKOUT_WINDOW = 2
MAX_SETUP_AGE = 12
BREAKOUT_LOOKBACK = 20
RETEST_LOOKBACK = 12
CONFIRMATION_LOOKBACK = 4
MAX_CONFIRMATION_AGE = 2
MAX_RECENT_RETEST_AGE = 2

ENTRY_ZONE_ATR = 0.30
MAX_ENTRY_EXTENSION_ATR = 1.20
BREAKOUT_VOLUME_MIN = 1.20
BREAKOUT_BODY_MIN = 0.50
CONFIRMATION_BODY_MIN = 0.40
VOLUME_RECOVERY_MIN = 0.85
MICRO_BODY_MIN = 0.45

SL_ATR = 1.10
TP1_RR = 1.50
TP2_RR = 2.50

TOP_SYMBOLS = 10
KLINE_LIMIT = 180
TIMEOUT = 15

session = requests.Session()
session.headers.update({"User-Agent": "BinanceSmartScanner/4.25.2"})


def now_ms():
    return int(time.time() * 1000)


def fmt(x, n=4):
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return "NA"
    return f"{x:.{n}f}"


def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def api(path, params=None):
    last = None
    for attempt in range(3):
        try:
            r = session.get(BASE_URL + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(str(last))


def get_symbols():
    data = api("/fapi/v1/exchangeInfo")
    out = []
    for s in data.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
        ):
            out.append(s["symbol"])
    return out


def get_top_symbols():
    allowed = set(get_symbols())
    tickers = api("/fapi/v1/ticker/24hr")
    rows = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in allowed:
            continue
        qv = num(t.get("quoteVolume"))
        if qv is not None:
            rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:TOP_SYMBOLS]


def get_klines(symbol, interval=INTERVAL, limit=KLINE_LIMIT):
    raw = api("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    candles = []
    cutoff = now_ms()
    for row in raw:
        if len(row) < 12:
            continue
        try:
            c = {
                "open_time": int(row[0]),
                "open": num(row[1]),
                "high": num(row[2]),
                "low": num(row[3]),
                "close": num(row[4]),
                "volume": num(row[5]),
                "close_time": int(row[6]),
            }
        except Exception:
            continue
        if c["close_time"] <= cutoff and all(c[k] is not None for k in ("open","high","low","close","volume")):
            if c["high"] >= max(c["open"], c["close"]) and c["low"] <= min(c["open"], c["close"]):
                candles.append(c)
    if len(candles) < 80:
        raise RuntimeError(f"not enough closed candles: {len(candles)}")
    return candles


def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = max(d, 0.0)
        l = max(-d, 0.0)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))


def atr(candles, period=14):
    if len(candles) <= period:
        return None
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            prev = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - prev), abs(c["low"] - prev)))
    return sum(trs[-period:]) / period


def add_metrics(candles):
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    rr = rsi(closes, 14)
    aa = atr(candles, 14)

    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    c = candles[-1]
    rng = c["high"] - c["low"]
    body = abs(c["close"] - c["open"])
    c["ema20"] = e20
    c["ema50"] = e50
    c["rsi"] = rr
    c["atr"] = aa
    c["vol_ratio"] = (c["volume"] / avg_vol) if avg_vol and avg_vol > 0 else None
    c["body_ratio"] = (body / rng) if rng > 0 else 0.0
    c["bull"] = c["close"] > c["open"]
    c["bear"] = c["close"] < c["open"]
    return candles


def trend(candles):
    c = candles[-1]
    if c["ema20"] is None or c["ema50"] is None:
        return "NEUTRAL"
    if c["ema20"] > c["ema50"] and c["close"] > c["ema20"]:
        return "LONG"
    if c["ema20"] < c["ema50"] and c["close"] < c["ema20"]:
        return "SHORT"
    return "NEUTRAL"


def get_mtf(symbol):
    out = {}
    for tf in ("15m", "1h", "4h"):
        cs = get_klines(symbol, tf, 120)
        add_metrics(cs)
        out[tf] = trend(cs)
    return out


def alignment(main, h1, h4):
    higher = [h1, h4]
    opp = "SHORT" if main == "LONG" else "LONG" if main == "SHORT" else None
    same = main
    if main == "NEUTRAL":
        return "MIXED"
    if h1 == opp and h4 == opp:
        return "COUNTER_TREND"
    if (h1 == opp and h4 == same) or (h4 == opp and h1 == same):
        return "MIXED"
    if (h1 == opp or h4 == opp) and (h1 == "NEUTRAL" or h4 == "NEUTRAL"):
        return "PARTIAL_COUNTER_TREND"
    if h1 == same and h4 in (same, "NEUTRAL"):
        return "ALIGNED"
    if h4 == same and h1 in (same, "NEUTRAL"):
        return "ALIGNED"
    return "MIXED"


def breakout_quality(c, direction):
    score = 0
    reasons = []
    if c["body_ratio"] >= BREAKOUT_BODY_MIN:
        score += 1
        reasons.append("BODY")
    if c["vol_ratio"] is not None and c["vol_ratio"] >= BREAKOUT_VOLUME_MIN:
        score += 1
        reasons.append("VOLUME")
    if (direction == "LONG" and c["close"] > c["open"]) or (direction == "SHORT" and c["close"] < c["open"]):
        score += 1
        reasons.append("DIRECTIONAL_CLOSE")
    return score, reasons


def find_breakout(candles):
    start = max(50, len(candles) - MAX_SETUP_AGE - 1)
    end = len(candles) - 1
    candidates = []
    for i in range(start, end + 1):
        c = candles[i]
        if i < 5:
            continue
        prev_high = max(x["high"] for x in candles[i-5:i])
        prev_low = min(x["low"] for x in candles[i-5:i])
        direction = None
        level = None
        if c["close"] > prev_high:
            direction, level = "LONG", prev_high
        elif c["close"] < prev_low:
            direction, level = "SHORT", prev_low
        if direction:
            q, reasons = breakout_quality(c, direction)
            if q >= 2:
                candidates.append({
                    "index": i,
                    "age": len(candles) - 1 - i,
                    "direction": direction,
                    "level": level,
                    "quality": q,
                    "reasons": reasons,
                })
    return candidates[-1] if candidates else None


def retest_class(c, b):
    atrv = c.get("atr")
    if not atrv or atrv <= 0:
        return "INVALID", "ATR_UNAVAILABLE"
    level = b["level"]
    direction = b["direction"]
    touch = (c["low"] <= level + atrv * 0.35 and c["high"] >= level - atrv * 0.35)
    body_cross = (c["open"] <= level <= c["close"]) if direction == "LONG" else (c["open"] >= level >= c["close"])
    close_hold = c["close"] >= level if direction == "LONG" else c["close"] <= level
    if touch and close_hold and body_cross:
        return "HOLD_RETEST_STRONG", "TOUCH|BODY|CLOSE"
    if touch and close_hold:
        return "HOLD_RETEST", "TOUCH|CLOSE"
    if touch and not close_hold:
        return "REJECTION_NO_HOLD", "TOUCH|CLOSE_FAIL"
    if touch:
        return "WICK_TOUCH_ONLY", "WICK_TOUCH"
    return "WEAK_RETEST", "NO_ZONE_INTERSECTION"


def find_retests(candles, b):
    valid = []
    diagnostics = []
    start = b["index"] + 1
    end = len(candles)
    for i in range(start, end):
        c = candles[i]
        cls, why = retest_class(c, b)
        item = {"index": i, "age": len(candles)-1-i, "class": cls, "why": why, "atr": c.get("atr")}
        if cls in ("HOLD_RETEST_STRONG", "HOLD_RETEST"):
            valid.append(item)
        else:
            diagnostics.append(item)
    return valid, diagnostics


def confirmation(candles, b, retest):
    if not retest:
        return {"status":"WAITING", "age":None, "checked":0, "index":None}
    candidates = []
    start = retest["index"] + 1
    end = min(len(candles), retest["index"] + 1 + CONFIRMATION_LOOKBACK)
    for i in range(start, end):
        c = candles[i]
        if c["body_ratio"] < CONFIRMATION_BODY_MIN:
            continue
        if b["direction"] == "LONG" and c["close"] > b["level"] and c["close"] > c["open"]:
            candidates.append(i)
        elif b["direction"] == "SHORT" and c["close"] < b["level"] and c["close"] < c["open"]:
            candidates.append(i)
    if not candidates:
        return {"status":"WAITING", "age":None, "checked":max(0, end-start), "index":None}
    i = candidates[-1]
    age = len(candles)-1-i
    return {
        "status": "CONFIRMED" if age <= MAX_CONFIRMATION_AGE else "STALE",
        "age": age,
        "checked": max(0, end-start),
        "index": i
    }


def volume_recovery(candles, conf):
    if conf["status"] != "CONFIRMED" or conf["index"] is None:
        return {"ok":False, "ratio":None}
    c = candles[conf["index"]]
    ratio = c.get("vol_ratio")
    return {"ok": ratio is not None and ratio >= VOLUME_RECOVERY_MIN, "ratio":ratio}


def entry_zone(price, b, atrv):
    if atrv is None or atrv <= 0:
        return {"status":"NO_ATR","ok":False,"low":None,"high":None,"extension":None}
    low = b["level"] - ENTRY_ZONE_ATR * atrv
    high = b["level"] + ENTRY_ZONE_ATR * atrv
    if price < low:
        ext = (low-price)/atrv
        return {"status":"BELOW","ok":False,"low":low,"high":high,"extension":ext}
    if price > high:
        ext = (price-high)/atrv
        return {"status":"ABOVE","ok":False,"low":low,"high":high,"extension":ext}
    return {"status":"INSIDE","ok":True,"low":low,"high":high,"extension":0.0}


def micro_trigger(candles, b):
    if len(candles) < 2:
        return False, 0.0
    c = candles[-1]
    p = candles[-2]
    if c["body_ratio"] < MICRO_BODY_MIN:
        return False, c["body_ratio"]
    if b["direction"] == "LONG":
        return c["bull"] and c["close"] > p["high"], c["body_ratio"]
    return c["bear"] and c["close"] < p["low"], c["body_ratio"]


def rsi_ok(c, direction):
    r = c.get("rsi")
    if r is None:
        return False
    return 45 <= r <= 75 if direction == "LONG" else 25 <= r <= 55


def readiness(b, retest, conf, vol, zone, micro_ok, align, rsi_okay):
    if not b:
        return 0, "NO BREAKOUT"
    age = b["age"]
    if age > RECENT_BREAKOUT_WINDOW:
        return 0, "WINDOW CLOSED"
    if not retest:
        return 0, "WAIT RETEST"
    if conf["status"] != "CONFIRMED":
        return 1, "WAIT CONFIRMATION"
    if not vol["ok"]:
        return 2, "WAIT VOLUME RECOVERY"
    if not zone["ok"]:
        return 3, "WAIT ENTRY ZONE"
    if not micro_ok:
        return 4, "WAIT MICRO TRIGGER"
    if not rsi_okay:
        return 5, "RSI BLOCKED"
    if align in ("COUNTER_TREND","PARTIAL_COUNTER_TREND"):
        return 5, "COUNTER_TREND_BLOCKED"
    return 5, "ENTRY READY"


def lifecycle(b, retest, conf, vol, zone, micro_ok, align, rsi_okay):
    if not b:
        return "NO_CURRENT_BREAKOUT"
    age = b["age"]
    if age > MAX_SETUP_AGE:
        return "BREAKOUT_EXPIRED"
    if age == CURRENT_BREAKOUT_WINDOW:
        if not retest:
            return "NEW_BREAKOUT"
    if age <= RECENT_BREAKOUT_WINDOW:
        if not retest:
            return "WAIT_RETEST"
        if conf["status"] == "WAITING":
            return "RETEST_HELD"
        if conf["status"] == "STALE":
            return "CONFIRMATION_STALE"
        if not vol["ok"]:
            return "CONFIRMATION"
        if not zone["ok"]:
            return "VOLUME_RECOVERED"
        if not micro_ok:
            return "ENTRY_ZONE_READY"
        if align in ("COUNTER_TREND","PARTIAL_COUNTER_TREND"):
            return "COUNTER_TREND_BLOCKED"
        if not rsi_okay:
            return "RSI_BLOCKED"
        return "ENTRY_READY"
    return "LATE_SETUP"


def technical_score(b, retest, conf, vol, zone, micro_ok):
    score = 0
    if b: score += 2
    if retest: score += 2
    if conf["status"] == "CONFIRMED": score += 2
    if vol["ok"]: score += 1
    if zone["ok"]: score += 1
    if micro_ok: score += 2
    return min(score, 10)


def risk_plan(price, b, atrv):
    if not b or atrv is None or atrv <= 0:
        return None
    if b["direction"] == "LONG":
        sl = price - SL_ATR * atrv
        risk = price - sl
        return {"entry":price, "sl":sl, "tp1":price+risk*TP1_RR, "tp2":price+risk*TP2_RR}
    sl = price + SL_ATR * atrv
    risk = sl - price
    return {"entry":price, "sl":sl, "tp1":price-risk*TP1_RR, "tp2":price-risk*TP2_RR}


def analyze(symbol):
    try:
        candles = get_klines(symbol)
        add_metrics(candles)
        main = trend(candles)
        mtf = get_mtf(symbol)
        align = alignment(main, mtf["1h"], mtf["4h"])
        b = find_breakout(candles)
        price = candles[-1]["close"]
        atrv = candles[-1].get("atr")

        if not b:
            print(f"\n{symbol}: NO BREAKOUT | MAIN {main} | 1H {mtf['1h']} | 4H {mtf['4h']} | ALIGN {align}")
            return

        valid, diagnostics = find_retests(candles, b)
        current_retest = next((x for x in valid if x["age"] == 0), None)
        historical_retest = next((x for x in valid if x["age"] > 0), None)
        authoritative = current_retest if b["age"] <= RECENT_BREAKOUT_WINDOW else historical_retest

        conf = confirmation(candles, b, authoritative)
        vol = volume_recovery(candles, conf)
        zone = entry_zone(price, b, atrv)
        micro_ok, micro_body = micro_trigger(candles, b)
        rsi_okay = rsi_ok(candles[-1], b["direction"])
        score = technical_score(b, authoritative, conf, vol, zone, micro_ok)
        life = lifecycle(b, authoritative, conf, vol, zone, micro_ok, align, rsi_okay)
        ready, ready_status = readiness(b, authoritative, conf, vol, zone, micro_ok, align, rsi_okay)

        final = (
            b["age"] <= RECENT_BREAKOUT_WINDOW
            and current_retest is not None
            and conf["status"] == "CONFIRMED"
            and vol["ok"]
            and zone["ok"]
            and micro_ok
            and rsi_okay
            and align not in ("COUNTER_TREND","PARTIAL_COUNTER_TREND")
        )
        plan = risk_plan(price, b, atrv) if final else None

        print("\n" + "="*70)
        print(symbol)
        print(f"PRICE: {fmt(price,6)}")
        print(f"MAIN: {main} | 1H: {mtf['1h']} | 4H: {mtf['4h']} | ALIGN: {align}")
        print(f"BREAKOUT: {b['direction']} | AGE {b['age']} | LEVEL {fmt(b['level'],6)} | QUALITY {b['quality']}/3 | {'|'.join(b['reasons'])}")
        print(f"CURRENT RETEST: {current_retest['class'] if current_retest else 'NONE'}")
        print(f"HISTORICAL RETEST: {historical_retest['class'] if historical_retest else 'NONE'}")
        print(f"AUTH RETEST: {authoritative['class'] if authoritative else 'NONE'}")
        print(f"CONFIRMATION: {conf['status']} | AGE {conf['age'] if conf['age'] is not None else 'NA'}")
        print(f"VOLUME: {'RECOVERED' if vol['ok'] else 'NOT FRESH'} | RATIO {fmt(vol['ratio'],2)}")
        print(f"ZONE: {zone['status']} | {fmt(zone['low'],6)} - {fmt(zone['high'],6)} | EXT {fmt(zone['extension'],3)} ATR")
        print(f"MICRO: {'YES' if micro_ok else 'NO'} | BODY {fmt(micro_body,2)}")
        print(f"RSI: {fmt(candles[-1].get('rsi'),2)} | {'OK' if rsi_okay else 'BLOCKED'}")
        print(f"TECHNICAL SCORE: {score}/10")
        print(f"READINESS: {ready}/5 | {ready_status}")
        print(f"LIFECYCLE: {life}")
        if plan:
            print(f"RISK PLAN: ENTRY {fmt(plan['entry'],6)} | SL {fmt(plan['sl'],6)} | TP1 {fmt(plan['tp1'],6)} | TP2 {fmt(plan['tp2'],6)}")
        else:
            print("RISK PLAN: NOT READY")
        print("="*70)
    except Exception as e:
        print(f"{symbol}: DATA_ERROR -> {type(e).__name__}: {e}")


def main():
    print("="*70)
    print("BINANCE FUTURES SMART SCANNER PHASE 4.25.2 CLOUD")
    print("MODE: ANALYSIS / PAPER ONLY")
    print("No API keys. No real orders.")
    print(f"UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)

    try:
        top = get_top_symbols()
    except Exception as e:
        print(f"TOP SYMBOL ERROR: {type(e).__name__}: {e}")
        return

    print("\nTOP VOLUMES")
    for sym, qv in top:
        print(f"{sym}: {qv/1e6:.2f}M")

    for sym, _ in top:
        analyze(sym)

    print("\n" + "="*70)
    print(">>> SCAN FINISHED <<<")
    print("="*70)


if __name__ == "__main__":
    main()
