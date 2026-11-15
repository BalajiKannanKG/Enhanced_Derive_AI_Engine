# industrial_scanner.py - Regime-Adaptive Multi-Timeframe Quantitative Confluence Engine (v2)
"""
Drop-in replacement for the v1 scanner. Same entry point, same output keys
(action / confidence / entry_price / stop_loss / take_profit / reasoning /
consensus_score / breakdown), plus extra fields the dashboard can ignore or show.

What changed vs v1 (business view):
  * v1 added fixed point bonuses; many could stack on correlated signals.
    v2 scores 7 independent factors, each -100..+100, then weights them by
    the detected MARKET REGIME (trend / range / squeeze / expansion).
  * Trend-following and mean-reversion no longer fight each other: the
    regime decides which family of signals counts.
  * Hard VETO gates (counter-macro trade, RSI exhaustion, poor reward:risk,
    thin data, dead volatility) turn weak setups into HOLD instead of a trade.
  * SL/TP are structure-aware (swing points, CPR levels) with ATR fallbacks,
    and a minimum reward:risk is enforced.
  * Output carries a setup GRADE (A/B/C), per-factor and per-timeframe
    breakdown, and warnings, so every signal is auditable.
  * backtest_scanner() replays the same logic over history so confidence can
    be checked against real hit rates instead of trusted blindly.

Pure numpy; no pandas_ta dependency. Zero network calls.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TF_WEIGHTS = {"d1": 0.20, "h4": 0.18, "h1": 0.18, "30m": 0.14, "15m": 0.12, "5m": 0.10, "3m": 0.08}

REGIME_WEIGHTS = {
    #            mtf   trend  mom   mrev  brk   struct candle diverg confirm chart
    "TRENDING":  (0.24, 0.18, 0.11, 0.04, 0.07, 0.08, 0.06, 0.07, 0.06, 0.09),
    "RANGING":   (0.10, 0.04, 0.07, 0.22, 0.04, 0.18, 0.10, 0.11, 0.05, 0.09),
    "SQUEEZE":   (0.15, 0.07, 0.10, 0.00, 0.24, 0.07, 0.07, 0.05, 0.12, 0.13),
    "EXPANSION": (0.20, 0.15, 0.11, 0.00, 0.20, 0.07, 0.05, 0.05, 0.08, 0.09),
}
FACTOR_NAMES = ("mtf", "trend", "momentum", "mean_reversion", "breakout", "structure", "candle", "divergence", "confirmation", "chart_pattern")
SUPPORT_NAMES = {"S1", "S2", "S3", "BC", "PIVOT", "SwingLow"}
RESIST_NAMES = {"R1", "R2", "R3", "TC", "PIVOT", "SwingHigh"}

DEFAULT_PROFILE = {
    "primary_tf": "5m",
    "confluence_tf": "h1",
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 3.0,
    "min_consensus_score": 40,   # composite is a weighted average, so 40 is already selective
    "min_adx": 0.0,              # ADX is handled by regime weighting, not a hard gate
    "min_rr": 1.5,
    "min_agreeing_factors": 3,
}
MIN_CANDLES = 30


# --------------------------------------------------------------------------
# Indicator helpers (numpy only)
# --------------------------------------------------------------------------
def _ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def _true_range(h, l, c):
    pc = np.concatenate(([c[0]], c[:-1]))
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def _rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up, dn = _wilder(np.maximum(d, 0), n), _wilder(np.maximum(-d, 0), n)
    rs = np.where(dn == 0, 100.0, up / np.where(dn == 0, 1, dn))
    return np.where(dn == 0, 100.0, 100 - 100 / (1 + rs))


def _adx_di(h, l, c, n: int = 14):
    up, dn = np.diff(h, prepend=h[0]), -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = _wilder(_true_range(h, l, c), n)
    atr = np.where(atr == 0, 1e-9, atr)
    pdi, ndi = 100 * _wilder(pdm, n) / atr, 100 * _wilder(ndm, n) / atr
    dx = 100 * np.abs(pdi - ndi) / np.where(pdi + ndi == 0, 1, pdi + ndi)
    return _wilder(dx, n), pdi, ndi


def _stoch(h, l, c, k: int = 14, d: int = 3):
    kk = np.full(len(c), 50.0)
    for i in range(k - 1, len(c)):
        hh, ll = h[i - k + 1:i + 1].max(), l[i - k + 1:i + 1].min()
        kk[i] = 50.0 if hh == ll else 100 * (c[i] - ll) / (hh - ll)
    dd = np.array([kk[max(0, i - d + 1):i + 1].mean() for i in range(len(kk))])
    return kk, dd


def _bbands(c: np.ndarray, n: int = 16, mult: float = 1.986):
    mid, up, lo = np.empty_like(c), np.empty_like(c), np.empty_like(c)
    for i in range(len(c)):
        w = c[max(0, i - n + 1):i + 1]
        m, s = w.mean(), w.std()
        mid[i], up[i], lo[i] = m, m + mult * s, m - mult * s
    return lo, mid, up


def _macd_hist(c: np.ndarray):
    m = _ema(c, 12) - _ema(c, 26)
    return m - _ema(m, 9)


def _efficiency_ratio(c: np.ndarray, n: int = 20) -> float:
    if len(c) <= n:
        return 0.5
    net = abs(c[-1] - c[-n - 1])
    path = np.abs(np.diff(c[-n - 1:])).sum()
    return float(net / path) if path > 0 else 0.0


def _percentile_rank(series: np.ndarray, value: float) -> float:
    s = series[np.isfinite(series)]
    return float((s <= value).mean() * 100) if len(s) else 50.0


def _swings(h: np.ndarray, l: np.ndarray, k: int = 3):
    hi, lo = _swing_idx(h, l, k)
    return [float(h[i]) for i in hi], [float(l[i]) for i in lo]


def _sgn(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _clip(x: float, lim: float = 100.0) -> float:
    return float(max(-lim, min(lim, x)))


def _swing_idx(h: np.ndarray, l: np.ndarray, k: int = 3):
    """Swing high/low indices. Plateaus (equal highs/lows) collapse to a single swing."""
    hi, lo = [], []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k:i + k + 1].max():
            if hi and i - hi[-1] <= k and h[i] <= h[hi[-1]]:
                continue            # same plateau / weaker neighbour
            if hi and i - hi[-1] <= k:
                hi[-1] = i          # stronger neighbour replaces it
                continue
            hi.append(i)
        if l[i] == l[i - k:i + k + 1].min():
            if lo and i - lo[-1] <= k and l[i] >= l[lo[-1]]:
                continue
            if lo and i - lo[-1] <= k:
                lo[-1] = i
                continue
            lo.append(i)
    return hi, lo


def _macd(c: np.ndarray):
    line = _ema(c, 12) - _ema(c, 26)
    sig = _ema(line, 9)
    return line, sig, line - sig


def _supertrend(h, l, c, n: int = 10, mult: float = 3.0):
    atr = _wilder(_true_range(h, l, c), n)
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fu, fl, d = ub.copy(), lb.copy(), np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < fl[i] else 1
        else:
            d[i] = 1 if c[i] > fu[i] else -1
    return d, np.where(d == 1, fl, fu)


# --------------------------------------------------------------------------
# Per-timeframe state
# --------------------------------------------------------------------------
def _build_state(tf: dict) -> Optional[dict]:
    """Turn one engine timeframe dict (or raw candles) into a feature state."""
    candles = (tf or {}).get("candles") or []
    if len(candles) < 10:
        return None
    o = np.array([x["open"] for x in candles], float)
    h = np.array([x["high"] for x in candles], float)
    l = np.array([x["low"] for x in candles], float)
    c = np.array([x["close"] for x in candles], float)

    atr_s = _wilder(_true_range(h, l, c), 14)
    adx_s, pdi_s, ndi_s = _adx_di(h, l, c)
    rsi_s = _rsi(c)
    k_s, d_s = _stoch(h, l, c)
    bb_lo, bb_mid, bb_up = _bbands(c)
    hist = _macd_hist(c)
    bbw = (bb_up - bb_lo) / np.where(bb_mid == 0, 1, bb_mid)

    def pick(key, fallback):
        # prefer engine values: they were computed on the full 200+ candle frame
        v = tf.get(key)
        ok = isinstance(v, (int, float)) and math.isfinite(v) and v > 0
        return float(v) if ok else float(fallback)

    ema20 = pick("ema20", _ema(c, 20)[-1])
    ema30 = pick("ema30", _ema(c, 30)[-1])
    ema100 = pick("ema100", _ema(c, 100)[-1])
    ema200 = pick("ema200", _ema(c, 200)[-1])
    atr = pick("atr", atr_s[-1])
    adx = pick("adx", adx_s[-1])
    rsi = pick("rsi", rsi_s[-1])
    sk_hist = tf.get("stoch_k_history") or []
    sd_hist = tf.get("stoch_d_history") or []
    stoch_k = float(sk_hist[-1]) if sk_hist else float(k_s[-1])
    stoch_d = float(sd_hist[-1]) if sd_hist else float(d_s[-1])
    stoch_k_prev = float(sk_hist[-2]) if len(sk_hist) > 1 else float(k_s[-2])
    stoch_d_prev = float(sd_hist[-2]) if len(sd_hist) > 1 else float(d_s[-2])

    trend = tf.get("trend")
    if trend not in ("BULLISH", "BEARISH"):
        trend = "BULLISH" if ema30 > ema100 else ("BEARISH" if ema30 < ema100 else "FLAT")

    atr = atr if atr > 0 else float(atr_s[-1]) or 1e-6
    sh, sl_ = _swings(h, l)
    macd_line, macd_sig, macd_h = _macd(c)
    st_dir, st_line = _supertrend(h, l, c)
    flips = np.nonzero(np.diff(st_dir) != 0)[0]
    st_age = int(len(c) - 2 - flips[-1]) if len(flips) else 99
    e20s, e30s = _ema(c, 20), _ema(c, 30)
    gap = np.abs(e20s - e30s) / np.where(atr_s == 0, 1e-9, atr_s)
    sbb_lo, _sm, sbb_up = _bbands(c, 20, 2.0)
    sq = (sbb_up < e20s + 1.5 * atr_s) & (sbb_lo > e20s - 1.5 * atr_s)
    on_idx = np.nonzero(sq)[0]
    since_sq = int(len(c) - 1 - on_idx[-1]) if len(on_idx) else 99
    run = 0
    if len(on_idx):
        j = on_idx[-1]
        while j >= 0 and sq[j]:
            run += 1; j -= 1
    sma20 = np.array([c[max(0, i - 19):i + 1].mean() for i in range(len(c))])
    return {
        "o": o, "h": h, "l": l, "c": c, "n": len(c), "price": float(c[-1]),
        "ema20": ema20, "ema30": ema30, "ema100": ema100, "ema200": ema200,
        "atr": atr, "atr_series": atr_s, "adx": adx, "pdi": float(pdi_s[-1]), "ndi": float(ndi_s[-1]),
        "rsi": rsi, "rsi_series": rsi_s,
        "stoch_k": stoch_k, "stoch_d": stoch_d, "stoch_k_prev": stoch_k_prev, "stoch_d_prev": stoch_d_prev,
        "bb_lo": float(bb_lo[-1]), "bb_up": float(bb_up[-1]), "bb_mid": float(bb_mid[-1]),
        "bbw_pct": _percentile_rank(bbw, float(bbw[-1])),
        "atr_pct": _percentile_rank(atr_s, float(atr_s[-1])),
        "atr_slope": float(atr_s[-1] - atr_s[-4]) if len(atr_s) > 4 else 0.0,
        "macd_hist": float(hist[-1]), "macd_hist_prev": float(hist[-2]),
        "er": _efficiency_ratio(c),
        "trend": trend,
        "swing_highs": sh, "swing_lows": sl_,
        "macd_series": macd_line, "macd_sig_series": macd_sig, "macd_hist_series": macd_h,
        "st_dir": int(st_dir[-1]), "st_age": st_age, "st_line": float(st_line[-1]),
        "ema_gap": gap, "e20s": e20s, "e30s": e30s,
        "squeeze_on": bool(sq[-1]), "since_squeeze": since_sq, "squeeze_run": run,
        "sq_mom": float(c[-1] - sma20[-1]), "sq_mom_prev": float(c[-2] - sma20[-2]),
        "don_hi": float(h[-21:-1].max()) if len(h) > 21 else float(h[:-1].max()),
        "don_lo": float(l[-21:-1].min()) if len(l) > 21 else float(l[:-1].min()),
        "data_ready": bool(tf.get("data_ready")),
    }


def _tf_direction_score(s: dict) -> float:
    """Single-timeframe directional bias, -100..+100."""
    sc = 60 * (1 if s["trend"] == "BULLISH" else -1 if s["trend"] == "BEARISH" else 0)
    if s["ema20"] > s["ema30"] > s["ema100"]:
        sc += 20
    elif s["ema20"] < s["ema30"] < s["ema100"]:
        sc -= 20
    sc += 10 * _sgn(s["price"] - s["ema200"])
    sc += 10 * _sgn(s["macd_hist"])
    return _clip(sc)


# --------------------------------------------------------------------------
# Regime detection
# --------------------------------------------------------------------------
def _detect_regime(p: dict) -> tuple[str, str]:
    squeeze = p["bbw_pct"] <= 20
    if squeeze and p["adx"] < 22:
        return "SQUEEZE", f"BB width in bottom {p['bbw_pct']:.0f}% of range, ADX {p['adx']:.1f}"
    if p["atr_pct"] >= 85 and p["atr_slope"] > 0 and p["bbw_pct"] >= 70:
        return "EXPANSION", f"ATR at {p['atr_pct']:.0f}th pct and rising"
    if p["adx"] >= 22 and p["er"] >= 0.25:
        return "TRENDING", f"ADX {p['adx']:.1f}, efficiency {p['er']:.2f}"
    if p["adx"] < 20 or p["er"] < 0.18:
        return "RANGING", f"ADX {p['adx']:.1f}, efficiency {p['er']:.2f}"
    return ("TRENDING" if p["adx"] >= 22 else "RANGING"), f"transitional (ADX {p['adx']:.1f})"


# --------------------------------------------------------------------------
# Factors, each returns (score -100..100, note)
# --------------------------------------------------------------------------
def _f_mtf(states: dict[str, dict]) -> tuple[float, str, float]:
    num = den = 0.0
    parts, scores = [], {}
    for tf, s in states.items():
        w = TF_WEIGHTS.get(tf, 0.05)
        sc = _tf_direction_score(s)
        scores[tf] = sc
        num += w * sc
        den += w
    if den == 0:
        return 0.0, "no timeframes", 0.0
    score = num / den
    d = _sgn(score)
    agree = sum(TF_WEIGHTS.get(t, .05) for t, v in scores.items() if _sgn(v) == d and d != 0) / den if d else 0.0
    parts = [f"{t}:{v:+.0f}" for t, v in scores.items()]
    return _clip(score), f"MTF weighted {score:+.0f}, {agree*100:.0f}% weight agrees [{' '.join(parts)}]", agree


def _f_trend(p: dict) -> tuple[float, str]:
    if p["adx"] < 18:
        return 0.0, f"no trend (ADX {p['adx']:.1f})"
    di_dir = _sgn(p["pdi"] - p["ndi"])
    stack = 1 if p["ema20"] > p["ema30"] > p["ema100"] else -1 if p["ema20"] < p["ema30"] < p["ema100"] else 0
    d = di_dir if di_dir == stack else (stack or di_dir)
    if d == 0:
        return 0.0, "EMA/DI disagree"
    strength = min(100, 30 + (p["adx"] - 18) * 3.5)
    sc = d * strength
    notes = [f"ADX {p['adx']:.1f} DI{'+' if d > 0 else '-'}"]
    dist = (p["price"] - p["ema20"]) / p["atr"]
    if abs(dist) <= 0.6:
        sc += d * 15
        notes.append("pullback to EMA20")
    elif abs(dist) > 2.5 and _sgn(dist) == d:
        sc *= 0.6
        notes.append(f"overextended {abs(dist):.1f}ATR from EMA20")
    if (d > 0 and p["price"] < p["ema100"]) or (d < 0 and p["price"] > p["ema100"]):
        sc *= 0.5
        notes.append("price on wrong side of EMA100")
    return _clip(sc), ", ".join(notes)


def _f_momentum(p: dict) -> tuple[float, str]:
    sc = (p["rsi"] - 50) * 2.0
    sc = _clip(sc, 60)
    h, hp = p["macd_hist"], p["macd_hist_prev"]
    macd = 25 * _sgn(h) + 15 * _sgn(h - hp)
    return _clip(sc * 0.6 + macd), f"RSI {p['rsi']:.1f}, MACD hist {h:+.4f} ({'rising' if h > hp else 'falling'})"


def _f_mean_reversion(p: dict) -> tuple[float, str]:
    sc, notes = 0.0, []
    k, d = p["stoch_k"], p["stoch_d"]
    if k < 20 and p["rsi"] < 38:
        sc += 55
        notes.append(f"oversold (K {k:.0f}, RSI {p['rsi']:.0f})")
    elif k > 80 and p["rsi"] > 62:
        sc -= 55
        notes.append(f"overbought (K {k:.0f}, RSI {p['rsi']:.0f})")
    cross_up = p["stoch_k_prev"] <= p["stoch_d_prev"] and k > d
    cross_dn = p["stoch_k_prev"] >= p["stoch_d_prev"] and k < d
    if cross_up and k < 40:
        sc += 25
        notes.append("bullish stoch cross low")
    elif cross_dn and k > 60:
        sc -= 25
        notes.append("bearish stoch cross high")
    if p["price"] <= p["bb_lo"]:
        sc += 20
        notes.append("at lower band")
    elif p["price"] >= p["bb_up"]:
        sc -= 20
        notes.append("at upper band")
    return _clip(sc), ", ".join(notes) or "neutral oscillators"


def _f_breakout(p: dict) -> tuple[float, str]:
    last_rng = max(p["h"][-1] - p["l"][-1], 1e-9)
    body = abs(p["c"][-1] - p["o"][-1]) / last_rng
    expanding = p["atr_slope"] > 0
    sc, notes = 0.0, []
    if p["price"] > p["don_hi"] and body >= 0.55:
        sc += 60
        notes.append("Donchian-20 high break")
    elif p["price"] < p["don_lo"] and body >= 0.55:
        sc -= 60
        notes.append("Donchian-20 low break")
    if sc != 0:
        if expanding:
            sc *= 1.25
            notes.append("ATR expanding")
        else:
            sc *= 0.6
            notes.append("no ATR expansion (fake-out risk)")
        if p["bbw_pct"] <= 30:
            sc *= 1.2
            notes.append("squeeze release")
    return _clip(sc), ", ".join(notes) or "no breakout"


def _levels(p: dict, cpr: dict) -> list[tuple[str, float]]:
    lv = [(k.upper(), float(v)) for k, v in (cpr or {}).items()
          if k in ("pivot", "bc", "tc", "r1", "r2", "r3", "s1", "s2", "s3") and isinstance(v, (int, float)) and v > 0]
    lv += [("SwingHigh", x) for x in p["swing_highs"][-4:]] + [("SwingLow", x) for x in p["swing_lows"][-4:]]
    return lv


def _f_structure(p: dict, cpr: dict, macro_dir: int) -> tuple[float, str]:
    price, atr = p["price"], p["atr"]
    sc, notes = 0.0, []
    bull_rej = p["c"][-1] > p["o"][-1] and (p["c"][-1] - p["l"][-1]) / max(p["h"][-1] - p["l"][-1], 1e-9) > 0.6
    bear_rej = p["c"][-1] < p["o"][-1] and (p["h"][-1] - p["c"][-1]) / max(p["h"][-1] - p["l"][-1], 1e-9) > 0.6
    for name, lvl in _levels(p, cpr):
        dist = abs(price - lvl)
        if dist > 0.7 * atr:
            continue
        is_support = lvl <= price + 0.2 * atr and name not in ("R1", "R2", "R3", "SwingHigh")
        is_resist = lvl >= price - 0.2 * atr and name not in ("S1", "S2", "S3", "SwingLow")
        if is_support and bull_rej:
            sc += 30
            notes.append(f"bullish rejection at {name} {lvl:.3f}")
        elif is_support:
            sc += 15
            notes.append(f"near support {name}")
        if is_resist and bear_rej:
            sc -= 30
            notes.append(f"bearish rejection at {name} {lvl:.3f}")
        elif is_resist:
            sc -= 15
            notes.append(f"near resistance {name}")
    piv = (cpr or {}).get("pivot")
    if piv and macro_dir:
        if (price > piv and macro_dir > 0) or (price < piv and macro_dir < 0):
            sc += 15 * macro_dir
            notes.append("holding correct side of CPR pivot")
    return _clip(sc), ", ".join(notes) or "no key level in play"


def _location(p: dict, cpr: dict) -> tuple[list, list]:
    """Which support / resistance zones the latest candle is touching."""
    atr, lo, hi = p["atr"], p["l"][-1], p["h"][-1]
    sup = [n for n, v in _levels(p, cpr) if n in SUPPORT_NAMES and abs(lo - v) <= 0.8 * atr]
    res = [n for n, v in _levels(p, cpr) if n in RESIST_NAMES and abs(hi - v) <= 0.8 * atr]
    if lo <= p["bb_lo"] + 0.2 * atr:
        sup.append("BB lower")
    if hi >= p["bb_up"] - 0.2 * atr:
        res.append("BB upper")
    return sup, res


def _detect_candle_patterns(p: dict, cpr: dict) -> list[dict]:
    """
    1-, 2- and 3-candle patterns. Reversal patterns only count when there is a
    prior move to reverse, and are boosted at support/resistance (otherwise damped).
    """
    o, h, l, c, atr = p["o"], p["h"], p["l"], p["c"], p["atr"]
    n = len(c)
    if n < 6:
        return []
    i = n - 1
    sup, res = _location(p, cpr)
    out: list[dict] = []
    B = lambda k: abs(c[k] - o[k])
    R = lambda k: max(h[k] - l[k], 1e-9)
    U = lambda k: h[k] - max(o[k], c[k])
    L = lambda k: min(o[k], c[k]) - l[k]
    bull = lambda k: c[k] > o[k]
    bear = lambda k: c[k] < o[k]

    def prior(first: int) -> int:
        if first - 4 < 0:
            return 0
        mv = c[first - 1] - c[first - 4]
        return 1 if mv > 0.6 * atr else (-1 if mv < -0.6 * atr else 0)

    def add(name: str, bias: int, base: float, bars: int, reversal: bool):
        s_ = float(base)
        at_lvl = bool(sup if bias > 0 else res)
        if reversal:
            pr = prior(n - bars)
            if pr == bias:
                return  # nothing to reverse
            if pr == 0:
                s_ *= 0.6
            s_ *= 1.35 if at_lvl else 0.85
        out.append({"name": name, "bias": bias, "strength": int(min(100, s_)), "bars": bars, "at_level": at_lvl})

    ri, bi, ui, li = R(i), B(i), U(i), L(i)
    pr1 = prior(i)
    if ri >= 0.4 * atr:
        if bi <= 0.1 * ri:
            if li >= 0.65 * ri:
                add("Dragonfly Doji", 1, 45, 1, True)
            elif ui >= 0.65 * ri:
                add("Gravestone Doji", -1, 45, 1, True)
            elif pr1 != 0:
                add("Doji (indecision after move)", -pr1, 22, 1, True)
        elif li >= 2 * bi and ui <= 0.3 * ri:
            add("Hammer", 1, 58, 1, True) if pr1 <= 0 else add("Hanging Man", -1, 45, 1, True)
        elif ui >= 2 * bi and li <= 0.3 * ri:
            add("Shooting Star", -1, 58, 1, True) if pr1 >= 0 else add("Inverted Hammer", 1, 42, 1, True)
        elif bi >= 0.85 * ri and bi >= 0.8 * atr:
            add("Bullish Marubozu" if bull(i) else "Bearish Marubozu", 1 if bull(i) else -1, 40, 1, False)

    b0 = B(i - 1)
    if b0 >= 0.3 * atr:
        if bull(i) and bear(i - 1) and c[i] >= o[i - 1] and o[i] <= c[i - 1] and B(i) > b0:
            add("Bullish Engulfing", 1, 65, 2, True)
        if bear(i) and bull(i - 1) and c[i] <= o[i - 1] and o[i] >= c[i - 1] and B(i) > b0:
            add("Bearish Engulfing", -1, 65, 2, True)
        mid = (o[i - 1] + c[i - 1]) / 2
        if bear(i - 1) and bull(i) and o[i] <= c[i - 1] and mid < c[i] < o[i - 1]:
            add("Piercing Line", 1, 55, 2, True)
        if bull(i - 1) and bear(i) and o[i] >= c[i - 1] and o[i - 1] < c[i] < mid:
            add("Dark Cloud Cover", -1, 55, 2, True)
        inside = max(o[i], c[i]) <= max(o[i - 1], c[i - 1]) and min(o[i], c[i]) >= min(o[i - 1], c[i - 1])
        if inside and B(i) < 0.6 * b0:
            add("Bullish Harami", 1, 40, 2, True) if bear(i - 1) else add("Bearish Harami", -1, 40, 2, True)
    if abs(l[i] - l[i - 1]) <= 0.1 * atr and bear(i - 1) and bull(i):
        add("Tweezer Bottom", 1, 50, 2, True)
    if abs(h[i] - h[i - 1]) <= 0.1 * atr and bull(i - 1) and bear(i):
        add("Tweezer Top", -1, 50, 2, True)

    b2 = B(i - 2)
    if b2 >= 0.7 * atr and B(i - 1) <= 0.35 * b2:
        if bear(i - 2) and bull(i) and c[i] > (o[i - 2] + c[i - 2]) / 2:
            add("Morning Star", 1, 75, 3, True)
        if bull(i - 2) and bear(i) and c[i] < (o[i - 2] + c[i - 2]) / 2:
            add("Evening Star", -1, 75, 3, True)
    ks = (i - 2, i - 1, i)
    if all(bull(k) and B(k) >= 0.5 * R(k) for k in ks) and c[i] > c[i - 1] > c[i - 2] \
            and all(o[k - 1] <= o[k] <= c[k - 1] for k in ks[1:]):
        add("Three White Soldiers", 1, 55, 3, False)
    if all(bear(k) and B(k) >= 0.5 * R(k) for k in ks) and c[i] < c[i - 1] < c[i - 2] \
            and all(c[k - 1] <= o[k] <= o[k - 1] for k in ks[1:]):
        add("Three Black Crows", -1, 55, 3, False)
    if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
        out.append({"name": "Inside Bar (coiling)", "bias": 0, "strength": 0, "bars": 2, "at_level": False})
    return sorted(out, key=lambda x: -x["strength"])


def _f_candle(an: dict) -> tuple[float, str]:
    pats = an["patterns"]
    bulls = [x["strength"] for x in pats if x["bias"] > 0]
    bears = [x["strength"] for x in pats if x["bias"] < 0]
    sc = (max(bulls, default=0) - max(bears, default=0))
    names = [f"{x['name']}({x['strength']}{'@lvl' if x['at_level'] else ''})" for x in pats if x["strength"] > 0][:3]
    return _clip(sc), ", ".join(names) or "no pattern"


# ----- Divergence (regular + hidden) --------------------------------------------
def _detect_divergences(p: dict) -> list[dict]:
    h, l, atr = p["h"], p["l"], p["atr"]
    n = len(h)
    hi_i, lo_i = _swing_idx(h, l, 3)
    out: list[dict] = []
    inds = {"RSI": (p["rsi_series"], 2.0), "MACD": (p["macd_series"], 0.03 * atr)}
    for name, (ser, thr) in inds.items():
        for side, idxs, px in (("low", lo_i, l), ("high", hi_i, h)):
            if len(idxs) < 2:
                continue
            a, b = idxs[-2], idxs[-1]
            if n - 1 - b > 30 or not (4 <= b - a <= 45):
                continue
            dpx, dind = px[b] - px[a], ser[b] - ser[a]
            fresh = (n - 1 - b) <= 8
            mult = 1.15 if fresh else 0.8
            if side == "low":
                if dpx < -0.15 * atr and dind > thr:
                    out.append({"kind": "Regular Bullish", "indicator": name, "bias": 1, "strength": int(55 * mult), "bars_ago": n - 1 - b})
                elif dpx > 0.15 * atr and dind < -thr:
                    out.append({"kind": "Hidden Bullish", "indicator": name, "bias": 1, "strength": int(35 * mult), "bars_ago": n - 1 - b})
            else:
                if dpx > 0.15 * atr and dind < -thr:
                    out.append({"kind": "Regular Bearish", "indicator": name, "bias": -1, "strength": int(55 * mult), "bars_ago": n - 1 - b})
                elif dpx < -0.15 * atr and dind > thr:
                    out.append({"kind": "Hidden Bearish", "indicator": name, "bias": -1, "strength": int(35 * mult), "bars_ago": n - 1 - b})
    return sorted(out, key=lambda x: -x["strength"])


def _f_divergence(an: dict) -> tuple[float, str]:
    divs = an["divergences"]
    if not divs:
        return 0.0, "no divergence"
    sc = 0.0
    for d_ in divs:
        sc += d_["bias"] * d_["strength"]
    for bias in (1, -1):  # RSI + MACD agreeing is stronger than either alone
        inds = {d_["indicator"] for d_ in divs if d_["bias"] == bias and d_["kind"].startswith("Regular")}
        if len(inds) == 2:
            sc += bias * 20
    return _clip(sc), ", ".join(f"{d_['kind']} {d_['indicator']} ({d_['bars_ago']}b ago)" for d_ in divs[:3])


# ----- Other confirmations: supertrend, Fibonacci, squeeze, convergence, double top/bottom -----
def _detect_confirmations(p: dict, cpr: dict) -> list[dict]:
    h, l, c, o, atr, price = p["h"], p["l"], p["c"], p["o"], p["atr"], p["price"]
    n = len(c)
    out: list[dict] = []

    def add(name, bias, strength, kind):
        out.append({"name": name, "bias": bias, "strength": strength, "kind": kind})

    # Supertrend
    if p["st_age"] <= 3:
        add(f"Supertrend flipped {'up' if p['st_dir'] > 0 else 'down'} {p['st_age']+1} bars ago", p["st_dir"], 40, "supertrend")
    else:
        add(f"Supertrend {'bullish' if p['st_dir'] > 0 else 'bearish'}", p["st_dir"], 20, "supertrend")

    # Fibonacci retracement of the latest swing leg
    hi_i, lo_i = _swing_idx(h, l, 3)
    hi_i, lo_i = [x for x in hi_i if n - 1 - x <= 60], [x for x in lo_i if n - 1 - x <= 60]
    if hi_i and lo_i:
        ih, il = hi_i[-1], lo_i[-1]
        leg = h[ih] - l[il]
        if leg >= 2 * atr:
            if il < ih:   # up-leg, looking for a pullback entry
                retr = (h[ih] - price) / leg
                if 0.382 <= retr <= 0.618:
                    add(f"Fib {retr*100:.0f}% pullback in up-leg", 1, 30 if retr >= 0.5 else 22, "fib")
            else:         # down-leg
                retr = (price - l[il]) / leg
                if 0.382 <= retr <= 0.618:
                    add(f"Fib {retr*100:.0f}% rally in down-leg", -1, 30 if retr >= 0.5 else 22, "fib")

    # Volatility squeeze (Bollinger inside Keltner) and release
    if p["squeeze_on"]:
        add(f"Squeeze ON ({p['squeeze_run']} bars) - energy building", 0, 0, "squeeze")
    elif p["since_squeeze"] <= 3 and p["squeeze_run"] >= 4:
        d_ = _sgn(p["sq_mom"])
        add(f"Squeeze released {p['since_squeeze']} bars ago, momentum {'up' if d_ > 0 else 'down'}", d_,
            45 if abs(p["sq_mom"]) > abs(p["sq_mom_prev"]) else 30, "squeeze")

    # MACD convergence toward signal / cross
    hs = p["macd_hist_series"]
    if len(hs) >= 4:
        if hs[-1] < 0 and hs[-1] > hs[-2] > hs[-3]:
            add("MACD histogram converging up (pre-cross)", 1, 18, "macd")
        elif hs[-1] > 0 and hs[-1] < hs[-2] < hs[-3]:
            add("MACD histogram converging down (pre-cross)", -1, 18, "macd")
        if hs[-2] <= 0 < hs[-1]:
            add("MACD bullish signal cross", 1, 28, "macd")
        elif hs[-2] >= 0 > hs[-1]:
            add("MACD bearish signal cross", -1, 28, "macd")

    # EMA20/30 convergence (coil) and breakout from it
    gap = p["ema_gap"]
    if len(gap) >= 20 and gap[-1] < 0.5 * gap[-20:].mean():
        e_hi, e_lo = max(p["e20s"][-1], p["e30s"][-1]), min(p["e20s"][-1], p["e30s"][-1])
        if price > e_hi and c[-1] > o[-1]:
            add("Breakout above converged EMA20/30 coil", 1, 28, "ema_conv")
        elif price < e_lo and c[-1] < o[-1]:
            add("Breakdown below converged EMA20/30 coil", -1, 28, "ema_conv")
        else:
            add("EMA20/30 converging (coil)", 0, 0, "ema_conv")

    return out


def _f_confirmation(an: dict) -> tuple[float, str]:
    confs = [x for x in an["confirmations"] if x["strength"] > 0]
    sc = sum(x["bias"] * x["strength"] for x in confs)
    return _clip(sc), ", ".join(x["name"] for x in confs[:4]) or "no confirmations"



# ----- Chart patterns: double top/bottom, head & shoulders, triangles, wedges, flags, pennants -----
def _line(xs, ys):
    m, b = np.polyfit(np.asarray(xs, float), np.asarray(ys, float), 1)
    return float(m), float(b)


def _detect_chart_patterns(p: dict, cpr: dict) -> list[dict]:
    """
    Geometric pattern recognition from swing points and fitted trendlines.
    Each result: name, bias, kind (reversal/continuation), status (forming/confirmed),
    breakout level, measured-move target, stop anchor. 'confirmed' = price closed beyond the
    pattern boundary within the last bars (stale breakouts >2.5 ATR away are ignored).
    """
    h, l, c, atr, px = p["h"], p["l"], p["c"], p["atr"], p["price"]
    n = len(c)
    out: list[dict] = []
    if n < 30:
        return out
    hi_i, lo_i = _swing_idx(h, l, 3)
    hi_i = [x for x in hi_i if n - 1 - x <= 60]
    lo_i = [x for x in lo_i if n - 1 - x <= 60]

    o = p["o"]

    def add(name, bias, kind, status, level, target, stop, bars):
        if status == "confirmed" and bias != 0:
            strong = any((c[k] - o[k]) * bias > 0 and abs(c[k] - o[k]) >= 0.5 * max(h[k] - l[k], 1e-9) for k in (n - 1, n - 2))
            if not strong:
                status = "forming"       # beyond the line but no decisive candle yet
        base = 62 if status == "confirmed" else 28
        if bias == 0 and status != "confirmed":
            base = 0
        if status == "confirmed" and p["atr_slope"] > 0:
            base += 8
        out.append({"name": name, "bias": bias, "kind": kind, "status": status, "strength": int(base),
                    "level": round(float(level), 3), "target": round(float(target), 3) if target is not None else None,
                    "stop_anchor": round(float(stop), 3) if stop is not None else None, "bars": int(bars)})

    # --- double top/bottom and head & shoulders (bottoms are tops on the mirrored series) ---
    for top in (True, False):
        PK = h if top else -l
        TR = l if top else -h
        pk_i = hi_i if top else lo_i
        pxt, bias, sg = (px if top else -px), (-1 if top else 1), (1 if top else -1)
        if len(pk_i) >= 2:
            a, b = pk_i[-2], pk_i[-1]
            if 6 <= b - a <= 40 and abs(PK[a] - PK[b]) <= 0.3 * atr and n - 1 - b <= 25:
                neck = float(TR[a:b + 1].min())
                depth = min(PK[a], PK[b]) - neck
                if depth >= 2.5 * atr:
                    brk = neck - pxt
                    nm = "Double Top" if top else "Double Bottom"
                    if 0.1 * atr < brk <= 2.5 * atr:
                        add(nm, bias, "reversal", "confirmed", sg * neck, sg * (neck - depth), sg * max(PK[a], PK[b]), n - a)
                    elif -0.7 * atr < brk <= 0.1 * atr:
                        add(nm, bias, "reversal", "forming", sg * neck, sg * (neck - depth), sg * max(PK[a], PK[b]), n - a)
        if len(pk_i) >= 3:
            i1, i2, i3 = pk_i[-3:]
            if i2 - i1 >= 4 and i3 - i2 >= 4 and n - 1 - i3 <= 25 and PK[i2] - max(PK[i1], PK[i3]) >= 1.0 * atr:
                t1 = i1 + int(np.argmin(TR[i1:i2 + 1]))
                t2 = i2 + int(np.argmin(TR[i2:i3 + 1]))
                if t2 > t1:
                    m, b0 = _line([t1, t2], [TR[t1], TR[t2]])
                    head_h = PK[i2] - (m * i2 + b0)
                    if head_h >= 2.5 * atr and abs(PK[i1] - PK[i3]) <= max(0.6 * atr, 0.2 * head_h):
                        neck_now = m * (n - 1) + b0
                        brk = neck_now - pxt
                        nm = "Head & Shoulders" if top else "Inverse Head & Shoulders"
                        if 0.1 * atr < brk <= 2.5 * atr:
                            add(nm, bias, "reversal", "confirmed", sg * neck_now, sg * (neck_now - head_h), sg * PK[i3], n - i1)
                        elif -0.7 * atr < brk <= 0.1 * atr:
                            add(nm, bias, "reversal", "forming", sg * neck_now, sg * (neck_now - head_h), sg * PK[i3], n - i1)

    # --- triangles and wedges: trendlines through recent swing highs / lows ---
    # Try several swing subsets (most touches first) so an impulsive move before the
    # pattern does not corrupt the trendline fit.
    def try_triangle(hs_, ls_) -> bool:
        start, end = min(hs_[0], ls_[0]), max(hs_[-1], ls_[-1])
        span = end - start
        if span < 12 or n - 1 - end > 8:
            return False
        mu, bu = _line(hs_, [h[i] for i in hs_])
        ml, bl = _line(ls_, [l[i] for i in ls_])
        if not (max(abs(h[i] - (mu * i + bu)) for i in hs_) <= 0.6 * atr
                and max(abs(l[i] - (ml * i + bl)) for i in ls_) <= 0.6 * atr):
            return False
        su, sl_ = mu * span / atr, ml * span / atr
        w0 = (mu * start + bu) - (ml * start + bl)
        w1 = (mu * end + bu) - (ml * end + bl)
        if not (w0 >= 3 * atr and 0.2 * atr < w1 <= 0.8 * w0):   # big enough, and lines converging
            return False
        nm = bias = kind = None
        if abs(su) <= 0.5 and sl_ >= 0.9:
            nm, bias, kind = "Ascending Triangle", 1, "continuation"
        elif abs(sl_) <= 0.5 and su <= -0.9:
            nm, bias, kind = "Descending Triangle", -1, "continuation"
        elif su <= -0.5 and sl_ >= 0.5 and len(hs_) + len(ls_) >= 6 and w1 <= 0.6 * w0:
            nm, bias, kind = "Symmetrical Triangle", 0, "continuation"
        elif su > 0.8 and sl_ > 0.8 and sl_ > su:
            nm, bias, kind = "Rising Wedge", -1, "reversal"
        elif su < -0.8 and sl_ < -0.8 and su < sl_:
            nm, bias, kind = "Falling Wedge", 1, "reversal"
        if not nm:
            return False
        up_now, lo_now = mu * (n - 1) + bu, ml * (n - 1) + bl
        if 0.1 * atr < px - up_now <= 3.0 * atr and bias >= 0:
            add(nm, 1, kind, "confirmed", up_now, up_now + w0, lo_now, n - start)
        elif 0.1 * atr < lo_now - px <= 3.0 * atr and bias <= 0:
            add(nm, -1, kind, "confirmed", lo_now, lo_now - w0, up_now, n - start)
        elif lo_now - 0.3 * atr <= px <= up_now + 0.3 * atr:
            add(nm, bias, kind, "forming", up_now if bias >= 0 else lo_now,
                (up_now + w0) if bias >= 0 else (lo_now - w0), None, n - start)
        else:
            return False
        return True

    combos = sorted(((nh, nl) for nh in (4, 3, 2) for nl in (4, 3, 2) if nh + nl >= 5), key=lambda x: -(x[0] + x[1]))
    for nh, nl in combos:
        if len(hi_i) >= nh and len(lo_i) >= nl and try_triangle(hi_i[-nh:], lo_i[-nl:]):
            break

    # --- flags and pennants: sharp pole, then a shallow channel (flag) or small triangle (pennant) ---
    cands = []
    for j in (0, 1, 2, 3, 4):                # j = bars since a possible breakout
        e = n - 1 - j
        for m_ in range(5, 21):
            s0 = e - m_ + 1
            for pl in range(3, 13):
                a = s0 - pl
                if a < 0:
                    break
                move = c[s0] - c[a]
                if abs(move) < 5 * atr:
                    continue
                path = float(np.abs(np.diff(c[a:s0 + 1])).sum())
                if path <= 0 or abs(move) / path < 0.7:
                    continue
                d = 1 if move > 0 else -1
                pole_top, pole_bot = h[a:s0 + 1].max(), l[a:s0 + 1].min()
                pole_h = pole_top - pole_bot
                seg_h, seg_l = h[s0:e + 1], l[s0:e + 1]
                if d > 0:
                    retr, over = (pole_top - seg_l.min()) / pole_h, seg_h.max() - pole_top
                else:
                    retr, over = (seg_h.max() - pole_bot) / pole_h, pole_bot - seg_l.min()
                if retr > 0.5 or over > 0.3 * atr:
                    continue
                xs = np.arange(s0, e + 1)
                mu, bu = _line(xs, seg_h)
                ml, bl = _line(xs, seg_l)
                w0 = (mu * s0 + bu) - (ml * s0 + bl)
                w1 = (mu * e + bu) - (ml * e + bl)
                if w0 < 0.3 * atr or w1 < 0.1 * atr:
                    continue
                ratio, slope = w1 / w0, (mu + ml) / 2 / atr
                if ratio >= 0.75 and ((d > 0 and slope <= 0.03) or (d < 0 and slope >= -0.03)):
                    shape = "Flag"
                elif 0.1 <= ratio < 0.7:
                    shape = "Pennant"
                else:
                    continue
                up_now, lo_now = mu * (n - 1) + bu, ml * (n - 1) + bl
                brk = (px - up_now) if d > 0 else (lo_now - px)
                lvl = up_now if d > 0 else lo_now
                if 0.1 * atr < brk <= 3.5 * atr:
                    status = "confirmed"
                elif -0.8 * atr <= brk <= 0.1 * atr and j == 0 and lo_now - 0.3 * atr <= px <= up_now + 0.3 * atr:
                    status = "forming"
                else:
                    continue
                nm = ("Bull " if d > 0 else "Bear ") + shape
                stop = float(seg_l.min()) if d > 0 else float(seg_h.max())
                cands.append((status == "confirmed", pole_h, -m_, (nm, d, status, lvl, lvl + d * pole_h, stop, m_ + pl)))
    if cands:
        best = max(cands, key=lambda x: x[:3])[3]
        add(best[0], best[1], "continuation", best[2], best[3], best[4], best[5], best[6])

    return sorted(out, key=lambda x: (x["status"] != "confirmed", -x["strength"]))


def _f_chart(an: dict) -> tuple[float, str]:
    cps = [x for x in an["chart_patterns"] if x["strength"] > 0]
    if not cps:
        return 0.0, "no chart pattern"
    bull = max((x["strength"] for x in cps if x["bias"] > 0), default=0)
    bear = max((x["strength"] for x in cps if x["bias"] < 0), default=0)
    return _clip(1.2 * (bull - bear)), ", ".join(f"{x['name']} ({x['status']})" for x in cps[:3])


def _analyze(p: dict, cpr: dict) -> dict:
    return {"patterns": _detect_candle_patterns(p, cpr), "divergences": _detect_divergences(p),
            "confirmations": _detect_confirmations(p, cpr),
            "chart_patterns": _detect_chart_patterns(p, cpr)}


def _trade_levels(p: dict, d: int, cpr: dict, prof: dict, anchor: Optional[float] = None,
                  price: Optional[float] = None, target: Optional[float] = None):
    """Structure-aware SL/TP. Returns (levels, None) or (None, reason)."""
    atr = p["atr"]
    price = price or p["price"]
    sl_dist = atr * prof.get("sl_atr_mult", 1.5) * (1.25 if p["atr_pct"] > 92 else 1.0)
    swings = p["swing_lows"] if d > 0 else p["swing_highs"]
    cands = [abs(price - sw) + 0.25 * atr for sw in swings if (price - sw) * d > 0]
    if anchor is not None and (price - anchor) * d > 0:
        cands = [abs(price - anchor) + 0.25 * atr]
    cands = [x for x in cands if 1.0 * atr <= x <= 2.5 * atr]
    if cands:
        sl_dist = max(sl_dist * 0.8, min(cands))
    min_rr = prof.get("min_rr", 1.5)
    tp_dist = max(atr * prof.get("tp_atr_mult", 3.0), sl_dist * min_rr)
    if target is not None and (target - price) * d > 0.5 * atr:      # pattern measured move
        tp_dist = max(min(abs(target - price), 6 * atr), sl_dist * min_rr)
    blockers = sorted(abs(lv - price) for _, lv in _levels(p, cpr) if (lv - price) * d > 0.3 * atr)
    note = None
    if blockers and blockers[0] < tp_dist:
        rr_lvl = (blockers[0] - 0.1 * atr) / sl_dist
        if rr_lvl < min_rr:
            return None, f"nearest opposing level gives only {rr_lvl:.2f}R"
        tp_dist = blockers[0] - 0.1 * atr
        note = "TP capped just before opposing level"
    return {"sl": round(price - d * sl_dist, 3), "tp": round(price + d * tp_dist, 3),
            "rr": round(tp_dist / sl_dist, 2), "note": note}, None


# --------------------------------------------------------------------------
# Core evaluation
# --------------------------------------------------------------------------
def _evaluate(
    primary: dict, states: dict[str, dict], macro: Optional[dict], price: float,
    cpr: dict, profile: dict,
) -> dict:
    warnings: list[str] = []
    regime, regime_note = _detect_regime(primary)
    macro_dir = _sgn(_tf_direction_score(macro)) if macro else 0

    f_mtf, n_mtf, agree = _f_mtf(states) if states else (0.0, "single timeframe", 0.0)
    f_trend, n_trend = _f_trend(primary)
    f_mom, n_mom = _f_momentum(primary)
    f_mr, n_mr = _f_mean_reversion(primary)
    f_brk, n_brk = _f_breakout(primary)
    f_str, n_str = _f_structure(primary, cpr, macro_dir)
    an = _analyze(primary, cpr)
    f_can, n_can = _f_candle(an)
    f_div, n_div = _f_divergence(an)
    f_conf, n_conf = _f_confirmation(an)
    f_chart, n_chart = _f_chart(an)

    factors = dict(zip(FACTOR_NAMES, (f_mtf, f_trend, f_mom, f_mr, f_brk, f_str, f_can, f_div, f_conf, f_chart)))
    notes = dict(zip(FACTOR_NAMES, (n_mtf, n_trend, n_mom, n_mr, n_brk, n_str, n_can, n_div, n_conf, n_chart)))
    weights = REGIME_WEIGHTS[regime]
    # Without higher-TF context, renormalise so a single-TF scan is not artificially diluted
    if not states:
        w = list(weights); w[0] = 0.0
        tot = sum(w); weights = tuple(x / tot for x in w)
    score = sum(wt * factors[n] for wt, n in zip(weights, FACTOR_NAMES))
    direction = _sgn(score)
    atr, rsi = primary["atr"], primary["rsi"]

    # ----- VETO / quality gates ------------------------------------------------
    vetoes: list[str] = []
    min_score = profile.get("min_consensus_score", 40)
    threshold = min_score

    if direction != 0 and macro_dir != 0 and direction != macro_dir:
        if regime in ("TRENDING", "EXPANSION"):
            threshold += 15
            warnings.append("Counter-macro-trend setup: threshold raised +15")
        if abs(score) < threshold:
            vetoes.append("counter-macro-trend")
    if direction > 0 and rsi > 75 and f_brk <= 0:
        vetoes.append(f"RSI exhaustion {rsi:.0f} on long")
    if direction < 0 and rsi < 25 and f_brk >= 0:
        vetoes.append(f"RSI exhaustion {rsi:.0f} on short")
    if primary["atr_pct"] < 10:
        vetoes.append("dead volatility (ATR bottom 10%)")
    if primary["adx"] < profile.get("min_adx", 0.0):
        vetoes.append(f"ADX {primary['adx']:.1f} below profile minimum")
    agreeing = sum(1 for v in factors.values() if _sgn(v) == direction and abs(v) >= 15) if direction else 0
    if direction and agreeing < profile.get("min_agreeing_factors", 3):
        vetoes.append(f"only {agreeing} independent factors agree")
    if primary["atr_pct"] > 92:
        warnings.append("Extreme volatility: stops widened, size down")
    if not primary.get("data_ready") and primary["n"] < 75:
        warnings.append("Limited candle history: lower reliability")

    action = "HOLD"
    if abs(score) >= threshold and not vetoes:
        action = "BUY" if direction > 0 else "SELL"

    # ----- Structure-aware SL / TP ----------------------------------------------
    sl = tp = 0.0
    rr = 0.0
    if action in ("BUY", "SELL"):
        d = 1 if action == "BUY" else -1
        lv, why = _trade_levels(primary, d, cpr, profile, price=price)
        if lv is None:
            action = "HOLD"
            vetoes.append(why)
        else:
            sl, tp, rr = lv["sl"], lv["tp"], lv["rr"]
            if lv["note"]:
                warnings.append(lv["note"])

    # ----- Confidence & grade -----------------------------------------------------
    # Confidence is a CONVICTION RANK (how many independent things agree), not a win probability.
    # Deliberately capped at 80; check it against backtest_scanner() before relying on it.
    conf = 30 + 0.45 * abs(score) + 8 * (agree if states else 0.5) + 2 * max(0, agreeing - 3)
    conf -= 6 * len(warnings)
    if action == "HOLD":
        conf = min(conf, 50) - 8 * len(vetoes)
    conf = int(max(20, min(80, conf)))
    grade = "-"
    if action != "HOLD":
        grade = "A" if (abs(score) >= threshold + 20 and agreeing >= 5 and rr >= 2) else \
                "B" if (abs(score) >= threshold + 8 and agreeing >= 4) else "C"
        if grade == "A" and conf < 65: grade = "B"

    return {
        "action": action, "confidence": conf, "score": int(round(score)),
        "entry": round(price, 3), "sl": sl, "tp": tp, "rr": rr, "grade": grade,
        "regime": regime, "regime_note": regime_note, "factors": factors, "notes": notes,
        "weights": dict(zip(FACTOR_NAMES, weights)), "vetoes": vetoes, "warnings": warnings,
        "agreeing": agreeing, "mtf_agreement": round(agree, 2), "atr": atr, "analysis": an, "f_mtf": f_mtf,
    }



# --------------------------------------------------------------------------
# Trade opportunity finder: named setups per timeframe, ranked
# --------------------------------------------------------------------------
def _find_opportunities(states: dict, cpr: dict, prof: dict, price: float, mtf_sign: int, macro_dir: int,
                        cache: Optional[dict] = None, limit: int = 6) -> list[dict]:
    opps: list[dict] = []
    for tf, p in states.items():
        if p["n"] < MIN_CANDLES:
            continue
        an = (cache or {}).get(tf) or _analyze(p, cpr)
        regime, _ = _detect_regime(p)
        tdir_raw = _tf_direction_score(p)
        tdir = _sgn(tdir_raw) if abs(tdir_raw) >= 50 else 0
        pats = [x for x in an["patterns"] if x["strength"] >= 40]
        divs, confs = an["divergences"], an["confirmations"]
        k_up = p["stoch_k_prev"] <= p["stoch_d_prev"] and p["stoch_k"] > p["stoch_d"]
        k_dn = p["stoch_k_prev"] >= p["stoch_d_prev"] and p["stoch_k"] < p["stoch_d"]
        sup, res = _location(p, cpr)

        def stoch_cross(d_):
            return (k_up and p["stoch_k"] < 50) if d_ > 0 else (k_dn and p["stoch_k"] > 50)

        def conf_hits(d_, kinds=None):
            return [x["name"] for x in confs if x["bias"] == d_ and x["strength"] > 0 and (not kinds or x["kind"] in kinds)]

        def emit(kind, d_, why: list[str], trigger: str, anchor_extreme: Optional[float], hold_bars: int,
                 counter_trend: bool = False, target: Optional[float] = None):
            lv, _ = _trade_levels(p, d_, cpr, prof, anchor=anchor_extreme, price=price, target=target)
            if lv is None:
                return
            # Alignment score, NOT a probability. Max ~83, so "A" is genuinely rare.
            score = 15 + 7 * min(len(why), 6)
            score += 8 if d_ == tdir else (-12 if tdir == -d_ else 0)
            score += 8 if d_ == mtf_sign else (-10 if mtf_sign == -d_ else 0)
            score += 5 if lv["rr"] >= 2.5 else 0
            score += 5 if (sup if d_ > 0 else res) else 0
            if counter_trend and macro_dir == -d_:
                score -= 8
            score = int(max(0, min(95, score)))
            if score < 45:
                return
            opps.append({
                "type": kind, "timeframe": tf, "action": "BUY" if d_ > 0 else "SELL", "setup_score": score,
                "grade": "A" if (score >= 75 and lv["rr"] >= 2.0) else "B" if score >= 60 else "C",
                "entry_price": round(price, 3), "stop_loss": lv["sl"], "take_profit": lv["tp"], "risk_reward": lv["rr"],
                "confirmations": why, "trigger": trigger,
                "invalidation": f"Close {'below' if d_ > 0 else 'above'} {lv['sl']} or setup fails within {hold_bars} {tf} bars",
                "counter_trend": bool(counter_trend and (tdir == -d_ or macro_dir == -d_)),
                "regime": regime,
            })

        for d_ in (1, -1):
            side = "Bullish" if d_ > 0 else "Bearish"
            anchor = float(p["l"][-3:].min()) if d_ > 0 else float(p["h"][-3:].max())
            d_pats = [x for x in pats if x["bias"] == d_]
            pat_names = [f"{x['name']} ({x['strength']})" for x in d_pats[:2]]
            reg_div = [x for x in divs if x["bias"] == d_ and x["kind"].startswith("Regular")]
            hid_div = [x for x in divs if x["bias"] == d_ and x["kind"].startswith("Hidden")]
            div_names = [f"{x['kind']} {x['indicator']} divergence" for x in (reg_div or hid_div)[:2]]

            # 1. Trend pullback
            near_ma = abs(p["price"] - p["ema20"]) <= 0.8 * p["atr"] or abs(p["price"] - p["ema30"]) <= 0.8 * p["atr"]
            if tdir == d_ and p["adx"] >= 20 and near_ma:
                trig = pat_names[:1] or (["Stochastic cross"] if stoch_cross(d_) else []) or conf_hits(d_, {"macd"})[:1]
                if trig:
                    why = [f"{side} EMA stack + DI (ADX {p['adx']:.0f})", "Pullback to EMA20/30"] + conf_hits(d_, {"supertrend", "fib", "macd"}) + pat_names
                    if stoch_cross(d_): why.append("Stochastic cross")
                    if len(why) >= 4:
                        emit("Trend Pullback", d_, why, trig[0], anchor, 12)

            # 2. Reversal with regular divergence at a level
            at_zone = bool(sup if d_ > 0 else res)
            if reg_div and (d_pats or stoch_cross(d_)) and at_zone:
                why = div_names + pat_names + conf_hits(d_) + (["Stochastic cross"] if stoch_cross(d_) else []) + [f"At {(sup if d_ > 0 else res)[0]}"]
                emit("Divergence Reversal", d_, why, (pat_names or ["Stochastic cross"])[0], anchor, 15, counter_trend=True)

            # 3. Hidden divergence continuation
            if hid_div and tdir == d_ and (d_pats or stoch_cross(d_)):
                why = div_names + [f"{side} trend intact"] + pat_names + conf_hits(d_, {"supertrend", "fib"})
                emit("Hidden Divergence Continuation", d_, why, (pat_names or ["Stochastic cross"])[0], anchor, 12)

            # 4. Squeeze breakout
            sq_conf = [x for x in confs if x["kind"] == "squeeze" and x["bias"] == d_ and x["strength"] > 0]
            rng_last = max(p["h"][-1] - p["l"][-1], 1e-9)
            body_ok = abs(p["c"][-1] - p["o"][-1]) / rng_last >= 0.55 and (p["c"][-1] > p["o"][-1]) == (d_ > 0)
            donchian = p["price"] > p["don_hi"] if d_ > 0 else p["price"] < p["don_lo"]
            if sq_conf and (body_ok or donchian):
                why = [x["name"] for x in sq_conf] + (["Donchian-20 break"] if donchian else []) + \
                      (["Strong breakout candle"] if body_ok else []) + conf_hits(d_, {"ema_conv", "supertrend"}) + \
                      (["ATR expanding"] if p["atr_slope"] > 0 else [])
                emit("Squeeze Breakout", d_, why, "Squeeze release + breakout candle", None, 10)

            # 5. Range fade
            if regime == "RANGING" and (sup if d_ > 0 else res):
                ext = p["stoch_k"] < 25 if d_ > 0 else p["stoch_k"] > 75
                rs_ext = p["rsi"] < 40 if d_ > 0 else p["rsi"] > 60
                if ext and rs_ext and (d_pats or stoch_cross(d_)):
                    why = [f"Range regime (ADX {p['adx']:.0f})", f"At {(sup if d_ > 0 else res)[0]}",
                           f"Stoch {p['stoch_k']:.0f} / RSI {p['rsi']:.0f} extreme"] + pat_names + div_names
                    emit("Range Fade", d_, why, (pat_names or ["Stochastic cross"])[0], anchor, 10, counter_trend=True)

            # 6. Chart-pattern breakout (double top/bottom, H&S, triangles, wedges, flags, pennants)
            for cp in [x for x in an["chart_patterns"] if x["status"] == "confirmed" and x["bias"] == d_][:1]:
                extra = bool(pat_names or div_names or stoch_cross(d_) or conf_hits(d_, {"supertrend", "macd", "squeeze"}))
                if cp["kind"] == "continuation" and d_ != tdir and d_ != mtf_sign:
                    continue            # continuation patterns must agree with the prevailing trend
                if cp["kind"] == "reversal" and not extra:
                    continue            # reversal patterns need a second, independent confirmation
                why = [f"{cp['name']} breakout ({cp['bars']} bars)", f"Boundary {cp['level']}",
                       f"Measured target {cp['target']}"] + div_names + pat_names + conf_hits(d_, {"supertrend", "macd", "squeeze"})
                emit(f"Chart Pattern: {cp['name']}", d_, why, f"Close beyond {cp['level']}", cp["stop_anchor"], 15,
                     counter_trend=(cp["kind"] == "reversal"), target=cp["target"])

    # de-duplicate identical (type, tf, action) and rank
    best: dict = {}
    for o_ in opps:
        key = (o_["type"], o_["timeframe"], o_["action"])
        if key not in best or o_["setup_score"] > best[key]["setup_score"]:
            best[key] = o_
    ranked = sorted(best.values(), key=lambda x: (-x["setup_score"], -x["risk_reward"]))
    return ranked[:limit]

# --------------------------------------------------------------------------
# Public API (signature identical to v1, optional 5th arg)
# --------------------------------------------------------------------------
def perform_industrial_technical_scan(
    symbol: str, timeframes_dict: dict, current_price: float, cpr: dict,
    profile: Optional[dict] = None,
) -> dict:
    prof = {**DEFAULT_PROFILE, **(profile or {})}
    try:
        states: dict[str, dict] = {}
        for tf, data in (timeframes_dict or {}).items():
            st = _build_state(data)
            if st is not None and st["n"] >= 10:
                states[tf] = st

        primary_key = next((k for k in (prof["primary_tf"], "5m", "3m", "15m") if k in states), None)
        macro_key = next((k for k in (prof["confluence_tf"], "h1", "h4", "d1") if k in states and k != primary_key), None)
        if primary_key is None or states[primary_key]["n"] < MIN_CANDLES:
            return _hold(current_price, "Insufficient local candle history for quantitative evaluation.")

        price = float(current_price) if current_price and current_price > 0 else states[primary_key]["price"]
        r = _evaluate(states[primary_key], states, states.get(macro_key), price, cpr or {}, prof)
    except Exception as exc:  # never let the scanner crash the HTTP handler
        return _hold(current_price, f"Scanner error: {exc}")

    f = r["factors"]
    cache = {primary_key: r["analysis"]}
    opps = _find_opportunities(states, cpr or {}, prof, price, _sgn(r["f_mtf"]),
                               _sgn(_tf_direction_score(states[macro_key])) if macro_key else 0, cache)
    an = r["analysis"]
    reasoning = [f"[{r['regime']}] {r['regime_note']}"]
    ordered = sorted(FACTOR_NAMES, key=lambda n: -abs(f[n] * r["weights"][n]))
    for n in ordered:
        if r["weights"][n] > 0 and abs(f[n]) >= 15:
            reasoning.append(f"{n.replace('_', ' ').title()} {f[n]:+.0f}: {r['notes'][n]}")
    if opps and r["action"] == "HOLD":
        b = opps[0]
        reasoning.append(f"Best setup elsewhere: {b['grade']} {b['type']} {b['action']} on {b['timeframe']} (score {b['setup_score']})")
    if r["action"] == "HOLD":
        reasoning.append("HOLD: " + ("; ".join(r["vetoes"]) if r["vetoes"] else f"composite {r['score']:+d} below threshold"))
    else:
        reasoning.append(f"Grade {r['grade']} | R:R {r['rr']} | {r['agreeing']} factors agree")
    reasoning += [f"WARNING: {w}" for w in r["warnings"]]

    p = states[primary_key]
    return {
        "action": r["action"],
        "confidence": r["confidence"],
        "consensus_score": r["score"],
        "entry_price": r["entry"],
        "stop_loss": r["sl"],
        "take_profit": r["tp"],
        "reasoning": " | ".join(reasoning),
        # ---- new in v2 (UI may ignore) ----
        "grade": r["grade"],
        "regime": r["regime"],
        "risk_reward": r["rr"],
        "factor_scores": {k: round(v, 1) for k, v in f.items()},
        "timeframe_bias": {tf: round(_tf_direction_score(s)) for tf, s in states.items()},
        "vetoes": r["vetoes"],
        "warnings": r["warnings"],
        "candle_patterns": [{k: x[k] for k in ("name", "bias", "strength", "at_level")} for x in an["patterns"]],
        "divergences": an["divergences"],
        "chart_patterns": an["chart_patterns"],
        "confirmations": [{k: x[k] for k in ("name", "bias", "strength")} for x in an["confirmations"]],
        "opportunities": opps,
        "breakdown": {
            "adx": round(p["adx"], 1), "rsi": round(p["rsi"], 1), "stoch_k": round(p["stoch_k"], 1),
            "atr": round(p["atr"], 3), "atr_percentile": round(p["atr_pct"]), "bb_width_percentile": round(p["bbw_pct"]),
            "efficiency_ratio": round(p["er"], 2),
            "macro_trend": states[macro_key]["trend"] if macro_key else "FLAT",
            "primary_tf": primary_key, "macro_tf": macro_key, "mtf_agreement": r["mtf_agreement"],
        },
    }


def _hold(price: float, why: str) -> dict:
    return {
        "action": "HOLD", "confidence": 0, "consensus_score": 0,
        "entry_price": round(float(price or 0), 3), "stop_loss": 0.0, "take_profit": 0.0,
        "reasoning": why, "grade": "-", "regime": "UNKNOWN", "risk_reward": 0.0,
        "factor_scores": {}, "timeframe_bias": {}, "vetoes": [why], "warnings": [], "breakdown": {},
        "candle_patterns": [], "divergences": [], "chart_patterns": [], "confirmations": [], "opportunities": [],
    }


# --------------------------------------------------------------------------
# Validation: replay the logic over history to check it against reality
# --------------------------------------------------------------------------
def backtest_scanner(candles: list[dict], profile: Optional[dict] = None, warmup: int = 60,
                     max_hold: int = 40, cooldown: int = 5, cpr: Optional[dict] = None) -> dict:
    """
    Single-timeframe replay (no look-ahead). Opens a trade whenever the scanner says
    BUY/SELL, then walks forward until SL, TP or max_hold bars (SL wins ties).
    Returns win rate, expectancy in R, profit factor and per-regime / per-grade stats.
    Use it to compare against the 'confidence' number before trusting it.
    """
    prof = {**DEFAULT_PROFILE, **(profile or {})}
    trades, i = [], warmup
    while i < len(candles) - 2:
        window = candles[max(0, i - 74):i + 1]
        st = _build_state({"candles": window})
        if st is None:
            i += 1
            continue
        r = _evaluate(st, {}, None, st["price"], cpr or {}, prof)
        if r["action"] == "HOLD":
            i += 1
            continue
        d = 1 if r["action"] == "BUY" else -1
        risk = abs(r["entry"] - r["sl"])
        outcome_r, j = 0.0, i + 1
        for j in range(i + 1, min(len(candles), i + 1 + max_hold)):
            hi, lo = candles[j]["high"], candles[j]["low"]
            hit_sl = lo <= r["sl"] if d > 0 else hi >= r["sl"]
            hit_tp = hi >= r["tp"] if d > 0 else lo <= r["tp"]
            if hit_sl:
                outcome_r = -1.0; break
            if hit_tp:
                outcome_r = abs(r["tp"] - r["entry"]) / risk; break
        else:
            outcome_r = d * (candles[min(len(candles) - 1, i + max_hold)]["close"] - r["entry"]) / risk
        trades.append({"r": outcome_r, "regime": r["regime"], "grade": r["grade"], "conf": r["confidence"]})
        i = j + cooldown

    if not trades:
        return {"trades": 0, "note": "No signals generated."}
    rs = np.array([t["r"] for t in trades])
    wins, losses = rs[rs > 0], rs[rs < 0]

    def group(key):
        out = {}
        for k in sorted({t[key] for t in trades}):
            g = np.array([t["r"] for t in trades if t[key] == k])
            out[k] = {"trades": len(g), "win_rate": round(float((g > 0).mean()) * 100, 1), "expectancy_R": round(float(g.mean()), 3)}
        return out

    return {
        "trades": len(trades),
        "win_rate_pct": round(float((rs > 0).mean()) * 100, 1),
        "avg_confidence": round(float(np.mean([t["conf"] for t in trades])), 1),
        "expectancy_R": round(float(rs.mean()), 3),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) and losses.sum() != 0 else None,
        "max_consecutive_losses": int(max((len(list(g)) for k, g in __import__("itertools").groupby(rs < 0) if k), default=0)),
        "by_regime": group("regime"), "by_grade": group("grade"),
    }


def backtest_opportunities(candles: list[dict], profile: Optional[dict] = None, min_score: int = 60,
                           warmup: int = 60, max_hold: int = 40, cooldown: int = 5, cpr: Optional[dict] = None) -> dict:
    """
    Replays _find_opportunities() on one timeframe, takes the top-ranked setup whenever its
    score >= min_score, and reports results PER SETUP TYPE. Tells you which setups are worth
    trading on your data and which to switch off.
    """
    prof = {**DEFAULT_PROFILE, **(profile or {})}
    trades, i = [], warmup
    while i < len(candles) - 2:
        st = _build_state({"candles": candles[max(0, i - 74):i + 1]})
        if st is None:
            i += 1
            continue
        opps = _find_opportunities({"tf": st}, cpr or {}, prof, st["price"], 0, 0, limit=1)
        if not opps or opps[0]["setup_score"] < min_score:
            i += 1
            continue
        o_ = opps[0]
        d = 1 if o_["action"] == "BUY" else -1
        risk = abs(o_["entry_price"] - o_["stop_loss"])
        if risk <= 0:
            i += 1
            continue
        res_r, j = None, i + 1
        for j in range(i + 1, min(len(candles), i + 1 + max_hold)):
            hi, lo = candles[j]["high"], candles[j]["low"]
            if (lo <= o_["stop_loss"]) if d > 0 else (hi >= o_["stop_loss"]):
                res_r = -1.0; break
            if (hi >= o_["take_profit"]) if d > 0 else (lo <= o_["take_profit"]):
                res_r = o_["risk_reward"]; break
        if res_r is None:
            res_r = d * (candles[min(len(candles) - 1, i + max_hold)]["close"] - o_["entry_price"]) / risk
        trades.append({"r": res_r, "type": o_["type"], "grade": o_["grade"], "score": o_["setup_score"]})
        i = j + cooldown
    if not trades:
        return {"trades": 0, "note": "No setups met the minimum score."}
    rs = np.array([t["r"] for t in trades])

    def group(key):
        out = {}
        for k in sorted({t[key] for t in trades}):
            g = np.array([t["r"] for t in trades if t[key] == k])
            out[k] = {"trades": len(g), "win_rate": round(float((g > 0).mean()) * 100, 1), "expectancy_R": round(float(g.mean()), 3)}
        return out
    return {"trades": len(trades), "win_rate_pct": round(float((rs > 0).mean()) * 100, 1),
            "expectancy_R": round(float(rs.mean()), 3), "by_setup_type": group("type"), "by_grade": group("grade")}
