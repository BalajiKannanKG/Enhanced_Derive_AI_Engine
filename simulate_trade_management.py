"""Compare exit rules on identical entries: today's fixed stop/target against break-even + trailing stop.

Both rules use the same entries, the same initial stop (1.5 ATR) and pay the same spread; only the exit differs,
so the comparison isolates what trade management does. Prices are synthetic (three kinds of market) unless you pass
your own candles, which is the more useful use: ``python simulate_trade_management.py --csv candles.csv``
(columns: open,high,low,close; oldest first).

How a bar is simulated (deliberately pessimistic):
  * a stop is checked first, then the target, using the bar's low/high, so if both could have been hit the loss wins;
  * the manager acts at the bar's close, using the best price of the bars so far, and a new stop applies from the
    next bar. The live engine acts every second, so it can only do better than this on timing.

Nothing here predicts live results. A better exit rule cannot create an edge that the entries do not have: on a
pure random walk every rule has an expectancy of zero before costs.
"""
import argparse
import csv
import math
import random
import statistics

from trade_manager import ManagementConfig, decide

INITIAL_STOP_ATR = 1.5
FIXED_TARGET_ATR = 3.0
SPREAD_ATR = 0.05
ATR_PERIOD = 14
CFG = ManagementConfig(be_trigger_r=1.0, trail_start_r=1.5, trail_atr_mult=2.0)


def make_bars(kind, n, seed):
    rng = random.Random(seed)
    price, bars, drift = 1000.0, [], 0.0
    for i in range(n):
        if kind == "trending":
            if rng.random() < 0.01:                         # regimes last ~100 bars
                drift = rng.choice((-1, 1)) * rng.uniform(0.15, 0.45)
            step = drift + rng.gauss(0, 1.0)
        elif kind == "mean-reverting":
            step = -0.12 * (price - 1000.0) + rng.gauss(0, 1.0)
        else:
            step = rng.gauss(0, 1.0)
        o, c = price, price + step
        wick = abs(rng.gauss(0, 0.45))
        bars.append((o, max(o, c) + wick, min(o, c) - wick, c))
        price = c
    return bars


def atr_series(bars):
    out, trs = [], []
    prev_close = bars[0][3]
    for o, h, l, c in bars:
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        window = trs[-ATR_PERIOD:]
        out.append(sum(window) / len(window))
        prev_close = c
    return out


def entries(bars, atrs, kind, entry_rule, rng, gap=30):
    """Non-overlapping entry points: (bar index, direction)."""
    points, i = [], 60
    while i < len(bars) - 200:
        if entry_rule == "with-trend":
            momentum = bars[i][3] - bars[i - 20][3]
            direction = 1 if momentum > 0 else -1
        else:
            direction = rng.choice((1, -1))
        points.append((i, direction))
        i += gap + rng.randint(0, 20)
    return points


def run_trade(bars, atrs, start, direction, managed):
    atr = atrs[start]
    risk = INITIAL_STOP_ATR * atr
    spread = SPREAD_ATR * atr
    entry = bars[start][3] + (spread if direction > 0 else 0.0)            # buys fill at the ask
    sl = entry - direction * risk
    tp = entry + direction * FIXED_TARGET_ATR * atr
    best = entry
    for j in range(start + 1, len(bars)):
        o, h, l, c = bars[j]
        hit_sl = (l <= sl) if direction > 0 else (h >= sl)
        hit_tp = tp > 0 and ((h >= tp) if direction > 0 else (l <= tp))
        if hit_sl:
            exit_price = sl
            break
        if hit_tp:
            exit_price = tp
            break
        best = max(best, h) if direction > 0 else min(best, l)
        if managed:
            bid = c
            ask = c + spread
            d = decide("BUY" if direction > 0 else "SELL", entry, sl, tp, atrs[j], bid, ask, risk, best, CFG)
            if d.new_sl is not None:
                sl = d.new_sl
            if d.new_tp is not None:
                tp = d.new_tp
        if j - start > 400:                                                # safety: close at the market
            exit_price = c
            break
    else:
        exit_price = bars[-1][3]
    gross = (exit_price - entry) * direction
    cost = 0.0 if direction > 0 else spread                               # the buy paid the spread at entry; a sell pays at exit
    return (gross - cost) / risk, (best - entry) * direction / risk


def summarize(results):
    rs = [r for r, _ in results]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(rs), "win_rate": 100 * len(wins) / len(rs),
        "avg_win_R": statistics.mean(wins) if wins else 0.0, "avg_loss_R": statistics.mean(losses) if losses else 0.0,
        "expectancy_R": statistics.mean(rs), "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "runners_3R_plus": 100 * sum(1 for r in rs if r >= 3.0) / len(rs),
        "stderr_R": statistics.pstdev(rs) / math.sqrt(len(rs)),
    }


def simulate(bars, entry_rule, seed):
    atrs = atr_series(bars)
    rng = random.Random(seed)
    points = entries(bars, atrs, None, entry_rule, rng)
    fixed = [run_trade(bars, atrs, i, d, managed=False) for i, d in points]
    managed = [run_trade(bars, atrs, i, d, managed=True) for i, d in points]
    return fixed, managed


def report(title, fixed, managed):
    a, b = summarize(fixed), summarize(managed)
    print(f"\n{title}  ({a['trades']} trades)")
    print(f"  {'':22s}{'fixed SL/TP':>14s}{'managed':>12s}")
    for key, label, fmt in (("win_rate", "win rate %", "{:.1f}"), ("avg_win_R", "average win (R)", "{:.2f}"), ("avg_loss_R", "average loss (R)", "{:.2f}"),
                            ("expectancy_R", "expectancy (R/trade)", "{:+.3f}"), ("profit_factor", "profit factor", "{:.2f}"), ("runners_3R_plus", "trades reaching 3R+ %", "{:.1f}")):
        print(f"  {label:22s}{fmt.format(a[key]):>14s}{fmt.format(b[key]):>12s}")
    print(f"  {'(std. error of expectancy)':22s}{a['stderr_R']:>14.3f}{b['stderr_R']:>12.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="your own candles (open,high,low,close; oldest first)")
    parser.add_argument("--seeds", type=int, default=12, help="synthetic runs per market type")
    args = parser.parse_args()
    if args.csv:
        with open(args.csv, newline="") as fh:
            bars = [(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])) for r in csv.DictReader(fh)]
        for rule in ("random", "with-trend"):
            fixed, managed = simulate(bars, rule, 1)
            report(f"your candles, {rule} entries", fixed, managed)
        return
    for kind, rule in (("trending", "with-trend"), ("trending", "random"), ("random walk", "random"), ("mean-reverting", "random")):
        fixed_all, managed_all = [], []
        for seed in range(args.seeds):
            bars = make_bars("random" if kind == "random walk" else kind, 6000, seed)
            fixed, managed = simulate(bars, rule, seed)
            fixed_all += fixed
            managed_all += managed
        report(f"{kind} market, {rule} entries", fixed_all, managed_all)


if __name__ == "__main__":
    main()
