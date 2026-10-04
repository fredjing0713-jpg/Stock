"""Broker interface. The engine only talks to brokers through this class."""

from __future__ import annotations

import abc
import asyncio
import time
from typing import Awaitable, Callable

from ..models import AccountSnapshot, Fill, MarginRate, OrderInfo, OrderRequest, Position, Quote

FillHandler = Callable[[Fill], "Awaitable[None] | None"]
ConnectionHandler = Callable[[bool, str], "Awaitable[None] | None"]  # (connected, detail)


class BrokerError(RuntimeError):
    """Raised when the broker cannot perform a request (disconnected, timeout, rejected...)."""


class Broker(abc.ABC):
    """All methods are async and must raise BrokerError (not library errors) on failure."""

    def __init__(self) -> None:
        self._fill_handlers: list[FillHandler] = []
        self._conn_handlers: list[ConnectionHandler] = []

    # ---------------------------------------------------------------- lifecycle
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    async def run_forever(self) -> None:
        """Keep the connection alive (reconnect loop). Default: nothing to do."""
        await asyncio.Event().wait()

    # ---------------------------------------------------------------- reads
    @abc.abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def positions(self) -> dict[str, Position]:
        """Non-zero stock positions keyed by provider-style symbol."""

    @abc.abstractmethod
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Best-effort quotes. Symbols that cannot be priced map to a Quote with all None."""

    @abc.abstractmethod
    async def margin_rate(self, symbol: str, qty: int, price: float) -> MarginRate | None:
        """Margin requirement rate for buying `qty` shares at `price`; None if unknown."""

    @abc.abstractmethod
    async def open_orders(self) -> list[OrderInfo]:
        """Working (not done) orders placed by this bot (tag starts with BOT_TAG_PREFIX)."""

    @abc.abstractmethod
    async def get_order(self, order_id: str) -> OrderInfo | None: ...

    # ---------------------------------------------------------------- writes
    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderInfo: ...

    @abc.abstractmethod
    async def modify_order(
        self,
        order_id: str,
        *,
        qty: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderInfo: ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    # ---------------------------------------------------------------- helpers
    async def wait_for_done(self, order_id: str, timeout: float, poll: float = 0.25) -> OrderInfo | None:
        """Poll until the order is done or `timeout` seconds pass; return the latest snapshot."""
        deadline = time.monotonic() + timeout
        info = await self.get_order(order_id)
        while info is not None and not info.is_done and time.monotonic() < deadline:
            await asyncio.sleep(poll)
            info = await self.get_order(order_id)
        return info

    def add_fill_handler(self, handler: FillHandler) -> None:
        self._fill_handlers.append(handler)

    def add_connection_handler(self, handler: ConnectionHandler) -> None:
        self._conn_handlers.append(handler)

    async def _emit_fill(self, fill: Fill) -> None:
        for h in list(self._fill_handlers):
            r = h(fill)
            if asyncio.iscoroutine(r):
                await r

    async def _emit_connection(self, connected: bool, detail: str = "") -> None:
        for h in list(self._conn_handlers):
            r = h(connected, detail)
            if asyncio.iscoroutine(r):
                await r
