import asyncio
import ccxt
import json
import logging
import os
import socket
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ─────────────────────────────────────────────────────────
# НАСТРОЙКИ — редактируй под себя
# ─────────────────────────────────────────────────────────

EXCHANGES = ["binance", "bybit", "okx", "kucoin", "mexc", "bitget", "htx", "gate"]
SYMBOLS = [
    "XRP/USDT", "DOGE/USDT", "TRX/USDT", "ADA/USDT", "LTC/USDT",
    "SOL/USDT", "DOT/USDT", "AVAX/USDT", "LINK/USDT", "ATOM/USDT",
    "NEAR/USDT", "APT/USDT", "ARB/USDT", "SHIB/USDT", "PEPE/USDT",
]

# Межбиржевой арбитраж оказался нерабочей моделью для ретейла (см. историю
# сигналов) — выключен. Тренд-стратегия ниже работает независимо от этого флага.
ENABLE_ARBITRAGE = False

# MIN_SPREAD_PERCENT сравнивается с net_spread_percent — спредом,
# из которого уже вычтены комиссии обеих бирж (см. EXCHANGE_TAKER_FEE_PERCENT).
MIN_SPREAD_PERCENT = 0.2
POLL_INTERVAL_SECONDS = 90

# Комиссия тейкера в процентах на каждой бирже (примерные значения для
# спота без VIP-скидок). Используются, чтобы не слать сигналы, которые
# на самом деле убыточны после комиссий на покупку+продажу.
# Минимальный суточный объём (в USDT), ниже которого котировка биржи
# по этой паре игнорируется — иначе спред считается по цене последней
# случайной сделки на неликвидном рынке, а не по реально исполнимой цене.
MIN_24H_VOLUME_USDT = 200_000

DEFAULT_TAKER_FEE_PERCENT = 0.1
EXCHANGE_TAKER_FEE_PERCENT = {
    "binance": 0.1,
    "bybit": 0.1,
    "okx": 0.1,
    "kucoin": 0.1,
    "mexc": 0.1,
    "bitget": 0.1,
    "htx": 0.2,
    "gate": 0.2,
}

# Пока False — кнопка "Исполнить" будет только писать "dry_run",
# ничего не покупая/продавая по-настоящему. Включай True только
# когда полностью доверяешь логике бота.
LIVE_TRADING = False

MAX_TRADE_USDT = 10

# ─────────────────────────────────────────────────────────
# Трендследующая стратегия (спот, одна биржа, MA crossover) —
# отдельно от межбиржевого арбитража выше.
# ─────────────────────────────────────────────────────────

TREND_EXCHANGE_ID = "bybit"  # основной капитал у пользователя там
# Список отобран бэктестом (MA + ATR-стоп, 3 года): SOL и ещё 9 монет
# показали слабую или отрицательную доходность на этой стратегии — убраны.
TREND_SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "TRX/USDT"]
TREND_TIMEFRAME = "1d"
TREND_FAST_MA = 20
TREND_SLOW_MA = 50

# ATR-стоп — выход при падении цены от входа на ATR_MULTIPLIER * ATR,
# подстраивается под волатильность конкретной монеты (см. backtest.py)
TREND_ATR_PERIOD = 14
TREND_ATR_MULTIPLIER = 3

# Проверяем раз в 4 часа — стратегия на дневных свечах, чаще смысла нет
TREND_CHECK_INTERVAL_SECONDS = 4 * 60 * 60

# Размер РЕАЛЬНОЙ сделки по кнопке (работает только если LIVE_TRADING = True)
TREND_MAX_TRADE_USDT = 20

# Виртуальный капитал на каждый символ в бумажном (paper) портфеле —
# бот "торгует" ими сам, полностью автоматически, не трогая реальные деньги
PAPER_INITIAL_CAPITAL_PER_SYMBOL = 1000

# Алерт на аномальную волатильность — не анализ новостей (ненадёжно), а просто
# сигнал "что-то резко случилось, проверь сам" на случай неожиданных заявлений
# (Трамп, регуляторы, взломы и т.п.), которые MA-стратегия увидит только
# постфактум на следующей дневной свече.
VOLATILITY_CHECK_INTERVAL_SECONDS = 15 * 60
VOLATILITY_WINDOW_TIMEFRAME = "5m"
VOLATILITY_WINDOW_CANDLES = 12  # ~1 час
VOLATILITY_SPIKE_THRESHOLD_PERCENT = 5
VOLATILITY_ALERT_COOLDOWN_SECONDS = 2 * 60 * 60  # не спамить повторно по той же монете

# Ссылки для кнопок "Открыть биржу" — ведут на страницу торговли этой парой
EXCHANGE_URLS = {
    "binance": "https://www.binance.com/en/trade/{base}_{quote}",
    "bybit": "https://www.bybit.com/en/trade/spot/{base}/{quote}",
    "okx": "https://www.okx.com/trade-spot/{base}-{quote}",
    "kucoin": "https://www.kucoin.com/trade/{base}-{quote}",
    "mexc": "https://www.mexc.com/exchange/{base}_{quote}",
    "bitget": "https://www.bitget.com/spot/{base}{quote}",
    "htx": "https://www.htx.com/trade/{base_lower}_{quote_lower}",
    "gate": "https://www.gate.io/trade/{base}_{quote}",
}

# ─────────────────────────────────────────────────────────

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=2, encoding="utf-8")],
)
# httpx логирует каждый запрос к Telegram вместе с токеном бота в URL
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("arbi-bot")

pending_trades = {}
trade_counter = 0

# Каждый сигнал, прошедший порог MIN_SPREAD_PERCENT, пишется сюда — это
# журнал НАЙДЕННЫХ возможностей, а не реально исполненных сделок (пока
# LIVE_TRADING = False). На его основе считается % "прибыльных" сигналов.
SIGNAL_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals_log.jsonl")


def log_signal(symbol, result):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "buy_exchange": result["buy_exchange"],
        "sell_exchange": result["sell_exchange"],
        "buy_price": result["buy_price"],
        "sell_price": result["sell_price"],
        "spread_percent": result["spread_percent"],
        "net_spread_percent": result["net_spread_percent"],
    }
    try:
        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Не удалось записать сигнал в лог: {e}")


def compute_stats():
    if not os.path.exists(SIGNAL_LOG_PATH):
        return None
    entries = []
    with open(SIGNAL_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not entries:
        return None

    total = len(entries)
    profitable = [e for e in entries if e["net_spread_percent"] > 0]
    net_values = [e["net_spread_percent"] for e in entries]

    return {
        "total": total,
        "profitable": len(profitable),
        "win_rate": len(profitable) / total * 100,
        "avg_net": sum(net_values) / total,
        "best": max(entries, key=lambda e: e["net_spread_percent"]),
        "worst": min(entries, key=lambda e: e["net_spread_percent"]),
    }


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TREND_STATE_PATH = os.path.join(BASE_DIR, "trend_state.json")
REAL_POSITIONS_PATH = os.path.join(BASE_DIR, "trend_real_positions.json")
PAPER_STATE_PATH = os.path.join(BASE_DIR, "paper_portfolio.json")
PAPER_TRADES_LOG_PATH = os.path.join(BASE_DIR, "paper_trades.jsonl")
PENDING_STATE_PATH = os.path.join(BASE_DIR, "pending_trades.json")
VOLATILITY_STATE_PATH = os.path.join(BASE_DIR, "volatility_state.json")


def load_pending_state():
    global trade_counter
    data = load_json(PENDING_STATE_PATH, {"counter": 0, "trades": {}})
    pending_trades.update(data.get("trades", {}))
    trade_counter = data.get("counter", 0)


def save_pending_state():
    save_json(PENDING_STATE_PATH, {"counter": trade_counter, "trades": pending_trades})


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sync_exchange_time(exchange):
    # Bybit отклоняет подписанные запросы, если часы ПК расходятся с биржей больше чем на 5 с,
    # а служба времени Windows бывает выключена. ccxt сам сверяет время только после первого
    # подписанного запроса при загрузке рынков, поэтому делаем это заранее, перед каждой проверкой.
    try:
        exchange.load_time_difference()
    except Exception as e:
        logger.warning(f"Не удалось сверить время с {exchange.id}: {e}")


TELEGRAM_SEND_ATTEMPTS = 3


async def send_to_chat(context, text, reply_markup=None):
    # Сообщение вторично: к этому моменту сделка уже записана, и сбой Telegram
    # не должен обрывать проверку и оставлять состояние недосохранённым.
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        try:
            await context.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=reply_markup)
            return True
        except BadRequest as e:  # наследник NetworkError, но повтор тут бесполезен
            logger.error(f"Telegram отклонил сообщение: {e}")
            return False
        except NetworkError as e:
            logger.warning(f"Telegram: сбой отправки, попытка {attempt}/{TELEGRAM_SEND_ATTEMPTS}: {e}")
            if attempt < TELEGRAM_SEND_ATTEMPTS:
                await asyncio.sleep(10 * attempt)
        except TelegramError as e:
            logger.error(f"Telegram отклонил сообщение: {e}")
            return False
    logger.error(f"Telegram: сообщение не отправлено после {TELEGRAM_SEND_ATTEMPTS} попыток: {text[:80]!r}")
    return False


def compute_trend_signal(exchange, symbol, fast=TREND_FAST_MA, slow=TREND_SLOW_MA,
                          atr_period=TREND_ATR_PERIOD, timeframe=TREND_TIMEFRAME):
    limit = max(slow, atr_period) + 6
    candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if not candles:
        return None
    current_price = candles[-1][4]

    # Сигнал и ATR считаем только по закрытым свечам, как в бэктесте: незакрытая дневная
    # свеча меняется весь день, и у пересечения средних сигнал прыгал бы туда-обратно.
    # Текущая цена при этом нужна для стопа и цены сделки.
    if candles[-1][0] + exchange.parse_timeframe(timeframe) * 1000 > exchange.milliseconds():
        candles = candles[:-1]
    if len(candles) < slow:
        return None

    closes = [c[4] for c in candles]
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    fast_ma = sum(closes[-fast:]) / fast
    slow_ma = sum(closes[-slow:]) / slow
    signal = 1 if fast_ma > slow_ma else 0

    trs = []
    for i in range(1, len(candles)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-atr_period:]) / atr_period if len(trs) >= atr_period else None

    return {"signal": signal, "price": current_price, "atr": atr}


def paper_buy(paper_portfolio, symbol, price, fee_percent=DEFAULT_TAKER_FEE_PERCENT):
    state = paper_portfolio.setdefault(
        symbol, {"cash": PAPER_INITIAL_CAPITAL_PER_SYMBOL, "coin_qty": 0.0, "avg_entry_price": None}
    )
    if state["coin_qty"] > 0 or state["cash"] <= 0:
        return None
    buy_price = price * (1 + fee_percent / 100)
    qty = state["cash"] / buy_price
    state["coin_qty"] = qty
    state["avg_entry_price"] = buy_price
    state["cash"] = 0.0
    return {"qty": qty, "price": buy_price}


def paper_sell(paper_portfolio, symbol, price, fee_percent=DEFAULT_TAKER_FEE_PERCENT):
    state = paper_portfolio.get(symbol)
    if not state or state["coin_qty"] <= 0:
        return None
    sell_price = price * (1 - fee_percent / 100)
    proceeds = state["coin_qty"] * sell_price
    pnl_percent = (sell_price - state["avg_entry_price"]) / state["avg_entry_price"] * 100
    qty = state["coin_qty"]
    state["cash"] = proceeds
    state["coin_qty"] = 0.0
    state["avg_entry_price"] = None
    return {"qty": qty, "price": sell_price, "pnl_percent": pnl_percent, "proceeds": proceeds}


def log_paper_trade(symbol, side, info):
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "side": side, **info}
    try:
        with open(PAPER_TRADES_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"Не удалось записать бумажную сделку в лог: {e}")


def compute_paper_stats(instances=None):
    if not os.path.exists(PAPER_TRADES_LOG_PATH):
        return None
    closed_trades = []
    with open(PAPER_TRADES_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry["side"] == "sell":
                closed_trades.append(entry)

    paper_portfolio = load_json(PAPER_STATE_PATH, {})
    trend_state = load_json(TREND_STATE_PATH, {})
    exchange = instances.get(TREND_EXCHANGE_ID) if instances else None
    total_equity = 0.0
    positions = []
    for symbol in TREND_SYMBOLS:
        state = paper_portfolio.get(symbol, {"cash": PAPER_INITIAL_CAPITAL_PER_SYMBOL, "coin_qty": 0.0})
        if state["coin_qty"] > 0 and state.get("avg_entry_price"):
            # незакрытую позицию оцениваем по текущей рыночной цене, а не по цене
            # входа — иначе нереализованный P&L всегда показывал бы 0%
            entry = state["avg_entry_price"]
            price = entry
            if exchange:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    if ticker and ticker.get("last"):
                        price = ticker["last"]
                except Exception as e:
                    logger.warning(f"Paper stats: не удалось получить цену {symbol}: {e}")
            total_equity += state["coin_qty"] * price
            stop = trend_state.get(symbol, {}).get("stop_price")
            positions.append({
                "symbol": symbol,
                "pnl_percent": (price - entry) / entry * 100,
                "pnl_usdt": state["coin_qty"] * (price - entry),
                "to_stop_percent": (price - stop) / price * 100 if stop else None,
            })
        else:
            total_equity += state["cash"]

    stats = {
        "closed_trades": len(closed_trades),
        "total_equity": total_equity,
        "initial_total": PAPER_INITIAL_CAPITAL_PER_SYMBOL * len(TREND_SYMBOLS),
        "positions": positions,
    }
    if closed_trades:
        wins = [t for t in closed_trades if t["pnl_percent"] > 0]
        stats["win_rate"] = len(wins) / len(closed_trades) * 100
        stats["avg_pnl_percent"] = sum(t["pnl_percent"] for t in closed_trades) / len(closed_trades)
    return stats


async def handle_trend_entry(context, symbol, price, paper_portfolio, stop_price=None):
    global trade_counter

    paper_result = paper_buy(paper_portfolio, symbol, price)
    if paper_result:
        log_paper_trade(symbol, "buy", paper_result)

    real_qty = TREND_MAX_TRADE_USDT / price if price else 0
    trade_counter += 1
    trade_id = str(trade_counter)
    pending_trades[trade_id] = {
        "kind": "trend",
        "exchange": TREND_EXCHANGE_ID,
        "symbol": symbol,
        "qty": real_qty,
    }
    save_pending_state()

    stop_line = f"\nСтоп-лосс (ATR x{TREND_ATR_MULTIPLIER}): {stop_price:.6g}" if stop_price else ""
    text = (
        f"📈 Тренд-сигнал: ВХОД в {symbol} ({TREND_EXCHANGE_ID})\n"
        f"Цена: {price}{stop_line}\n"
        f"Бумажная сделка уже записана автоматически (виртуальные деньги)"
    )
    keyboard = [[
        InlineKeyboardButton(f"🟢 Купить (~{TREND_MAX_TRADE_USDT} USDT)", callback_data=f"buy:{trade_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{trade_id}"),
    ]]
    await send_to_chat(context, text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_trend_exit(context, symbol, price, paper_portfolio, reason="ma_cross"):
    global trade_counter

    paper_result = paper_sell(paper_portfolio, symbol, price)
    pnl_line = ""
    if paper_result:
        log_paper_trade(symbol, "sell", paper_result)
        pnl_line = f"\nБумажный P&L сделки: {paper_result['pnl_percent']:+.2f}%"

    real_positions = load_json(REAL_POSITIONS_PATH, {})
    real_qty = real_positions.get(symbol, 0)

    trade_counter += 1
    trade_id = str(trade_counter)
    pending_trades[trade_id] = {
        "kind": "trend",
        "exchange": TREND_EXCHANGE_ID,
        "symbol": symbol,
        "qty": real_qty,
    }
    save_pending_state()

    reason_label = "🛑 СТОП-ЛОСС" if reason == "stop_loss" else "📉 Разворот тренда (MA)"
    text = f"{reason_label}: ВЫХОД из {symbol} ({TREND_EXCHANGE_ID})\nЦена: {price}{pnl_line}"
    keyboard = [[
        InlineKeyboardButton("🔴 Продать", callback_data=f"sell:{trade_id}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{trade_id}"),
    ]]
    await send_to_chat(context, text, reply_markup=InlineKeyboardMarkup(keyboard))


async def trend_check_job(context: ContextTypes.DEFAULT_TYPE):
    instances = context.bot_data["instances"]
    exchange = instances.get(TREND_EXCHANGE_ID)
    if not exchange:
        return
    await asyncio.to_thread(sync_exchange_time, exchange)

    trend_state = load_json(TREND_STATE_PATH, {})
    paper_portfolio = load_json(PAPER_STATE_PATH, {})

    for symbol in TREND_SYMBOLS:
        try:
            data = await asyncio.to_thread(compute_trend_signal, exchange, symbol)
        except Exception as e:
            logger.warning(f"Trend: не удалось получить данные {symbol}: {e}")
            continue
        if not data:
            continue

        state = trend_state.get(symbol, {"in_position": False})
        was_in_position = state.get("in_position", False)

        if was_in_position:
            stop_price = state.get("stop_price")
            # Стоп проверяется раз в TREND_CHECK_INTERVAL_SECONDS по текущей цене —
            # это не то же самое, что мгновенный стоп-ордер на бирже: между проверками
            # цена может провалиться глубже, чем сам уровень стопа (проскальзывание).
            if stop_price and data["price"] <= stop_price:
                await handle_trend_exit(context, symbol, data["price"], paper_portfolio, reason="stop_loss")
                # Стоп пробит — тренд сломан, даже если MA ещё смотрят вверх. Не входим снова,
                # пока MA не развернутся вниз и обратно (бэктест: +134% против +122%, просадка ниже).
                trend_state[symbol] = {"in_position": False, "wait_new_cross": True}
            elif data["signal"] == 0:
                await handle_trend_exit(context, symbol, data["price"], paper_portfolio, reason="ma_cross")
                trend_state[symbol] = {"in_position": False}
        elif state.get("wait_new_cross"):
            if data["signal"] == 0:
                trend_state[symbol] = {"in_position": False}
        elif data["signal"] == 1:
            stop_price = data["price"] - TREND_ATR_MULTIPLIER * data["atr"] if data["atr"] else None
            await handle_trend_entry(context, symbol, data["price"], paper_portfolio, stop_price=stop_price)
            trend_state[symbol] = {"in_position": True, "stop_price": stop_price}

        # Сохраняем после каждой монеты: сделка уже записана в журнал, и если проверка
        # оборвётся на следующей монете, при повторе она не должна исполниться второй раз.
        save_json(TREND_STATE_PATH, trend_state)
        save_json(PAPER_STATE_PATH, paper_portfolio)


def compute_recent_move(exchange, symbol, timeframe=VOLATILITY_WINDOW_TIMEFRAME, candles_count=VOLATILITY_WINDOW_CANDLES):
    candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=candles_count)
    if len(candles) < 2:
        return None
    start_price = candles[0][4]
    current_price = candles[-1][4]
    if not start_price:
        return None
    pct_change = (current_price - start_price) / start_price * 100
    return {"pct_change": pct_change, "price": current_price}


async def volatility_check_job(context: ContextTypes.DEFAULT_TYPE):
    instances = context.bot_data["instances"]
    exchange = instances.get(TREND_EXCHANGE_ID)
    if not exchange:
        return
    await asyncio.to_thread(sync_exchange_time, exchange)

    state = load_json(VOLATILITY_STATE_PATH, {})
    now = datetime.now(timezone.utc)

    for symbol in TREND_SYMBOLS:
        try:
            move = await asyncio.to_thread(compute_recent_move, exchange, symbol)
        except Exception as e:
            logger.warning(f"Volatility: не удалось получить данные {symbol}: {e}")
            continue
        if not move or abs(move["pct_change"]) < VOLATILITY_SPIKE_THRESHOLD_PERCENT:
            continue

        last_alert_iso = state.get(symbol)
        if last_alert_iso:
            elapsed = (now - datetime.fromisoformat(last_alert_iso)).total_seconds()
            if elapsed < VOLATILITY_ALERT_COOLDOWN_SECONDS:
                continue

        direction = "вверх 📈" if move["pct_change"] > 0 else "вниз 📉"
        text = (
            f"⚡ Аномальное движение: {symbol}\n"
            f"{move['pct_change']:+.1f}% за последний час ({direction})\n"
            f"Текущая цена: {move['price']}\n"
            f"Возможна важная новость — стоит проверить самостоятельно, "
            f"бот новости не анализирует, только резкость движения цены."
        )
        await send_to_chat(context, text)
        state[symbol] = now.isoformat()

    save_json(VOLATILITY_STATE_PATH, state)


async def format_paper_stats_text(instances):
    stats = await asyncio.to_thread(compute_paper_stats, instances)
    if not stats:
        return "Пока нет данных по бумажному портфелю."

    pnl_total = stats["total_equity"] - stats["initial_total"]
    pnl_total_percent = pnl_total / stats["initial_total"] * 100

    lines = ["📄 Бумажный портфель (виртуальные деньги)", ""]
    for p in sorted(stats["positions"], key=lambda p: -p["pnl_percent"]):
        stop_part = f", до стопа {p['to_stop_percent']:.1f}%" if p["to_stop_percent"] is not None else ""
        lines.append(f"{p['symbol']}: {p['pnl_percent']:+.2f}% ({p['pnl_usdt']:+.2f} USDT){stop_part}")
    if stats["positions"]:
        lines.append("")
    lines.append(f"Итого: {pnl_total:+.2f} USDT ({pnl_total_percent:+.2f}%) "
                 f"от {stats['initial_total']:.0f} USDT")
    if stats["closed_trades"]:
        lines.append(f"Закрытых сделок: {stats['closed_trades']}")
        lines.append(f"Win rate: {stats['win_rate']:.1f}%")
        lines.append(f"Средний P&L на сделке: {stats['avg_pnl_percent']:+.2f}%")
    else:
        lines.append("Закрытых сделок пока нет.")

    return "\n".join(lines)


async def paperstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await format_paper_stats_text(context.bot_data["instances"])
    await update.message.reply_text(text)


# Статистика в Telegram в каждый чётный час по местному времени (00:00, 02:00, ... 22:00).
# Часовой пояс задан явно, чтобы при переходе на зимнее время расписание не сдвигалось.
PAPER_STATS_TIMEZONE = ZoneInfo("Europe/Chisinau")


async def paper_stats_job(context: ContextTypes.DEFAULT_TYPE):
    stats_text = await format_paper_stats_text(context.bot_data["instances"])
    await send_to_chat(context, stats_text)


def load_api_credentials(ex_id):
    prefix = ex_id.upper()
    api_key = os.getenv(f"{prefix}_API_KEY")
    secret = os.getenv(f"{prefix}_SECRET")
    password = os.getenv(f"{prefix}_PASSWORD")
    creds = {}
    if api_key and secret:
        creds["apiKey"] = api_key
        creds["secret"] = secret
        if password:
            creds["password"] = password
    return creds


def init_exchanges():
    instances = {}
    for ex_id in EXCHANGES:
        try:
            exchange_class = getattr(ccxt, ex_id)
            # adjustForTimeDifference: ccxt сверяет время с биржей и поправляет подписанные
            # запросы — иначе при сбитых часах Windows Bybit отклоняет их (retCode 10002)
            config = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True}}
            config.update(load_api_credentials(ex_id))
            instances[ex_id] = exchange_class(config)
        except Exception as e:
            logger.warning(f"Не удалось инициализировать {ex_id}: {e}")
    return instances


def fetch_prices(instances, symbol):
    prices = {}
    for ex_id, exchange in instances.items():
        try:
            ticker = exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.debug(f"{ex_id}: нет пары {symbol} или ошибка ({e})")
            continue
        if not ticker:
            continue
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if not bid or not ask:
            continue
        volume_quote = ticker.get("quoteVolume")
        if volume_quote is None or volume_quote < MIN_24H_VOLUME_USDT:
            logger.debug(f"{ex_id}: {symbol} отфильтрован по низкому объёму ({volume_quote})")
            continue
        prices[ex_id] = {"bid": bid, "ask": ask}
    return prices


def find_best_spread(prices):
    if len(prices) < 2:
        return None
    # buy_price = ask (реальная цена покупки), sell_price = bid (реальная цена продажи) —
    # а не цена последней сделки, которая на неликвидном рынке может быть случайной.
    buy_exchange = min(prices, key=lambda ex: prices[ex]["ask"])
    sell_exchange = max(prices, key=lambda ex: prices[ex]["bid"])
    if buy_exchange == sell_exchange:
        return None
    buy_price = prices[buy_exchange]["ask"]
    sell_price = prices[sell_exchange]["bid"]
    if buy_price <= 0:
        return None
    spread_percent = (sell_price - buy_price) / buy_price * 100

    buy_fee_percent = EXCHANGE_TAKER_FEE_PERCENT.get(buy_exchange, DEFAULT_TAKER_FEE_PERCENT)
    sell_fee_percent = EXCHANGE_TAKER_FEE_PERCENT.get(sell_exchange, DEFAULT_TAKER_FEE_PERCENT)
    net_spread_percent = spread_percent - buy_fee_percent - sell_fee_percent

    return {
        "buy_exchange": buy_exchange,
        "sell_exchange": sell_exchange,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "spread_percent": spread_percent,
        "net_spread_percent": net_spread_percent,
    }


def check_balances(instances, result, symbol):
    base, quote = symbol.split("/")
    buy_ex = instances[result["buy_exchange"]]
    sell_ex = instances[result["sell_exchange"]]
    try:
        buy_balance = buy_ex.fetch_balance()
        sell_balance = sell_ex.fetch_balance()
    except Exception as e:
        logger.warning(f"Не удалось получить баланс: {e}")
        return None

    usdt_available = buy_balance.get(quote, {}).get("free", 0) or 0
    coin_available = sell_balance.get(base, {}).get("free", 0) or 0

    trade_usdt = min(MAX_TRADE_USDT, usdt_available)
    trade_qty_by_usdt = trade_usdt / result["buy_price"] if result["buy_price"] else 0
    trade_qty = min(coin_available, trade_qty_by_usdt)

    if trade_qty <= 0:
        return None

    return {"quantity": trade_qty, "usdt_amount": trade_qty * result["buy_price"]}


def execute_trade_sync(instances, result, symbol, qty):
    buy_ex = instances[result["buy_exchange"]]
    sell_ex = instances[result["sell_exchange"]]

    if not LIVE_TRADING:
        return "тестовый режим — сделка НЕ исполнена по-настоящему"

    try:
        buy_order = buy_ex.create_market_buy_order(symbol, qty)
        sell_order = sell_ex.create_market_sell_order(symbol, qty)
        return f"исполнено ✅ (buy={buy_order.get('id')}, sell={sell_order.get('id')})"
    except Exception as e:
        return f"ошибка: {e}"


def execute_single_leg_sync(instances, ex_id, side, symbol, qty):
    exchange = instances[ex_id]

    if not LIVE_TRADING:
        return "тестовый режим — заявка НЕ исполнена по-настоящему"

    try:
        if side == "buy":
            order = exchange.create_market_buy_order(symbol, qty)
        else:
            order = exchange.create_market_sell_order(symbol, qty)
        return f"исполнено ✅ (id={order.get('id')})"
    except Exception as e:
        return f"ошибка: {e}"


def build_exchange_url(ex_id, symbol):
    base, quote = symbol.split("/")
    template = EXCHANGE_URLS.get(ex_id)
    if not template:
        return None
    return template.format(base=base, quote=quote, base_lower=base.lower(), quote_lower=quote.lower())


async def check_prices_job(context: ContextTypes.DEFAULT_TYPE):
    global trade_counter
    instances = context.bot_data["instances"]

    for symbol in SYMBOLS:
        prices = await asyncio.to_thread(fetch_prices, instances, symbol)
        result = find_best_spread(prices)
        if result:
            logger.info(
                f"{symbol}: спред {result['spread_percent']:.3f}% (гросс), "
                f"{result['net_spread_percent']:.3f}% (нетто после комиссий) "
                f"({result['buy_exchange']} -> {result['sell_exchange']})"
            )
        if not result or result["net_spread_percent"] < MIN_SPREAD_PERCENT:
            continue

        log_signal(symbol, result)
        sizing = await asyncio.to_thread(check_balances, instances, result, symbol)
        balance_verified = sizing is not None
        if not sizing and result["buy_price"]:
            fallback_qty = MAX_TRADE_USDT / result["buy_price"]
            sizing = {"quantity": fallback_qty, "usdt_amount": fallback_qty * result["buy_price"]}

        text = (
            f"🔥 Арбитраж {symbol}\n"
            f"Купить на {result['buy_exchange']}: {result['buy_price']}\n"
            f"Продать на {result['sell_exchange']}: {result['sell_price']}\n"
            f"Спред: {result['spread_percent']:.2f}% (гросс) / "
            f"{result['net_spread_percent']:.2f}% (после комиссий)"
        )

        keyboard_rows = []

        trade_counter += 1
        trade_id = str(trade_counter)
        pending_trades[trade_id] = {
            "kind": "arbitrage",
            "result": result,
            "symbol": symbol,
            "qty": sizing["quantity"] if sizing else 0,
        }
        save_pending_state()

        if sizing and sizing["quantity"] > 0:
            if balance_verified:
                text += f"\nПредлагаемая сумма: {sizing['usdt_amount']:.2f} USDT"
            else:
                text += (
                    f"\n⚠️ Баланс не проверен (нет ключей или ошибка API)\n"
                    f"Ориентировочная сумма: {sizing['usdt_amount']:.2f} USDT"
                )
            keyboard_rows.append([
                InlineKeyboardButton(f"🟢 Купить ({result['buy_exchange']})", callback_data=f"buy:{trade_id}"),
                InlineKeyboardButton(f"🔴 Продать ({result['sell_exchange']})", callback_data=f"sell:{trade_id}"),
            ])
            keyboard_rows.append([
                InlineKeyboardButton("✅ Исполнить оба", callback_data=f"execute:{trade_id}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{trade_id}"),
            ])
        else:
            text += "\n⚠️ Не удалось определить сумму сделки"
            keyboard_rows.append([InlineKeyboardButton("❌ Скрыть", callback_data=f"skip:{trade_id}")])

        link_row = []
        buy_url = build_exchange_url(result["buy_exchange"], symbol)
        sell_url = build_exchange_url(result["sell_exchange"], symbol)
        if buy_url:
            link_row.append(InlineKeyboardButton(f"Открыть {result['buy_exchange']}", url=buy_url))
        if sell_url:
            link_row.append(InlineKeyboardButton(f"Открыть {result['sell_exchange']}", url=sell_url))
        if link_row:
            keyboard_rows.append(link_row)

        markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
        await send_to_chat(context, text, reply_markup=markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, trade_id = query.data.split(":")

    if action in ("buy", "sell"):
        trade = pending_trades.get(trade_id)
        if not trade:
            await query.edit_message_text(query.message.text + "\n\n(эта сделка уже устарела)")
            return
        instances = context.bot_data["instances"]
        kind = trade.get("kind", "arbitrage")

        if kind == "trend":
            ex_id = trade["exchange"]
        else:
            ex_id = trade["result"]["buy_exchange"] if action == "buy" else trade["result"]["sell_exchange"]

        label = "Купить" if action == "buy" else "Продать"
        status = await asyncio.to_thread(execute_single_leg_sync, instances, ex_id, action, trade["symbol"], trade["qty"])

        if kind == "trend" and LIVE_TRADING and "ошибка" not in status:
            real_positions = load_json(REAL_POSITIONS_PATH, {})
            if action == "buy":
                real_positions[trade["symbol"]] = trade["qty"]
            else:
                real_positions.pop(trade["symbol"], None)
            save_json(REAL_POSITIONS_PATH, real_positions)

        await query.edit_message_text(query.message.text + f"\n\n▶️ {label} ({ex_id}): {status}")
        return

    trade = pending_trades.pop(trade_id, None)
    save_pending_state()

    if not trade:
        await query.edit_message_text(query.message.text + "\n\n(эта сделка уже устарела)")
        return

    if action == "skip":
        await query.edit_message_text(query.message.text + "\n\n❌ Пропущено вручную")
        return

    if action == "execute":
        instances = context.bot_data["instances"]
        status = await asyncio.to_thread(execute_trade_sync, instances, trade["result"], trade["symbol"], trade["qty"])
        await query.edit_message_text(query.message.text + f"\n\n▶️ {status}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = compute_stats()
    if not stats:
        await update.message.reply_text("Пока нет ни одного сигнала для статистики.")
        return

    text = (
        "📊 Статистика сигналов (найденные возможности, не реальные сделки)\n"
        f"Всего сигналов: {stats['total']}\n"
        f"Прибыльных после комиссий: {stats['profitable']} ({stats['win_rate']:.1f}%)\n"
        f"Средний net-спред: {stats['avg_net']:.3f}%\n"
        f"Лучший: {stats['best']['symbol']} {stats['best']['net_spread_percent']:.3f}% "
        f"({stats['best']['buy_exchange']}→{stats['best']['sell_exchange']})\n"
        f"Худший: {stats['worst']['symbol']} {stats['worst']['net_spread_percent']:.3f}% "
        f"({stats['worst']['buy_exchange']}→{stats['worst']['sell_exchange']})"
    )
    await update.message.reply_text(text)


SINGLE_INSTANCE_PORT = 47913


def acquire_single_instance_lock():
    # Две копии с одним токеном ловят 409 Conflict от Telegram — не даём стартовать второй.
    # Порт освобождается ОС автоматически, даже если процесс упал.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        logger.error("Бот уже запущен — второй экземпляр не стартует")
        raise SystemExit(0)
    return lock


def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_TOKEN или CHAT_ID — проверь файл .env")

    instance_lock = acquire_single_instance_lock()  # сокет должен жить, пока работает бот
    load_pending_state()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.bot_data["instances"] = init_exchanges()
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("paperstats", paperstats_command))
    # По умолчанию APScheduler молча пропускает запуск, опоздавший больше чем на 1 секунду
    # (сон ПК, нагрузка). Разрешаем запуск с любым опозданием, но пропущенные схлопываем в один.
    job_kwargs = {"misfire_grace_time": None, "coalesce": True}
    jq = application.job_queue
    if ENABLE_ARBITRAGE:
        jq.run_repeating(check_prices_job, interval=POLL_INTERVAL_SECONDS, first=5, job_kwargs=job_kwargs)
    jq.run_repeating(trend_check_job, interval=TREND_CHECK_INTERVAL_SECONDS, first=20, job_kwargs=job_kwargs)
    jq.run_repeating(volatility_check_job, interval=VOLATILITY_CHECK_INTERVAL_SECONDS, first=30, job_kwargs=job_kwargs)
    jq.run_custom(paper_stats_job, job_kwargs={
        "trigger": "cron", "hour": "*/2", "minute": 0, "timezone": PAPER_STATS_TIMEZONE, **job_kwargs,
    })

    mode = "LIVE (кнопка исполняет реальные сделки)" if LIVE_TRADING else "ТЕСТОВЫЙ (кнопка не исполняет по-настоящему)"
    logger.info(f"Бот запущен. Бирж: {len(application.bot_data['instances'])}, режим: {mode}")

    application.run_polling()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logger.exception("Бот упал с необработанной ошибкой")
        raise
