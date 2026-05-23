from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from fastmcp.exceptions import ToolError

from sql.sql_tools import (
    AsyncpgSqlRunner,
    _serialize_value,
    get_insert_sql_runner,
    get_select_sql_runner,
    insert_sql,
    select_sql,
    set_insert_sql_runner,
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


def test_sql_runners_use_separate_connection_strings(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_SQL_SELECT_DATABASE_URL", "postgresql://select")
    monkeypatch.setenv("WORKSPACE_MCP_SQL_INSERT_DATABASE_URL", "postgresql://insert")
    set_select_sql_runner(None)
    set_insert_sql_runner(None)

    try:
        select_runner = get_select_sql_runner()
        insert_runner = get_insert_sql_runner()
    finally:
        set_select_sql_runner(None)
        set_insert_sql_runner(None)

    assert select_runner._database_url == "postgresql://select"
    assert insert_runner._database_url == "postgresql://insert"


class FakeAcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class FakeTransactionContext:
    def __init__(self, conn, readonly):
        self.conn = conn
        self.readonly = readonly

    async def __aenter__(self):
        self.conn.readonly_values.append(self.readonly)
        return self

    async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquireContext(self.conn)


class FakeAttribute:
    def __init__(self, name):
        self.name = name


class FakeStatement:
    def __init__(self, conn):
        self.conn = conn

    def get_attributes(self):
        return [FakeAttribute(column) for column in self.conn.columns]

    async def fetch(self, limit=None):
        self.conn.fetch_limit = limit
        if self.conn.fetch_error is not None:
            raise RuntimeError(self.conn.fetch_error)
        if limit is None:
            return self.conn.records
        return self.conn.records[:limit]

    def get_statusmsg(self):
        return self.conn.status


class FakeConnection:
    def __init__(
        self,
        *,
        columns=None,
        records=None,
        status=None,
        fetch_error=None,
    ):
        self.columns = columns or ["id", "name"]
        self.records = records or [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "beta"},
            {"id": 3, "name": "gamma"},
        ]
        self.status = status
        self.fetch_error = fetch_error
        self.readonly_values = []
        self.executed = []
        self.prepared_query = None
        self.fetch_limit = None

    def transaction(self, *, readonly=False):
        return FakeTransactionContext(self, readonly)

    async def execute(self, query, *args):
        self.executed.append((query, args))

    async def prepare(self, query):
        self.prepared_query = query
        return FakeStatement(self)


@pytest.mark.asyncio
async def test_asyncpg_sql_runner_select_wraps_query_verbatim_and_caps_returned_rows():
    conn = FakeConnection()
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)
    query = " select id, name from accounts; "

    columns, rows, truncated = await runner.run_select_query(
        query,
        max_rows=2,
        statement_timeout_ms=750,
    )

    assert conn.readonly_values == [True]
    assert conn.executed == [
        ("select set_config('statement_timeout', $1, true)", ("750ms",))
    ]
    assert conn.prepared_query == (
        f"select * from ({query}) as mcp_readonly_query limit $1"
    )
    assert conn.fetch_limit == 3
    assert columns == ["id", "name"]
    assert rows == [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    assert truncated is True


@pytest.mark.asyncio
async def test_asyncpg_sql_runner_insert_passes_query_verbatim_and_returns_status():
    conn = FakeConnection(
        columns=["id"],
        records=[{"id": 1}, {"id": 2}, {"id": 3}],
        status="INSERT 0 3",
    )
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)
    query = "insert into accounts(name) values ('alpha') returning id"

    status, columns, rows, truncated = await runner.run_insert_query(
        query,
        max_rows=2,
        statement_timeout_ms=750,
    )

    assert conn.readonly_values == [False]
    assert conn.executed == [
        ("select set_config('statement_timeout', $1, true)", ("750ms",))
    ]
    assert conn.prepared_query == query
    assert conn.fetch_limit is None
    assert status == "INSERT 0 3"
    assert columns == ["id"]
    assert rows == [{"id": 1}, {"id": 2}]
    assert truncated is True


@pytest.mark.asyncio
async def test_asyncpg_sql_runner_returns_query_errors_verbatim():
    conn = FakeConnection(fetch_error="permission denied for table accounts")
    runner = AsyncpgSqlRunner("postgresql://example")
    runner._pool = FakePool(conn)

    with pytest.raises(ToolError, match="permission denied for table accounts"):
        await runner.run_select_query(
            "delete from accounts",
            max_rows=2,
            statement_timeout_ms=750,
        )


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


class FakeInsertRunner:
    def __init__(self):
        self.calls = []

    async def run_insert_query(self, query, *, max_rows, statement_timeout_ms):
        self.calls.append(
            {
                "query": query,
                "max_rows": max_rows,
                "statement_timeout_ms": statement_timeout_ms,
            }
        )
        return "INSERT 0 1", ["id"], [{"id": 1}], False


@pytest.mark.asyncio
async def test_insert_sql_uses_separate_runner_and_returns_payload(monkeypatch):
    runner = FakeInsertRunner()
    set_insert_sql_runner(runner)
    monkeypatch.setenv("WORKSPACE_MCP_SQL_MAX_ROWS", "5")
    monkeypatch.setenv("WORKSPACE_MCP_SQL_STATEMENT_TIMEOUT_MS", "250")
    monkeypatch.setenv("USER_GOOGLE_EMAIL", "user@example.com")
    monkeypatch.setattr("sql.sql_tools.get_context", lambda: None)
    query = "insert into accounts(name) values ('alpha') returning id"

    try:
        result = await insert_sql(query, max_rows=10)
    finally:
        set_insert_sql_runner(None)

    assert runner.calls == [
        {
            "query": query,
            "max_rows": 5,
            "statement_timeout_ms": 250,
        }
    ]
    assert result["status"] == "INSERT 0 1"
    assert result["columns"] == ["id"]
    assert result["rows"] == [{"id": 1}]
    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert isinstance(result["execution_time_ms"], float)


@pytest.mark.asyncio
async def test_sql_help_returns_placeholder_message():
    assert await sql_help() == "INSERT_PLACEHOLDER"
