from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError

import core.tool_registry as registry
import sql.sql_tools as sql_tools
from sql.sql_tools import (
    AsyncpgSqlRunner,
    _serialize_value,
    get_select_sql_runner,
    select_sql,
    set_select_sql_runner,
    sql_help,
)


def test_serialize_value_converts_non_json_scalars():
    value = {
        "created": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "day": date(2026, 1, 2),
        "amount": Decimal("12.34"),
        "id": UUID("11111111-1111-1111-1111-111111111111"),
    }

    serialized = _serialize_value(value)

    assert serialized == {
        "created": "2026-01-02 03:04:05+00:00",
        "day": "2026-01-02",
        "amount": "12.34",
        "id": "11111111-1111-1111-1111-111111111111",
    }


def test_select_connection(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SQL_SELECT_DATABASE_URL", "postgresql://select")
    set_select_sql_runner(None)

    try:
        select_runner = get_select_sql_runner()
    finally:
        set_select_sql_runner(None)

    assert select_runner._database_url == "postgresql://select"


@pytest.fixture
def sql_server():
    # Copy registrations so read-only filtering does not mutate the shared server.
    isolated = FastMCP(name="sql_test")
    for name, tool in registry.get_tool_components(sql_tools.server).items():
        if name.startswith("sql_"):
            isolated.add_tool(tool)
    return isolated


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [False, True])
async def test_sql_discovery(sql_server, monkeypatch, read_only):
    monkeypatch.setattr(registry, "get_enabled_tools", lambda: None)
    monkeypatch.setattr(registry, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(registry, "is_read_only_mode", lambda: read_only)
    monkeypatch.setattr(registry, "is_permissions_mode", lambda: False)

    registry.filter_server_tools(sql_server)
    tools = await sql_server.list_tools(run_middleware=False)

    assert {tool.name for tool in tools} == {"sql_select", "sql_help"}


@pytest.mark.asyncio
async def test_insert_cannot_execute(sql_server, monkeypatch):
    get_pool = AsyncMock(side_effect=AssertionError("SQL must not execute"))
    monkeypatch.setattr(AsyncpgSqlRunner, "_get_pool", get_pool)

    with pytest.raises(NotFoundError, match="sql_insert"):
        await sql_server.call_tool(
            "sql_insert",
            {"query": "insert into accounts default values"},
            run_middleware=False,
        )

    get_pool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_error", [None, RuntimeError("No request context")])
async def test_audit_no_env_identity(monkeypatch, context_error):
    def get_context():
        if context_error is not None:
            raise context_error
        return None

    monkeypatch.setattr(sql_tools, "get_context", get_context)
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "configured@example.com")

    assert await sql_tools._get_current_user_email() is None


@pytest.mark.asyncio
async def test_audit_context_identity(monkeypatch):
    context = SimpleNamespace()
    resolver = AsyncMock(return_value="authenticated@example.com")
    monkeypatch.setattr(sql_tools, "get_context", lambda: context)
    monkeypatch.setattr(sql_tools, "resolve_authenticated_email", resolver)

    assert await sql_tools._get_current_user_email() == "authenticated@example.com"
    resolver.assert_awaited_once_with(context)


class FakeAcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.conn.released = True
        return False


class FakeTransactionContext:
    def __init__(self, conn, readonly):
        self.conn = conn
        self.readonly = readonly

    async def __aenter__(self):
        self.conn.readonly_values.append(self.readonly)
        self.conn.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.conn.in_transaction = False
        self.conn.transaction_error = exc_type
        self.conn.transaction_closed = True
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquireContext(self.conn)


class FakeAttribute:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    async def fetch(self, limit):
        assert self.conn.in_transaction
        self.conn.fetch_limit = limit
        if self.conn.fetch_error is not None:
            raise RuntimeError(self.conn.fetch_error)
        return self.conn.records[:limit]


class FakeStatement:
    def __init__(self, conn):
        self.conn = conn

    def get_attributes(self):
        return [FakeAttribute(column) for column in self.conn.columns]

    async def cursor(self):
        assert self.conn.in_transaction
        self.conn.cursor_opened = True
        return FakeCursor(self.conn)


class FakeConnection:
    def __init__(
        self,
        *,
        columns=None,
        records=None,
        fetch_error=None,
    ):
        self.columns = columns or ["id", "name"]
        self.records = (
            records
            if records is not None
            else [
                {"id": 1, "name": "alpha"},
                {"id": 2, "name": "beta"},
                {"id": 3, "name": "gamma"},
            ]
        )
        self.fetch_error = fetch_error
        self.readonly_values = []
        self.executed = []
        self.prepared_query = None
        self.fetch_limit = None
        self.in_transaction = False
        self.cursor_opened = False
        self.transaction_closed = False
        self.transaction_error = None
        self.released = False

    def transaction(self, *, readonly=False):
        return FakeTransactionContext(self, readonly)

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def prepare(self, query):
        self.prepared_query = query
        return FakeStatement(self)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        " select id, name from accounts; ",
        "select id, name from accounts -- trailing comment",
        "select id, name from accounts; -- trailing comment",
        "select id, name from accounts /* trailing comment */",
        "select id, name from accounts; /* trailing comment */",
        "select '; -- /* */' as name",
    ],
)
async def test_select_preserves_sql(query):
    conn = FakeConnection()
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)

    columns, rows, truncated = await runner.run_select_query(
        query,
        max_rows=2,
        statement_timeout_ms=750,
    )

    assert conn.readonly_values == [True]
    assert conn.executed == [
        ("select set_config('statement_timeout', $1, true)", ("750ms",))
    ]
    assert conn.prepared_query == query
    assert conn.cursor_opened is True
    assert conn.fetch_limit == 3
    assert columns == ["id", "name"]
    assert rows == [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    assert truncated is True
    assert conn.transaction_closed is True
    assert conn.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("row_count", [0, 1, 2, 3, 100])
async def test_select_row_bounds(row_count):
    records = [{"id": index} for index in range(row_count)]
    conn = FakeConnection(columns=["id"], records=records)
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)

    columns, rows, truncated = await runner.run_select_query(
        "select id from accounts",
        max_rows=2,
        statement_timeout_ms=750,
    )

    assert conn.fetch_limit == 3
    assert columns == ["id"]
    assert rows == records[:2]
    assert truncated is (row_count > 2)
    assert conn.transaction_closed is True
    assert conn.released is True


@pytest.mark.asyncio
async def test_select_error_cleanup():
    conn = FakeConnection(fetch_error="permission denied for table accounts")
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)

    with pytest.raises(ToolError, match="permission denied for table accounts"):
        await runner.run_select_query(
            "delete from accounts",
            max_rows=2,
            statement_timeout_ms=750,
        )

    assert conn.transaction_closed is True
    assert conn.transaction_error is RuntimeError
    assert conn.released is True


class FakeSelectRunner:
    def __init__(self):
        self.calls = []

    async def run_select_query(self, query, *, max_rows, statement_timeout_ms):
        self.calls.append(
            {
                "query": query,
                "max_rows": max_rows,
                "statement_timeout_ms": statement_timeout_ms,
            }
        )
        return ["id"], [{"id": 1}], False


@pytest.mark.asyncio
async def test_select_sql_passes_query_verbatim_and_returns_payload(monkeypatch):
    runner = FakeSelectRunner()
    set_select_sql_runner(runner)
    monkeypatch.setenv("WORKSPACE_MCP_SQL_MAX_ROWS", "5")
    monkeypatch.setenv("WORKSPACE_MCP_SQL_STATEMENT_TIMEOUT_MS", "250")
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "user@example.com")
    monkeypatch.setattr("sql.sql_tools.get_context", lambda: None)
    query = " drop table accounts "

    try:
        result = await select_sql(query, max_rows=10)
    finally:
        set_select_sql_runner(None)

    assert runner.calls == [
        {
            "query": query,
            "max_rows": 5,
            "statement_timeout_ms": 250,
        }
    ]
    assert result["columns"] == ["id"]
    assert result["rows"] == [{"id": 1}]
    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert isinstance(result["execution_time_ms"], float)


@pytest.mark.asyncio
async def test_sql_help_returns_placeholder_message():
    assert await sql_help() == "INSERT_PLACEHOLDER"
