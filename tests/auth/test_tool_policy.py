from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from asyncpg import InterfaceError
from fastmcp.exceptions import ToolError

from auth import tool_policy
from auth.tool_policy import (
    AsyncpgToolPolicyStore,
    PolicyDatabaseError,
    ToolPolicyMiddleware,
    ToolPolicyService,
    extract_email_domain,
    resolve_authenticated_email,
)


class FakePolicyStore:
    def __init__(
        self,
        *,
        allowed_domains: set[str] | None = None,
        permissions: dict[tuple[str, str], tuple[bool, datetime | None]] | None = None,
        fail_domains: bool = False,
        fail_permissions: bool = False,
    ):
        self.allowed_domains = allowed_domains or set()
        self.permissions = permissions or {}
        self.fail_domains = fail_domains
        self.fail_permissions = fail_permissions

    async def is_domain_allowed(self, domain: str) -> bool:
        if self.fail_domains:
            raise PolicyDatabaseError("domain lookup failed")
        return domain in self.allowed_domains

    async def is_user_tool_allowed(
        self, email: str, tool_group: str, *, now: datetime
    ) -> bool:
        if self.fail_permissions:
            raise PolicyDatabaseError("tool lookup failed")
        allowed = self.permissions.get((email, tool_group))
        if allowed is None:
            return False
        enabled, expires_at = allowed
        return enabled and (expires_at is None or expires_at > now)


class FakeFastMCPContext:
    def __init__(self, email: str | None = None):
        self.state = {}
        if email:
            self.state["authenticated_user_email"] = email

    async def get_state(self, key: str):
        return self.state.get(key)


def test_extract_email_domain_normalizes_valid_email():
    assert extract_email_domain("User@Example.COM") == "example.com"


@pytest.mark.asyncio
async def test_resolve_context_email():
    email = await resolve_authenticated_email(
        FakeFastMCPContext("context@example.com"),
    )
    assert email == "context@example.com"


@pytest.mark.asyncio
async def test_unconfigured_policy_allows_unrestricted_google_tool():
    service = ToolPolicyService(None)

    decision = await service.authorize(email=None, tool_name="search_gmail_messages")

    assert decision.allowed


@pytest.mark.asyncio
async def test_unconfigured_policy_denies_restricted_sql_tool():
    service = ToolPolicyService(None)

    decision = await service.authorize(email="user@example.com", tool_name="sql_select")

    assert not decision.allowed
    assert "WORKSPACE_MCP_POLICY_DATABASE_URL" in decision.reason


@pytest.mark.asyncio
async def test_policy_allows_domain_for_unrestricted_tool():
    service = ToolPolicyService(FakePolicyStore(allowed_domains={"example.com"}))

    decision = await service.authorize(
        email="User@Example.com", tool_name="search_gmail_messages"
    )

    assert decision.allowed
    assert decision.email == "user@example.com"


@pytest.mark.asyncio
async def test_policy_denies_disabled_domain():
    service = ToolPolicyService(FakePolicyStore(allowed_domains={"example.com"}))

    decision = await service.authorize(
        email="user@blocked.com", tool_name="search_gmail_messages"
    )

    assert not decision.allowed
    assert "blocked.com" in decision.reason


@pytest.mark.asyncio
async def test_policy_allows_unexpired_restricted_permission():
    now = datetime.now(timezone.utc)
    service = ToolPolicyService(
        FakePolicyStore(
            allowed_domains={"example.com"},
            permissions={
                ("user@example.com", "sql"): (True, now + timedelta(minutes=5))
            },
        )
    )

    decision = await service.authorize(
        email="user@example.com", tool_name="sql_select", now=now
    )

    assert decision.allowed


@pytest.mark.asyncio
async def test_policy_denies_disabled_restricted_permission():
    now = datetime.now(timezone.utc)
    service = ToolPolicyService(
        FakePolicyStore(
            allowed_domains={"example.com"},
            permissions={("user@example.com", "sql"): (False, None)},
        )
    )

    decision = await service.authorize(
        email="user@example.com", tool_name="sql_select", now=now
    )

    assert not decision.allowed


@pytest.mark.asyncio
async def test_policy_denies_expired_restricted_permission():
    now = datetime.now(timezone.utc)
    service = ToolPolicyService(
        FakePolicyStore(
            allowed_domains={"example.com"},
            permissions={
                ("user@example.com", "sql"): (True, now - timedelta(seconds=1))
            },
        )
    )

    decision = await service.authorize(
        email="user@example.com", tool_name="sql_select", now=now
    )

    assert not decision.allowed


@pytest.mark.asyncio
async def test_policy_fail_closed_on_database_error():
    service = ToolPolicyService(FakePolicyStore(fail_domains=True))

    decision = await service.authorize(email="user@example.com", tool_name="sql_select")

    assert not decision.allowed
    assert "lookup failed" in decision.reason


@pytest.mark.asyncio
async def test_policy_middleware_enforces_after_auth_context(monkeypatch):
    middleware = ToolPolicyMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="sql_select", arguments={}),
        fastmcp_context=FakeFastMCPContext("user@example.com"),
    )
    observed = {}

    class FakeService:
        async def authorize(self, *, email, tool_name, now=None):  # noqa: ARG002
            observed["email"] = email
            observed["tool_name"] = tool_name
            return SimpleNamespace(
                allowed=True, reason=None, tool_group="sql", email=email
            )

    monkeypatch.setattr(
        "auth.tool_policy.get_tool_policy_service", lambda: FakeService()
    )

    async def call_next(ctx):
        assert ctx is context
        return "ok"

    assert await middleware.on_call_tool(context, call_next) == "ok"
    assert observed == {"email": "user@example.com", "tool_name": "sql_select"}


@pytest.mark.asyncio
async def test_policy_middleware_raises_tool_error_when_denied(monkeypatch):
    middleware = ToolPolicyMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="sql_select", arguments={}),
        fastmcp_context=FakeFastMCPContext("user@example.com"),
    )

    class FakeService:
        async def authorize(self, *, email, tool_name, now=None):  # noqa: ARG002
            return SimpleNamespace(
                allowed=False,
                reason="not allowed",
                tool_group="sql",
                email=email,
            )

    monkeypatch.setattr(
        "auth.tool_policy.get_tool_policy_service", lambda: FakeService()
    )

    async def call_next(ctx):  # noqa: ARG001
        raise AssertionError("denied calls must not reach the tool")

    with pytest.raises(ToolError, match="Access denied: not allowed"):
        await middleware.on_call_tool(context, call_next)


DB_TEST_BUDGET = 0.05
CLEANUP_TEST_BUDGET = 0.02
TEST_TIMEOUT = 1.0
CONCURRENT_POLICY_CALLS = 10
TIMEOUT_MESSAGE = "MCP policy database operation timed out."


@pytest.fixture
def short_deadlines(monkeypatch):
    monkeypatch.setattr(
        tool_policy, "POLICY_DB_TIMEOUT_SECONDS", DB_TEST_BUDGET, raising=False
    )
    monkeypatch.setattr(
        tool_policy,
        "POLICY_CLEANUP_TIMEOUT_SECONDS",
        CLEANUP_TEST_BUDGET,
        raising=False,
    )


async def stall(*args, **kwargs):  # noqa: ARG001
    await asyncio.Event().wait()


def policy_context(email=None):
    return SimpleNamespace(
        message=SimpleNamespace(
            name="sql_select", arguments={"user_google_email": "user@example.com"}
        ),
        fastmcp_context=FakeFastMCPContext(email),
    )


def allow_sql_store():
    return FakePolicyStore(
        allowed_domains={"example.com"},
        permissions={("user@example.com", "sql"): (True, None)},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [None, FakeFastMCPContext()])
async def test_no_env_identity(monkeypatch, context):
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "user@example.com")

    assert await resolve_authenticated_email(context) is None


@pytest.mark.asyncio
async def test_identity_error_is_denied(monkeypatch):
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "user@example.com")
    context = SimpleNamespace(
        get_state=AsyncMock(side_effect=RuntimeError("unavailable"))
    )

    assert await resolve_authenticated_email(context) is None


@pytest.mark.asyncio
async def test_identity_cancel_propagates():
    context = SimpleNamespace(get_state=AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await resolve_authenticated_email(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_email", ["", "user@example.com"])
async def test_anonymous_call_is_denied(monkeypatch, configured_email):
    monkeypatch.setenv("USER_GOOGLE_EMAIL", configured_email)
    service = ToolPolicyService(allow_sql_store())
    monkeypatch.setattr(tool_policy, "get_tool_policy_service", lambda: service)
    call_next = AsyncMock()

    with pytest.raises(ToolError, match="Authenticated Google user email is required"):
        await ToolPolicyMiddleware().on_call_tool(policy_context(), call_next)

    call_next.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
@pytest.mark.parametrize("stage", ["domain", "permission"])
async def test_timeout_denies_execution(monkeypatch, stage):
    store = allow_sql_store()
    method = "is_domain_allowed" if stage == "domain" else "is_user_tool_allowed"
    monkeypatch.setattr(store, method, stall)
    service = ToolPolicyService(store)
    monkeypatch.setattr(tool_policy, "get_tool_policy_service", lambda: service)
    call_next = AsyncMock()

    with pytest.raises(ToolError, match=TIMEOUT_MESSAGE):
        await asyncio.wait_for(
            ToolPolicyMiddleware().on_call_tool(
                policy_context("user@example.com"), call_next
            ),
            TEST_TIMEOUT,
        )

    call_next.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_checks_share_deadline(monkeypatch):
    calls = []

    async def slow_domain(domain):  # noqa: ARG001
        calls.append("domain")
        await asyncio.sleep(DB_TEST_BUDGET * 0.6)
        return True

    async def slow_permission(email, tool_group, *, now):  # noqa: ARG001
        calls.append("permission")
        await asyncio.sleep(DB_TEST_BUDGET * 0.6)
        return True

    store = SimpleNamespace(
        is_domain_allowed=slow_domain, is_user_tool_allowed=slow_permission
    )
    decision = await asyncio.wait_for(
        ToolPolicyService(store).authorize(
            email="user@example.com", tool_name="sql_select"
        ),
        TEST_TIMEOUT,
    )

    assert calls == ["domain", "permission"]
    assert not decision.allowed
    assert decision.reason == TIMEOUT_MESSAGE


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_service_cancel_propagates():
    started = asyncio.Event()

    async def blocked_domain(domain):  # noqa: ARG001
        started.set()
        await stall()

    store = SimpleNamespace(is_domain_allowed=blocked_domain)
    task = asyncio.create_task(
        ToolPolicyService(store).authorize(
            email="user@example.com", tool_name="sql_select"
        )
    )
    await asyncio.wait_for(started.wait(), TEST_TIMEOUT)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, TEST_TIMEOUT)


@pytest.fixture
def policy_pool(monkeypatch):
    available = asyncio.BoundedSemaphore(1)
    conn = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"enabled": True, "expires_at": None}),
        terminate=Mock(side_effect=available.release),
    )

    async def acquire():
        await available.acquire()
        return conn

    async def release(conn, **kwargs):  # noqa: ARG001
        available.release()

    pool = SimpleNamespace(
        acquire_op=AsyncMock(side_effect=acquire),
        release=AsyncMock(side_effect=release),
        terminate=Mock(side_effect=conn.terminate),
        _release_slot=available.release,
    )

    class Acquisition:
        def __await__(self):
            return pool.acquire_op().__await__()

        async def __aenter__(self):
            return await self

        async def __aexit__(self, *exc):
            await pool.release(conn)

    pool.acquire = Mock(side_effect=Acquisition)
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setattr("asyncpg.create_pool", create_pool)
    return pool, conn, create_pool


async def check_store(store, lookup):
    if lookup == "domain":
        return await store.is_domain_allowed("example.com")
    return await store.is_user_tool_allowed(
        "user@example.com", "sql", now=datetime.now(timezone.utc)
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
@pytest.mark.parametrize("lookup", ["domain", "permission"])
@pytest.mark.parametrize("stage", ["initialization", "acquisition", "query", "cleanup"])
async def test_store_timeout_recovers(policy_pool, lookup, stage):
    pool, conn, create_pool = policy_pool
    operation = {
        "initialization": create_pool,
        "acquisition": pool.acquire_op,
        "query": conn.fetchrow,
        "cleanup": pool.release,
    }[stage]
    original_effect = operation.side_effect
    operation.side_effect = stall
    store = AsyncpgToolPolicyStore("postgresql://policy")

    with pytest.raises(PolicyDatabaseError, match=TIMEOUT_MESSAGE):
        await asyncio.wait_for(check_store(store, lookup), TEST_TIMEOUT)

    if stage == "query":
        pool.release.assert_awaited_once()
    if stage == "cleanup":
        conn.terminate.assert_called_once()

    operation.side_effect = original_effect
    assert await asyncio.wait_for(check_store(store, lookup), TEST_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_pool_lock_timeout(policy_pool):
    store = AsyncpgToolPolicyStore("postgresql://policy")

    async with store._lock:
        with pytest.raises(PolicyDatabaseError, match=TIMEOUT_MESSAGE):
            await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)

    assert await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
@pytest.mark.parametrize("stage", ["initialization", "acquisition", "query", "cleanup"])
async def test_store_cancel_recovers(policy_pool, stage):
    pool, conn, create_pool = policy_pool
    started = asyncio.Event()

    async def blocked(*args, **kwargs):  # noqa: ARG001
        started.set()
        await stall()

    operation = {
        "initialization": create_pool,
        "acquisition": pool.acquire_op,
        "query": conn.fetchrow,
        "cleanup": pool.release,
    }[stage]
    original_effect = operation.side_effect
    operation.side_effect = blocked
    store = AsyncpgToolPolicyStore("postgresql://policy")
    task = asyncio.create_task(store.is_domain_allowed("example.com"))
    try:
        await asyncio.wait_for(started.wait(), TEST_TIMEOUT)
    finally:
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, TEST_TIMEOUT)

    if stage == "query":
        pool.release.assert_awaited_once()
    if stage == "cleanup":
        conn.terminate.assert_called_once()

    operation.side_effect = original_effect
    assert await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_cancel_with_stuck_cleanup(policy_pool):
    pool, conn, _ = policy_pool
    started = asyncio.Event()

    async def blocked_query(*args):  # noqa: ARG001
        started.set()
        await stall()

    conn.fetchrow.side_effect = blocked_query
    release_effect = pool.release.side_effect
    pool.release.side_effect = stall
    store = AsyncpgToolPolicyStore("postgresql://policy")
    task = asyncio.create_task(store.is_domain_allowed("example.com"))
    try:
        await asyncio.wait_for(started.wait(), TEST_TIMEOUT)
    finally:
        task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, TEST_TIMEOUT)

    conn.terminate.assert_called_once()
    pool.release.side_effect = release_effect
    conn.fetchrow.side_effect = None
    assert await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
@pytest.mark.parametrize("error", [asyncio.TimeoutError, asyncio.CancelledError])
async def test_detached_cleanup_error(policy_pool, error):
    pool, conn, _ = policy_pool

    # asyncpg detaches the proxy when its reset timeout has already discarded it.
    async def failed_release(*args, **kwargs):  # noqa: ARG001
        pool._release_slot()
        raise error

    release_effect = pool.release.side_effect
    terminate_effect = conn.terminate.side_effect
    pool.release.side_effect = failed_release
    conn.terminate.side_effect = InterfaceError(
        "connection has been released back to the pool"
    )
    store = AsyncpgToolPolicyStore("postgresql://policy")
    expected = (
        PolicyDatabaseError if error is asyncio.TimeoutError else asyncio.CancelledError
    )

    with pytest.raises(expected) as caught:
        await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)

    if error is asyncio.TimeoutError:
        assert str(caught.value) == TIMEOUT_MESSAGE

    pool.release.side_effect = release_effect
    conn.terminate.side_effect = terminate_effect
    assert await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_concurrent_db_recovery(policy_pool):
    _, conn, _ = policy_pool
    conn.fetchrow.side_effect = stall
    store = AsyncpgToolPolicyStore("postgresql://policy")
    service = ToolPolicyService(store)
    decisions = await asyncio.wait_for(
        asyncio.gather(
            *(
                service.authorize(email="user@example.com", tool_name="sql_select")
                for _ in range(CONCURRENT_POLICY_CALLS)
            )
        ),
        TEST_TIMEOUT,
    )

    assert all(
        not decision.allowed and decision.reason == TIMEOUT_MESSAGE
        for decision in decisions
    )
    conn.fetchrow.side_effect = None
    decision = await asyncio.wait_for(
        service.authorize(email="user@example.com", tool_name="sql_select"),
        TEST_TIMEOUT,
    )
    assert decision.allowed


@pytest.mark.asyncio
@pytest.mark.usefixtures("short_deadlines")
async def test_retired_pool_denies_query(policy_pool):
    pool, conn, _ = policy_pool
    store = AsyncpgToolPolicyStore("postgresql://policy")
    acquire = pool.acquire_op.side_effect

    async def retired_acquire():
        result = await acquire()
        store._pool = None
        return result

    pool.acquire_op.side_effect = retired_acquire
    with pytest.raises(PolicyDatabaseError, match="MCP policy database is unavailable"):
        await asyncio.wait_for(store.is_domain_allowed("example.com"), TEST_TIMEOUT)

    conn.fetchrow.assert_not_awaited()
    conn.terminate.assert_called_once()
