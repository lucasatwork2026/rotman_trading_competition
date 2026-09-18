"""CSV recording utilities for RIT competition rounds."""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class RoundRecorder:
    METRIC_FIELDS = [
        "timestamp_utc",
        "period",
        "tick",
        "state",
        "spot",
        "forecast_vol",
        "portfolio_delta",
        "delta_limit_excess",
        "etf_position",
        "option_gross",
        "option_net",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
        "peak_pnl",
        "drawdown",
    ]
    POSITION_FIELDS = [
        "timestamp_utc",
        "period",
        "tick",
        "ticker",
        "position",
        "bid",
        "ask",
        "last",
        "vwap",
        "realized_pnl",
        "unrealized_pnl",
    ]
    TRADE_FIELDS = [
        "timestamp_utc",
        "period",
        "tick",
        "ticker",
        "side",
        "quantity",
        "reason",
    ]
    SUMMARY_FIELDS = [
        "run_id",
        "period",
        "finished_utc",
        "finish_reason",
        "samples",
        "ending_pnl",
        "maximum_pnl",
        "minimum_pnl",
        "maximum_drawdown",
        "orders",
        "option_contract_volume",
        "etf_share_volume",
        "metrics_file",
        "positions_file",
        "trades_file",
    ]

    def __init__(self, log_dir: str | None = None) -> None:
        self.root = Path(log_dir or os.getenv("RIT_LOG_DIR", "rit_logs"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.period: int | None = None
        self.last_tick: int | None = None
        self.metrics_path: Path | None = None
        self.positions_path: Path | None = None
        self.trades_path: Path | None = None
        self.pnl_values: list[float] = []
        self.peak_pnl = float("-inf")
        self.maximum_drawdown = 0.0
        self.order_count = 0
        self.option_contract_volume = 0
        self.etf_share_volume = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _append(path: Path, fields: list[str], row: dict[str, Any]) -> None:
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()

    def start_round(self, period: int) -> None:
        if self.period is not None and self.period != period:
            self.finish_round("period_changed")
        if self.period == period:
            return
        self.period = period
        self.last_tick = None
        prefix = f"run_{self.run_id}_period_{period}"
        self.metrics_path = self.root / f"{prefix}_metrics.csv"
        self.positions_path = self.root / f"{prefix}_positions.csv"
        self.trades_path = self.root / f"{prefix}_trades.csv"
        self.pnl_values = []
        self.peak_pnl = float("-inf")
        self.maximum_drawdown = 0.0
        self.order_count = 0
        self.option_contract_volume = 0
        self.etf_share_volume = 0

    def record_snapshot(
        self,
        period: int,
        tick: int,
        state: str,
        spot: float,
        sigma: float,
        delta: float,
        securities: Iterable[dict[str, Any]],
        option_tickers: set[str],
    ) -> None:
        self.start_round(period)
        if tick == self.last_tick:
            return
        self.last_tick = tick
        items = list(securities)
        now = self._now()
        realized = sum(float(item.get("realized", 0) or 0) for item in items)
        unrealized = sum(float(item.get("unrealized", 0) or 0) for item in items)
        total_pnl = realized + unrealized
        self.pnl_values.append(total_pnl)
        self.peak_pnl = max(self.peak_pnl, total_pnl)
        drawdown = self.peak_pnl - total_pnl
        self.maximum_drawdown = max(self.maximum_drawdown, drawdown)
        positions = {
            str(item.get("ticker", "")).upper(): int(item.get("position", 0) or 0)
            for item in items
        }
        option_values = [positions.get(ticker, 0) for ticker in option_tickers]

        assert self.metrics_path is not None
        self._append(
            self.metrics_path,
            self.METRIC_FIELDS,
            {
                "timestamp_utc": now,
                "period": period,
                "tick": tick,
                "state": state,
                "spot": f"{spot:.6f}",
                "forecast_vol": f"{sigma:.8f}",
                "portfolio_delta": f"{delta:.4f}",
                "delta_limit_excess": f"{max(abs(delta) - 7000, 0):.4f}",
                "etf_position": positions.get("RTM", 0),
                "option_gross": sum(abs(value) for value in option_values),
                "option_net": sum(option_values),
                "realized_pnl": f"{realized:.4f}",
                "unrealized_pnl": f"{unrealized:.4f}",
                "total_pnl": f"{total_pnl:.4f}",
                "peak_pnl": f"{self.peak_pnl:.4f}",
                "drawdown": f"{drawdown:.4f}",
            },
        )

        assert self.positions_path is not None
        for item in items:
            self._append(
                self.positions_path,
                self.POSITION_FIELDS,
                {
                    "timestamp_utc": now,
                    "period": period,
                    "tick": tick,
                    "ticker": item.get("ticker", ""),
                    "position": item.get("position", 0),
                    "bid": item.get("bid", ""),
                    "ask": item.get("ask", ""),
                    "last": item.get("last", ""),
                    "vwap": item.get("vwap", ""),
                    "realized_pnl": item.get("realized", 0),
                    "unrealized_pnl": item.get("unrealized", 0),
                },
            )

    def record_trade(
        self, period: int, tick: int, ticker: str, signed_quantity: int, reason: str
    ) -> None:
        if signed_quantity == 0:
            return
        self.start_round(period)
        assert self.trades_path is not None
        quantity = abs(signed_quantity)
        self.order_count += 1
        if ticker == "RTM":
            self.etf_share_volume += quantity
        else:
            self.option_contract_volume += quantity
        self._append(
            self.trades_path,
            self.TRADE_FIELDS,
            {
                "timestamp_utc": self._now(),
                "period": period,
                "tick": tick,
                "ticker": ticker,
                "side": "BUY" if signed_quantity > 0 else "SELL",
                "quantity": quantity,
                "reason": reason,
            },
        )

    def finish_round(self, reason: str) -> None:
        if self.period is None:
            return
        summary_path = self.root / "round_summaries.csv"
        ending = self.pnl_values[-1] if self.pnl_values else 0.0
        maximum = max(self.pnl_values) if self.pnl_values else 0.0
        minimum = min(self.pnl_values) if self.pnl_values else 0.0
        self._append(
            summary_path,
            self.SUMMARY_FIELDS,
            {
                "run_id": self.run_id,
                "period": self.period,
                "finished_utc": self._now(),
                "finish_reason": reason,
                "samples": len(self.pnl_values),
                "ending_pnl": f"{ending:.4f}",
                "maximum_pnl": f"{maximum:.4f}",
                "minimum_pnl": f"{minimum:.4f}",
                "maximum_drawdown": f"{self.maximum_drawdown:.4f}",
                "orders": self.order_count,
                "option_contract_volume": self.option_contract_volume,
                "etf_share_volume": self.etf_share_volume,
                "metrics_file": self.metrics_path.name if self.metrics_path else "",
                "positions_file": self.positions_path.name if self.positions_path else "",
                "trades_file": self.trades_path.name if self.trades_path else "",
            },
        )
        self.period = None

