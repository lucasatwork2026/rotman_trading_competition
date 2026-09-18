"""Conservative RIT volatility-case trading bot.

Run the RIT Client first, enable the REST API, then execute this file.
The bot trades the most attractive near-the-money straddle and delta-hedges
with RTM while maintaining buffers below every stated case limit.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import time
from dataclasses import dataclass
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
    poll_seconds: float = float(os.getenv("RIT_POLL_SECONDS", "0.35"))
    edge_threshold: float = float(os.getenv("RIT_EDGE_THRESHOLD", "0.06"))
    exit_threshold: float = float(os.getenv("RIT_EXIT_THRESHOLD", "0.025"))
    target_contracts: int = int(os.getenv("RIT_TARGET_CONTRACTS", "200"))
    option_order_size: int = int(os.getenv("RIT_OPTION_ORDER_SIZE", "50"))
    hedge_trigger: int = int(os.getenv("RIT_HEDGE_TRIGGER", "1000"))
    stop_opening_ticks: int = int(os.getenv("RIT_STOP_OPENING_TICKS", "12"))
    default_total_ticks: int = int(os.getenv("RIT_TOTAL_TICKS", "600"))

    # Buffers below official limits: ETF 50,000; options 2,500 gross/1,000 net.
    etf_position_cap: int = 48_000
    option_gross_cap: int = 2_400
    option_net_cap: int = 900
    etf_max_order: int = 10_000
    option_max_order: int = 100
    contract_multiplier: int = 100


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


def news_text(items: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(items, key=lambda x: int(x.get("news_id", 0)))
    return "\n".join(
        " ".join(str(item.get(k, "")) for k in ("headline", "body")) for item in ordered
    )


def volatility_from_news(items: Iterable[dict[str, Any]], fallback: float = 0.20) -> float:
    """Use the newest exact weekly forecast; otherwise the newest range midpoint."""
    text = news_text(items)
    exact = re.findall(
        r"(?:this|current)\s+week[^%]{0,100}?(\d+(?:\.\d+)?)\s*%", text, re.I
    )
    if exact:
        return float(exact[-1]) / 100.0
    ranges = re.findall(
        r"next\s+week[^%]{0,100}?(\d+(?:\.\d+)?)\s*(?:-|to|and)\s*"
        r"(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    )
    if ranges:
        lo, hi = map(float, ranges[-1])
        return (lo + hi) / 200.0
    return fallback


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
    client.market_order(ticker, "BUY" if signed > 0 else "SELL", abs(signed))
    positions[ticker] = current + signed
    print(f"ORDER {ticker} {'BUY' if signed > 0 else 'SELL'} {abs(signed)}")
    return signed


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
    candidates: list[tuple[float, int, str]] = []
    for strike in range(48, 53):
        call = securities.get(f"RTM{strike}C")
        put = securities.get(f"RTM{strike}P")
        if not call or not put:
            continue
        fair_call, _ = black_scholes(spot, strike, years, rate, sigma, "C")
        fair_put, _ = black_scholes(spot, strike, years, rate, sigma, "P")
        buy_edge = fair_call + fair_put - float(call["ask"]) - float(put["ask"])
        sell_edge = float(call["bid"]) + float(put["bid"]) - fair_call - fair_put
        # Prefer near-ATM contracts when edges are similar; their straddle delta is smallest.
        distance_penalty = 0.002 * abs(strike - spot)
        candidates.append((buy_edge - distance_penalty, strike, "BUY"))
        candidates.append((sell_edge - distance_penalty, strike, "SELL"))
    if not candidates:
        return None
    edge, strike, direction = max(candidates)
    return strike, edge, direction


def run() -> None:
    settings = Settings()
    client = RITClient(settings)
    running = True

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("Connected strategy started. Press Ctrl+C for a clean stop.")

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
            sigma = volatility_from_news(client.news())
            positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            choice = choose_straddle(securities, spot, years, sigma, settings.risk_free_rate)
            if choice:
                strike, edge, direction = choice
                active_tickers = {f"RTM{strike}C", f"RTM{strike}P"}
                opening_allowed = tick < total_ticks - settings.stop_opening_ticks
                desired = settings.target_contracts * (1 if direction == "BUY" else -1)
                if not opening_allowed or edge < settings.edge_threshold:
                    desired = 0
                for ticker in OPTION_TICKERS:
                    current = positions.get(ticker, 0)
                    if ticker not in active_tickers:
                        target = 0
                    else:
                        # A smaller exit threshold avoids churning around the entry threshold.
                        target = desired if desired or edge < settings.exit_threshold else current
                    submit_toward(client, ticker, current, target, positions, settings)

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
                    client, "RTM", current_stock, target_stock, positions, settings
                )
            print(
                f"tick={tick:>3}/{total_ticks} spot={spot:.2f} "
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
    run()
