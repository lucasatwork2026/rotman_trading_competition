"""RIT volatility strategy with scheduled pre-news flattening.

This is an alternative to strategy.py. It closes all RTM option and ETF
positions immediately before the scheduled volatility announcements, then
waits for a genuinely new news item before rebuilding the ATM straddle.
"""

from __future__ import annotations

import time
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
    news_signature,
    portfolio_delta,
    remaining_years,
    submit_toward,
    volatility_from_news,
)


# Flatten one tick before the scheduled new-volatility announcements.
FLATTEN_TO_REENTRY = {73: 74, 148: 149, 223: 224}


def flatten_tick_due(tick: int, completed: set[int]) -> tuple[int, int] | None:
    """Return the flatten/re-entry pair due now, allowing a one-tick catch-up."""
    for flatten_tick, reentry_tick in FLATTEN_TO_REENTRY.items():
        if flatten_tick not in completed and flatten_tick <= tick <= reentry_tick:
            return flatten_tick, reentry_tick
    return None


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
    settings = Settings()
    client = RITClient(settings)

    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
    last_news: tuple[str, ...] | None = None
    completed_flats: set[int] = set()
    awaiting_news_after: int | None = None
    last_period: int | None = None
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
                last_news = None
                completed_flats.clear()
                awaiting_news_after = None
                recorder.start_round(period)
                print(f"PERIOD {period}: schedule state reset")

            tick = int(case.get("tick", 0))
            total_ticks = int(case.get("ticks_per_period") or settings.default_total_ticks)
            years = remaining_years(tick, total_ticks)
            securities = as_map(client.securities())
            if "RTM" not in securities:
                raise RuntimeError("RTM was not returned by GET /securities")

            spot = mid(securities["RTM"])
            news_items = client.news()
            signature = news_signature(news_items)
            sigma = volatility_from_news(news_items)
            positions = {
                ticker: int(sec.get("position", 0))
                for ticker, sec in securities.items()
            }

            just_flattened = False
            due = flatten_tick_due(tick, completed_flats)
            if due:
                flatten_tick, reentry_tick = due
                completed_flats.add(flatten_tick)
                awaiting_news_after = reentry_tick
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                # Snapshot existing news so it cannot trigger a stale re-entry.
                last_news = signature
                just_flattened = True
                print(
                    f"PRE-NEWS FLATTEN at tick={tick}; waiting for new news "
                    f"at or after tick={reentry_tick}"
                )

            if not just_flattened:
                new_information = bool(signature) and signature != last_news
                if awaiting_news_after is not None:
                    if tick >= awaiting_news_after and new_information:
                        option_targets = signal_targets(
                            securities,
                            spot,
                            years,
                            sigma,
                            settings,
                            tick,
                            total_ticks,
                        )
                        last_news = signature
                        awaiting_news_after = None
                    elif tick >= awaiting_news_after:
                        print(
                            f"WAITING tick={tick}: scheduled news has not appeared yet"
                        )
                elif new_information:
                    option_targets = signal_targets(
                        securities,
                        spot,
                        years,
                        sigma,
                        settings,
                        tick,
                        total_ticks,
                    )
                    last_news = signature

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
            delta = portfolio_delta(
                securities, spot, years, sigma, settings.risk_free_rate
            )
            current_stock = positions.get("RTM", 0)

            if awaiting_news_after is not None or just_flattened:
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
                f"waiting_for_news_{awaiting_news_after}"
                if awaiting_news_after is not None
                else "active"
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
                f"forecast_vol={sigma:.1%} delta={delta:,.0f}"
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
