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

## Scheduled pre-announcement flatten alternative

`strategy_scheduled_flatten.py` preserves the version 2 signal and risk logic but
adds the requested announcement protection:

- Tick 73: flatten every option and RTM position; tick 74: rebuild from RTM data.
- Tick 148: flatten every option and RTM position; tick 149: rebuild from RTM data.
- Tick 223: flatten every option and RTM position; tick 224: rebuild from RTM data.
- Ticks 0-9 are observation-only: the bot remains completely flat.
- At tick 10 it estimates volatility from the sample standard deviation of observed
  RTM mid-price log returns and annualizes it using the case clock.
- Thereafter it uses a rolling 30-return market-data window. It does not use the
  20% fallback or the news value for option pricing.
- Scheduled re-entry at ticks 74, 149, and 224 uses the latest RTM realized-volatility
  estimate and is identified in the console with `source=RTM_REALIZED`.
- While flat, it retries the signal every five ticks until an executable edge is
  available; after opening, it locks the target to prevent transaction-cost churn.
- The market-data version uses a $0.05 executable-edge threshold by default.

Run this alternative with:

```bash
python strategy_scheduled_flatten.py
```

The original `strategy.py` remains unchanged and available.

## Algorithmic ETF Arbitrage case

`strategy_etf_arbitrage.py` is a separate strategy for the BULL/BEAR/RITC
case. It does not modify either volatility-case strategy. It trades the
executable relationship

```text
RITC price in USD * USD/CAD = BULL price in CAD + BEAR price in CAD
```

The strategy:

- buys cheap RITC and shorts BULL/BEAR, or shorts rich RITC and buys both
  stocks, only after bid/ask prices, displayed-depth VWAP, and all three market
  fees are included;
- evaluates each fixed-price RITC tender using both immediate ETF liquidation
  and a stock-hedged route, then takes the more profitable valid route;
- trades USD back toward zero after ETF transactions;
- reads the live gross/net limits from `GET /limits`, counts each RITC share
  twice, and keeps a configurable buffer below the reported limits;
- respects the 10,000-share maximum security order and 2,500,000-unit maximum
  currency order; and
- writes tick metrics plus order/tender events to `etf_logs`.

The two ETF converters are not called because the case rules restrict converter
use to manual interaction in the RIT Client.

Run it in PowerShell with:

```powershell
cd $HOME\Documents\rotman_trading_competition-main
$env:RIT_API_KEY="YOUR_KEY"
python .\strategy_etf_arbitrage.py
```

Useful practice-heat settings:

| Variable | Default | Meaning |
|---|---:|---|
| `RIT_ETF_ARB_EDGE_CAD` | `0.15` | Minimum net ETF-basket edge per share |
| `RIT_ETF_TENDER_EDGE_CAD` | `0.10` | Minimum net tender edge per share |
| `RIT_ETF_ARB_CLIP` | `1000` | Maximum units in one three-leg opportunity |
| `RIT_ETF_MAX_INVENTORY` | `10000` | Local inventory cap per traded security |
| `RIT_ETF_LIMIT_BUFFER` | `0.90` | Fraction of live gross/net limits available |
| `RIT_ETF_POLL_SECONDS` | `0.25` | Delay between loops |
| `RIT_ETF_LOG_DIR` | `etf_logs` | Output directory for CSV history |

The defaults prioritize executable profit and limit safety. They cannot
guarantee positive or maximum P&L because tender frequency, spreads, volatility,
liquidity, order sequencing, and competing algorithms change between heats.

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
