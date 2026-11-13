# Futures Desk

Day-trade desk for **NSE F&O (India)** and **US stock futures** (the names CoinDCX Global lists).
Toggle the market in the header. Every pick comes from a rule that was backtested, and the
backtest verdict is shown next to each pick.

## Run

```bash
cd futures-desk
python3 -m pip install -r requirements.txt
python3 -m uvicorn app:app --host 127.0.0.1 --port 8877
# open http://127.0.0.1:8877
```

First start downloads about 2 years of data (NSE daily F&O bhavcopy, US daily bars) and runs
the backtests. This takes a few minutes. Data is cached in `data/`. After that the app
refreshes itself every 20 minutes.

## What it does

- **Picks**: today's setups, graded A to C. Each shows entry, stop, target, position size for your
  risk per trade, and the out-of-sample backtest stats of the rule behind it.
  A setup from a rule that loses money out-of-sample is hidden (a count is shown).
- **Live ORB board**: opening-range breakout tracker for the highest relative-volume names.
- **Scanner**: the whole universe with technicals, TradingView rating, change and RVOL.
- **Backtests**: every rule, in-sample vs out-of-sample, with verdict. Also the ORB forward test.
- **Detail view**: daily candles (EMA/SMA, Bollinger, Supertrend, signals, trade levels, pivots,
  Volume/OI/RSI), intraday 5m with the opening range, TradingView live chart, fundamentals, news.

## How picks are validated

- Rules are fixed in advance (`desk/strategies.py`), not tuned to the data.
- Pessimistic fills: if stop and target hit in the same bar the stop wins. A gap through the stop
  exits at the open. Costs: India 0.08%, US 0.10% round trip.
- Results are in R-multiples. The first 60% of dates are in-sample, the last 40% are out-of-sample.
- Verdicts: VALIDATED needs OOS expectancy >= 0.05R, PF >= 1.2, day-clustered t >= 1.5 and a
  profitable in-sample. MARGINAL and REJECTED follow lower or failing thresholds.

## Honest findings (2025-03 to 2026-11)

- **US**: only the RSI(2) deep washout long is VALIDATED (OOS 132 trades, +0.11R, PF 1.9, t 1.6).
  The edge is thin and was found among many rules, so some of it is likely luck.
- **India**: no rule is validated. The best (Gap & Go, +0.07R) is not reliable after costs.
- ORB cannot be backtested on free data (no intraday history). The desk records each completed
  session's 5-minute bars in `data/intraday/` and forward-tests it. It needs about 60 sessions.

No strategy here guarantees profit. Size small and always use the stop.
This is not investment advice.

## Data sources (all free)

NSE bhavcopy and intraday, Nasdaq charts, TradingView scanner (snapshot, ratings, fundamentals),
Google News RSS.
