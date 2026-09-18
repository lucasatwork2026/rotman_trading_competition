# RITCx Volatility Trading Strategy

A simple, conservative Python bot for the RTM volatility case. It reads analyst
volatility news, values European options with Black-Scholes, trades the best
near-the-money straddle only when its executable edge clears transaction costs,
and delta-hedges with RTM.

## Rule safeguards

- Targets portfolio delta near zero and starts hedging at 1,000 shares-equivalent,
  well inside the official +/-7,000 penalty boundary.
- Uses an ETF cap of 48,000 versus the official 50,000-share gross/net limit.
- Uses option caps of 2,400 gross and 900 net versus official limits of 2,500 and
  1,000 contracts.
- Limits option child orders to 50 contracts (official maximum: 100) so one fill
  cannot normally create more than 5,000 shares-equivalent of temporary delta.
- Limits ETF orders to the official 10,000-share maximum.
- Values buys at the ask and sells at the bid; the default edge threshold is
  $0.06 per option share, covering the two option commissions ($0.04 per straddle
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
| `RIT_EDGE_THRESHOLD` | `0.06` | Minimum executable straddle edge per option share |
| `RIT_EXIT_THRESHOLD` | `0.025` | Lower threshold used to avoid rapid entry/exit churn |
| `RIT_TARGET_CONTRACTS` | `200` | Target contracts in each straddle leg |
| `RIT_OPTION_ORDER_SIZE` | `50` | Contracts per child order; never exceeds 100 |
| `RIT_HEDGE_TRIGGER` | `1000` | Delta magnitude that triggers an ETF hedge |
| `RIT_RISK_FREE_RATE` | `0.0` | Annual continuously compounded rate |
| `RIT_TOTAL_TICKS` | `600` | Fallback ticks per case if the API omits it |

Start with the defaults in a practice heat. Confirm the exact option ticker names,
news wording, total ticks, and API port before increasing size. This is a competition
template, not a guarantee of profit.

## Test without placing orders

The included tests exercise pricing, news parsing, time scaling, and risk guards;
they do not connect to RIT:

```bash
python -m unittest -v
```
