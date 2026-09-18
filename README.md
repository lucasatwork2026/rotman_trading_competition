# RITCx Volatility Trading Strategy

A simple, conservative Python bot for the RTM volatility case. It reads analyst
volatility news, values European options with Black-Scholes, trades one
near-the-money straddle only when its executable edge clears transaction costs,
and delta-hedges with RTM. Version 2 locks its target until the next news release
to prevent spread, commission, and hedge churn.

## Rule safeguards

- Rehedges only when portfolio delta reaches 4,500 shares-equivalent, remaining
  inside the official +/-7,000 boundary while avoiding excessive ETF fees.
- Uses an ETF cap of 48,000 versus the official 50,000-share gross/net limit.
- Uses option caps of 2,400 gross and 900 net versus official limits of 2,500 and
  1,000 contracts.
- Targets only 75 contracts per straddle leg and limits child orders to 50
  contracts (official maximum: 100), so one fill
  cannot normally create more than 5,000 shares-equivalent of temporary delta.
- Limits ETF orders to the official 10,000-share maximum.
- Values buys at the ask and sells at the bid; the default edge threshold is
  $0.08 per option share, covering the two option commissions ($0.04 per straddle
  share) plus a safety margin.
- Stops opening new positions near expiry and relies on the case's automatic ETF
  close-out and cash settlement of European options.

## Setup

1. Open and log into the RIT Client.
2. Enable the RIT REST API and note the API key shown by the client.
3. Install Python 3.10+. No third-party packages are required.

4. Set the API key and run the bot.

   PowerShell:

   ```powershell
   $env:RIT_API_KEY="YOUR_KEY"
   python strategy.py
   ```

   Command Prompt:

   ```bat
   set RIT_API_KEY=YOUR_KEY
   python strategy.py
   ```

The default endpoint is `http://localhost:9999/v1`. If the RIT Client uses a
different endpoint, set `RIT_BASE_URL`.

## Conservative tuning

Environment variables allow changes without editing code:

| Variable | Default | Meaning |
|---|---:|---|
| `RIT_EDGE_THRESHOLD` | `0.08` | Minimum executable straddle edge per option share |
| `RIT_TARGET_CONTRACTS` | `75` | Target contracts in each straddle leg |
| `RIT_OPTION_ORDER_SIZE` | `50` | Contracts per child order; never exceeds 100 |
| `RIT_HEDGE_TRIGGER` | `4500` | Delta magnitude that triggers an ETF hedge |
| `RIT_RISK_FREE_RATE` | `0.0` | Annual continuously compounded rate |
| `RIT_TOTAL_TICKS` | `600` | Fallback ticks per case if the API omits it |

Start with the defaults in a practice heat. Confirm the exact option ticker names,
news wording, total ticks, and API port before increasing size. This is a competition
template, not a guarantee of profit.

## Scheduled pre-news flatten alternative

`strategy_scheduled_flatten.py` preserves the version 2 signal and risk logic but
adds the requested announcement protection:

- Tick 73: flatten every option and RTM position; tick 74: rebuild only after new news.
- Tick 148: flatten every option and RTM position; tick 149: rebuild only after new news.
- Tick 223: flatten every option and RTM position; tick 224: rebuild only after new news.
- If the API has not published a new news item yet, the bot remains flat instead of
  trading from the previous volatility forecast.
- At tick 0, the scheduled version requires a parseable volatility announcement;
  it never opens a position using the 20% fallback assumption. Console output
  identifies the accepted value with `source=NEWS`.

Run this alternative with:

```bash
python strategy_scheduled_flatten.py
```

The original `strategy.py` remains unchanged and available.

### Round metrics and P&L files

The scheduled-flatten strategy automatically creates a `rit_logs` folder. It
records each round in four CSV outputs:

- `*_metrics.csv`: one row per tick with realized, unrealized, and total P&L,
  delta, limits, option gross/net exposure, peak P&L, and drawdown.
- `*_positions.csv`: bid, ask, last, VWAP, position, and P&L for every ticker.
- `*_trades.csv`: every order submitted by the strategy and its reason.
- `round_summaries.csv`: one row per completed round with ending P&L, maximum
  drawdown, order count, and ETF/option trading volume.

To save logs elsewhere, set `RIT_LOG_DIR` before starting:

```powershell
$env:RIT_LOG_DIR="$HOME\Documents\RIT_Competition_Logs"
python .\strategy_scheduled_flatten.py
```

## Test without placing orders

The included tests exercise pricing, news parsing, time scaling, and risk guards;
they do not connect to RIT:

```bash
python -m unittest -v
```
