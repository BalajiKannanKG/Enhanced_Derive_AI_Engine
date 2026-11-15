"""Direct Python integration with a locally running MetaTrader 5 terminal."""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime, time as datetime_time, timedelta
from typing import Any

# The Python package exposes ORDER_FILLING_* constants but not the corresponding
# SYMBOL_FILLING_* bit flags stored in SymbolInfo.filling_mode.
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2

# Fallbacks used by ``MT5Bridge._filling_mode`` when the MetaTrader5 Python
# package is unavailable (``mt5`` is ``None``). The values mirror the documented
# C++ ``OrderFillingType`` enum assignments so callers always receive a valid
# ``type_filling`` integer, even on import failure.
_FALLBACK_ORDER_FILLING_IOC = 1
_FALLBACK_ORDER_FILLING_FOK = 2
_FALLBACK_ORDER_FILLING_RETURN = 0

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


class MT5Bridge:
    def __init__(
        self,
        terminal_path: str | None = None,
        symbol: str | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        self.terminal_path = terminal_path or os.environ.get("MT5_TERMINAL_PATH")
        self.symbol = symbol or os.environ.get("MT5_SYMBOL", "Volatility 25 Index")
        self.symbols = tuple(dict.fromkeys([self.symbol, *(symbols or [])]))
        self._lock = threading.RLock()
        self._initialized = False
        self._status = "NOT CONNECTED"
        self._detail = "MT5 connection has not been initialized."

    def _error_text(self) -> str:
        if mt5 is None:
            return "Install the MetaTrader5 Python package in the active environment."
        error = mt5.last_error()
        return f"MetaTrader5 error {error[0]}: {error[1]}"

    def connect(self) -> bool:
        with self._lock:
            if mt5 is None:
                self._status = "PYTHON PACKAGE MISSING"
                self._detail = self._error_text()
                return False
            if self._initialized:
                terminal = mt5.terminal_info()
                account = mt5.account_info()
                if (
                    terminal is not None
                    and account is not None
                    and getattr(terminal, "connected", False)
                ):
                    return True
                mt5.shutdown()
                self._initialized = False

            initialized = (
                mt5.initialize(path=self.terminal_path)
                if self.terminal_path
                else mt5.initialize()
            )
            if not initialized:
                self._status = "NOT CONNECTED"
                self._detail = self._error_text()
                return False

            self._initialized = True
            terminal = mt5.terminal_info()
            account = mt5.account_info()
            if (
                terminal is None
                or account is None
                or not getattr(terminal, "connected", False)
            ):
                self._status = "NOT CONNECTED"
                self._detail = self._error_text()
                mt5.shutdown()
                self._initialized = False
                return False

            self._status = "CONNECTED"
            self._detail = "Connected to the logged-in MT5 terminal."
            return True

    def disconnect(self) -> None:
        with self._lock:
            if mt5 is not None and self._initialized:
                mt5.shutdown()
            self._initialized = False
            self._status = "NOT CONNECTED"
            self._detail = "MT5 connection is closed."

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if hasattr(value, "_asdict"):
            return value._asdict()
        if hasattr(value, "__dict__"):
            return vars(value)
        return dict(value)

    def _position_data(self, position: Any) -> dict[str, Any]:
        data = self._as_dict(position)
        position_type = (
            "BUY" if data["type"] == mt5.POSITION_TYPE_BUY else "SELL"
        )
        return {
            "ticket": int(data["ticket"]),
            "identifier": int(data.get("identifier", data["ticket"])),
            "symbol": str(data["symbol"]),
            "magic": int(data["magic"]),
            "type": position_type,
            "price": float(data["price_open"]),
            "volume": float(data["volume"]),
            "profit": float(data["profit"]),
            "swap": float(data.get("swap", 0.0)),
            "tp": float(data["tp"]),
            "sl": float(data["sl"]),
            "time": int(data["time"]),
        }

    def account_snapshot(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        with self._lock:
            if not self.connect():
                return None, self.status()

            account = mt5.account_info()
            positions = mt5.positions_get()
            terminal = mt5.terminal_info()
            if (
                account is None
                or positions is None
                or terminal is None
                or not getattr(terminal, "connected", False)
            ):
                self._status = "CONNECTION ERROR"
                self._detail = self._error_text()
                return None, self.status()

            symbols_data: dict[str, dict[str, Any]] = {}
            for symbol in self.symbols:
                selected = mt5.symbol_select(symbol, True)
                info = mt5.symbol_info(symbol) if selected else None
                tick = mt5.symbol_info_tick(symbol) if selected else None
                symbols_data[symbol] = {
                    "available": bool(info is not None and tick is not None),
                    "detail": (
                        "Quote and symbol settings available."
                        if info is not None and tick is not None
                        else f"MT5 quote or symbol settings unavailable: {self._error_text()}"
                    ),
                    "quote": (
                        {
                            "bid": float(tick.bid),
                            "ask": float(tick.ask),
                            "time": int(tick.time),
                        }
                        if tick is not None
                        else None
                    ),
                    "volume_min": float(info.volume_min) if info is not None else None,
                    "volume_max": float(info.volume_max) if info is not None else None,
                    "volume_step": float(info.volume_step) if info is not None else None,
                    "digits": int(info.digits) if info is not None else None,
                }
            now = datetime.now().astimezone()
            day_start = datetime.combine(now.date(), datetime_time.min, tzinfo=now.tzinfo)
            deals = mt5.history_deals_get(day_start, now)
            if deals is None:
                self._status = "ACCOUNT DATA ERROR"
                self._detail = self._error_text()
                return None, self.status()

            realized_today = sum(
                float(deal.profit)
                + float(deal.commission)
                + float(deal.swap)
                + float(getattr(deal, "fee", 0.0))
                for deal in deals
            )
            balance = float(account.balance)
            floating_pnl = float(account.profit)
            trade_allowed = bool(
                getattr(account, "trade_allowed", False)
                and getattr(terminal, "trade_allowed", False)
                and not getattr(terminal, "tradeapi_disabled", True)
            )
            position_data = [self._position_data(position) for position in positions]
            state = {
                "balance": balance,
                "equity": float(account.equity),
                "profit": floating_pnl,
                "open_positions": len(position_data),
                "positions": position_data,
                "daily_start_balance": balance - realized_today,
                "daily_pnl": realized_today + floating_pnl,
                "currency": str(account.currency),
                "account_server": str(account.server),
                "symbols": symbols_data,
                "quote": symbols_data[self.symbol]["quote"],
            }
            self._status = "CONNECTED" if trade_allowed else "CONNECTED - TRADING DISABLED"
            self._detail = (
                f"Connected to the {account.server} account."
                if trade_allowed
                else "Connected, but MT5 or account Algo Trading permission is disabled."
            )
            return state, self.status(trade_allowed=trade_allowed)

    def market_data_snapshot(
        self,
        timeframes: dict[str, str],
        history_count: int = 300,
        include_history: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        with self._lock:
            if not self.connect():
                return None, self.status()

            snapshot: dict[str, Any] = {}
            for symbol in self.symbols:
                selected = mt5.symbol_select(symbol, True)
                tick = mt5.symbol_info_tick(symbol) if selected else None
                symbol_data: dict[str, Any] = {
                    "quote": (
                        {
                            "bid": float(tick.bid),
                            "ask": float(tick.ask),
                            "time": int(tick.time),
                        }
                        if tick is not None
                        else None
                    ),
                    "history": {},
                }
                if include_history and selected:
                    for timeframe, mt5_timeframe in timeframes.items():
                        mt5_constant = getattr(mt5, mt5_timeframe, None)
                        if mt5_constant is None:
                            symbol_data["history"][timeframe] = None
                            continue
                        symbol_data["history"][timeframe] = mt5.copy_rates_from_pos(
                            symbol, mt5_constant, 0, history_count
                        )
                snapshot[symbol] = symbol_data

            return snapshot, self.status()

    def status(self, trade_allowed: bool | None = None) -> dict[str, Any]:
        terminal_path = self.terminal_path
        return {
            "status": self._status,
            "configured": bool(mt5 is not None),
            "state_available": self._initialized and self._status.startswith("CONNECTED"),
            "connection_verified": self._initialized and self._status.startswith("CONNECTED"),
            "trade_allowed": trade_allowed if trade_allowed is not None else False,
            "terminal_path": terminal_path,
            "detail": self._detail,
        }

    def _trade_result(self, result: Any) -> tuple[bool, str, dict[str, Any]]:
        if result is None:
            return False, self._error_text(), {}
        data = self._as_dict(result)
        accepted_codes = {
            mt5.TRADE_RETCODE_DONE,
            mt5.TRADE_RETCODE_DONE_PARTIAL,
            mt5.TRADE_RETCODE_PLACED,
        }
        succeeded = int(data.get("retcode", -1)) in accepted_codes
        message = str(data.get("comment") or data.get("retcode"))
        return succeeded, message, data

    @staticmethod
    def _filling_mode(info: Any) -> int:
        # Mirror the documented C++ ``OrderFillingType`` enum values so the
        # bridge always returns a valid ``type_filling`` integer, whether or not
        # the MetaTrader5 Python package is installed/imported successfully.
        order_filling_ioc = getattr(mt5, "ORDER_FILLING_IOC", _FALLBACK_ORDER_FILLING_IOC)
        order_filling_fok = getattr(mt5, "ORDER_FILLING_FOK", _FALLBACK_ORDER_FILLING_FOK)
        order_filling_return = getattr(mt5, "ORDER_FILLING_RETURN", _FALLBACK_ORDER_FILLING_RETURN)
        if info is None or not hasattr(info, "filling_mode"):
            return order_filling_ioc
        supported = int(getattr(info, "filling_mode", 0))
        if supported & _SYMBOL_FILLING_FOK:
            return order_filling_fok
        if supported & _SYMBOL_FILLING_IOC:
            return order_filling_ioc
        return order_filling_return

    @staticmethod
    def _risk_sized_volume(
        order_type: int,
        symbol: str,
        price: float,
        stop_loss: float,
        info: Any,
        account: Any,
        max_risk_percent: float,
    ) -> tuple[float | None, float | None, str | None]:
        volume_min = float(info.volume_min)
        volume_max = float(info.volume_max)
        volume_step = float(info.volume_step)
        if (
            not all(math.isfinite(value) for value in (volume_min, volume_max, volume_step))
            or volume_min <= 0
            or volume_max < volume_min
            or volume_step <= 0
        ):
            return None, None, "MT5 symbol volume limits are invalid."

        risk_cap = float(account.balance) * max_risk_percent / 100.0
        free_margin = float(account.margin_free)
        last_step = math.floor((volume_max - volume_min) / volume_step + 1e-9)

        def candidate(
            index: int,
        ) -> tuple[float, float | None, float | None, str | None]:
            volume = round(volume_min + index * volume_step, 8)
            risk = mt5.order_calc_profit(order_type, symbol, volume, price, stop_loss)
            if risk is None or not math.isfinite(float(risk)):
                return volume, None, None, f"Could not calculate order risk: {mt5.last_error()}"
            margin = mt5.order_calc_margin(order_type, symbol, volume, price)
            if margin is None or not math.isfinite(float(margin)):
                return volume, None, None, f"Could not calculate required margin: {mt5.last_error()}"
            return volume, abs(float(risk)), float(margin), None

        min_volume, min_risk, min_margin, error = candidate(0)
        if error:
            return None, None, error
        if min_risk is None or min_margin is None:
            return None, None, "Could not calculate risk and margin for the minimum broker volume."
        if min_risk > risk_cap:
            return None, None, (
                f"Minimum broker volume {min_volume:g} risks {min_risk:.2f} "
                f"{account.currency}, exceeding the {max_risk_percent:.2f}% "
                f"account limit ({risk_cap:.2f})."
            )
        if min_margin > free_margin:
            return None, None, "Insufficient free margin for the minimum broker volume."

        low = 0
        high = last_step
        while low < high:
            middle = (low + high + 1) // 2
            volume, risk, margin, error = candidate(middle)
            if error:
                return None, None, error
            if (
                risk is not None
                and margin is not None
                and risk <= risk_cap
                and margin <= free_margin
            ):
                low = middle
            else:
                high = middle - 1

        if low > 0:
            volume, risk, margin, error = candidate(low)
            if error:
                return None, None, error
            if (
                risk is not None
                and margin is not None
                and risk <= risk_cap
                and margin <= free_margin
            ):
                return volume, risk, None

        volume, risk, _, error = candidate(low)
        if error:
            return None, None, error
        return volume, risk, None

    def place_market_order(
        self,
        payload: dict[str, Any],
        max_risk_percent: float,
    ) -> tuple[bool, str, dict[str, Any]]:
        with self._lock:
            if not self.connect():
                return False, self._detail, {}

            account = mt5.account_info()
            positions = mt5.positions_get()
            if account is None or positions is None:
                return False, self._error_text(), {}
            
            MAX_ALLOWED_POSITIONS = 5
            if len(positions) >= MAX_ALLOWED_POSITIONS:
                return False, f"Maximum open positions limit ({MAX_ALLOWED_POSITIONS}) reached.", {}

            symbol = str(payload.get("symbol") or self.symbol)
            if symbol not in self.symbols:
                return False, f"MT5 symbol {symbol} is not in the configured supported-symbol list.", {}
            if not mt5.symbol_select(symbol, True):
                return False, f"Unable to select MT5 symbol {symbol}: {self._error_text()}", {}

            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            if tick is None or info is None:
                return False, f"MT5 quote/specification unavailable: {self._error_text()}", {}

            action = str(payload.get("action", "")).upper()
            if action not in ("BUY", "SELL"):
                return False, "Order action must be BUY or SELL.", {}
            order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
            uses_distance = "sl_distance" in payload or "tp_distance" in payload
            risk_sized = payload.get("risk_sized") is True
            try:
                price = float(tick.ask if action == "BUY" else tick.bid)
                volume = float(
                    info.volume_min if risk_sized else payload.get("volume", 0.0)
                )
                if uses_distance:
                    stop_distance = float(payload.get("sl_distance", 0.0))
                    target_distance = float(payload.get("tp_distance", 0.0))
                else:
                    stop_loss = float(payload.get("sl", 0.0))
                    take_profit = float(payload.get("tp", 0.0))
            except (TypeError, ValueError):
                return False, "Volume, stop-loss, and take-profit values must be numbers.", {}
            values = [price, volume]
            if uses_distance:
                values.extend((stop_distance, target_distance))
                if stop_distance <= 0 or target_distance <= 0:
                    return False, "Stop-loss and take-profit distances must be greater than zero.", {}
            else:
                values.extend((stop_loss, take_profit))
            if not all(math.isfinite(value) for value in values):
                return False, "Price, volume, and stop-loss/take-profit values must be finite numbers.", {}
            digits = int(info.digits)
            price = round(price, digits)
            if uses_distance:
                if action == "BUY":
                    stop_loss = price - stop_distance
                    take_profit = price + target_distance
                else:
                    stop_loss = price + stop_distance
                    take_profit = price - target_distance
            stop_loss = round(stop_loss, digits)
            take_profit = round(take_profit, digits)
            if volume < info.volume_min or volume > info.volume_max:
                return False, f"Volume must be between {info.volume_min} and {info.volume_max}.", {}
            steps = (volume - info.volume_min) / info.volume_step
            if not math.isclose(steps, round(steps), abs_tol=1e-7):
                return False, f"Volume must follow the {info.volume_step} lot step.", {}
            if (action == "BUY" and not 0 < stop_loss < price < take_profit) or (
                action == "SELL" and not 0 < take_profit < price < stop_loss
            ):
                return False, "Stop-loss and take-profit must be on the correct side of the current MT5 quote.", {}

            min_stop_distance = int(getattr(info, "trade_stops_level", 0)) * float(info.point)
            if min_stop_distance and (
                abs(price - stop_loss) < min_stop_distance
                or abs(price - take_profit) < min_stop_distance
            ):
                return False, "Stop-loss or take-profit is inside the broker's minimum stop distance.", {}

            risk_cap = float(account.balance) * max_risk_percent / 100.0
            if risk_sized:
                sized_volume, risk_amount, sizing_error = self._risk_sized_volume(
                    order_type,
                    symbol,
                    price,
                    stop_loss,
                    info,
                    account,
                    max_risk_percent,
                )
                if sizing_error:
                    return False, sizing_error, {}
                if sized_volume is None or risk_amount is None:
                    return False, "Could not determine a volume within the configured risk limit.", {}
                volume = sized_volume
            else:
                risk = mt5.order_calc_profit(order_type, symbol, volume, price, stop_loss)
                if risk is None or not math.isfinite(float(risk)):
                    return False, f"Could not calculate order risk: {self._error_text()}", {}
                risk_amount = abs(float(risk))
                if risk_amount > risk_cap:
                    return False, (
                        f"Estimated stop-loss risk {risk_amount:.2f} {account.currency} exceeds "
                        f"the {max_risk_percent:.2f}% account limit ({risk_cap:.2f})."
                    ), {}

            margin = mt5.order_calc_margin(order_type, symbol, volume, price)
            if margin is None or float(margin) > float(account.margin_free):
                return False, "Insufficient free margin or unable to calculate required margin.", {}

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": round(stop_loss, int(info.digits)),
                "tp": round(take_profit, int(info.digits)),
                "deviation": 50,
                "magic": 123456,
                "comment": "Enhanced Derive AI Engine",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(info),
            }
            checked = mt5.order_check(request)
            if checked is None or int(checked.retcode) != 0:
                message = (
                    str(getattr(checked, "comment", ""))
                    if checked is not None
                    else self._error_text()
                )
                return False, f"MT5 order check rejected the request: {message}", {}

            succeeded, message, result = self._trade_result(mt5.order_send(request))
            if succeeded:
                result["risk_amount"] = risk_amount
                result["risk_cap"] = risk_cap
                result["quote_price"] = price
                result["sl"] = stop_loss
                result["tp"] = take_profit
                result["volume"] = volume
            return succeeded, message, result

    def modify_position(
        self, ticket: int, stop_loss: float, take_profit: float
    ) -> tuple[bool, str, dict[str, Any]]:
        with self._lock:
            if not self.connect():
                return False, self._detail, {}
            position = mt5.positions_get(ticket=int(ticket))
            if not position:
                return False, f"MT5 position {ticket} was not found.", {}
            item = position[0]
            info = mt5.symbol_info(item.symbol)
            if info is None:
                return False, f"MT5 symbol {item.symbol} is unavailable.", {}
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": item.symbol,
                "sl": round(float(stop_loss), int(info.digits)),
                "tp": round(float(take_profit), int(info.digits)),
            }
            return self._trade_result(mt5.order_send(request))


    def get_position_by_ticket(self, ticket: int) -> dict[str, Any] | None:
        """Helper to fetch an open position as a dictionary by its ticket ID."""
        with self._lock:
            if not self.connect():
                return None
            positions = mt5.positions_get(ticket=int(ticket))
            if not positions:
                return None
            return self._position_data(positions[0])
    def close_position(self, ticket: int, volume: float = None) -> tuple[bool, str, dict[str, Any]]:
        with self._lock:
            if not self.connect():
                return False, self._detail, {}
            position_data = mt5.positions_get(ticket=int(ticket))
            if not position_data:
                return False, f"MT5 position {ticket} was not found.", {}
            position = position_data[0]
            tick = mt5.symbol_info_tick(position.symbol)
            if tick is None:
                return False, f"No live quote for {position.symbol}.", {}
            info = mt5.symbol_info(position.symbol)
            if info is None:
                return False, f"MT5 symbol {position.symbol} is unavailable.", {}
            
            close_volume = float(volume) if volume is not None else float(position.volume)
            is_buy = position.type == mt5.POSITION_TYPE_BUY
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": int(ticket),
                "symbol": position.symbol,
                "volume": close_volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "price": float(tick.bid if is_buy else tick.ask),
                "deviation": 50,
                "magic": int(position.magic),
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(info),
            }
            return self._trade_result(mt5.order_send(request))
    
    def trade_history(self, days: int = 365) -> list[dict[str, Any]] | None:
        with self._lock:
            if not self.connect():
                return None
            end = datetime.now().astimezone()
            start = end - timedelta(days=days)
            deals = mt5.history_deals_get(start, end)
            if deals is None:
                self._status = "HISTORY DATA ERROR"
                self._detail = self._error_text()
                return None

            history = []
            for deal in deals:
                if deal.symbol not in self.symbols or deal.type not in (
                    mt5.DEAL_TYPE_BUY,
                    mt5.DEAL_TYPE_SELL,
                ):
                    continue
                item = self._as_dict(deal)
                pnl = (
                    float(deal.profit)
                    + float(deal.commission)
                    + float(deal.swap)
                    + float(getattr(deal, "fee", 0.0))
                )
                history.append({
                    "trade_id": int(deal.position_id),
                    "deal_id": int(deal.ticket),
                    "entry_time": datetime.fromtimestamp(
                        int(deal.time), tz=datetime.now().astimezone().tzinfo
                    ).isoformat(),
                    "type": "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL",
                    "symbol": str(deal.symbol),
                    "lot_size": float(deal.volume),
                    "entry_price": float(deal.price),
                    "exit_price": float(deal.price),
                    "pnl": pnl,
                    "result": "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
                    "magic": int(deal.magic),
                    "comment": str(deal.comment),
                    "deal_entry": int(deal.entry),
                })
            return history