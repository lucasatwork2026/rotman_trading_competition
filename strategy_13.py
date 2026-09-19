"""Single-option RIT volatility-case trading bot.

Run the RIT Client first, enable the REST API, then execute this file.
The bot stays inactive before tick 74. While flat and outside the pre-announcement
close-out window, it scans every tick for the single option with the largest
executable Black-Scholes pricing gap and enters when edge >= 0.06. It records the
entry edge and exits the whole trade once about 90% of that gap has been realized:
the executable exit-side edge must fall to <= max($0.01, 10% of entry edge). After
fully flattening options and RTM, it immediately resumes scanning for another gap.

Normal delta hedging uses a +/-1,000 trigger and targets +/-300 rather than zero.
An emergency safenet activates at +/-6,000: it market-hedges aggressively and, if
the +/-50,000 RTM cap cannot contain risk, cuts option exposure until the best
achievable delta is within +/-4,000. Planned liquidation begins five ticks before
each Announcement: option exposure is reduced progressively using <=100-contract
market orders with RTM re-hedging after each batch, then any remainder is force-
flattened one tick before Announcements at 74, 149, and 224. The close-out window
always overrides gap trading and prevents new entries.

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
    edge_threshold: float = float(os.getenv("RIT_EDGE_THRESHOLD", "0.06"))
    edge_capture_fraction: float = float(
        os.getenv("RIT_EDGE_CAPTURE_FRACTION", "0.90")
    )
    exit_edge_floor: float = float(os.getenv("RIT_EXIT_EDGE_FLOOR", "0.01"))
    option_order_size: int = int(os.getenv("RIT_OPTION_ORDER_SIZE", "100"))
    first_trading_tick: int = int(os.getenv("RIT_FIRST_TRADING_TICK", "74"))
    hedge_trigger: int = int(os.getenv("RIT_HEDGE_TRIGGER", "1000"))
    hedge_target: int = int(os.getenv("RIT_HEDGE_TARGET", "300"))

    # Emergency risk layer. The official CRO penalty boundary is +/-7,000, so
    # start acting before it is reached and leave a buffer when ETF capacity binds.
    actual_delta_limit: int = int(os.getenv("RIT_ACTUAL_DELTA_LIMIT", "7000"))
    emergency_trigger: int = int(os.getenv("RIT_EMERGENCY_TRIGGER", "6000"))
    emergency_safe_delta: int = int(os.getenv("RIT_EMERGENCY_SAFE_DELTA", "4000"))
    emergency_max_orders_per_cycle: int = int(
        os.getenv("RIT_EMERGENCY_MAX_ORDERS", "8")
    )

    closeout_hedge_trigger: int = int(os.getenv("RIT_CLOSEOUT_HEDGE_TRIGGER", "200"))
    closeout_start_ticks: int = int(os.getenv("RIT_CLOSEOUT_START_TICKS", "2"))
    force_flat_ticks: int = int(os.getenv("RIT_FORCE_FLAT_TICKS", "1"))
    stop_opening_ticks: int = int(os.getenv("RIT_STOP_OPENING_TICKS", "12"))
    default_total_ticks: int = int(os.getenv("RIT_TOTAL_TICKS", "600"))
    option_limit_fraction: float = float(
        os.getenv("RIT_OPTION_LIMIT_FRACTION", str(2.90 / 3.0))
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


def option_delta_breakdown(
    securities: dict[str, dict[str, Any]],
    spot: float,
    years: float,
    sigma: float,
    rate: float,
    settings: Settings,
) -> tuple[float, dict[str, tuple[int, float, float]]]:
    """Return total option delta and per-ticker (position, delta, contribution)."""
    total = 0.0
    components: dict[str, tuple[int, float, float]] = {}
    for ticker in OPTION_TICKERS:
        sec = securities.get(ticker)
        if not sec:
            continue
        position = int(sec.get("position", 0))
        if position == 0:
            continue
        strike, kind = parse_option(ticker)
        _, option_delta = black_scholes(spot, strike, years, rate, sigma, kind)
        contribution = position * settings.contract_multiplier * option_delta
        components[ticker] = (position, option_delta, contribution)
        total += contribution
    return total, components


def portfolio_delta(
    securities: dict[str, dict[str, Any]], spot: float, years: float, sigma: float, rate: float
) -> float:
    stock_delta = float(securities["RTM"].get("position", 0))
    option_delta = 0.0
    for ticker in OPTION_TICKERS:
        sec = securities.get(ticker)
        if not sec:
            continue
        strike, kind = parse_option(ticker)
        _, d = black_scholes(spot, strike, years, rate, sigma, kind)
        option_delta += float(sec.get("position", 0)) * 100.0 * d
    return stock_delta + option_delta


def shrink_target_after_emergency_cut(
    option_targets: dict[str, int], ticker: str, new_position: int
) -> None:
    """Prevent the normal loop from immediately rebuilding an emergency cut.

    If the current signal was targeting a larger position in the same direction,
    shrink that target to the new risk-reduced position. A target of zero stays
    zero so an already-planned unwind can continue.
    """
    old_target = option_targets.get(ticker, 0)
    if old_target == 0:
        return
    same_direction = (old_target > 0 and new_position > 0) or (old_target < 0 and new_position < 0)
    if new_position == 0:
        option_targets[ticker] = 0
    elif same_direction and abs(old_target) > abs(new_position):
        option_targets[ticker] = new_position


def emergency_risk_safenet(
    client: RITClient,
    settings: Settings,
    securities: dict[str, dict[str, Any]],
    spot: float,
    years: float,
    sigma: float,
    option_targets: dict[str, int],
) -> tuple[dict[str, dict[str, Any]], float, float, bool]:
    """Emergency delta control for normal trading.

    1. Trigger before the official +/-7,000 penalty boundary.
    2. Market-hedge RTM aggressively toward the best feasible stock position.
    3. If even +/-50,000 RTM leaves too much residual delta, close option
       contracts that contribute most strongly to the dangerous direction.
    4. Shrink the corresponding option target so the cut is not rebuilt.

    Returns refreshed securities, spot, current delta, and whether emergency mode
    took any action. It deliberately uses market orders only.
    """
    acted = False
    orders_used = 0

    while orders_used < settings.emergency_max_orders_per_cycle:
        positions = {t: int(s.get("position", 0)) for t, s in securities.items()}
        current_stock = positions.get("RTM", 0)
        option_delta, components = option_delta_breakdown(
            securities, spot, years, sigma, settings.risk_free_rate, settings
        )
        total_delta = current_stock + option_delta
        best_achievable_abs_delta = max(
            0.0, abs(option_delta) - settings.etf_position_cap
        )
        emergency_needed = (
            abs(total_delta) >= settings.emergency_trigger
            or best_achievable_abs_delta > settings.emergency_safe_delta
        )
        if not emergency_needed:
            return securities, spot, total_delta, acted

        if not acted:
            if abs(total_delta) >= settings.emergency_trigger:
                print(
                    f"EMERGENCY DELTA: {total_delta:,.0f} breached +/-"
                    f"{settings.emergency_trigger:,} buffer "
                    f"(penalty boundary +/-{settings.actual_delta_limit:,})"
                )
            else:
                print(
                    f"EMERGENCY CAPACITY: best possible ETF hedge still leaves "
                    f"|delta|={best_achievable_abs_delta:,.0f} > "
                    f"{settings.emergency_safe_delta:,}; reducing options"
                )

        # Best stock hedge for the current option book, clipped to the ETF cap.
        best_stock = max(
            -settings.etf_position_cap,
            min(settings.etf_position_cap, round(-option_delta)),
        )

        if current_stock != best_stock:
            before = current_stock
            filled = submit_toward(
                client, "RTM", current_stock, best_stock, positions, settings
            )
            if filled == 0:
                print("EMERGENCY: RTM hedge order could not be submitted; stopping this cycle")
                return securities, spot, total_delta, acted
            acted = True
            orders_used += 1
            print(
                f"EMERGENCY RTM: {before:,} -> {positions.get('RTM', before):,} "
                f"toward best feasible {best_stock:,}"
            )
            securities = as_map(client.securities())
            spot = mid(securities["RTM"])
            continue

        # We are already at the best feasible ETF position. If the residual is
        # still above the safe level, ETF capacity is binding and options must fall.
        residual = total_delta
        if abs(residual) <= settings.emergency_safe_delta:
            return securities, spot, residual, acted

        excess_option_delta = max(
            0.0,
            abs(option_delta) - (settings.etf_position_cap + settings.emergency_safe_delta),
        )
        if excess_option_delta <= 0:
            # Numerical/rounding corner case: nothing to cut.
            return securities, spot, residual, acted

        danger_sign = 1.0 if option_delta > 0 else -1.0
        candidates: list[tuple[float, float, str, int, float, float]] = []
        for ticker, (position, option_d, contribution) in components.items():
            # Closing only helps if this position contributes in the dangerous direction.
            if contribution * danger_sign <= 0:
                continue
            delta_per_contract = abs(option_d) * settings.contract_multiplier
            if delta_per_contract <= 1e-9:
                continue
            candidates.append(
                (delta_per_contract, abs(contribution), ticker, position, option_d, contribution)
            )

        # Largest |delta| per contract first minimizes option contracts/commissions.
        candidates.sort(reverse=True)
        cut_filled = False
        for delta_per_contract, _, ticker, position, option_d, contribution in candidates:
            contracts_needed = max(1, math.ceil(excess_option_delta / delta_per_contract))
            batch = min(
                abs(position),
                settings.option_max_order,
                settings.option_order_size,
                contracts_needed,
            )
            if batch <= 0:
                continue
            target = position - (batch if position > 0 else -batch)
            old_target = option_targets.get(ticker, 0)
            filled = submit_toward(
                client, ticker, position, target, positions, settings
            )
            if filled == 0:
                continue

            acted = True
            cut_filled = True
            orders_used += 1
            new_position = positions.get(ticker, position)
            shrink_target_after_emergency_cut(option_targets, ticker, new_position)
            print(
                f"EMERGENCY OPTION CUT: {ticker} {position:+d} -> {new_position:+d}; "
                f"option_delta/contract={option_d * settings.contract_multiplier:+.1f}; "
                f"target {old_target:+d} -> {option_targets.get(ticker, 0):+d}"
            )
            break

        if not cut_filled:
            print(
                "EMERGENCY: ETF is at its hedge limit but no risk-reducing option "
                "close could be submitted this cycle"
            )
            return securities, spot, residual, acted

        securities = as_map(client.securities())
        spot = mid(securities["RTM"])

    # Order-budget reached: return fresh risk and let the next polling cycle continue.
    positions = {t: int(s.get("position", 0)) for t, s in securities.items()}
    option_delta, _ = option_delta_breakdown(
        securities, spot, years, sigma, settings.risk_free_rate, settings
    )
    total_delta = positions.get("RTM", 0) + option_delta
    best_achievable_abs_delta = max(
        0.0, abs(option_delta) - settings.etf_position_cap
    )
    if (
        abs(total_delta) >= settings.emergency_trigger
        or best_achievable_abs_delta > settings.emergency_safe_delta
    ):
        print(
            f"EMERGENCY: cycle order budget reached with delta={total_delta:,.0f}, "
            f"best_possible_abs_delta={best_achievable_abs_delta:,.0f}; "
            "will continue next poll"
        )
    return securities, spot, total_delta, acted


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


def remaining_executable_edge(
    securities: dict[str, dict[str, Any]],
    ticker: str,
    direction: str,
    spot: float,
    years: float,
    sigma: float,
    rate: float,
) -> float:
    """Executable edge still left in an already-open trade.

    For a long option, exiting means selling at the bid, so remaining edge is
    fair value minus bid. For a short option, exiting means buying at the ask,
    so remaining edge is ask minus fair value. A negative value means the
    original mispricing has not merely converged but reversed.
    """
    sec = securities[ticker]
    strike, kind = parse_option(ticker)
    fair_value, _ = black_scholes(spot, strike, years, rate, sigma, kind)
    direction = direction.upper()
    if direction == "BUY":
        return fair_value - float(sec["bid"])
    if direction == "SELL":
        return float(sec["ask"]) - fair_value
    raise ValueError(f"Unrecognized trade direction: {direction}")


def exit_edge_threshold(entry_edge: float, settings: Settings) -> float:
    """Remaining-edge level that means the configured gap fraction was captured."""
    remaining_fraction = max(0.0, 1.0 - settings.edge_capture_fraction)
    return max(settings.exit_edge_floor, entry_edge * remaining_fraction)


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


def closeout_hedge_once(
    client: RITClient,
    settings: Settings,
    sigma: float,
) -> tuple[bool, float]:
    """Move RTM one market-order step toward delta zero during liquidation.

    Returns (order_submitted_or_not_needed, current_delta_after_step). One option
    batch can change delta by at most about 10,000 shares, matching the RTM max
    order size, so re-hedging after every <=100-contract option batch is usually
    enough to keep the unwind controlled.
    """
    case = client.case()
    if str(case.get("status", "")).upper() != "ACTIVE":
        return False, 0.0

    secs = as_map(client.securities())
    if "RTM" not in secs:
        return False, 0.0
    positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
    stock = positions.get("RTM", 0)
    years = remaining_years(
        int(case.get("tick", 0)),
        int(case.get("ticks_per_period") or settings.default_total_ticks),
    )
    spot = mid(secs["RTM"])
    delta = portfolio_delta(secs, spot, years, sigma, settings.risk_free_rate)
    if abs(delta) < settings.closeout_hedge_trigger:
        return True, delta

    target_stock = max(
        -settings.etf_position_cap,
        min(settings.etf_position_cap, round(stock - delta)),
    )
    if target_stock == stock:
        # ETF capacity is binding. Continue reducing options; that is the only
        # remaining way to lower delta risk.
        return True, delta

    filled = submit_toward(client, "RTM", stock, target_stock, positions, settings)
    if filled == 0:
        return False, delta

    secs = as_map(client.securities())
    spot = mid(secs["RTM"])
    years = remaining_years(
        int(case.get("tick", 0)),
        int(case.get("ticks_per_period") or settings.default_total_ticks),
    )
    delta = portfolio_delta(secs, spot, years, sigma, settings.risk_free_rate)
    return True, delta


def planned_unwind_step(
    client: RITClient,
    settings: Settings,
    sigma: float,
    tick: int,
    announcement_tick: int,
) -> bool:
    """Perform one time-aware planned liquidation step for this tick.

    With n ticks left before the announcement and Q option contracts outstanding,
    close ceil(Q / n) contracts during this tick. Each individual option order is
    capped at 100 contracts, and RTM is re-hedged after every option batch.

    Returns True only when both options and RTM are fully flat.
    """
    secs = as_map(client.securities())
    positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
    held = [t for t in OPTION_TICKERS if positions.get(t, 0)]
    stock = positions.get("RTM", 0)

    if not held:
        # Once options are gone, progressively remove any remaining stock hedge.
        if stock == 0:
            return True
        submit_toward(client, "RTM", stock, 0, positions, settings)
        secs = as_map(client.securities())
        positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
        return not any(positions.get(t, 0) for t in OPTION_TICKERS) and positions.get("RTM", 0) == 0

    # If risk has already become large, first use available RTM capacity before
    # starting this tick's option reduction.
    case = client.case()
    years = remaining_years(
        int(case.get("tick", tick)),
        int(case.get("ticks_per_period") or settings.default_total_ticks),
    )
    spot = mid(secs["RTM"])
    delta = portfolio_delta(secs, spot, years, sigma, settings.risk_free_rate)
    if abs(delta) >= settings.emergency_trigger:
        closeout_hedge_once(client, settings, sigma)
        secs = as_map(client.securities())
        positions = {t: int(s.get("position", 0)) for t, s in secs.items()}

    total_contracts = sum(abs(positions.get(t, 0)) for t in OPTION_TICKERS)
    ticks_left = max(1, announcement_tick - tick)
    contracts_to_close = max(1, math.ceil(total_contracts / ticks_left))
    remaining_to_close = contracts_to_close

    print(
        f"PLANNED UNWIND tick={tick}: announcement={announcement_tick}, "
        f"options_left={total_contracts}, ticks_left={ticks_left}, "
        f"close_this_tick={contracts_to_close}"
    )

    while remaining_to_close > 0:
        secs = as_map(client.securities())
        positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
        held = [t for t in OPTION_TICKERS if positions.get(t, 0)]
        if not held:
            break

        # Close the position with the largest absolute delta contribution first.
        spot = mid(secs["RTM"])
        case = client.case()
        years = remaining_years(
            int(case.get("tick", tick)),
            int(case.get("ticks_per_period") or settings.default_total_ticks),
        )
        ranked: list[tuple[float, str]] = []
        for ticker in held:
            strike, kind = parse_option(ticker)
            _, d = black_scholes(
                spot, strike, years, settings.risk_free_rate, sigma, kind
            )
            contribution = abs(positions[ticker] * settings.contract_multiplier * d)
            ranked.append((contribution, ticker))
        ranked.sort(reverse=True)
        ticker = ranked[0][1]
        position = positions[ticker]

        batch = min(
            abs(position),
            settings.option_max_order,
            settings.option_order_size,
            remaining_to_close,
        )
        target = position - (batch if position > 0 else -batch)
        filled = submit_toward(client, ticker, position, target, positions, settings)
        if filled == 0:
            print(f"PLANNED UNWIND: could not close {ticker}; retry next poll")
            return False

        remaining_to_close -= abs(filled)

        # Immediately re-hedge after every <=100-contract option batch.
        ok, post_delta = closeout_hedge_once(client, settings, sigma)
        if not ok:
            print("PLANNED UNWIND: RTM re-hedge failed; retry next poll")
            return False
        print(
            f"PLANNED UNWIND batch: {ticker} closed={abs(filled)}, "
            f"remaining_planned={remaining_to_close}, post_hedge_delta={post_delta:,.0f}"
        )

    secs = as_map(client.securities())
    positions = {t: int(s.get("position", 0)) for t, s in secs.items()}
    return not any(positions.get(t, 0) for t in OPTION_TICKERS) and positions.get("RTM", 0) == 0


def flatten_all(client: RITClient, settings: Settings, sigma: float) -> bool:
    """Force-flat all options and RTM with market orders.

    Used from A-1 onward. Option orders use the full allowed <=100-contract batch
    size; after every option batch, RTM is immediately re-hedged. Once options are
    gone, any residual RTM hedge is liquidated in <=10,000-share orders.
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

        if held:
            spot = mid(secs["RTM"])
            years = remaining_years(
                int(case.get("tick", 0)),
                int(case.get("ticks_per_period") or settings.default_total_ticks),
            )
            ranked: list[tuple[float, str]] = []
            for ticker in held:
                strike, kind = parse_option(ticker)
                _, d = black_scholes(
                    spot, strike, years, settings.risk_free_rate, sigma, kind
                )
                contribution = abs(positions[ticker] * settings.contract_multiplier * d)
                ranked.append((contribution, ticker))
            ranked.sort(reverse=True)
            ticker = ranked[0][1]
            position = positions[ticker]
            batch = min(
                abs(position),
                settings.option_max_order,
                settings.option_order_size,
            )
            target = position - (batch if position > 0 else -batch)
            if not submit_toward(client, ticker, position, target, positions, settings):
                return False

            # Re-hedge immediately after each option batch. If ETF capacity binds,
            # the next option batch will continue reducing the unhedgeable exposure.
            closeout_hedge_once(client, settings, sigma)
            continue

        # No options remain: remove the stock hedge completely. submit_toward
        # automatically respects the 10,000-share max order size, so this loop
        # repeats until RTM is zero or the time budget expires.
        if not submit_toward(client, "RTM", stock, 0, positions, settings):
            return False

    return False


def run() -> None:
    settings = Settings()
    if not 0.0 <= settings.edge_capture_fraction < 1.0:
        raise ValueError("RIT_EDGE_CAPTURE_FRACTION must be in [0, 1)")

    client = RITClient(settings)
    running = True
    last_news: tuple[str, ...] | None = None
    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}

    # Active gap-trade state. Only one option signal is intentionally held at a time.
    active_ticker: str | None = None
    active_direction: str | None = None
    active_entry_edge: float | None = None
    exit_in_progress = False

    completed_closeouts: set[int] = set()
    pending_announcement: int | None = None
    previous_tick: int | None = None
    closeout_signature: tuple[str, ...] = ()
    last_progressive_unwind_tick: int | None = None

    def clear_active_trade() -> None:
        nonlocal active_ticker, active_direction, active_entry_edge, exit_in_progress
        active_ticker = None
        active_direction = None
        active_entry_edge = None
        exit_in_progress = False

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("Connected strategy started. Press Ctrl+C for a clean stop.")
    print(
        f"Edge trading: enter >= {settings.edge_threshold:.3f}; "
        f"capture={settings.edge_capture_fraction:.0%}; "
        f"exit when remaining executable edge <= max({settings.exit_edge_floor:.3f}, "
        f"{1.0 - settings.edge_capture_fraction:.0%} of entry edge)"
    )
    print(f"Normal hedge band: trigger=+/-{settings.hedge_trigger:,}, "
          f"target=+/-{settings.hedge_target:,}; close-out trigger=+/-{settings.closeout_hedge_trigger:,}")
    print(f"Emergency safenet: trigger=+/-{settings.emergency_trigger:,}, "
          f"ETF-bound residual target=+/-{settings.emergency_safe_delta:,}, "
          f"penalty boundary=+/-{settings.actual_delta_limit:,}")
    print(f"Planned close-out: start A-{settings.closeout_start_ticks}, "
          f"force-flat from A-{settings.force_flat_ticks}; "
          "<=100 option contracts/order with RTM re-hedge after each batch")

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
                last_progressive_unwind_tick = None
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                clear_active_trade()
            previous_tick = tick

            # Start planned liquidation several ticks before each announcement.
            # This has absolute priority over edge harvesting/re-entry.
            due = [a for a in settings.announcement_ticks
                   if a - settings.closeout_start_ticks <= tick
                   and a not in completed_closeouts]
            if due and pending_announcement is None:
                pending_announcement = max(due)
                completed_closeouts.update(a for a in due if a < pending_announcement)
                closeout_signature = news_signature(client.news())
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                clear_active_trade()
                last_progressive_unwind_tick = None
                print(
                    f"PLANNED CLOSE-OUT tick={tick}: begin A-{settings.closeout_start_ticks} "
                    f"unwind before Announcement at {pending_announcement}"
                )

            if pending_announcement is not None:
                items = client.news()
                sigma_close = volatility_from_news(items)
                force_flat_from = pending_announcement - settings.force_flat_ticks

                if tick >= force_flat_from:
                    # A-1 and later: no more pacing. Market-flatten everything.
                    if not flatten_all(client, settings, sigma_close):
                        time.sleep(settings.poll_seconds)
                        continue
                else:
                    # Before A-1, perform at most one time-aware liquidation slice
                    # per simulator tick. Never resume gap trading in this window.
                    if last_progressive_unwind_tick != tick:
                        planned_unwind_step(
                            client, settings, sigma_close, tick, pending_announcement
                        )
                        last_progressive_unwind_tick = tick
                    time.sleep(settings.poll_seconds)
                    continue

                signature_now = news_signature(items)
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
                last_progressive_unwind_tick = None
                last_news = None
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                clear_active_trade()
                print("CLOSE-OUT: assumed ledger flat in options and RTM; release received")

            if tick < settings.first_trading_tick:
                time.sleep(settings.poll_seconds)
                continue

            # Fresh market snapshot for normal trading.
            years = remaining_years(tick, total_ticks)
            securities = as_map(client.securities())
            if "RTM" not in securities:
                raise RuntimeError("RTM was not returned by GET /securities")
            spot = mid(securities["RTM"])
            news_items = client.news()
            sigma = volatility_from_news(news_items)
            signature = news_signature(news_items)
            if signature != last_news:
                last_news = signature
                print(f"NEWS SET UPDATED tick={tick}; continuing edge scan with sigma={sigma:.1%}")

            positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            # If an emergency option cut fully removed the active trade on the
            # prior cycle, finish flattening any residual RTM hedge before searching.
            if (
                active_ticker is not None
                and positions.get(active_ticker, 0) == 0
                and option_targets.get(active_ticker, 0) == 0
            ):
                exit_in_progress = True

            # Continue an edge-triggered exit until BOTH the option and RTM are flat.
            # flatten_all uses market orders and up to 100 option contracts/order.
            if exit_in_progress:
                option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                if not flatten_all(client, settings, sigma):
                    time.sleep(settings.poll_seconds)
                    continue

                print("EDGE EXIT COMPLETE: options and RTM flat; immediately resuming market scan")
                clear_active_trade()

                # Refresh because the market may have moved during multi-order exit.
                case = client.case()
                if str(case.get("status", "")).upper() != "ACTIVE":
                    time.sleep(settings.poll_seconds)
                    continue
                tick = int(case.get("tick", tick))
                total_ticks = int(case.get("ticks_per_period") or total_ticks)
                years = remaining_years(tick, total_ticks)
                securities = as_map(client.securities())
                spot = mid(securities["RTM"])
                news_items = client.news()
                sigma = volatility_from_news(news_items)
                positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            # Monitor the economic reason for an open trade every tick. We do this
            # even while the bot is still working toward the full option target: if
            # the gap disappears quickly, stop adding exposure and exit immediately.
            if active_ticker is not None and active_direction is not None and active_entry_edge is not None:
                remaining_edge = remaining_executable_edge(
                    securities,
                    active_ticker,
                    active_direction,
                    spot,
                    years,
                    sigma,
                    settings.risk_free_rate,
                )
                threshold = exit_edge_threshold(active_entry_edge, settings)
                if remaining_edge <= threshold:
                    print(
                        f"EDGE EXIT TRIGGER tick={tick}: {active_ticker} "
                        f"side={active_direction} entry_edge={active_entry_edge:.3f} "
                        f"remaining_edge={remaining_edge:.3f} <= {threshold:.3f}; "
                        f"captured about {settings.edge_capture_fraction:.0%} of gap"
                    )
                    option_targets = {ticker: 0 for ticker in OPTION_TICKERS}
                    exit_in_progress = True
                    if not flatten_all(client, settings, sigma):
                        time.sleep(settings.poll_seconds)
                        continue

                    print("EDGE EXIT COMPLETE: options and RTM flat; immediately resuming market scan")
                    clear_active_trade()

                    # Refresh quotes before looking for the next gap.
                    case = client.case()
                    if str(case.get("status", "")).upper() != "ACTIVE":
                        time.sleep(settings.poll_seconds)
                        continue
                    tick = int(case.get("tick", tick))
                    total_ticks = int(case.get("ticks_per_period") or total_ticks)
                    years = remaining_years(tick, total_ticks)
                    securities = as_map(client.securities())
                    spot = mid(securities["RTM"])
                    news_items = client.news()
                    sigma = volatility_from_news(news_items)
                    positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            # Do not open if the multi-order exit advanced time into A-5..A.
            closeout_window_now = any(
                a not in completed_closeouts
                and a - settings.closeout_start_ticks <= tick <= a
                for a in settings.announcement_ticks
            )
            opening_allowed = (
                tick < total_ticks - settings.stop_opening_ticks
                and not closeout_window_now
                and pending_announcement is None
            )

            # When flat, search EVERY tick. This is the key difference from the old
            # news-only entry logic: after harvesting one gap, another opportunity
            # can be entered before the next announcement.
            search_best_edge: float | None = None
            if active_ticker is None and opening_allowed:
                # Require a genuinely flat option book before assigning a new signal.
                # Edge exits deliberately flatten RTM too, but a zero option book is
                # the essential condition for the single-option strategy.
                option_book_flat = not any(positions.get(t, 0) for t in OPTION_TICKERS)
                if option_book_flat:
                    choice = choose_option(
                        securities, spot, years, sigma, settings.risk_free_rate
                    )
                    if choice is not None:
                        ticker, edge, direction, option_delta = choice
                        search_best_edge = edge
                        if edge >= settings.edge_threshold:
                            contracts = target_contracts(option_delta, settings)
                            if contracts > 0:
                                desired = contracts if direction == "BUY" else -contracts
                                option_targets = {t: 0 for t in OPTION_TICKERS}
                                option_targets[ticker] = desired
                                active_ticker = ticker
                                active_direction = direction
                                active_entry_edge = edge
                                threshold = exit_edge_threshold(edge, settings)
                                hedge_shares = -desired * settings.contract_multiplier * option_delta
                                print(
                                    f"NEW GAP TRADE tick={tick} option={ticker} "
                                    f"side={direction} entry_edge={edge:.3f} "
                                    f"exit_edge<={threshold:.3f} delta={option_delta:.3f} "
                                    f"target={desired} theoretical_hedge={hedge_shares:,.0f} RTM"
                                )

            # Work toward the current target. Options remain market orders, <=100
            # contracts per polling cycle/order as before.
            for ticker in OPTION_TICKERS:
                submit_toward(
                    client,
                    ticker,
                    positions.get(ticker, 0),
                    option_targets[ticker],
                    positions,
                    settings,
                )

            # Refresh quotes with assumed positions, then control portfolio delta.
            securities = as_map(client.securities())
            positions = {t: int(s.get("position", 0)) for t, s in securities.items()}
            spot = mid(securities["RTM"])
            delta = portfolio_delta(
                securities, spot, years, sigma, settings.risk_free_rate
            )

            # Emergency layer takes priority over the normal 1000 -> 300 band.
            option_delta_now, _ = option_delta_breakdown(
                securities, spot, years, sigma, settings.risk_free_rate, settings
            )
            best_achievable_abs_delta = max(
                0.0, abs(option_delta_now) - settings.etf_position_cap
            )
            if (
                abs(delta) >= settings.emergency_trigger
                or best_achievable_abs_delta > settings.emergency_safe_delta
            ):
                securities, spot, delta, _ = emergency_risk_safenet(
                    client,
                    settings,
                    securities,
                    spot,
                    years,
                    sigma,
                    option_targets,
                )
                positions = {t: int(s.get("position", 0)) for t, s in securities.items()}

            # If emergency handling brought risk below its trigger (or was never
            # needed), apply the ordinary hysteresis hedge.
            if abs(delta) < settings.emergency_trigger and abs(delta) >= settings.hedge_trigger:
                current_stock = positions.get("RTM", 0)
                desired_delta = math.copysign(settings.hedge_target, delta)
                shares_to_trade = round(delta - desired_delta)
                target_stock = max(
                    -settings.etf_position_cap,
                    min(settings.etf_position_cap, current_stock - shares_to_trade),
                )
                submit_toward(
                    client, "RTM", current_stock, target_stock, positions, settings
                )
                securities = as_map(client.securities())
                positions = {t: int(s.get("position", 0)) for t, s in securities.items()}
                spot = mid(securities["RTM"])
                delta = portfolio_delta(
                    securities, spot, years, sigma, settings.risk_free_rate
                )

            active_status = "flat/searching"
            if active_ticker is not None and active_entry_edge is not None and active_direction is not None:
                try:
                    rem = remaining_executable_edge(
                        securities,
                        active_ticker,
                        active_direction,
                        spot,
                        years,
                        sigma,
                        settings.risk_free_rate,
                    )
                    active_status = (
                        f"active={active_ticker}:{active_direction} "
                        f"entry_edge={active_entry_edge:.3f} remain={rem:.3f} "
                        f"exit_at={exit_edge_threshold(active_entry_edge, settings):.3f}"
                    )
                except (KeyError, ValueError):
                    active_status = f"active={active_ticker}:{active_direction}"
            elif search_best_edge is not None:
                active_status = f"search_best_edge={search_best_edge:.3f}"

            print(
                f"tick={tick:>3}/{total_ticks} spot={spot:.2f} "
                f"forecast_vol={sigma:.1%} delta={delta:,.0f} {active_status} "
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
