import json
from types import SimpleNamespace

import pytest

from core import cli


class FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "PostgreSQL query to execute verbatim.",
                    }
                },
            },
            "annotations": {"readOnlyHint": self.name == "sql_select"},
        }


class FakeClient:
    def __init__(self, url: str, *, auth: object) -> None:
        self.url = url
        self.auth = auth
        self.initialize_result = SimpleNamespace(
            model_dump=lambda mode: {
                "serverInfo": {"name": "workspace-mcp"},
                "instructions": "Use the published tool schemas.",
            }
        )

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def list_tools(self) -> list[FakeTool]:
        return [
            FakeTool("sql_select", "Runs a PostgreSQL query."),
            FakeTool("sql_insert", "Runs an INSERT query."),
            FakeTool("sql_help", "Returns SQL tool guidance."),
        ]


@pytest.mark.asyncio
async def test_list_tools_json_prints_llm_visible_metadata(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_build_oauth", lambda: object())
    monkeypatch.setattr(cli, "Client", FakeClient)

    await cli._list_tools("http://example.test/mcp", json_output=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["initialize"]["serverInfo"]["name"] == "workspace-mcp"
    assert [tool["name"] for tool in payload["tools"]] == ["sql_select", "sql_insert", "sql_help"]
    assert payload["tools"][0]["inputSchema"]["properties"]["query"]["type"] == "string"
    assert payload["tools"][0]["annotations"]["readOnlyHint"] is True


@pytest.mark.asyncio
async def test_list_tools_json_can_filter_to_single_tool(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_build_oauth", lambda: object())
    monkeypatch.setattr(cli, "Client", FakeClient)

    await cli._list_tools("http://example.test/mcp", json_output=True, tool_name="sql_select")

    payload = json.loads(capsys.readouterr().out)
    assert [tool["name"] for tool in payload["tools"]] == ["sql_select"]


@pytest.mark.asyncio
async def test_list_tools_text_output_remains_compact(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_build_oauth", lambda: object())
    monkeypatch.setattr(cli, "Client", FakeClient)

    await cli._list_tools("http://example.test/mcp")

    output = capsys.readouterr().out
    assert "sql_select" in output
    assert "Runs a PostgreSQL query." in output
    assert "3 tools available" in output


@pytest.mark.asyncio
async def test_list_tools_filter_errors_when_tool_is_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_build_oauth", lambda: object())
    monkeypatch.setattr(cli, "Client", FakeClient)

    with pytest.raises(SystemExit) as exc:
        await cli._list_tools("http://example.test/mcp", tool_name="missingTool")

    assert exc.value.code == 1
    assert "missingTool" in capsys.readouterr().err
