"""Single-option RIT volatility-case trading bot.

Run the RIT Client first, enable the REST API, then execute this file.
The bot stays inactive before tick 74.  On each new volatility-news release,
it selects the single option with the largest executable Black-Scholes pricing
gap, targets two-thirds of the option net limit (or the largest position that
can be fully delta-hedged within the RTM limit), and re-hedges with RTM whenever
portfolio delta reaches +/-5,000. Fully close options and RTM one tick before
Announcements at 74, 149, and 224; News entries do not schedule close-outs.
Orders are submitted once and assumed fully filled. No order-status polling.
Run this bot alone: manual or other-bot trades are not added to its ledger.
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
    api_key: str = os.getenv("RIT_API_KEY", "WQ2T4420")
    risk_free_rate: float = float(os.getenv("RIT_RISK_FREE_RATE", "0.0"))
    poll_seconds: float = float(os.getenv("RIT_POLL_SECONDS", "0.50"))
    edge_threshold: float = float(os.getenv("RIT_EDGE_THRESHOLD", "0.08"))
    option_order_size: int = int(os.getenv("RIT_OPTION_ORDER_SIZE", "50"))
    first_trading_tick: int = int(os.getenv("RIT_FIRST_TRADING_TICK", "74"))
    hedge_trigger: int = int(os.getenv("RIT_HEDGE_TRIGGER", "500"))
    stop_opening_ticks: int = int(os.getenv("RIT_STOP_OPENING_TICKS", "12"))
    default_total_ticks: int = int(os.getenv("RIT_TOTAL_TICKS", "600"))
    option_limit_fraction: float = float(
        os.getenv("RIT_OPTION_LIMIT_FRACTION", str(2.0 / 3.0))
    )

    announcement_ticks: tuple[int, ...] = (74, 149, 224)

    option_fee_per_contract: float = 2.00
    etf_fee_per_share: float = 0.02

    # Official case limits.
    etf_position_cap: int = 50_000
    option_gross_cap: int = 2_500
    option_net_cap: int = 1_000
    etf_max_order: int = 10_000
    option_max_order: int = 100
    contract_multiplier: int = 100


@dataclass
class TransactionCosts:
    """Estimated commissions on this bot's assumed full fills in the current run.

    Orders are assumed fully filled after one submission; no fill polling.
    Both entry and exit count as traded volume. Excludes bid/ask spread,
    slippage, delta fines, manual trades and trades before this process started.
    Simulator P&L already includes commissions: do not subtract these again.
    """
    option_contracts: int = 0
    etf_shares: int = 0

    def record(self, ticker: str, newly_filled: int) -> None:
        if newly_filled < 0:
            raise ValueError("Filled volume must be nonnegative")
        if ticker == "RTM":
            self.etf_shares += newly_filled
        else:
            self.option_contracts += newly_filled

    def summary(self, settings: Settings) -> str:
        options = self.option_contracts * settings.option_fee_per_contract
        etf = self.etf_shares * settings.etf_fee_per_share
        return (f"cumulative_fees=${options + etf:,.2f} "
                f"(options=${options:,.2f}, RTM=${etf:,.2f}; "
                f"contracts_traded={self.option_contracts:,}, shares_traded={self.etf_shares:,})")


class RITClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.costs = TransactionCosts()
        # Seed once from the account, then update only from our own submissions.
        # Quotes continue to refresh; reported positions never overwrite this ledger.
        self.assumed_positions: dict[str, int] | None = None
        self.uncertain_submissions = 0
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
                if method == "POST" or exc.code != 429:
                    raise
                delay = float(exc.headers.get("Retry-After", 0.25 * (attempt + 1)))
                time.sleep(delay)
        raise RuntimeError("RIT API rate limit persisted after four retries")

    def case(self) -> dict[str, Any]:
        return self._request("GET", "case")

    def securities(self) -> list[dict[str, Any]]:
        rows = self._request("GET", "securities")
        if self.assumed_positions is None:
            self.assumed_positions = {
                str(row["ticker"]).upper(): int(row.get("position", 0)) for row in rows
            }
        # Use fresh bid/ask quotes with the persistent assumed position ledger.
        return [dict(row, position=self.assumed_positions.get(str(row["ticker"]).upper(), 0))
                for row in rows]

    def execute_toward(self, ticker: str, signed: int, positions: dict[str, int]) -> int:
        if self.assumed_positions is None:
            self.assumed_positions = dict(positions)
        return self.market_order(ticker, "BUY" if signed > 0 else "SELL", abs(signed))

    def news(self) -> list[dict[str, Any]]:
        return self._request("GET", "news", params={"limit": 50})

    def market_order(self, ticker: str, action: str, quantity: int) -> int:
        """Submit once, assume the entire order filled, never poll its status.

        A timeout/5xx can conceal an accepted order: assume success and do not
        resend, as requested. Explicit HTTP rejection (including 429) is not
        counted. Fees and positions are estimates under this execution model.
        """
        if quantity <= 0:
            return 0
        if self.assumed_positions is None:
            self.securities()
        action = action.upper()
        signed = int(quantity) if action == "BUY" else -int(quantity)
        try:
            result = self._request("POST", "orders", params={
                "ticker": ticker, "type": "MARKET", "quantity": int(quantity), "action": action,
            })
        except HTTPError as exc:
            if exc.code < 500:
                print(f"REJECTED {ticker}: HTTP {exc.code}; ledger unchanged")
                return 0
            self.uncertain_submissions += 1
            print(f"ASSUMED FILL {ticker}: HTTP {exc.code}; no resubmission")
        except (URLError, TimeoutError, ValueError) as exc:
            self.uncertain_submissions += 1
            print(f"ASSUMED FILL {ticker}: response unavailable ({exc}); no resubmission")
        else:
            if isinstance(result, dict) and str(result.get("status", "")).upper() == "REJECTED":
                print(f"REJECTED {ticker}: ledger unchanged")
                return 0
        self.assumed_positions[ticker] = self.assumed_positions.get(ticker, 0) + signed
        self.costs.record(ticker, abs(signed))
        print(f"SUBMITTED ONCE {ticker} {action} {abs(signed)}; "
              f"assumed_position={self.assumed_positions[ticker]}")
        print(self.fee_summary())
        return signed

    def fee_summary(self) -> str:
        return ("ESTIMATED " + self.costs.summary(self.settings)
                + f" uncertain_submissions={self.uncertain_submissions}")

    def reset_transaction_costs(self) -> None:
        self.costs = TransactionCosts()
        self.assumed_positions = None
        self.uncertain_submissions = 0


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
    filled = client.execute_toward(ticker, signed, positions)
    positions[ticker] = current + filled
    return filled


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


def choose_option(
    securities: dict[str, dict[str, Any]], spot: float, years: float, sigma: float, rate: float
) -> tuple[str, float, str, float] | None:
    """Return ticker, executable edge, direction, and delta for the best option.

    A long candidate is valued against its ask and a short candidate against
    its bid, so the quoted spread is already included in the comparison.
    """
    best: tuple[str, float, str, float] | None = None
    for ticker in OPTION_TICKERS:
        sec = securities.get(ticker)
        if not sec:
            continue
        strike, kind = parse_option(ticker)
        fair_value, option_delta = black_scholes(
            spot, strike, years, rate, sigma, kind
        )
        buy_edge = fair_value - float(sec["ask"])
        sell_edge = float(sec["bid"]) - fair_value
        if buy_edge >= sell_edge:
            candidate = (ticker, buy_edge, "BUY", option_delta)
        else:
            candidate = (ticker, sell_edge, "SELL", option_delta)
        if best is None or candidate[1] > best[1]:
            best = candidate
    return best


def target_contracts(option_delta: float, settings: Settings) -> int:
    """Largest requested position allowed by both option and hedge capacity."""
    two_thirds_limit = math.floor(
        settings.option_net_cap * settings.option_limit_fraction
    )
    shares_per_contract = abs(option_delta) * settings.contract_multiplier
    if shares_per_contract < 1e-12:
        hedgeable_limit = two_thirds_limit
    else:
        hedgeable_limit = math.floor(settings.etf_position_cap / shares_per_contract)
    return max(0, min(two_thirds_limit, hedgeable_limit))


def flatten_all(client: RITClient, settings: Settings, sigma: float) -> bool:
    """Close in batches using the assumed ledger, never stale reported positions.

    Unwind the stock hedge alongside options. Limit each option batch to about
    2,000 delta so closing an option does not abruptly strand a large RTM hedge.
    Return False if interrupted/inactive or more work is needed; caller retries.
    API latency means a one-tick lead cannot guarantee completion before news.
    """
    deadline = time.monotonic() + 1.0
    for _ in range(100):
        if time.monotonic() >= deadline:
            return False
        case = client.case()
        if str(case.get("status", "")).upper() != "ACTIVE":
            return False
        secs = as_map(client.securities())
        positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
        held = [t for t in OPTION_TICKERS if positions.get(t, 0)]
        stock = positions.get("RTM", 0)
        if not held and stock == 0:
            return True
        years = remaining_years(
            int(case.get("tick", 0)),
            int(case.get("ticks_per_period") or settings.default_total_ticks),
        )
        spot = mid(secs["RTM"])
        delta = portfolio_delta(secs, spot, years, sigma, settings.risk_free_rate)
        if not held:
            # Full close-out must remove even a residual smaller than 5,000.
            if not submit_toward(client, "RTM", stock, 0, positions, settings):
                return False
        elif abs(delta) >= settings.hedge_trigger:
            target = max(-settings.etf_position_cap,
                         min(settings.etf_position_cap, round(stock - delta)))
            if target != stock:
                if not submit_toward(client, "RTM", stock, target, positions, settings):
                    return False
                continue
            # At the stock cap, continue reducing the option exposure.
            ticker = held[0]
            if not submit_toward(client, ticker, positions[ticker], 0, positions, settings):
                return False
        else:
            ticker = held[0]
            strike, kind = parse_option(ticker)
            _, d = black_scholes(spot, strike, years, settings.risk_free_rate, sigma, kind)
            batch = min(abs(positions[ticker]), settings.option_max_order,
                        max(1, int(2000 / max(abs(d) * settings.contract_multiplier, 1e-9))))
            target = positions[ticker] - (batch if positions[ticker] > 0 else -batch)
            if not submit_toward(client, ticker, positions[ticker], target, positions, settings):
                return False
    return False


def run() -> None:
    settings = Settings()
    client = RITClient(settings)
    running = True
    last_news: tuple[str, ...] | None = None
    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}

    completed_closeouts: set[int] = set()
    pending_announcement: int | None = None
    previous_tick: int | None = None
    closeout_signature: tuple[str, ...] = ()

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

            if previous_tick is not None and tick < previous_tick:
                print("PREVIOUS RUN " + client.fee_summary())
                client.reset_transaction_costs()
                completed_closeouts.clear()
                pending_announcement = None
                last_news = None
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
            previous_tick = tick

            # Catch a missed polling tick too. Finish liquidation before any
            # new signal can create exposure, even if execution crosses release.
            due = [a for a in settings.announcement_ticks
                   if a - 1 <= tick and a not in completed_closeouts]
            if due and pending_announcement is None:
                pending_announcement = max(due)
                completed_closeouts.update(a for a in due if a < pending_announcement)
                closeout_signature = news_signature(client.news())
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                print(f"CLOSE-OUT tick={tick}: before Announcement at {pending_announcement}")

            if pending_announcement is not None:
                items = client.news()
                if not flatten_all(client, settings, volatility_from_news(items)):
                    time.sleep(settings.poll_seconds)
                    continue
                signature_now = news_signature(items)
                # Confirm the scheduled release is visible before opening again.
                # RIT provides tick on news; headline is a fallback for variants.
                number = settings.announcement_ticks.index(pending_announcement) + 1
                released = any(
                    int(item.get("tick", -1)) == pending_announcement
                    or re.search(rf"announcement\s*{number}\b",
                                 str(item.get("headline", "")), re.I)
                    for item in items
                )
                if tick < pending_announcement or not (
                    released or signature_now != closeout_signature
                ):
                    time.sleep(settings.poll_seconds)
                    continue
                completed_closeouts.add(pending_announcement)
                pending_announcement = None
                last_news = None  # Recompute from fresh quotes after close-out.
                print("CLOSE-OUT: assumed ledger flat in options and RTM; release received")

            if tick < settings.first_trading_tick:
                time.sleep(settings.poll_seconds)
                continue

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
                choice = choose_option(
                    securities, spot, years, sigma, settings.risk_free_rate
                )
                opening_allowed = tick < total_ticks - settings.stop_opening_ticks
                if choice and opening_allowed:
                    ticker, edge, direction, option_delta = choice
                    if edge >= settings.edge_threshold:
                        contracts = target_contracts(option_delta, settings)
                        desired = contracts if direction == "BUY" else -contracts
                        option_targets[ticker] = desired
                        hedge_shares = -desired * settings.contract_multiplier * option_delta
                        print(
                            f"NEW SIGNAL news={len(signature)} option={ticker} "
                            f"side={direction} edge={edge:.3f} delta={option_delta:.3f} "
                            f"target={desired} theoretical_hedge={hedge_shares:,.0f} RTM"
                        )
                    else:
                        print(
                            f"NO TRADE news={len(signature)}: best single-option edge "
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
                )

            # Refresh quotes with assumed positions, then hedge the full portfolio.
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
                f"forecast_vol={sigma:.1%} delta={delta:,.0f} "
                f"{client.fee_summary()}"
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"API error: {exc}")
            time.sleep(1.0)
        except Exception as exc:
            print(f"Strategy error: {exc}")
            time.sleep(1.0)
        time.sleep(settings.poll_seconds)

    print("FINAL " + client.fee_summary())


if __name__ == "__main__":
    run()
