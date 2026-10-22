# Crypto Paper Trader

A small, exchange-independent Python project for learning how a trading bot is structured. It reads historical crypto candles from a CSV file and simulates a long-only moving-average strategy. The included candles are synthetic examples. It does not connect to an exchange, request credentials, or place real orders.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Run the included example

From this folder:

```powershell
python -m paperbot --prices sample_prices.csv --output-dir run-output
```

The program prints a summary and writes `trades.csv` and `equity_curve.csv` into `run-output/`.

## Use your own candles

Create a CSV with these columns, in this order or any order:

```csv
timestamp,open,high,low,close
2025-01-01T00:00:00Z,100,103,99,102
2025-01-02T00:00:00Z,102,104,100,101
```

Prices must be positive, timestamps must be ISO 8601 and strictly increasing, and each candle's high/low must contain its open and close. Use one symbol and one candle interval per file. Volume is not used.

```powershell
python -m paperbot --prices path\to\your_candles.csv --output-dir run-output --fast 5 --slow 20 --starting-cash 10000
```

Risk controls are configurable too. For example, add `--max-position-fraction 0.10 --stop-loss-fraction 0.03 --max-daily-loss-fraction 0.03 --max-drawdown-fraction 0.10` to set the limits explicitly. Fractions use decimal form, so `0.03` means 3%.

## Simulation behavior and controls

- The sample strategy buys when the fast simple moving average crosses above the slow average and exits when it crosses below.
- A signal calculated at a candle close is filled at the next candle's open. Slippage is applied against the simulated trader and fees are charged on both sides.
- The bot is long-only, holds at most one position, and does not use margin or short selling.
- It limits position size to 10% of current equity, uses a 3% stop loss, blocks new entries after a 3% loss from the start of that UTC day, and blocks new entries after a 10% drawdown from the equity peak. These are example settings, not guarantees or personalized risk advice.
- The daily-loss and drawdown limits prevent new entries; they do not force-close an open position. The stop loss remains active. Any open position is marked to market and closed at the final candle for the report.
- Risk limits, fees, slippage, moving-average windows, and starting cash can be changed with command-line options.

This is an educational simulator. Historical simulation can be misleading and does not predict future results. Review the code and the assumptions before using any strategy beyond paper trading.

## Binance Spot bot

The optional live runner connects to Binance Spot through CCXT. It is long-only, uses the same moving-average crossover, and sends capped immediate-or-cancel limit orders. Futures, margin, leverage, and withdrawals are not used.

Install the additional dependency:

```powershell
python -m pip install -r requirements-live.txt
```

Start with public-data preview. It uses no API keys and never submits orders:

```powershell
python -m paperbot.live --mode preview --symbol BTC/USDT --timeframe 1h
```

To rehearse authenticated order handling, create Binance Spot Testnet keys and set them in the current PowerShell session:

```powershell
$env:BINANCE_TESTNET_API_KEY = "your-testnet-key"
$env:BINANCE_TESTNET_SECRET = "your-testnet-secret"
python -m paperbot.live --mode testnet --symbol BTC/USDT --timeframe 1h
```

The testnet keys are separate from production keys. For production, set `BINANCE_API_KEY` and `BINANCE_SECRET` locally, then explicitly provide the pair and capital limits. Example command (replace the sample amounts with limits you chose):

```powershell
python -m paperbot.live --mode live --confirm-live --symbol BTC/USDT --capital-limit 500 --max-quote-per-trade 25 --timeframe 1h
```

Never paste keys into chat or commit them to files. Use an API key restricted to the permissions needed for Spot trading and account reads, with withdrawals disabled. Binance documents separate trading and user-data permissions, and CCXT documents that sandbox mode must be selected before other exchange calls. See [Binance Spot API security](https://developers.binance.com/en/docs/products/spot/rest-api) and the [CCXT sandbox and order manual](https://github.com/ccxt/ccxt/wiki/Manual).

Preview and testnet default to a 1,000 quote-currency virtual capital limit and 100 quote-currency per-trade cap. Live mode requires you to state both limits and the symbol explicitly. Position sizing defaults to 10%, maximum slippage to 0.2%, the stop to 3%, the daily loss limit to 3%, and the drawdown halt to 10%. These are example controls, not personalized settings.

The stop is monitored by this running process; Binance does not hold a protective stop order. If the program, computer, or network goes offline, the stop monitor is offline too, and an open position remains on the exchange. Daily-loss and drawdown limits stop new entries but do not close a position. The local state file tracks only this bot's virtual allocation. Use a dedicated account or keep other trading activity out of the same pair, and reconcile Binance order history if the program reports an unresolved order.
