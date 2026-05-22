"""PostgreSQL MCP tools."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastmcp.exceptions import ToolError as ToolExecutionError
from fastmcp.server.dependencies import get_context
from mcp.types import ToolAnnotations
from pydantic import Field

from auth.tool_policy import resolve_authenticated_email
from core.server import server

logger = logging.getLogger(__name__)

DEFAULT_SQL_MAX_ROWS = 100
DEFAULT_SQL_STATEMENT_TIMEOUT_MS = 5000
ABSOLUTE_SQL_MAX_ROWS = 1000
ABSOLUTE_SQL_STATEMENT_TIMEOUT_MS = 30000


class SqlConfigurationError(ValueError):
    """Raised for invalid SQL tool configuration."""


class AsyncpgSqlRunner:
    """Executes SQL queries through asyncpg."""

    def __init__(self, database_url: str, *, config_name: str = "database_url"):
        if not database_url.strip():
            raise SqlConfigurationError(f"{config_name} is required.")
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
                    max_size=int(os.getenv("WORKSPACE_MCP_SQL_POOL_MAX_SIZE", "5")),
                )
            except Exception as exc:  # pragma: no cover - exact driver errors vary
                raise ToolExecutionError(str(exc)) from exc
            return self._pool

    async def run_select_query(
        self,
        query: str,
        *,
        max_rows: int,
        statement_timeout_ms: int,
    ) -> tuple[list[str], list[dict[str, Any]], bool]:
        wrapped_query = f"select * from ({query}) as mcp_readonly_query limit $1"
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "select set_config('statement_timeout', $1, true)",
                        f"{statement_timeout_ms}ms",
                    )
                    statement = await conn.prepare(wrapped_query)
                    columns = [attr.name for attr in statement.get_attributes()]
                    records = await statement.fetch(max_rows + 1)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc

        truncated = len(records) > max_rows
        visible_records = records[:max_rows]
        rows = [
            {column: _serialize_value(record[column]) for column in columns}
            for record in visible_records
        ]
        return columns, rows, truncated

    async def run_insert_query(
        self,
        query: str,
        *,
        max_rows: int,
        statement_timeout_ms: int,
    ) -> tuple[str | None, list[str], list[dict[str, Any]], bool]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "select set_config('statement_timeout', $1, true)",
                        f"{statement_timeout_ms}ms",
                    )
                    statement = await conn.prepare(query)
                    columns = [attr.name for attr in statement.get_attributes()]
                    records = await statement.fetch()
                    get_status = getattr(statement, "get_statusmsg", None)
                    status = get_status() if get_status else None
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc

        truncated = len(records) > max_rows
        visible_records = records[:max_rows]
        rows = [
            {column: _serialize_value(record[column]) for column in columns}
            for record in visible_records
        ]
        return status, columns, rows, truncated


_select_sql_runner: AsyncpgSqlRunner | None = None
_insert_sql_runner: AsyncpgSqlRunner | None = None


def get_select_sql_runner() -> AsyncpgSqlRunner:
    """Return the process-wide SELECT SQL runner."""
    global _select_sql_runner
    if _select_sql_runner is None:
        config_name = "WORKSPACE_MCP_SQL_SELECT_DATABASE_URL"
        database_url = os.getenv(config_name, "").strip()
        _select_sql_runner = AsyncpgSqlRunner(database_url, config_name=config_name)
    return _select_sql_runner


def get_insert_sql_runner() -> AsyncpgSqlRunner:
    """Return the process-wide INSERT SQL runner."""
    global _insert_sql_runner
    if _insert_sql_runner is None:
        config_name = "WORKSPACE_MCP_SQL_INSERT_DATABASE_URL"
        database_url = os.getenv(config_name, "").strip()
        _insert_sql_runner = AsyncpgSqlRunner(database_url, config_name=config_name)
    return _insert_sql_runner


def set_select_sql_runner(runner: AsyncpgSqlRunner | None) -> None:
    """Replace the process-wide SELECT SQL runner for tests."""
    global _select_sql_runner
    _select_sql_runner = runner


def set_insert_sql_runner(runner: AsyncpgSqlRunner | None) -> None:
    """Replace the process-wide INSERT SQL runner for tests."""
    global _insert_sql_runner
    _insert_sql_runner = runner


@server.tool(
    name="selectSql",
    title="Select SQL",
    description="Runs a PostgreSQL query through the configured SELECT role and returns capped rows.",
    tags={"beta"},
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def select_sql(
    query: Annotated[
        str,
        Field(
            description="PostgreSQL query to execute verbatim using WORKSPACE_MCP_SQL_SELECT_DATABASE_URL.",
        ),
    ],
    max_rows: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=ABSOLUTE_SQL_MAX_ROWS,
            description="Optional row cap, limited by WORKSPACE_MCP_SQL_MAX_ROWS.",
        ),
    ] = None,
) -> dict[str, Any]:
    """Run a PostgreSQL query through the configured SELECT role."""
    effective_max_rows = _resolve_effective_max_rows(max_rows)
    statement_timeout_ms = _get_positive_int_env(
        "WORKSPACE_MCP_SQL_STATEMENT_TIMEOUT_MS",
        DEFAULT_SQL_STATEMENT_TIMEOUT_MS,
        maximum=ABSOLUTE_SQL_STATEMENT_TIMEOUT_MS,
    )
    user_email = await _get_current_user_email()
    query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    started = time.perf_counter()

    columns, rows, truncated = await get_select_sql_runner().run_select_query(
        query,
        max_rows=effective_max_rows,
        statement_timeout_ms=statement_timeout_ms,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    logger.info(
        "selectSql completed user=%s tool_group=sql query_digest=%s duration_ms=%s row_count=%s truncated=%s",
        user_email,
        query_digest,
        duration_ms,
        len(rows),
        truncated,
    )

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "execution_time_ms": duration_ms,
    }


@server.tool(
    name="insertSql",
    title="Insert SQL",
    description="Runs a PostgreSQL query through the configured INSERT role and returns status plus capped rows.",
    tags={"beta"},
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def insert_sql(
    query: Annotated[
        str,
        Field(
            description="PostgreSQL query to execute verbatim using WORKSPACE_MCP_SQL_INSERT_DATABASE_URL.",
        ),
    ],
    max_rows: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            le=ABSOLUTE_SQL_MAX_ROWS,
            description="Optional cap for rows returned by INSERT ... RETURNING, limited by WORKSPACE_MCP_SQL_MAX_ROWS.",
        ),
    ] = None,
) -> dict[str, Any]:
    """Run a PostgreSQL query through the configured INSERT role."""
    effective_max_rows = _resolve_effective_max_rows(max_rows)
    statement_timeout_ms = _get_positive_int_env(
        "WORKSPACE_MCP_SQL_STATEMENT_TIMEOUT_MS",
        DEFAULT_SQL_STATEMENT_TIMEOUT_MS,
        maximum=ABSOLUTE_SQL_STATEMENT_TIMEOUT_MS,
    )
    user_email = await _get_current_user_email()
    query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    started = time.perf_counter()

    status, columns, rows, truncated = await get_insert_sql_runner().run_insert_query(
        query,
        max_rows=effective_max_rows,
        statement_timeout_ms=statement_timeout_ms,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    logger.info(
        "insertSql completed user=%s tool_group=sql query_digest=%s duration_ms=%s row_count=%s truncated=%s status=%s",
        user_email,
        query_digest,
        duration_ms,
        len(rows),
        truncated,
        status,
    )

    return {
        "status": status,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "execution_time_ms": duration_ms,
    }


def _resolve_effective_max_rows(requested_max_rows: int | None) -> int:
    configured_max_rows = _get_positive_int_env(
        "WORKSPACE_MCP_SQL_MAX_ROWS",
        DEFAULT_SQL_MAX_ROWS,
        maximum=ABSOLUTE_SQL_MAX_ROWS,
    )
    if requested_max_rows is None:
        return configured_max_rows
    return min(requested_max_rows, configured_max_rows)


def _get_positive_int_env(name: str, default: int, *, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ToolExecutionError(f"{name} must be a positive integer.") from exc
    if value < 1:
        raise ToolExecutionError(f"{name} must be a positive integer.")
    return min(value, maximum)


async def _get_current_user_email() -> str | None:
    try:
        context = get_context()
    except Exception:
        context = None
    if context is None:
        return os.getenv("USER_GOOGLE_EMAIL")
    return await resolve_authenticated_email(context, {})


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, datetime_time, Decimal, UUID)):
        return str(value)
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    return str(value)
