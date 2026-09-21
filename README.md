# Crypto Trading Bot — Arbitrage Scanner & Trend-Following Strategy

A Python trading bot for spot crypto markets, combining a cross-exchange arbitrage
scanner with a backtested trend-following strategy, both wired into Telegram for
alerts and (optional, manually-confirmed) trade execution.

## Why this project is interesting

This isn't a "plug and play" bot — it's built around honest, iterative validation
against historical data, including reverting things that looked good in theory but
didn't hold up:

- Started with cross-exchange arbitrage. Backtesting revealed the early "profitable"
  signals were an artifact of using last-trade price on illiquid pairs instead of
  real bid/ask — fixed by switching the spread calculation to bid/ask and adding a
  minimum 24h volume filter.
- Tested several refinements — fixed-percent stop-loss, a take-profit target,
  symmetric long/short — against 3 years of historical data. Most of them *hurt*
  performance and were reverted based on the numbers, not intuition.
- Landed on an ATR-based stop-loss (scales with each asset's own volatility instead
  of a flat percentage), validated per-symbol with a Calmar-ratio screen across a
  13-coin candidate universe before selecting the final trading set.

## Features

- **`main.py`** — the live bot: Telegram integration (`python-telegram-bot`),
  `APScheduler` polling jobs, a paper-trading portfolio tracker with persistent
  JSON state, manual buy/sell confirmation via inline-keyboard buttons. Trade
  execution is gated behind a single `LIVE_TRADING` flag (defaults to `False`) —
  no unattended real-money execution.
- **`backtest.py`** — a backtesting engine built from scratch on `pandas`: OHLCV
  pagination via `ccxt`, MA-crossover + ATR-stop simulation, take-profit / short /
  mean-reversion variants, and Calmar-ratio-based screening across a coin universe.

## Stack

Python · [ccxt](https://github.com/ccxt/ccxt) · python-telegram-bot · pandas · APScheduler

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your own exchange/Telegram API keys
python main.py
```

## Safety design

- `LIVE_TRADING` defaults to `False` — every execution path is a dry-run until
  explicitly enabled.
- Every trade signal (arbitrage or trend) requires a manual tap on a Telegram
  button to execute for real — the bot never moves money on its own.
- A parallel paper-trading portfolio tracks what the strategy *would* have done,
  so performance can be validated against live data before committing real capital.

## Status

Actively paper-trading on BTC/ETH/BNB/TRX (Bybit) to validate the backtested
strategy against real-time data before any real capital is committed.
