"""Aggressive RIT volatility-case trading bot.

Run the RIT Client first, enable the REST API, then execute this file.
This is a higher-risk variant of strategy.py. It uses the same news-driven
ATM-straddle logic with larger targets, a lower entry threshold, faster orders,
and tighter delta hedging while maintaining buffers below the stated limits.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


N = NormalDist()
OPTION_TICKERS = tuple(
    f"RTM{strike}{kind}" for strike in range(48, 53) for kind in ("C", "P")
)


@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("RIT_BASE_URL", "http://localhost:9999/v1")
    api_key: str = os.getenv("RIT_API_KEY", "")
    risk_free_rate: float = float(os.getenv("RIT_RISK_FREE_RATE", "0.0"))
    poll_seconds: float = float(os.getenv("RIT_POLL_SECONDS", "0.25"))
    edge_threshold: float = float(os.getenv("RIT_EDGE_THRESHOLD", "0.04"))
    target_contracts: int = int(os.getenv("RIT_TARGET_CONTRACTS", "350"))
    option_order_size: int = int(os.getenv("RIT_OPTION_ORDER_SIZE", "100"))
    hedge_trigger: int = int(os.getenv("RIT_HEDGE_TRIGGER", "1500"))
    stop_opening_ticks: int = int(os.getenv("RIT_STOP_OPENING_TICKS", "8"))
    default_total_ticks: int = int(os.getenv("RIT_TOTAL_TICKS", "600"))
    option_fee_per_contract: float = float(os.getenv("RIT_OPTION_FEE", "2.00"))
    etf_fee_per_share: float = float(os.getenv("RIT_ETF_FEE", "0.02"))
    transaction_log: str = os.getenv(
        "RIT_TRANSACTION_LOG", "strategy_aggressive_transactions.csv"
    )

    # Buffers below official limits: ETF 50,000; options 2,500 gross/1,000 net.
    etf_position_cap: int = 48_000
    option_gross_cap: int = 2_400
    option_net_cap: int = 900
    etf_max_order: int = 10_000
    option_max_order: int = 100
    contract_multiplier: int = 100


class TransactionCosts:
    """Track estimated fees for fills submitted by this process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.option_contracts = 0
        self.etf_shares = 0
        self.cumulative_fees = 0.0
        self._ensure_header()

    def _ensure_header(self) -> None:
        if os.path.exists(self.settings.transaction_log):
            return
        try:
            with open(self.settings.transaction_log, "w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(
                    [
                        "timestamp_utc",
                        "tick",
                        "ticker",
                        "action",
                        "filled_quantity",
                        "estimated_fee",
                        "cumulative_fee",
                    ]
                )
        except OSError as exc:
            print(f"FEE LOG warning: could not create log file: {exc}")

    def record(
        self, tick: int, ticker: str, action: str, filled_quantity: int
    ) -> float:
        if filled_quantity <= 0:
            return 0.0
        if ticker == "RTM":
            self.etf_shares += filled_quantity
            fee = filled_quantity * self.settings.etf_fee_per_share
        else:
            self.option_contracts += filled_quantity
            fee = filled_quantity * self.settings.option_fee_per_contract
        self.cumulative_fees += fee
        try:
            with open(
                self.settings.transaction_log, "a", newline="", encoding="utf-8"
            ) as file:
                csv.writer(file).writerow(
                    [
                        datetime.now(timezone.utc).isoformat(),
                        tick,
                        ticker,
                        action,
                        filled_quantity,
                        f"{fee:.2f}",
                        f"{self.cumulative_fees:.2f}",
                    ]
                )
        except OSError as exc:
            print(f"FEE LOG warning: could not append transaction: {exc}")
        return fee

    def summary(self) -> str:
        return (
            f"fees=${self.cumulative_fees:,.2f} "
            f"(options={self.option_contracts:,} contracts, "
            f"ETF={self.etf_shares:,} shares)"
        )


class RITClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.base_url.rstrip("/")
        self.headers = {"X-API-Key": settings.api_key} if settings.api_key else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}/{path.lstrip('/')}{query}"
        for attempt in range(4):
            request = Request(url, method=method, headers=self.headers)
            try:
                with urlopen(request, timeout=2.0) as response:
                    content = response.read()
                    return json.loads(content) if content else None
            except HTTPError as exc:
                if exc.code != 429:
                    raise
                delay = float(exc.headers.get("Retry-After", 0.25 * (attempt + 1)))
                time.sleep(delay)
        raise RuntimeError("RIT API rate limit persisted after four retries")

    def case(self) -> dict[str, Any]:
        return self._request("GET", "case")

    def securities(self) -> list[dict[str, Any]]:
        return self._request("GET", "securities")

    def news(self) -> list[dict[str, Any]]:
        return self._request("GET", "news", params={"limit": 50})

    def market_order(self, ticker: str, action: str, quantity: int) -> Any:
        if quantity <= 0:
            return None
        return self._request(
            "POST",
            "orders",
            params={
                "ticker": ticker,
                "type": "MARKET",
                "quantity": int(quantity),
                "action": action.upper(),
            },
        )


def norm_cdf(x: float) -> float:
    return N.cdf(x)


def black_scholes(
    spot: float, strike: float, years: float, rate: float, sigma: float, kind: str
) -> tuple[float, float]:
    """Return option value per share and delta for a European call or put."""
    if years <= 0:
        call_value = max(spot - strike, 0.0)
        call_delta = 1.0 if spot > strike else (0.5 if spot == strike else 0.0)
    else:
        sigma = max(sigma, 1e-6)
        root_t = math.sqrt(years)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * years) / (
            sigma * root_t
        )
        d2 = d1 - sigma * root_t
        call_value = spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
        call_delta = norm_cdf(d1)
    if kind == "C":
        return call_value, call_delta
    return call_value - spot + strike * math.exp(-rate * max(years, 0.0)), call_delta - 1.0


def parse_option(ticker: str) -> tuple[float, str]:
    match = re.fullmatch(r"RTM(\d+(?:\.\d+)?)([CP])", ticker.upper())
    if not match:
        raise ValueError(f"Unrecognized option ticker: {ticker}")
    return float(match.group(1)), match.group(2)


def volatility_from_news(items: Iterable[dict[str, Any]], fallback: float = 0.20) -> float:
    """Use the newest exact forecast or range midpoint, whichever arrived last."""
    forecast = fallback
    ordered = sorted(items, key=lambda x: int(x.get("news_id", 0)))
    for item in ordered:
        text = " ".join(str(item.get(k, "")) for k in ("headline", "body"))
        exact = re.search(
            r"(?:this|current)\s+week[^%]{0,100}?(\d+(?:\.\d+)?)\s*%",
            text,
            re.I,
        )
        ranges = re.search(
            r"next\s+week[^%]{0,100}?(\d+(?:\.\d+)?)\s*(?:-|to|and)\s*"
            r"(\d+(?:\.\d+)?)\s*%",
            text,
            re.I,
        )
        if exact:
            forecast = float(exact.group(1)) / 100.0
        elif ranges:
            lo, hi = float(ranges.group(1)), float(ranges.group(2))
            forecast = (lo + hi) / 200.0
    return forecast


def news_signature(items: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """Create a stable identity for the currently available news set."""
    return tuple(
        f"{item.get('news_id', '')}|{item.get('headline', '')}|{item.get('body', '')}"
        for item in sorted(items, key=lambda x: int(x.get("news_id", 0)))
    )


def as_map(securities: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["ticker"]).upper(): item for item in securities}


def mid(sec: dict[str, Any]) -> float:
    bid, ask = float(sec["bid"]), float(sec["ask"])
    return (bid + ask) / 2.0


def remaining_years(tick: int, total_ticks: int) -> float:
    # One case month = 20/240 trading years.
    return max(total_ticks - tick, 0) / max(total_ticks, 1) / 12.0


def risk_ok(
    positions: dict[str, int], ticker: str, signed_quantity: int, settings: Settings
) -> bool:
    projected = dict(positions)
    projected[ticker] = projected.get(ticker, 0) + signed_quantity
    if ticker == "RTM":
        return abs(projected["RTM"]) <= settings.etf_position_cap
    option_positions = [projected.get(t, 0) for t in OPTION_TICKERS]
    return (
        sum(abs(x) for x in option_positions) <= settings.option_gross_cap
        and abs(sum(option_positions)) <= settings.option_net_cap
    )


def submit_toward(
    client: RITClient,
    ticker: str,
    current: int,
    target: int,
    positions: dict[str, int],
    settings: Settings,
    costs: TransactionCosts,
    tick: int,
) -> int:
    difference = target - current
    if difference == 0:
        return 0
    maximum = settings.etf_max_order if ticker == "RTM" else min(
        settings.option_max_order, settings.option_order_size
    )
    signed = int(math.copysign(min(abs(difference), maximum), difference))
    if not risk_ok(positions, ticker, signed, settings):
        print(f"SKIP {ticker}: local position-limit buffer would be exceeded")
        return 0
    action = "BUY" if signed > 0 else "SELL"
    response = client.market_order(ticker, action, abs(signed))
    filled = abs(signed)
    if isinstance(response, dict) and "quantity_filled" in response:
        filled = max(0, int(response["quantity_filled"]))
    actual_signed = filled if signed > 0 else -filled
    positions[ticker] = current + actual_signed
    fee = costs.record(tick, ticker, action, filled)
    print(
        f"ORDER {ticker} {action} requested={abs(signed)} filled={filled} "
        f"fee=${fee:,.2f} cumulative_fees=${costs.cumulative_fees:,.2f}"
    )
    return actual_signed


def portfolio_delta(
    securities: dict[str, dict[str, Any]], spot: float, years: float, sigma: float, rate: float
) -> float:
    delta = float(securities["RTM"].get("position", 0))
    for ticker in OPTION_TICKERS:
        sec = securities.get(ticker)
        if not sec:
            continue
        strike, kind = parse_option(ticker)
        _, option_delta = black_scholes(spot, strike, years, rate, sigma, kind)
        delta += float(sec.get("position", 0)) * 100.0 * option_delta
    return delta


def choose_straddle(
    securities: dict[str, dict[str, Any]], spot: float, years: float, sigma: float, rate: float
) -> tuple[int, float, str] | None:
    """Return strike, executable per-share edge, and BUY/SELL direction."""
    # Lock to one nearest-to-the-money strike. Searching every strike every loop
    # caused the first version to switch contracts and pay unnecessary costs.
    available = [
        strike
        for strike in range(48, 53)
        if f"RTM{strike}C" in securities and f"RTM{strike}P" in securities
    ]
    if not available:
        return None
    strike = min(available, key=lambda value: abs(value - spot))
    call = securities[f"RTM{strike}C"]
    put = securities[f"RTM{strike}P"]
    fair_call, _ = black_scholes(spot, strike, years, rate, sigma, "C")
    fair_put, _ = black_scholes(spot, strike, years, rate, sigma, "P")
    buy_edge = fair_call + fair_put - float(call["ask"]) - float(put["ask"])
    sell_edge = float(call["bid"]) + float(put["bid"]) - fair_call - fair_put
    if buy_edge >= sell_edge:
        return strike, buy_edge, "BUY"
    return strike, sell_edge, "SELL"


def run() -> None:
    settings = Settings()
    client = RITClient(settings)
    running = True
    last_news: tuple[str, ...] | None = None
    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
    costs = TransactionCosts(settings)

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("Connected AGGRESSIVE strategy started. Press Ctrl+C for a clean stop.")

    while running:
        try:
            case = client.case()
            if str(case.get("status", "")).upper() != "ACTIVE":
                time.sleep(0.5)
                continue

            tick = int(case.get("tick", 0))
            total_ticks = int(case.get("ticks_per_period") or settings.default_total_ticks)
            years = remaining_years(tick, total_ticks)
            securities = as_map(client.securities())
            if "RTM" not in securities:
                raise RuntimeError("RTM was not returned by GET /securities")
            spot = mid(securities["RTM"])
            news_items = client.news()
            sigma = volatility_from_news(news_items)
            positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            signature = news_signature(news_items)
            if signature and signature != last_news:
                last_news = signature
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                choice = choose_straddle(
                    securities, spot, years, sigma, settings.risk_free_rate
                )
                opening_allowed = tick < total_ticks - settings.stop_opening_ticks
                if choice and opening_allowed:
                    strike, edge, direction = choice
                    if edge >= settings.edge_threshold:
                        desired = settings.target_contracts * (
                            1 if direction == "BUY" else -1
                        )
                        option_targets[f"RTM{strike}C"] = desired
                        option_targets[f"RTM{strike}P"] = desired
                        print(
                            f"NEW SIGNAL news={len(signature)} strike={strike} "
                            f"side={direction} edge={edge:.3f} target={desired}"
                        )
                    else:
                        print(
                            f"NO TRADE news={len(signature)}: best ATM edge "
                            f"{edge:.3f} is below {settings.edge_threshold:.3f}"
                        )

            # Work toward the fixed target but never recalculate it between news
            # releases. This prevents quote noise from triggering round trips.
            for ticker in OPTION_TICKERS:
                submit_toward(
                    client,
                    ticker,
                    positions.get(ticker, 0),
                    option_targets[ticker],
                    positions,
                    settings,
                    costs,
                    tick,
                )

            # Re-read positions after option fills, then hedge the full portfolio.
            securities = as_map(client.securities())
            positions = {t: int(s.get("position", 0)) for t, s in securities.items()}
            spot = mid(securities["RTM"])
            delta = portfolio_delta(
                securities, spot, years, sigma, settings.risk_free_rate
            )
            if abs(delta) >= settings.hedge_trigger:
                current_stock = positions.get("RTM", 0)
                target_stock = max(
                    -settings.etf_position_cap,
                    min(settings.etf_position_cap, round(current_stock - delta)),
                )
                submit_toward(
                    client,
                    "RTM",
                    current_stock,
                    target_stock,
                    positions,
                    settings,
                    costs,
                    tick,
                )
            print(
                f"tick={tick:>3}/{total_ticks} spot={spot:.2f} "
                f"forecast_vol={sigma:.1%} delta={delta:,.0f} "
                f"{costs.summary()}"
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"API error: {exc}")
            time.sleep(1.0)
        except Exception as exc:
            print(f"Strategy error: {exc}")
            time.sleep(1.0)
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    run()
