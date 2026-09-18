"""RIT volatility strategy with scheduled pre-announcement flattening.

This is an alternative to strategy.py. It closes all RTM option and ETF
positions immediately before the scheduled volatility announcements, then
rebuilds the ATM straddle from rolling RTM realized volatility.
"""

from __future__ import annotations

import math
import os
import statistics
import time
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError

from round_recorder import RoundRecorder
from strategy import (
    OPTION_TICKERS,
    RITClient,
    Settings,
    as_map,
    choose_straddle,
    mid,
    portfolio_delta,
    remaining_years,
    submit_toward,
)


# Flatten one tick before the scheduled new-volatility announcements.
FLATTEN_TO_REENTRY = {73: 74, 148: 149, 223: 224}
WARMUP_END_TICK = 10
RV_WINDOW = int(os.getenv("RIT_RV_WINDOW", "30"))
SIGNAL_RETRY_TICKS = int(os.getenv("RIT_SIGNAL_RETRY_TICKS", "5"))
MARKET_EDGE_THRESHOLD = float(os.getenv("RIT_MARKET_EDGE_THRESHOLD", "0.05"))


def flatten_tick_due(tick: int, completed: set[int]) -> tuple[int, int] | None:
    """Return the flatten/re-entry pair due now, allowing a one-tick catch-up."""
    for flatten_tick, reentry_tick in FLATTEN_TO_REENTRY.items():
        if flatten_tick not in completed and flatten_tick <= tick <= reentry_tick:
            return flatten_tick, reentry_tick
    return None


def realized_volatility_from_prices(
    prices: list[float], total_ticks: int, window: int = RV_WINDOW
) -> float:
    """Annualized sample volatility of rolling RTM mid-price log returns."""
    selected = prices[-(window + 1) :]
    if len(selected) < 3:
        return float("nan")
    returns = [
        math.log(current / previous)
        for previous, current in zip(selected, selected[1:])
        if previous > 0 and current > 0
    ]
    if len(returns) < 2:
        return float("nan")
    # The full case represents 20/240 = 1/12 trading years.
    ticks_per_year = total_ticks * 12
    return statistics.stdev(returns) * math.sqrt(ticks_per_year)


def signal_targets(
    securities: dict[str, dict[str, Any]],
    spot: float,
    years: float,
    sigma: float,
    settings: Settings,
    tick: int,
    total_ticks: int,
) -> dict[str, int]:
    """Build a fresh target from the new analyst information."""
    targets = {ticker: 0 for ticker in OPTION_TICKERS}
    if tick >= total_ticks - settings.stop_opening_ticks:
        print("NO TRADE: too close to expiry to open a new position")
        return targets

    choice = choose_straddle(
        securities, spot, years, sigma, settings.risk_free_rate
    )
    if not choice:
        print("NO TRADE: no complete RTM call/put pair was available")
        return targets

    strike, edge, direction = choice
    if edge < settings.edge_threshold:
        print(
            f"NO TRADE: best ATM edge {edge:.3f} is below "
            f"{settings.edge_threshold:.3f}"
        )
        return targets

    desired = settings.target_contracts * (1 if direction == "BUY" else -1)
    targets[f"RTM{strike}C"] = desired
    targets[f"RTM{strike}P"] = desired
    print(
        f"NEW SIGNAL strike={strike} side={direction} edge={edge:.3f} "
        f"target={desired}"
    )
    return targets


def run() -> None:
    settings = replace(Settings(), edge_threshold=MARKET_EDGE_THRESHOLD)
    client = RITClient(settings)

    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
    completed_flats: set[int] = set()
    scheduled_reentry_tick: int | None = None
    last_period: int | None = None
    observed_prices: list[float] = []
    last_observed_tick: int | None = None
    initial_signal_created = False
    next_signal_tick = WARMUP_END_TICK
    recorder = RoundRecorder()
    was_active = False

    print(
        "Scheduled-flatten strategy started. "
        "Pre-news flatten ticks: 73, 148, 223. Press Ctrl+C to stop."
    )

    while True:
        try:
            case = client.case()
            if str(case.get("status", "")).upper() != "ACTIVE":
                if was_active:
                    recorder.finish_round(str(case.get("status", "round_ended")))
                    was_active = False
                time.sleep(0.5)
                continue
            was_active = True

            period = int(case.get("period", 1))
            if period != last_period:
                last_period = period
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                completed_flats.clear()
                scheduled_reentry_tick = None
                observed_prices = []
                last_observed_tick = None
                initial_signal_created = False
                next_signal_tick = WARMUP_END_TICK
                recorder.start_round(period)
                print(f"PERIOD {period}: schedule state reset")

            tick = int(case.get("tick", 0))
            total_ticks = int(case.get("ticks_per_period") or settings.default_total_ticks)
            years = remaining_years(tick, total_ticks)
            securities = as_map(client.securities())
            if "RTM" not in securities:
                raise RuntimeError("RTM was not returned by GET /securities")

            spot = mid(securities["RTM"])
            if tick != last_observed_tick:
                observed_prices.append(spot)
                last_observed_tick = tick
            sigma = realized_volatility_from_prices(observed_prices, total_ticks)
            positions = {
                ticker: int(sec.get("position", 0))
                for ticker, sec in securities.items()
            }

            just_flattened = False
            due = flatten_tick_due(tick, completed_flats)
            if due:
                flatten_tick, reentry_tick = due
                completed_flats.add(flatten_tick)
                scheduled_reentry_tick = reentry_tick
                initial_signal_created = False
                next_signal_tick = reentry_tick
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                just_flattened = True
                print(
                    f"PRE-NEWS FLATTEN at tick={tick}; scheduled market-volatility "
                    f"re-entry at tick={reentry_tick}"
                )

            if not just_flattened:
                if tick < WARMUP_END_TICK:
                    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                elif not math.isfinite(sigma):
                    print(f"WAITING tick={tick}: insufficient RTM return history")
                elif scheduled_reentry_tick is not None:
                    if tick >= scheduled_reentry_tick and tick >= next_signal_tick:
                        option_targets = signal_targets(
                            securities,
                            spot,
                            years,
                            sigma,
                            settings,
                            tick,
                            total_ticks,
                        )
                        if any(option_targets.values()):
                            scheduled_reentry_tick = None
                            initial_signal_created = True
                        else:
                            next_signal_tick = tick + SIGNAL_RETRY_TICKS
                            print(
                                f"RETRY scheduled for tick={next_signal_tick}"
                            )
                elif not initial_signal_created and tick >= next_signal_tick:
                    option_targets = signal_targets(
                        securities,
                        spot,
                        years,
                        sigma,
                        settings,
                        tick,
                        total_ticks,
                    )
                    if any(option_targets.values()):
                        initial_signal_created = True
                    else:
                        next_signal_tick = tick + SIGNAL_RETRY_TICKS
                        print(f"RETRY scheduled for tick={next_signal_tick}")

            # Flattening a 75-contract leg takes at most two child orders.
            for ticker in OPTION_TICKERS:
                signed = submit_toward(
                    client,
                    ticker,
                    positions.get(ticker, 0),
                    option_targets[ticker],
                    positions,
                    settings,
                )
                recorder.record_trade(
                    period, tick, ticker, signed, "option_target"
                )

            securities = as_map(client.securities())
            positions = {
                ticker: int(sec.get("position", 0))
                for ticker, sec in securities.items()
            }
            spot = mid(securities["RTM"])
            delta = (
                portfolio_delta(
                    securities, spot, years, sigma, settings.risk_free_rate
                )
                if math.isfinite(sigma)
                else 0.0
            )
            current_stock = positions.get("RTM", 0)

            warming_up = tick < WARMUP_END_TICK or not math.isfinite(sigma)
            if scheduled_reentry_tick is not None or just_flattened or warming_up:
                # The scheduled safety action means completely flat, including RTM.
                signed = submit_toward(
                    client, "RTM", current_stock, 0, positions, settings
                )
                recorder.record_trade(
                    period, tick, "RTM", signed, "scheduled_flatten"
                )
            elif abs(delta) >= settings.hedge_trigger:
                target_stock = max(
                    -settings.etf_position_cap,
                    min(settings.etf_position_cap, round(current_stock - delta)),
                )
                signed = submit_toward(
                    client, "RTM", current_stock, target_stock, positions, settings
                )
                recorder.record_trade(
                    period, tick, "RTM", signed, "delta_hedge"
                )

            state = (
                "warming_up"
                if warming_up
                else (
                    f"waiting_for_reentry_{scheduled_reentry_tick}"
                    if scheduled_reentry_tick is not None
                    else "active"
                )
            )
            recorder.record_snapshot(
                period,
                tick,
                state,
                spot,
                sigma,
                delta,
                securities.values(),
                set(OPTION_TICKERS),
            )
            print(
                f"tick={tick:>3}/{total_ticks} state={state} spot={spot:.2f} "
                f"forecast_vol={sigma:.1%} source=RTM_REALIZED "
                f"samples={len(observed_prices)} delta={delta:,.0f}"
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"API error: {exc}")
            time.sleep(1.0)
        except Exception as exc:
            print(f"Strategy error: {exc}")
            time.sleep(1.0)

        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nScheduled-flatten strategy stopped.")
