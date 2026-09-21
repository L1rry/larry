import ccxt
import pandas as pd
import numpy as np

EXCHANGE_ID = "binance"
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "LINK/USDT", "DOT/USDT", "AVAX/USDT", "LTC/USDT",
    "ATOM/USDT", "NEAR/USDT", "TRX/USDT",
]
TIMEFRAME = "1d"
LOOKBACK_DAYS = 1095  # ~3 года

FAST_MA = 20
SLOW_MA = 50
STOP_LOSS_PERCENT = 8  # жёсткий выход, если цена падает от закрытия входа на столько

ATR_PERIOD = 14
ATR_MULTIPLIER = 3  # стоп = цена входа − ATR_MULTIPLIER * ATR — подстраивается под волатильность актива

# Mean-reversion (покупка на просадке / продажа на возврате к среднему)
BB_PERIOD = 20
BB_STD_MULTIPLIER = 2  # вход, когда цена ниже средней на столько стандартных отклонений

TAKER_FEE_PERCENT = 0.1  # комиссия за сторону сделки
INITIAL_CAPITAL = 1000


def fetch_ohlcv_full(exchange, symbol, timeframe, since_ms, limit=1000):
    all_candles = []
    while True:
        candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=limit)
        if not candles:
            break
        all_candles += candles
        since_ms = candles[-1][0] + 1
        if len(candles) < limit:
            break
    return all_candles


def load_price_data(exchange, symbol):
    since_ms = exchange.milliseconds() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    candles = fetch_ohlcv_full(exchange, symbol, TIMEFRAME, since_ms)
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df


def add_atr(df, period=ATR_PERIOD):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(period).mean()
    return df


def backtest_ma_crossover(df, fast=FAST_MA, slow=SLOW_MA, fee_percent=TAKER_FEE_PERCENT,
                           stop_loss_percent=None, atr_multiplier=None, take_profit_percent=None):
    df = df.copy()
    if atr_multiplier:
        df = add_atr(df)
    df["fast_ma"] = df["close"].rolling(fast).mean()
    df["slow_ma"] = df["close"].rolling(slow).mean()
    df["signal"] = (df["fast_ma"] > df["slow_ma"]).astype(int)
    # торгуем на закрытии свечи, где сигнал уже подтверждён предыдущим баром —
    # без этого сдвига бэктест подглядывает в будущее (lookahead bias)
    df["position"] = df["signal"].shift(1).fillna(0)

    trades = []
    in_position = False
    entry_price = None
    entry_date = None
    stop_price = None
    take_profit_price = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        pos = row["position"]

        if in_position:
            # стоп-лосс проверяем по внутридневному минимуму — реагирует быстрее,
            # чем ожидание обратного пересечения скользящих средних
            if stop_price is not None and row["low"] <= stop_price:
                exit_price = stop_price * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    "entry_date": entry_date, "exit_date": row["date"],
                    "pnl_percent": pnl_percent, "reason": "stop_loss",
                })
                in_position = False
                continue
            if take_profit_price is not None and row["high"] >= take_profit_price:
                exit_price = take_profit_price * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    "entry_date": entry_date, "exit_date": row["date"],
                    "pnl_percent": pnl_percent, "reason": "take_profit",
                })
                in_position = False
                continue
            if pos == 0:
                exit_price = row["close"] * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    "entry_date": entry_date, "exit_date": row["date"],
                    "pnl_percent": pnl_percent, "reason": "ma_cross",
                })
                in_position = False
                continue

        if not in_position and pos == 1:
            entry_price = row["close"] * (1 + fee_percent / 100)
            entry_date = row["date"]
            if atr_multiplier and pd.notna(row.get("atr")):
                stop_price = row["close"] - atr_multiplier * row["atr"]
            elif stop_loss_percent:
                stop_price = row["close"] * (1 - stop_loss_percent / 100)
            else:
                stop_price = None
            take_profit_price = row["close"] * (1 + take_profit_percent / 100) if take_profit_percent else None
            in_position = True

    if in_position:
        row = df.iloc[-1]
        exit_price = row["close"] * (1 - fee_percent / 100)
        pnl_percent = (exit_price - entry_price) / entry_price * 100
        trades.append({
            "entry_date": entry_date, "exit_date": row["date"],
            "pnl_percent": pnl_percent, "reason": "open_at_end",
        })

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    for t in trades:
        equity *= (1 + t["pnl_percent"] / 100)
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_drawdown = 0
    for e in equity_curve:
        peak = max(peak, e)
        drawdown = (peak - e) / peak * 100
        max_drawdown = max(max_drawdown, drawdown)

    total_return_percent = (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    wins = [t for t in trades if t["pnl_percent"] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t["pnl_percent"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in trades if t["pnl_percent"] <= 0]
    avg_loss = sum(t["pnl_percent"] for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t["pnl_percent"] for t in wins)
    gross_loss = abs(sum(t["pnl_percent"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

    buy_hold_return = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0] * 100
    stop_loss_exits = sum(1 for t in trades if t.get("reason") == "stop_loss")
    take_profit_exits = sum(1 for t in trades if t.get("reason") == "take_profit")

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "profit_factor": profit_factor,
        "total_return_pct": total_return_percent,
        "max_drawdown_pct": max_drawdown,
        "buy_hold_return_pct": buy_hold_return,
        "stop_loss_exits": stop_loss_exits,
        "take_profit_exits": take_profit_exits,
    }


def main():
    exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})

    print(f"Стратегия: MA crossover {FAST_MA}/{SLOW_MA} + ATR-стоп x{ATR_MULTIPLIER}, "
          f"таймфрейм {TIMEFRAME}, ~{LOOKBACK_DAYS} дней истории\n")

    summary = []
    for symbol in SYMBOLS:
        try:
            df = load_price_data(exchange, symbol)
        except Exception as e:
            print(f"{symbol}: ошибка загрузки данных ({e})")
            continue
        if len(df) < SLOW_MA + ATR_PERIOD + 5:
            print(f"{symbol}: недостаточно данных")
            continue

        r = backtest_ma_crossover(df, atr_multiplier=ATR_MULTIPLIER)
        calmar = r["total_return_pct"] / r["max_drawdown_pct"] if r["max_drawdown_pct"] else float("inf")
        summary.append((symbol, r, calmar))

    summary.sort(key=lambda x: -x[2])

    print(f"{'Монета':12s} {'Сделок':>7s} {'Win%':>7s} {'ProfFct':>8s} {'Доходность':>12s} {'Просадка':>10s} {'Return/DD':>10s} {'Buy&hold':>10s}")
    for symbol, r, calmar in summary:
        print(f"{symbol:12s} {r['trades']:>7d} {r['win_rate']:>6.1f}% {r['profit_factor']:>8.2f} "
              f"{r['total_return_pct']:>+11.1f}% {r['max_drawdown_pct']:>9.1f}% {calmar:>10.2f} "
              f"{r['buy_hold_return_pct']:>+9.1f}%")


def test_take_profit():
    exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})
    final_symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "TRX/USDT"]
    tp_levels = [None, 15, 30, 50, 80]

    print(f"\nТейк-профит поверх ATR-стопа x{ATR_MULTIPLIER}, MA {FAST_MA}/{SLOW_MA}\n")

    for symbol in final_symbols:
        df = load_price_data(exchange, symbol)
        if len(df) < SLOW_MA + ATR_PERIOD + 5:
            print(f"{symbol}: недостаточно данных")
            continue

        print(f"=== {symbol} ===")
        header = "  " + "".ljust(18) + "".join(f"{('без TP' if tp is None else f'TP {tp}%'):>12s}" for tp in tp_levels)
        print(header)

        rows = {}
        for tp in tp_levels:
            r = backtest_ma_crossover(df, atr_multiplier=ATR_MULTIPLIER, take_profit_percent=tp)
            rows[tp] = r

        print("  " + "Сделок".ljust(18) + "".join(f"{rows[tp]['trades']:>12d}" for tp in tp_levels))
        print("  " + "  из них TP".ljust(18) + "".join(f"{rows[tp]['take_profit_exits']:>12d}" for tp in tp_levels))
        print("  " + "Win rate".ljust(18) + "".join(f"{rows[tp]['win_rate']:>11.1f}%" for tp in tp_levels))
        print("  " + "Profit factor".ljust(18) + "".join(f"{rows[tp]['profit_factor']:>12.2f}" for tp in tp_levels))
        print("  " + "Доходность".ljust(18) + "".join(f"{rows[tp]['total_return_pct']:>+11.1f}%" for tp in tp_levels))
        print("  " + "Просадка".ljust(18) + "".join(f"{rows[tp]['max_drawdown_pct']:>11.1f}%" for tp in tp_levels))
        print()


def backtest_long_short(df, fast=FAST_MA, slow=SLOW_MA, fee_percent=TAKER_FEE_PERCENT, atr_multiplier=ATR_MULTIPLIER):
    df = add_atr(df.copy())
    df["fast_ma"] = df["close"].rolling(fast).mean()
    df["slow_ma"] = df["close"].rolling(slow).mean()
    df["direction"] = np.where(df["fast_ma"] > df["slow_ma"], 1, -1)
    df["position"] = df["direction"].shift(1)

    trades = []
    in_position = False
    position_dir = None
    entry_price = None
    entry_date = None
    stop_price = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        pos = row["position"]
        if pd.isna(pos) or pd.isna(row["fast_ma"]) or pd.isna(row["slow_ma"]):
            continue

        if in_position:
            stopped = False
            if position_dir == 1 and stop_price is not None and row["low"] <= stop_price:
                exit_price = stop_price * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                stopped = True
            elif position_dir == -1 and stop_price is not None and row["high"] >= stop_price:
                exit_price = stop_price * (1 + fee_percent / 100)
                pnl_percent = (entry_price - exit_price) / entry_price * 100
                stopped = True

            if stopped:
                trades.append({"entry_date": entry_date, "exit_date": row["date"],
                                "pnl_percent": pnl_percent, "reason": "stop_loss", "dir": position_dir})
                in_position = False
            elif pos != position_dir:
                if position_dir == 1:
                    exit_price = row["close"] * (1 - fee_percent / 100)
                    pnl_percent = (exit_price - entry_price) / entry_price * 100
                else:
                    exit_price = row["close"] * (1 + fee_percent / 100)
                    pnl_percent = (entry_price - exit_price) / entry_price * 100
                trades.append({"entry_date": entry_date, "exit_date": row["date"],
                                "pnl_percent": pnl_percent, "reason": "flip", "dir": position_dir})
                in_position = False

        if not in_position:
            position_dir = pos
            entry_date = row["date"]
            if position_dir == 1:
                entry_price = row["close"] * (1 + fee_percent / 100)
                stop_price = row["close"] - atr_multiplier * row["atr"] if pd.notna(row["atr"]) else None
            else:
                entry_price = row["close"] * (1 - fee_percent / 100)
                stop_price = row["close"] + atr_multiplier * row["atr"] if pd.notna(row["atr"]) else None
            in_position = True

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    for t in trades:
        equity *= (1 + t["pnl_percent"] / 100)
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_drawdown = 0
    for e in equity_curve:
        peak = max(peak, e)
        max_drawdown = max(max_drawdown, (peak - e) / peak * 100)

    total_return_percent = (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    wins = [t for t in trades if t["pnl_percent"] > 0]
    losses = [t for t in trades if t["pnl_percent"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl_percent"] for t in wins)
    gross_loss = abs(sum(t["pnl_percent"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

    short_trades = [t for t in trades if t["dir"] == -1]
    short_wins = [t for t in short_trades if t["pnl_percent"] > 0]

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_return_pct": total_return_percent,
        "max_drawdown_pct": max_drawdown,
        "short_trades": len(short_trades),
        "short_win_rate": (len(short_wins) / len(short_trades) * 100) if short_trades else 0,
    }


def test_long_short():
    exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})
    final_symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "TRX/USDT"]

    print(f"\nЛонг+шорт vs только лонг, ATR-стоп x{ATR_MULTIPLIER}, MA {FAST_MA}/{SLOW_MA}")
    print("(фандинг-ставки перпетуалов НЕ учтены — реальный результат шортов будет немного отличаться)\n")

    print(f"{'Монета':12s} {'Сделок':>8s} {'Win%':>7s} {'ProfFct':>8s} {'Доходность':>12s} {'Просадка':>10s} {'  vs Long-only'}")
    for symbol in final_symbols:
        df = load_price_data(exchange, symbol)
        if len(df) < SLOW_MA + ATR_PERIOD + 5:
            continue

        long_only = backtest_ma_crossover(df, atr_multiplier=ATR_MULTIPLIER)
        long_short = backtest_long_short(df, atr_multiplier=ATR_MULTIPLIER)

        print(f"{symbol:12s} {long_short['trades']:>8d} {long_short['win_rate']:>6.1f}% "
              f"{long_short['profit_factor']:>8.2f} {long_short['total_return_pct']:>+11.1f}% "
              f"{long_short['max_drawdown_pct']:>9.1f}%   long-only={long_only['total_return_pct']:+.1f}%")
        print(f"{'':12s} (из них шортов: {long_short['short_trades']}, win rate шортов: {long_short['short_win_rate']:.1f}%)")


def backtest_mean_reversion(df, period=BB_PERIOD, std_mult=BB_STD_MULTIPLIER, fee_percent=TAKER_FEE_PERCENT,
                             atr_multiplier=ATR_MULTIPLIER):
    df = add_atr(df.copy())
    df["sma"] = df["close"].rolling(period).mean()
    df["std"] = df["close"].rolling(period).std()
    df["lower_band"] = df["sma"] - std_mult * df["std"]

    trades = []
    in_position = False
    entry_price = None
    entry_date = None
    stop_price = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        if pd.isna(row["lower_band"]) or pd.isna(row["atr"]):
            continue

        if in_position:
            if stop_price is not None and row["low"] <= stop_price:
                exit_price = stop_price * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                trades.append({"entry_date": entry_date, "exit_date": row["date"],
                                "pnl_percent": pnl_percent, "reason": "stop_loss"})
                in_position = False
                continue
            # цель — возврат к среднему (цена "поднялась туда, где была" до просадки)
            if row["high"] >= row["sma"]:
                exit_price = row["sma"] * (1 - fee_percent / 100)
                pnl_percent = (exit_price - entry_price) / entry_price * 100
                trades.append({"entry_date": entry_date, "exit_date": row["date"],
                                "pnl_percent": pnl_percent, "reason": "mean_reached"})
                in_position = False
                continue

        if not in_position and row["close"] < row["lower_band"]:
            entry_price = row["close"] * (1 + fee_percent / 100)
            entry_date = row["date"]
            stop_price = row["close"] - atr_multiplier * row["atr"]
            in_position = True

    if not trades:
        return {"trades": 0, "win_rate": 0, "profit_factor": 0, "total_return_pct": 0, "max_drawdown_pct": 0}

    equity = INITIAL_CAPITAL
    equity_curve = [equity]
    for t in trades:
        equity *= (1 + t["pnl_percent"] / 100)
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_drawdown = 0
    for e in equity_curve:
        peak = max(peak, e)
        max_drawdown = max(max_drawdown, (peak - e) / peak * 100)

    wins = [t for t in trades if t["pnl_percent"] > 0]
    losses = [t for t in trades if t["pnl_percent"] <= 0]
    gross_profit = sum(t["pnl_percent"] for t in wins)
    gross_loss = abs(sum(t["pnl_percent"] for t in losses))

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "profit_factor": gross_profit / gross_loss if gross_loss else float("inf"),
        "total_return_pct": (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100,
        "max_drawdown_pct": max_drawdown,
        "stop_loss_exits": sum(1 for t in trades if t["reason"] == "stop_loss"),
    }


def test_mean_reversion():
    exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True})

    print(f"\nMean-reversion: покупка ниже нижней полосы Боллинджера ({BB_PERIOD}, {BB_STD_MULTIPLIER}σ), "
          f"продажа на возврате к средней, ATR-стоп x{ATR_MULTIPLIER}\n")

    summary = []
    for symbol in SYMBOLS:
        try:
            df = load_price_data(exchange, symbol)
        except Exception as e:
            print(f"{symbol}: ошибка загрузки данных ({e})")
            continue
        if len(df) < BB_PERIOD + ATR_PERIOD + 5:
            continue

        r = backtest_mean_reversion(df)
        calmar = r["total_return_pct"] / r["max_drawdown_pct"] if r["max_drawdown_pct"] else 0
        summary.append((symbol, r, calmar))

    summary.sort(key=lambda x: -x[2])

    print(f"{'Монета':12s} {'Сделок':>7s} {'Win%':>7s} {'ProfFct':>8s} {'Доходность':>12s} {'Просадка':>10s} {'Return/DD':>10s}")
    for symbol, r, calmar in summary:
        print(f"{symbol:12s} {r['trades']:>7d} {r['win_rate']:>6.1f}% {r['profit_factor']:>8.2f} "
              f"{r['total_return_pct']:>+11.1f}% {r['max_drawdown_pct']:>9.1f}% {calmar:>10.2f}")


MINUTE_LOOKBACK_DAYS = 14  # больше — слишком много данных и долгая загрузка через REST


def test_mean_reversion_minute():
    exchange = ccxt.bybit({"enableRateLimit": True})  # та биржа, где реально торгуем
    symbols = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "TRX/USDT", "DOGE/USDT", "ADA/USDT"]

    print(f"\nMean-reversion на 1-минутных свечах, {MINUTE_LOOKBACK_DAYS} дней истории, Bybit")
    print(f"Боллинджер({BB_PERIOD}, {BB_STD_MULTIPLIER}σ) + ATR-стоп x{ATR_MULTIPLIER}, комиссия {TAKER_FEE_PERCENT}%/сторону\n")

    for symbol in symbols:
        since_ms = exchange.milliseconds() - MINUTE_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
        try:
            candles = fetch_ohlcv_full(exchange, symbol, "1m", since_ms)
        except Exception as e:
            print(f"{symbol}: ошибка загрузки ({e})")
            continue

        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["ts"], unit="ms")

        if len(df) < BB_PERIOD + ATR_PERIOD + 5:
            print(f"{symbol}: недостаточно данных ({len(df)} свечей)")
            continue

        r = backtest_mean_reversion(df)
        buy_hold = (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0] * 100

        # средняя длительность сделки в минутах — пересчитаем отдельно, т.к. backtest_mean_reversion
        # не возвращает entry/exit даты наружу
        print(f"{symbol:10s} свечей={len(df):>6d}  сделок={r['trades']:>4d}  win={r['win_rate']:>5.1f}%  "
              f"PF={r['profit_factor']:>5.2f}  доходность={r['total_return_pct']:>+7.2f}%  "
              f"просадка={r['max_drawdown_pct']:>6.2f}%  buy&hold(14д)={buy_hold:>+7.2f}%")


if __name__ == "__main__":
    import sys
    if "--tp" in sys.argv:
        test_take_profit()
    elif "--short" in sys.argv:
        test_long_short()
    elif "--mr" in sys.argv:
        test_mean_reversion()
    elif "--mr1m" in sys.argv:
        test_mean_reversion_minute()
    else:
        main()
