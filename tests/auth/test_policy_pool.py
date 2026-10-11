from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import asyncpg
import pytest
from asyncpg.pool import Pool, PoolConnectionProxy

from auth import tool_policy

TEST_TIMEOUT = 1.0
CLEANUP_BUDGET = 0.1
RESET_RESUME_DELAY = 0.01


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["release", "authorization"])
async def test_cancel_on_completion(operation: str) -> None:
    task: asyncio.Task[Any]

    async def complete(*args: Any, **kwargs: Any) -> bool:
        asyncio.get_running_loop().call_soon(task.cancel)
        return True

    store = tool_policy.AsyncpgToolPolicyStore("postgresql://unused")
    if operation == "release":
        call = store._release(SimpleNamespace(release=complete), SimpleNamespace())
    else:
        service = tool_policy.ToolPolicyService(
            SimpleNamespace(is_domain_allowed=complete)
        )
        call = service.authorize(
            email="user@example.com", tool_name="search_gmail_messages"
        )

    task = asyncio.create_task(call)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, TEST_TIMEOUT)


class _DriverConn:
    def __init__(self, holder: Any) -> None:
        self._holder = holder
        self._proxy: Any = None
        self._closed = False
        self._protocol = SimpleNamespace(queries_count=0, _is_cancelling=lambda: False)
        self.reset_started = asyncio.Event()
        self.reset_done = asyncio.Event()

    def _set_proxy(self, proxy: Any) -> None:
        self._proxy = proxy

    def _on_release(self) -> None:
        pass

    def is_closed(self) -> bool:
        return self._closed

    async def reset(self, *, timeout: float) -> None:  # noqa: ARG002
        self.reset_started.set()
        await self.reset_done.wait()
        raise RuntimeError("Reset failed after connection abort")

    def terminate(self) -> None:
        self._closed = True
        if self._proxy is not None:
            self._holder._release_on_close()


@pytest.mark.asyncio
async def test_cancel_preserves_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_policy, "POLICY_CLEANUP_TIMEOUT_SECONDS", CLEANUP_BUDGET)
    # A zero-minimum pool initializes holders without opening a database connection.
    pool = await asyncpg.create_pool(dsn="postgresql://unused", min_size=0, max_size=1)
    holder = await pool._queue.get()
    original = _DriverConn(holder)
    proxy = PoolConnectionProxy(holder, original)
    holder._con = original
    holder._proxy = proxy
    holder._generation = pool._generation
    holder._in_use = asyncio.get_running_loop().create_future()

    fresh = _DriverConn(holder)
    monkeypatch.setattr(Pool, "_get_new_connection", AsyncMock(return_value=fresh))
    store = tool_policy.AsyncpgToolPolicyStore("postgresql://unused")
    store._pool = pool
    release = asyncio.create_task(store._release(pool, proxy))
    checkout = asyncio.ensure_future(pool.acquire())
    holder_tasks: list[asyncio.Task[Any]] = []

    try:
        await asyncio.wait_for(original.reset_started.wait(), TEST_TIMEOUT)
        holder_tasks = [
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__ == "PoolConnectionHolder.release"
        ]
        release.cancel()

        # Let cancellation reach cleanup before the old reset reports its error.
        await asyncio.sleep(RESET_RESUME_DELAY)
        original.reset_done.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(release, TEST_TIMEOUT)

        fresh_proxy = await asyncio.wait_for(checkout, TEST_TIMEOUT)
        await asyncio.gather(*holder_tasks, return_exceptions=True)

        assert not fresh.is_closed()
        assert fresh_proxy._con is fresh
    finally:
        original.reset_done.set()
        release.cancel()
        checkout.cancel()
        await asyncio.gather(release, checkout, *holder_tasks, return_exceptions=True)
        pool.terminate()
