"""Database-backed MCP tool authorization."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

POLICY_DB_TIMEOUT_SECONDS = 5.0
POLICY_CLEANUP_TIMEOUT_SECONDS = 1.0
POLICY_DB_TIMEOUT_MESSAGE = "MCP policy database operation timed out."

_PolicyResult = TypeVar("_PolicyResult")

RESTRICTED_TOOL_GROUPS = frozenset({"sql"})
TOOL_GROUP_BY_TOOL_NAME = {
    "sql_select": "sql",
    "sql_help": "sql",
}


class PolicyDatabaseError(RuntimeError):
    """Raised when the policy database cannot be checked."""


async def _wait_policy(
    call: Awaitable[_PolicyResult], timeout: float
) -> _PolicyResult:
    # Shield the waiter so Python 3.10/3.11 cannot swallow cancellation at completion.
    waiter = asyncio.create_task(asyncio.wait_for(call, timeout=timeout))
    try:
        return await asyncio.shield(waiter)
    except asyncio.CancelledError:
        waiter.cancel()
        try:
            await waiter
        except (Exception, asyncio.CancelledError):
            pass
        raise


async def _policy_db_call(call: Awaitable[_PolicyResult]) -> _PolicyResult:
    try:
        return await _wait_policy(call, timeout=POLICY_DB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise PolicyDatabaseError(POLICY_DB_TIMEOUT_MESSAGE) from exc


class ToolPolicyStore(Protocol):
    """Storage interface for MCP authorization policy."""

    async def is_domain_allowed(self, domain: str) -> bool:
        """Return whether an email domain is enabled for MCP access."""

    async def is_user_tool_allowed(
        self, email: str, tool_group: str, *, now: datetime
    ) -> bool:
        """Return whether a user can call tools in the provided group."""


class AsyncpgToolPolicyStore:
    """Postgres-backed policy store for the ``mcp_auth`` schema."""

    def __init__(self, database_url: str):
        if not database_url.strip():
            raise ValueError("database_url must not be empty")
        self._database_url = database_url
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool

        async with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                import asyncpg

                self._pool = await asyncpg.create_pool(
                    dsn=self._database_url,
                    min_size=0,
                    max_size=int(os.getenv("WORKSPACE_MCP_POLICY_POOL_MAX_SIZE", "5")),
                )
            except asyncio.TimeoutError as exc:
                raise PolicyDatabaseError(POLICY_DB_TIMEOUT_MESSAGE) from exc
            except Exception as exc:  # pragma: no cover - exact driver errors vary
                raise PolicyDatabaseError(
                    "MCP policy database is unavailable."
                ) from exc
            return self._pool

    async def _release(self, pool: Any, conn: Any) -> None:
        # asyncpg shields reset; let it finish before another checkout reuses its holder.
        deadline = asyncio.get_running_loop().time() + POLICY_CLEANUP_TIMEOUT_SECONDS
        release = asyncio.ensure_future(
            pool.release(conn, timeout=POLICY_CLEANUP_TIMEOUT_SECONDS)
        )
        try:
            await _wait_policy(
                asyncio.shield(release),
                timeout=POLICY_CLEANUP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                await _wait_policy(asyncio.shield(release), timeout=remaining)
            except BaseException:
                await self._discard(pool, release)
            raise
        except BaseException:
            await self._discard(pool, release)
            raise

    async def _discard(self, pool: Any, release: asyncio.Future[Any]) -> None:
        if release.done():
            return

        # Retire the entire pool so an unfinished reset cannot affect fresh checkouts.
        if self._pool is pool:
            self._pool = None
        try:
            pool.terminate()
        except Exception:
            pass

        release.cancel()
        try:
            await release
        except (Exception, asyncio.CancelledError):
            pass

    async def _fetchrow(self, query: str, *args: Any) -> Any:
        pool = await self._get_pool()
        conn = await pool.acquire()
        if pool is not self._pool:
            # A queued acquisition can finish after its pool has been retired.
            conn.terminate()
            raise PolicyDatabaseError("MCP policy database is unavailable.")

        try:
            row = await conn.fetchrow(query, *args)
        except BaseException:
            # Preserve caller cancellation and query errors when cleanup also fails.
            try:
                await self._release(pool, conn)
            except Exception:
                pass
            raise

        await self._release(pool, conn)
        return row

    async def is_domain_allowed(self, domain: str) -> bool:
        try:
            row = await _policy_db_call(
                self._fetchrow(
                    """
                    select enabled
                    from mcp_auth.allowed_domains
                    where lower(domain) = lower($1)
                    """,
                    domain,
                )
            )
        except Exception as exc:
            if isinstance(exc, PolicyDatabaseError):
                raise
            raise PolicyDatabaseError("MCP policy domain lookup failed.") from exc
        return bool(row and row["enabled"])

    async def is_user_tool_allowed(
        self, email: str, tool_group: str, *, now: datetime
    ) -> bool:
        try:
            row = await _policy_db_call(
                self._fetchrow(
                    """
                    select enabled, expires_at
                    from mcp_auth.user_tool_permissions
                    where lower(email) = lower($1)
                      and tool_group = $2
                    """,
                    email,
                    tool_group,
                )
            )
        except Exception as exc:
            if isinstance(exc, PolicyDatabaseError):
                raise
            raise PolicyDatabaseError("MCP policy tool lookup failed.") from exc

        if not row or not row["enabled"]:
            return False

        expires_at = row["expires_at"]
        if expires_at is None:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > now


@dataclass(frozen=True)
class ToolPolicyDecision:
    """Authorization decision for a tool call."""

    allowed: bool
    email: str | None
    tool_group: str | None
    reason: str | None = None


class ToolPolicyService:
    """Authorizes MCP tool calls using optional DB-backed policy."""

    def __init__(
        self,
        store: ToolPolicyStore | None,
        *,
        restricted_tool_groups: frozenset[str] = RESTRICTED_TOOL_GROUPS,
    ):
        self._store = store
        self._restricted_tool_groups = restricted_tool_groups

    @property
    def has_policy_store(self) -> bool:
        return self._store is not None

    async def authorize(
        self,
        *,
        email: str | None,
        tool_name: str,
        now: datetime | None = None,
    ) -> ToolPolicyDecision:
        tool_group = get_tool_group_for_tool(tool_name)
        is_restricted = tool_group in self._restricted_tool_groups

        if self._store is None:
            if is_restricted:
                return ToolPolicyDecision(
                    allowed=False,
                    email=email,
                    tool_group=tool_group,
                    reason="Restricted tool group requires WORKSPACE_MCP_POLICY_DATABASE_URL.",
                )
            return ToolPolicyDecision(allowed=True, email=email, tool_group=tool_group)

        if not email:
            return ToolPolicyDecision(
                allowed=False,
                email=email,
                tool_group=tool_group,
                reason="Authenticated Google user email is required.",
            )

        normalized_email = email.strip().lower()
        domain = extract_email_domain(normalized_email)
        if not domain:
            return ToolPolicyDecision(
                allowed=False,
                email=normalized_email,
                tool_group=tool_group,
                reason="Authenticated Google user email is invalid.",
            )

        try:
            # Both lookups share one budget, including pool waits and normal cleanup.
            return await _policy_db_call(
                self._check_policy(normalized_email, domain, tool_group, now)
            )
        except PolicyDatabaseError as exc:
            logger.warning(
                "MCP policy lookup failed for user=%s tool_group=%s",
                normalized_email,
                tool_group,
            )
            return ToolPolicyDecision(
                allowed=False,
                email=normalized_email,
                tool_group=tool_group,
                reason=str(exc),
            )

    async def _check_policy(
        self, email: str, domain: str, tool_group: str | None, now: datetime | None
    ) -> ToolPolicyDecision:
        assert self._store is not None
        if not await self._store.is_domain_allowed(domain):
            return ToolPolicyDecision(
                allowed=False,
                email=email,
                tool_group=tool_group,
                reason=f"Domain '{domain}' is not authorized for MCP access.",
            )

        if tool_group is not None and tool_group in self._restricted_tool_groups:
            check_time = now or datetime.now(timezone.utc)
            allowed = await self._store.is_user_tool_allowed(
                email, tool_group, now=check_time
            )
            if not allowed:
                return ToolPolicyDecision(
                    allowed=False,
                    email=email,
                    tool_group=tool_group,
                    reason=f"User is not authorized for tool group '{tool_group}'.",
                )

        return ToolPolicyDecision(allowed=True, email=email, tool_group=tool_group)


class ToolPolicyMiddleware(Middleware):
    """FastMCP middleware that enforces DB-backed MCP tool policy."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = getattr(context.message, "name", None)
        if not tool_name:
            return await call_next(context)

        email = await resolve_authenticated_email(context.fastmcp_context)
        decision = await get_tool_policy_service().authorize(
            email=email,
            tool_name=tool_name,
        )
        if not decision.allowed:
            logger.info(
                "Denied MCP tool call user=%s tool=%s tool_group=%s reason=%s",
                decision.email,
                tool_name,
                decision.tool_group,
                decision.reason,
            )
            raise ToolError(f"Access denied: {decision.reason}")

        return await call_next(context)


def get_tool_group_for_tool(tool_name: str) -> str | None:
    """Return the stable authorization group for a tool name."""
    return TOOL_GROUP_BY_TOOL_NAME.get(tool_name)


def extract_email_domain(email: str | None) -> str | None:
    """Extract and normalize the domain from an email address."""
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if not local or not domain:
        return None
    return domain.lower()


async def resolve_authenticated_email(fastmcp_context: Any | None) -> str | None:
    """Resolve the authenticated Google email for policy checks."""
    if fastmcp_context is not None:
        try:
            authenticated = await fastmcp_context.get_state("authenticated_user_email")
            if authenticated:
                return str(authenticated)
        except Exception:
            logger.debug("Could not read authenticated email from FastMCP context")

    return None


_policy_service: ToolPolicyService | None = None


def get_tool_policy_service() -> ToolPolicyService:
    """Return the process-wide tool policy service."""
    global _policy_service
    if _policy_service is None:
        database_url = os.getenv("WORKSPACE_MCP_POLICY_DATABASE_URL", "").strip()
        store = AsyncpgToolPolicyStore(database_url) if database_url else None
        _policy_service = ToolPolicyService(store)
    return _policy_service


def set_tool_policy_service(service: ToolPolicyService | None) -> None:
    """Replace the process-wide policy service for tests."""
    global _policy_service
    _policy_service = service
