from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from auth.tool_policy import (
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
async def test_resolve_authenticated_email_prefers_context_over_arguments():
    email = await resolve_authenticated_email(
        FakeFastMCPContext("context@example.com"),
        {"user_google_email": "argument@example.com"},
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

    decision = await service.authorize(email="user@example.com", tool_name="selectSql")

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
        email="user@example.com", tool_name="insertSql", now=now
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
        email="user@example.com", tool_name="selectSql", now=now
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
        email="user@example.com", tool_name="insertSql", now=now
    )

    assert not decision.allowed


@pytest.mark.asyncio
async def test_policy_fail_closed_on_database_error():
    service = ToolPolicyService(FakePolicyStore(fail_domains=True))

    decision = await service.authorize(
        email="user@example.com", tool_name="selectSql"
    )

    assert not decision.allowed
    assert "lookup failed" in decision.reason


@pytest.mark.asyncio
async def test_policy_middleware_enforces_after_auth_context(monkeypatch):
    middleware = ToolPolicyMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="selectSql", arguments={}),
        fastmcp_context=FakeFastMCPContext("user@example.com"),
    )
    observed = {}

    class FakeService:
        async def authorize(self, *, email, tool_name, now=None):  # noqa: ARG002
            observed["email"] = email
            observed["tool_name"] = tool_name
            return SimpleNamespace(allowed=True, reason=None, tool_group="sql", email=email)

    monkeypatch.setattr("auth.tool_policy.get_tool_policy_service", lambda: FakeService())

    async def call_next(ctx):
        assert ctx is context
        return "ok"

    assert await middleware.on_call_tool(context, call_next) == "ok"
    assert observed == {"email": "user@example.com", "tool_name": "selectSql"}


@pytest.mark.asyncio
async def test_policy_middleware_raises_tool_error_when_denied(monkeypatch):
    middleware = ToolPolicyMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="insertSql", arguments={}),
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

    monkeypatch.setattr("auth.tool_policy.get_tool_policy_service", lambda: FakeService())

    async def call_next(ctx):  # noqa: ARG001
        raise AssertionError("denied calls must not reach the tool")

    with pytest.raises(ToolError, match="Access denied: not allowed"):
        await middleware.on_call_tool(context, call_next)
