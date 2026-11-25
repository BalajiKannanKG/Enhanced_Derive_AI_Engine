"""Stop management for open trades: break-even, then an ATR trailing stop that lets winners run.

Pure functions only: no MetaTrader5, no clocks, no I/O. The engine gathers the inputs (position, quote, ATR,
candles, the trade's original risk), calls ``decide()``, and applies whatever it returns. Keeping the rules here
makes every one of them unit-testable.

Rules, for a BUY (a SELL is the exact mirror image):

1. Hold. While the best profit reached so far is below ``be_trigger_r`` multiples of the trade's ORIGINAL risk
   (the distance from entry to the stop it was opened with), nothing is touched. The hard stop is the only way a
   losing trade ends: there is no time-based or indicator-based early exit.
2. Break-even. Once the trade has reached ``be_trigger_r`` the stop moves to entry plus a buffer that covers the
   spread, so a winner can no longer turn into a loser.
3. Trail. Once the trade has reached ``trail_start_r`` the stop follows the BEST price since entry at
   ``trail_atr_mult`` x ATR, and the fixed take-profit is removed so the trail is the exit. After
   ``trail_tighten_r`` the distance narrows to protect a large run. If the price has already fallen back
   past the trail level (a spike between two cycles), the stop falls back to break-even instead of nothing.

The stop only ever moves in the profitable direction, never past the market, and never closer to the market than
the broker allows. If a move is not currently allowed, nothing is changed and it is retried on the next cycle.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class ManagementConfig:
    be_trigger_r: float = 1.0          # move to break-even after this much profit, in multiples of the original risk
    be_buffer_atr: float = 0.1         # break-even buffer in ATR (never less than the current spread)
    trail_start_r: float = 1.5         # start trailing after this much profit
    trail_atr_mult: float = 2.0        # trailing distance from the best price, in ATR
    trail_tighten_r: float = 4.0       # after this much profit the trail narrows
    trail_tight_factor: float = 0.75   # ... to this fraction of trail_atr_mult
    min_step_atr: float = 0.1          # ignore stop moves smaller than this (avoids a modify request every second)
    remove_tp_when_trailing: bool = True


@dataclass(frozen=True)
class Decision:
    phase: str                    # "hold", "breakeven" or "trail"
    new_sl: Optional[float]       # None = leave the stop alone
    new_tp: Optional[float]       # None = leave the target alone, 0.0 = remove it
    r_multiple: float             # best profit reached so far, in units of the original risk
    reason: str


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def config_for_profile(profile: Mapping[str, Any]) -> ManagementConfig:
    """Build the management settings from a trading-style profile; missing keys fall back to the defaults."""
    base = ManagementConfig()
    return ManagementConfig(
        be_trigger_r=float(profile.get("break_even_trigger_r", base.be_trigger_r)),
        be_buffer_atr=float(profile.get("break_even_buffer_atr", base.be_buffer_atr)),
        trail_start_r=float(profile.get("trail_start_r", base.trail_start_r)),
        trail_atr_mult=float(profile.get("trail_atr_mult", base.trail_atr_mult)),
        trail_tighten_r=float(profile.get("trail_tighten_r", base.trail_tighten_r)),
        trail_tight_factor=float(profile.get("trail_tight_factor", base.trail_tight_factor)),
        min_step_atr=float(profile.get("trail_min_step_atr", base.min_step_atr)),
        remove_tp_when_trailing=bool(profile.get("remove_tp_when_trailing", base.remove_tp_when_trailing)),
    )


def best_price_since(side: str, candles: Sequence[Mapping[str, Any]], since_time: float, mark: float) -> float:
    """Best price reached in the trade's favour since it was opened.

    ``candles`` carry the candle's OPEN time. The candle that was already running when the trade opened is left
    out, because its extreme may have happened before the entry. The current price is always included.
    """
    best = float(mark)
    for candle in candles:
        stamp = candle.get("time")
        if not _finite(stamp) or float(stamp) < float(since_time):
            continue
        value = candle.get("high" if side == "BUY" else "low")
        if not _finite(value):
            continue
        value = float(value)
        best = max(best, value) if side == "BUY" else min(best, value)
    return best


def decide(
    side: str,
    entry: float,
    sl: float,
    tp: float,
    atr: float,
    bid: float,
    ask: float,
    initial_risk: float,
    best_price: float,
    config: ManagementConfig,
    min_stop_distance: float = 0.0,
) -> Decision:
    """Decide what to do with one open trade. See the module docstring for the rules."""
    direction = 1 if side == "BUY" else (-1 if side == "SELL" else 0)
    inputs = (entry, atr, bid, ask, initial_risk, best_price)
    if direction == 0 or not all(_finite(v) for v in inputs) or atr <= 0 or initial_risk <= 0 or bid <= 0 or ask <= 0:
        return Decision("hold", None, None, 0.0, "invalid input")
    entry, atr, bid, ask, initial_risk, best_price = (float(v) for v in inputs)
    sl = float(sl) if _finite(sl) else 0.0
    tp = float(tp) if _finite(tp) else 0.0
    min_stop_distance = max(float(min_stop_distance), 0.0) if _finite(min_stop_distance) else 0.0

    mark = bid if direction > 0 else ask                    # the price a stop on this side is compared with
    best = max(best_price, mark) if direction > 0 else min(best_price, mark)
    r_multiple = max((best - entry) * direction, 0.0) / initial_risk
    if r_multiple < config.be_trigger_r:
        return Decision("hold", None, None, r_multiple, f"best profit {r_multiple:.2f}R is below break-even at {config.be_trigger_r:.2f}R")

    spread = max(ask - bid, 0.0)
    be_level = entry + direction * max(config.be_buffer_atr * atr, spread)
    candidates = [(be_level, "break-even")]
    phase = "breakeven"
    if r_multiple >= config.trail_start_r:
        phase = "trail"
        distance = config.trail_atr_mult * atr
        if r_multiple >= config.trail_tighten_r:
            distance *= config.trail_tight_factor
        trailing_level = best - direction * distance
        if direction * (trailing_level - be_level) > 0:       # never trail below the break-even level
            candidates.insert(0, (trailing_level, f"trailing {distance / atr:.2f} ATR behind the best price"))

    # The fixed target is only dropped once the trade is protected (a stop at break-even or better is in place or
    # about to be placed). Removing it earlier would leave the original stop as the only protection and no target.
    remove_tp = phase == "trail" and config.remove_tp_when_trailing and tp > 0
    already_protected = sl > 0 and direction * (sl - entry) >= 0

    step = config.min_step_atr * atr
    waiting = False
    for level, label in candidates:                          # best level first; fall back if it cannot be placed
        if not (sl <= 0 or direction * (level - sl) > step):
            continue
        room = direction * (mark - level)                    # how far the stop would sit from the market
        if room > 0 and room >= min_stop_distance:
            fallback = " (the trail level is beyond the current price)" if (phase == "trail" and level == be_level and len(candidates) > 1) else ""
            return Decision(phase, round(level, 10), 0.0 if remove_tp else None, r_multiple, f"stop to {level:.5f}: {label}{fallback} at {r_multiple:.2f}R")
        waiting = True
    new_tp = 0.0 if (remove_tp and already_protected) else None
    if waiting:
        return Decision(phase, None, new_tp, r_multiple, "waiting: the stop would sit too close to the market")
    return Decision(phase, None, new_tp, r_multiple, "stop already at or beyond the target level")
