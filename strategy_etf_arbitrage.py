"""RITCx Algorithmic ETF Arbitrage strategy.

Trades the relationship

    RITC (USD) * USD/CAD = BULL (CAD) + BEAR (CAD)

and evaluates fixed-price RITC tender offers.  The strategy uses executable
bid/ask prices, market-depth VWAPs, transaction fees, weighted ETF position
limits, and an explicit USD hedge.  Converters are intentionally not called:
the case rules state that they are manual-only.

Run the RIT Client and case first, then:

    python strategy_etf_arbitrage.py

No strategy can guarantee maximum P&L.  Start with the default conservative
size in a practice heat and tune the environment variables documented in the
repository README only after inspecting the generated CSV logs.
"""

from __future__ import annotations

import csv
import json
import math
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TRADED_TICKERS = ("BULL", "BEAR", "RITC")
CURRENCY_TICKERS = ("CAD", "USD")


@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv("RIT_BASE_URL", "http://localhost:9999/v1")
    api_key: str = os.getenv("RIT_API_KEY", "")
    poll_seconds: float = float(os.getenv("RIT_ETF_POLL_SECONDS", "0.25"))
    arb_entry_edge_cad: float = float(os.getenv("RIT_ETF_ARB_EDGE_CAD", "0.15"))
    tender_edge_cad: float = float(os.getenv("RIT_ETF_TENDER_EDGE_CAD", "0.10"))
    max_arb_clip: int = int(os.getenv("RIT_ETF_ARB_CLIP", "1000"))
    max_inventory_units: int = int(os.getenv("RIT_ETF_MAX_INVENTORY", "10000"))
    position_limit_buffer: float = float(os.getenv("RIT_ETF_LIMIT_BUFFER", "0.90"))
    market_fee_per_share: float = float(os.getenv("RIT_ETF_MARKET_FEE", "0.02"))
    max_security_order: int = 10_000
    max_currency_order: int = 2_500_000
    currency_hedge_trigger: int = int(os.getenv("RIT_ETF_FX_HEDGE_TRIGGER", "100"))
    stop_opening_ticks: int = int(os.getenv("RIT_ETF_STOP_OPENING_TICKS", "5"))
    log_dir: str = os.getenv("RIT_ETF_LOG_DIR", "etf_logs")


class RITClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.base_url.rstrip("/")
        self.headers = {"X-API-Key": settings.api_key} if settings.api_key else {}

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}/{path.lstrip('/')}{query}",
            method=method,
            headers=self.headers,
        )
        for attempt in range(5):
            try:
                with urlopen(request, timeout=2.0) as response:
                    body = response.read()
                    return json.loads(body) if body else None
            except HTTPError as exc:
                if exc.code != 429:
                    detail = exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"RIT {method} {path} failed ({exc.code}): {detail}") from exc
                time.sleep(float(exc.headers.get("Retry-After", 0.15 * (attempt + 1))))
        raise RuntimeError(f"RIT {method} {path} remained rate-limited")

    def case(self) -> dict[str, Any]:
        return self._request("GET", "case")

    def securities(self) -> list[dict[str, Any]]:
        return self._request("GET", "securities")

    def limits(self) -> list[dict[str, Any]]:
        return self._request("GET", "limits")

    def tenders(self) -> list[dict[str, Any]]:
        return self._request("GET", "tenders")

    def book(self, ticker: str, depth: int = 100) -> dict[str, Any]:
        return self._request("GET", "securities/book", {"ticker": ticker, "limit": depth})

    def market_order(self, ticker: str, action: str, quantity: int) -> Any:
        return self._request(
            "POST",
            "orders",
            {
                "ticker": ticker,
                "type": "MARKET",
                "quantity": int(quantity),
                "action": action.upper(),
            },
        )

    def accept_tender(self, tender_id: int) -> Any:
        return self._request("POST", f"tenders/{tender_id}")

    def decline_tender(self, tender_id: int) -> Any:
        return self._request("DELETE", f"tenders/{tender_id}")


@dataclass(frozen=True)
class RiskCaps:
    gross_limit: float
    net_limit: float
    gross_now: float
    net_now: float


@dataclass(frozen=True)
class Opportunity:
    direction: str  # RICH: short ETF/buy stocks; CHEAP: buy ETF/short stocks
    quantity: int
    gross_edge_cad: float
    net_edge_cad: float
    ritc_vwap: float
    bull_vwap: float
    bear_vwap: float


def security_map(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("ticker", "")).upper(): item for item in items}


def quote(item: dict[str, Any], field: str) -> float:
    value = item.get(field)
    if value is None:
        raise ValueError(f"Missing {field} for {item.get('ticker', '?')}")
    return float(value)


def available_at_level(level: dict[str, Any]) -> int:
    quantity = int(float(level.get("quantity", 0) or 0))
    filled = int(float(level.get("quantity_filled", 0) or 0))
    return max(quantity - filled, 0)


def book_capacity(book: dict[str, Any], action: str) -> int:
    levels = book.get("asks" if action.upper() == "BUY" else "bids", []) or []
    return sum(available_at_level(level) for level in levels)


def book_vwap(book: dict[str, Any], action: str, quantity: int) -> float | None:
    """Executable VWAP for a market order, or None if displayed depth is insufficient."""
    if quantity <= 0:
        return None
    levels = book.get("asks" if action.upper() == "BUY" else "bids", []) or []
    remaining = quantity
    notional = 0.0
    for level in levels:
        available = available_at_level(level)
        take = min(remaining, available)
        if take:
            notional += take * float(level["price"])
            remaining -= take
        if remaining == 0:
            return notional / quantity
    return None


def weighted_position_risk(positions: dict[str, int]) -> tuple[int, int]:
    """Case rule: stock multiplier 1, ETF multiplier 2; currencies are excluded."""
    bull = int(positions.get("BULL", 0))
    bear = int(positions.get("BEAR", 0))
    ritc_weighted = 2 * int(positions.get("RITC", 0))
    gross = abs(bull) + abs(bear) + abs(ritc_weighted)
    net = bull + bear + ritc_weighted
    return gross, net


def project_positions(
    positions: dict[str, int], direction: str, quantity: int
) -> dict[str, int]:
    projected = dict(positions)
    sign = 1 if direction == "CHEAP" else -1
    projected["RITC"] = projected.get("RITC", 0) + sign * quantity
    projected["BULL"] = projected.get("BULL", 0) - sign * quantity
    projected["BEAR"] = projected.get("BEAR", 0) - sign * quantity
    return projected


def extract_risk_caps(items: Iterable[dict[str, Any]]) -> RiskCaps | None:
    """Select the tightest positive gross/net limits reported by GET /limits."""
    rows = list(items)
    gross_limits = [float(x.get("gross_limit", 0) or 0) for x in rows]
    net_limits = [float(x.get("net_limit", 0) or 0) for x in rows]
    gross_limits = [x for x in gross_limits if x > 0]
    net_limits = [x for x in net_limits if x > 0]
    if not gross_limits or not net_limits:
        return None
    gross_limit = min(gross_limits)
    net_limit = min(net_limits)
    # Use the row(s) associated with the tightest limits for live utilization.
    gross_now = max(
        (abs(float(x.get("gross", 0) or 0)) for x in rows if float(x.get("gross_limit", 0) or 0) == gross_limit),
        default=0.0,
    )
    net_now = max(
        (abs(float(x.get("net", 0) or 0)) for x in rows if float(x.get("net_limit", 0) or 0) == net_limit),
        default=0.0,
    )
    return RiskCaps(gross_limit, net_limit, gross_now, net_now)


def risk_ok(
    before: dict[str, int],
    after: dict[str, int],
    caps: RiskCaps,
    buffer: float,
) -> bool:
    before_gross, before_net = weighted_position_risk(before)
    after_gross, after_net = weighted_position_risk(after)
    projected_gross = caps.gross_now + (after_gross - before_gross)
    # Preserve the sign of locally projected net while being conservative about
    # any unclassified exposure already included by the simulator.
    projected_net_abs = caps.net_now + abs(after_net - before_net)
    return (
        projected_gross <= caps.gross_limit * buffer
        and projected_net_abs <= caps.net_limit * buffer
    )


def maximum_safe_quantity(
    positions: dict[str, int],
    direction: str,
    requested: int,
    caps: RiskCaps,
    settings: Settings,
) -> int:
    requested = min(requested, settings.max_inventory_units)
    low, high = 0, max(requested, 0)
    while low < high:
        middle = (low + high + 1) // 2
        after = project_positions(positions, direction, middle)
        inventory_ok = all(
            abs(after.get(ticker, 0))
            <= max(settings.max_inventory_units, abs(positions.get(ticker, 0)))
            for ticker in TRADED_TICKERS
        )
        if inventory_ok and risk_ok(positions, after, caps, settings.position_limit_buffer):
            low = middle
        else:
            high = middle - 1
    return low


def arbitrage_edges(
    ritc_price: float,
    bull_price: float,
    bear_price: float,
    usd_cad: float,
    market_fee: float,
) -> tuple[float, float]:
    """Return gross and net CAD edge per ETF unit for supplied executable legs."""
    gross = ritc_price * usd_cad - bull_price - bear_price
    fees = market_fee * usd_cad + 2.0 * market_fee
    return gross, gross - fees


def find_opportunity(
    securities: dict[str, dict[str, Any]],
    books: dict[str, dict[str, Any]],
    positions: dict[str, int],
    caps: RiskCaps,
    settings: Settings,
) -> Opportunity | None:
    usd_bid = quote(securities["USD"], "bid")
    usd_ask = quote(securities["USD"], "ask")
    candidates: list[Opportunity] = []

    for direction, ritc_action, stock_action, fx in (
        ("RICH", "SELL", "BUY", usd_bid),
        ("CHEAP", "BUY", "SELL", usd_ask),
    ):
        depth = min(
            book_capacity(books["RITC"], ritc_action),
            book_capacity(books["BULL"], stock_action),
            book_capacity(books["BEAR"], stock_action),
            settings.max_arb_clip,
        )
        quantity = maximum_safe_quantity(positions, direction, depth, caps, settings)
        if quantity <= 0:
            continue
        ritc_vwap = book_vwap(books["RITC"], ritc_action, quantity)
        bull_vwap = book_vwap(books["BULL"], stock_action, quantity)
        bear_vwap = book_vwap(books["BEAR"], stock_action, quantity)
        if None in (ritc_vwap, bull_vwap, bear_vwap):
            continue
        assert ritc_vwap is not None and bull_vwap is not None and bear_vwap is not None
        if direction == "RICH":
            gross, net = arbitrage_edges(
                ritc_vwap, bull_vwap, bear_vwap, fx, settings.market_fee_per_share
            )
        else:
            gross = bull_vwap + bear_vwap - ritc_vwap * fx
            net = gross - (settings.market_fee_per_share * fx + 2.0 * settings.market_fee_per_share)
        candidates.append(
            Opportunity(direction, quantity, gross, net, ritc_vwap, bull_vwap, bear_vwap)
        )
    profitable = [x for x in candidates if x.net_edge_cad >= settings.arb_entry_edge_cad]
    return max(profitable, key=lambda x: x.net_edge_cad, default=None)


class CSVLogger:
    def __init__(self, directory: str) -> None:
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.metrics = self.root / f"etf_{self.run_id}_metrics.csv"
        self.events = self.root / f"etf_{self.run_id}_events.csv"
        self.last_tick: tuple[int, int] | None = None

    @staticmethod
    def _append(path: Path, fields: list[str], row: dict[str, Any]) -> None:
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()

    def event(self, period: int, tick: int, kind: str, detail: str) -> None:
        self._append(
            self.events,
            ["timestamp_utc", "period", "tick", "kind", "detail"],
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "period": period,
                "tick": tick,
                "kind": kind,
                "detail": detail,
            },
        )

    def snapshot(
        self,
        period: int,
        tick: int,
        securities: dict[str, dict[str, Any]],
        rich_edge: float,
        cheap_edge: float,
        caps: RiskCaps | None,
    ) -> None:
        key = (period, tick)
        if key == self.last_tick:
            return
        self.last_tick = key
        positions = {ticker: int(item.get("position", 0) or 0) for ticker, item in securities.items()}
        realized = sum(float(x.get("realized", 0) or 0) for x in securities.values())
        unrealized = sum(float(x.get("unrealized", 0) or 0) for x in securities.values())
        gross, net = weighted_position_risk(positions)
        self._append(
            self.metrics,
            [
                "timestamp_utc", "period", "tick", "rich_edge_cad", "cheap_edge_cad",
                "bull_position", "bear_position", "ritc_position", "usd_position",
                "weighted_gross", "weighted_net", "gross_limit", "net_limit",
                "realized_pnl", "unrealized_pnl", "total_pnl",
            ],
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "period": period,
                "tick": tick,
                "rich_edge_cad": f"{rich_edge:.6f}",
                "cheap_edge_cad": f"{cheap_edge:.6f}",
                "bull_position": positions.get("BULL", 0),
                "bear_position": positions.get("BEAR", 0),
                "ritc_position": positions.get("RITC", 0),
                "usd_position": positions.get("USD", 0),
                "weighted_gross": gross,
                "weighted_net": net,
                "gross_limit": "" if caps is None else caps.gross_limit,
                "net_limit": "" if caps is None else caps.net_limit,
                "realized_pnl": f"{realized:.4f}",
                "unrealized_pnl": f"{unrealized:.4f}",
                "total_pnl": f"{realized + unrealized:.4f}",
            },
        )


def top_edges(securities: dict[str, dict[str, Any]], settings: Settings) -> tuple[float, float]:
    usd_bid, usd_ask = quote(securities["USD"], "bid"), quote(securities["USD"], "ask")
    rich_gross = (
        quote(securities["RITC"], "bid") * usd_bid
        - quote(securities["BULL"], "ask")
        - quote(securities["BEAR"], "ask")
    )
    cheap_gross = (
        quote(securities["BULL"], "bid")
        + quote(securities["BEAR"], "bid")
        - quote(securities["RITC"], "ask") * usd_ask
    )
    rich_fee = settings.market_fee_per_share * usd_bid + 2 * settings.market_fee_per_share
    cheap_fee = settings.market_fee_per_share * usd_ask + 2 * settings.market_fee_per_share
    return rich_gross - rich_fee, cheap_gross - cheap_fee


def send_market_chunks(
    client: RITClient,
    logger: CSVLogger,
    period: int,
    tick: int,
    ticker: str,
    action: str,
    quantity: int,
    reason: str,
    settings: Settings,
) -> None:
    maximum = settings.max_currency_order if ticker in CURRENCY_TICKERS else settings.max_security_order
    remaining = int(quantity)
    while remaining > 0:
        child = min(remaining, maximum)
        client.market_order(ticker, action, child)
        detail = f"{action} {child} {ticker}; reason={reason}"
        logger.event(period, tick, "ORDER", detail)
        print(f"ORDER {detail}")
        remaining -= child


def hedge_usd(
    client: RITClient,
    logger: CSVLogger,
    period: int,
    tick: int,
    settings: Settings,
) -> None:
    securities = security_map(client.securities())
    usd_position = int(float(securities.get("USD", {}).get("position", 0) or 0))
    if abs(usd_position) < settings.currency_hedge_trigger:
        return
    send_market_chunks(
        client,
        logger,
        period,
        tick,
        "USD",
        "SELL" if usd_position > 0 else "BUY",
        abs(usd_position),
        "FX_HEDGE_TO_ZERO",
        settings,
    )


def execute_opportunity(
    client: RITClient,
    logger: CSVLogger,
    period: int,
    tick: int,
    opportunity: Opportunity,
    settings: Settings,
) -> None:
    q = opportunity.quantity
    reason = f"ETF_{opportunity.direction}_EDGE_{opportunity.net_edge_cad:.4f}_CAD"
    # Lead with RITC, normally the least liquid/security-specific leg, and
    # immediately neutralize both underlying legs.
    if opportunity.direction == "RICH":
        legs = (("RITC", "SELL"), ("BULL", "BUY"), ("BEAR", "BUY"))
    else:
        legs = (("RITC", "BUY"), ("BULL", "SELL"), ("BEAR", "SELL"))
    for ticker, action in legs:
        send_market_chunks(client, logger, period, tick, ticker, action, q, reason, settings)
    hedge_usd(client, logger, period, tick, settings)


def tender_action(tender: dict[str, Any]) -> str:
    """Action is from the participant's perspective: BUY creates a long position."""
    return str(tender.get("action", "")).upper()


def tender_edges(
    tender: dict[str, Any],
    securities: dict[str, dict[str, Any]],
    ritc_book: dict[str, Any],
    settings: Settings,
) -> tuple[float | None, float]:
    """Return immediate-unwind and stock-hedged edge, both in CAD/share."""
    action = tender_action(tender)
    quantity = int(tender.get("quantity", 0) or 0)
    price = float(tender.get("price", 0) or 0)
    usd_bid, usd_ask = quote(securities["USD"], "bid"), quote(securities["USD"], "ask")
    if action == "BUY":
        exit_vwap = book_vwap(ritc_book, "SELL", quantity)
        immediate = None if exit_vwap is None else (
            (exit_vwap - price - settings.market_fee_per_share) * usd_bid
        )
        hedged = (
            quote(securities["BULL"], "bid")
            + quote(securities["BEAR"], "bid")
            - price * usd_ask
            - 2 * settings.market_fee_per_share
        )
    elif action == "SELL":
        exit_vwap = book_vwap(ritc_book, "BUY", quantity)
        immediate = None if exit_vwap is None else (
            (price - exit_vwap - settings.market_fee_per_share) * usd_ask
        )
        hedged = (
            price * usd_bid
            - quote(securities["BULL"], "ask")
            - quote(securities["BEAR"], "ask")
            - 2 * settings.market_fee_per_share
        )
    else:
        return None, float("-inf")
    return immediate, hedged


def process_tenders(
    client: RITClient,
    logger: CSVLogger,
    period: int,
    tick: int,
    securities: dict[str, dict[str, Any]],
    positions: dict[str, int],
    caps: RiskCaps,
    handled: set[int],
    settings: Settings,
) -> None:
    for tender in client.tenders():
        tender_id = int(tender.get("tender_id", tender.get("id", -1)))
        if tender_id < 0 or tender_id in handled:
            continue
        handled.add(tender_id)
        ticker = str(tender.get("ticker", "")).upper()
        quantity = int(tender.get("quantity", 0) or 0)
        action = tender_action(tender)
        fixed = bool(tender.get("is_fixed_bid", True))
        if ticker != "RITC" or action not in {"BUY", "SELL"} or quantity <= 0 or not fixed:
            client.decline_tender(tender_id)
            logger.event(period, tick, "TENDER_REJECT", f"id={tender_id}; unsupported tender")
            continue

        signed = quantity if action == "BUY" else -quantity
        tender_only = dict(positions)
        tender_only["RITC"] = tender_only.get("RITC", 0) + signed
        if not risk_ok(positions, tender_only, caps, settings.position_limit_buffer):
            client.decline_tender(tender_id)
            logger.event(period, tick, "TENDER_REJECT", f"id={tender_id}; position limit")
            continue

        ritc_book = client.book("RITC")
        immediate_edge, hedged_edge = tender_edges(tender, securities, ritc_book, settings)
        hedge_direction = "CHEAP" if action == "BUY" else "RICH"
        hedged_positions = project_positions(positions, hedge_direction, quantity)
        hedge_allowed = risk_ok(
            positions, hedged_positions, caps, settings.position_limit_buffer
        )
        routes: list[tuple[str, float]] = []
        if immediate_edge is not None and immediate_edge >= settings.tender_edge_cad:
            routes.append(("IMMEDIATE", immediate_edge))
        if hedged_edge >= settings.tender_edge_cad and hedge_allowed:
            routes.append(("STOCK_HEDGE", hedged_edge))

        if not routes:
            client.decline_tender(tender_id)
            logger.event(
                period,
                tick,
                "TENDER_REJECT",
                f"id={tender_id}; immediate={immediate_edge}; hedged={hedged_edge:.4f}",
            )
            continue

        route, edge = max(routes, key=lambda item: item[1])
        client.accept_tender(tender_id)
        logger.event(
            period,
            tick,
            "TENDER_ACCEPT",
            f"id={tender_id}; action={action}; qty={quantity}; route={route}; edge_cad={edge:.4f}",
        )
        print(
            f"TENDER ACCEPT id={tender_id} action={action} qty={quantity} "
            f"route={route} edge={edge:.4f} CAD/share"
        )
        if route == "IMMEDIATE":
            send_market_chunks(
                client,
                logger,
                period,
                tick,
                "RITC",
                "SELL" if action == "BUY" else "BUY",
                quantity,
                f"TENDER_{tender_id}_IMMEDIATE_UNWIND",
                settings,
            )
        else:
            stock_action = "SELL" if action == "BUY" else "BUY"
            for stock in ("BULL", "BEAR"):
                send_market_chunks(
                    client,
                    logger,
                    period,
                    tick,
                    stock,
                    stock_action,
                    quantity,
                    f"TENDER_{tender_id}_STOCK_HEDGE",
                    settings,
                )
        hedge_usd(client, logger, period, tick, settings)
        # Refresh after each accepted tender so subsequent checks use live positions.
        securities = security_map(client.securities())
        positions = {ticker: int(x.get("position", 0) or 0) for ticker, x in securities.items()}


def run() -> None:
    settings = Settings()
    client = RITClient(settings)
    logger = CSVLogger(settings.log_dir)
    running = True
    handled_tenders: set[int] = set()
    last_period: int | None = None

    def stop(*_: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print("ETF arbitrage strategy connected. Press Ctrl+C for a clean stop.")

    while running:
        try:
            case = client.case()
            if str(case.get("status", "")).upper() != "ACTIVE":
                time.sleep(0.5)
                continue
            tick = int(case.get("tick", 0))
            period = int(case.get("period", 1))
            total_ticks = int(case.get("ticks_per_period", 300) or 300)
            if period != last_period:
                handled_tenders.clear()
                last_period = period

            securities = security_map(client.securities())
            missing = [ticker for ticker in (*TRADED_TICKERS, "USD") if ticker not in securities]
            if missing:
                raise RuntimeError(f"Missing required securities: {', '.join(missing)}")
            positions = {
                ticker: int(float(item.get("position", 0) or 0))
                for ticker, item in securities.items()
            }
            caps = extract_risk_caps(client.limits())
            if caps is None:
                print(f"tick={tick}: waiting for valid gross/net limits from GET /limits")
                time.sleep(settings.poll_seconds)
                continue

            rich_top, cheap_top = top_edges(securities, settings)
            logger.snapshot(period, tick, securities, rich_top, cheap_top, caps)
            process_tenders(
                client,
                logger,
                period,
                tick,
                securities,
                positions,
                caps,
                handled_tenders,
                settings,
            )

            opening_allowed = tick < total_ticks - settings.stop_opening_ticks
            best: Opportunity | None = None
            if opening_allowed and max(rich_top, cheap_top) >= settings.arb_entry_edge_cad:
                books = {ticker: client.book(ticker) for ticker in TRADED_TICKERS}
                # Refresh positions because a tender may have just been accepted.
                current = security_map(client.securities())
                positions = {
                    ticker: int(float(item.get("position", 0) or 0))
                    for ticker, item in current.items()
                }
                best = find_opportunity(current, books, positions, caps, settings)
                if best:
                    execute_opportunity(client, logger, period, tick, best, settings)

            gross, net = weighted_position_risk(positions)
            decision = "NONE" if best is None else f"{best.direction}:{best.quantity}"
            print(
                f"tick={tick:>3}/{total_ticks} rich={rich_top:+.4f}CAD "
                f"cheap={cheap_top:+.4f}CAD decision={decision} "
                f"risk={gross}/{caps.gross_limit:.0f} net={net}/{caps.net_limit:.0f}"
            )
        except (URLError, TimeoutError) as exc:
            print(f"API connection error: {exc}")
            time.sleep(0.75)
        except Exception as exc:
            print(f"Strategy error: {exc}")
            time.sleep(0.75)
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    run()
