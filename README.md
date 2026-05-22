<!-- mcp-name: io.github.taylorwilsdon/workspace-mcp -->

# Google Workspace MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/workspace-mcp.svg)](https://pypi.org/project/workspace-mcp/)
[![Website](https://img.shields.io/badge/Website-workspacemcp.com-green.svg)](https://workspacemcp.com)

Workspace MCP is a FastMCP-based Python server for Google Workspace. It exposes MCP tools for Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Chat, Tasks, Contacts, Apps Script, and Google Custom Search.

Package name: `workspace-mcp`

Main entrypoints:

- `workspace-mcp` / `uv run main.py`: local or self-hosted MCP server.
- `workspace-cli`: command-line client for a running HTTP MCP endpoint.
- `fastmcp_server.py`: FastMCP Cloud entrypoint. It enforces OAuth 2.1 and stateless mode.

Useful links:

- Docs: <https://workspacemcp.com/docs>
- Quick start: <https://workspacemcp.com/quick-start>
- PyPI: <https://pypi.org/project/workspace-mcp/>

## Quick Start

Run directly from PyPI:

```bash
export GOOGLE_OAUTH_CLIENT_ID="your-client-id"
export GOOGLE_OAUTH_CLIENT_SECRET="your-client-secret"
export OAUTHLIB_INSECURE_TRANSPORT=1

uvx workspace-mcp --tool-tier core
```

Run from this repository:

```bash
uv sync --group dev
uv run main.py --tool-tier core
```

Start HTTP transport for modern MCP clients:

```bash
export GOOGLE_OAUTH_CLIENT_ID="your-client-id"
export GOOGLE_OAUTH_CLIENT_SECRET="your-client-secret"
export MCP_ENABLE_OAUTH21=true
export OAUTHLIB_INSECURE_TRANSPORT=1

uvx workspace-mcp --transport streamable-http --tool-tier core
```

HTTP transport serves MCP at:

```text
http://localhost:8000/mcp/
```

For public OAuth 2.1 PKCE clients, leave `GOOGLE_OAUTH_CLIENT_SECRET` unset and set a signing key:

```bash
export MCP_ENABLE_OAUTH21=true
export GOOGLE_OAUTH_CLIENT_ID="your-client-id"
export FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY="$(openssl rand -hex 32)"

uvx workspace-mcp --transport streamable-http --tool-tier core
```

## Google Setup

Create an OAuth client in Google Cloud Console and set the credentials with environment variables. Desktop OAuth clients are the simplest local path. Web/confidential clients also work, but their configured redirect URI must match the server callback URL.

Enable only the APIs you need:

- Calendar API
- Google Chat API
- Google Docs API
- Google Drive API
- Google Forms API
- Gmail API
- Google People API
- Google Sheets API
- Google Slides API
- Google Tasks API
- Apps Script API
- Custom Search API

For local OAuth callbacks over `http://`, set:

```bash
export OAUTHLIB_INSECURE_TRANSPORT=1
```

Do not use that setting for production HTTPS deployments.

## Running the Server

### Transports

```bash
# Local stdio transport. This is the default.
uvx workspace-mcp

# Streamable HTTP transport.
uvx workspace-mcp --transport streamable-http

# Same settings from source.
uv run main.py --transport streamable-http
```

`WORKSPACE_MCP_TRANSPORT` can be set to `stdio` or `streamable-http` when command flags are not available.

### Tool Selection

```bash
# Load selected services only.
uv run main.py --tools gmail drive calendar

# Load a tier across all services.
uv run main.py --tool-tier core
uv run main.py --tool-tier extended
uv run main.py --tool-tier complete

# Combine service selection and tiering.
uv run main.py --tools gmail drive --tool-tier extended

# Request read-only scopes and disable write tools.
uv run main.py --read-only

# Service-specific permissions.
uv run main.py --permissions gmail:organize drive:readonly
uv run main.py --permissions gmail:send drive:full --tool-tier core
```

Rules enforced by `main.py`:

- `--permissions` and `--read-only` are mutually exclusive.
- `--permissions` and `--tools` are mutually exclusive.
- `--single-user` is incompatible with OAuth 2.1, stateless mode, and service account mode.
- `WORKSPACE_MCP_TOOLS`, `WORKSPACE_MCP_TOOL_TIER`, `WORKSPACE_MCP_READ_ONLY`, and `WORKSPACE_MCP_PERMISSIONS` provide equivalent env-based configuration.
- Malformed non-empty env values fail closed at startup.

Permission levels:

- Gmail: `readonly`, `organize`, `drafts`, `send`, `full`.
- Tasks: `readonly`, `manage`, `full`.
- Other services: `readonly`, `full`.

### Docker

```bash
docker build -t workspace-mcp .
docker run --rm -p 8000:8000 \
  -e GOOGLE_OAUTH_CLIENT_ID="..." \
  -e GOOGLE_OAUTH_CLIENT_SECRET="..." \
  -e MCP_ENABLE_OAUTH21=true \
  workspace-mcp --transport streamable-http
```

## Authentication Modes

### Legacy OAuth 2.0

This is the default for stdio and local single-user workflows. Tools accept `user_google_email`, start a local callback server when needed, and cache credentials locally.

Use `start_google_auth` only for legacy OAuth 2.0 re-authentication or pre-authentication. It is disabled when OAuth 2.1 is enabled.

### OAuth 2.1

Use OAuth 2.1 for HTTP, bearer-token, multi-user, and hosted deployments:

```bash
export MCP_ENABLE_OAUTH21=true
uv run main.py --transport streamable-http
```

OAuth 2.1 uses FastMCP's Google provider. It supports Dynamic Client Registration for MCP clients by proxying registrations through your fixed Google OAuth client. Public deployments should restrict client redirect URIs:

```bash
export WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS="https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback,http://localhost:*/callback,http://127.0.0.1:*/callback"
```

### Stateless Mode

Stateless mode avoids local file writes and requires OAuth 2.1:

```bash
export MCP_ENABLE_OAUTH21=true
export WORKSPACE_MCP_STATELESS_MODE=true
uv run main.py --transport streamable-http
```

### OAuth Proxy Storage

OAuth proxy storage backends:

- `memory`: explicit in-memory development storage.
- `disk`: persistent single-server storage. Requires `workspace-mcp[disk]` when installing from package extras.
- `valkey`: distributed storage. Requires `workspace-mcp[valkey]`.

```bash
export WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=disk
export WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY=~/.fastmcp/oauth-proxy

export WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=valkey
export WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST=redis.example.com
export WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT=6379
```

Disk and Valkey storage are encrypted with Fernet. The key is derived from `FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY` when set, otherwise from `GOOGLE_OAUTH_CLIENT_SECRET`.

### External OAuth 2.1 Provider

External OAuth mode expects Google bearer tokens from an upstream auth system:

```bash
export MCP_ENABLE_OAUTH21=true
export EXTERNAL_OAUTH21_PROVIDER=true
uv run main.py --transport streamable-http
```

The server validates bearer tokens against Google userinfo and serves OAuth protected-resource metadata. It does not run the local authorize/token/register flow in this mode.

### Service Account Mode

Service account mode uses Google Workspace domain-wide delegation. This can impersonate domain users for the configured scopes, so use it only in controlled Workspace domains.

```bash
export GOOGLE_SERVICE_ACCOUNT_KEY_FILE="/path/to/service-account-key.json"
export USER_GOOGLE_EMAIL="user@yourdomain.com"
uv run main.py

export GOOGLE_SERVICE_ACCOUNT_KEY_JSON='{"type":"service_account","project_id":"...","private_key":"...","client_email":"..."}'
export USER_GOOGLE_EMAIL="user@yourdomain.com"
uv run main.py
```

Service account mode:

- Requires `USER_GOOGLE_EMAIL`.
- Cannot be combined with `MCP_ENABLE_OAUTH21=true`.
- Cannot be combined with `--single-user`.
- Accepts per-request `user_google_email` as the impersonation subject.
- Can restrict impersonation with `DWD_ALLOWED_DOMAINS`.

## Configuration Reference

### Authentication

| Variable | Purpose |
| --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth client ID. Required for OAuth modes. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth client secret. Optional for public OAuth 2.1 PKCE clients. |
| `GOOGLE_CLIENT_SECRET_PATH` | Path to a `client_secret.json` file. |
| `GOOGLE_CLIENT_SECRETS` | Backward-compatible alias for `GOOGLE_CLIENT_SECRET_PATH`. |
| `USER_GOOGLE_EMAIL` | Default user email outside OAuth 2.1. Required for service account mode. |
| `MCP_SINGLE_USER_MODE` | Legacy/plugin single-user flag. Prefer `--single-user` for direct CLI use. |
| `OAUTHLIB_INSECURE_TRANSPORT` | Set to `1` only for local `http://` OAuth callback development. |

### Server

| Variable | Purpose |
| --- | --- |
| `WORKSPACE_MCP_TRANSPORT` | `stdio` or `streamable-http`; used when `--transport` is not passed. |
| `WORKSPACE_MCP_BASE_URI` | Base URI without port. Defaults to `http://localhost`. |
| `WORKSPACE_MCP_PORT` | Main server or callback port. Defaults to `8000`. |
| `WORKSPACE_MCP_PORT_FALLBACK_COUNT` | Number of fallback callback ports to try for legacy stdio OAuth. |
| `PORT` | Overrides `WORKSPACE_MCP_PORT` unless the port resolver has already selected a fallback. |
| `WORKSPACE_MCP_HOST` | HTTP bind host. Defaults to `0.0.0.0`. |
| `WORKSPACE_EXTERNAL_URL` | Public external URL for reverse proxy deployments. |
| `GOOGLE_OAUTH_REDIRECT_URI` | Explicit OAuth callback URL. |
| `OAUTH_CUSTOM_REDIRECT_URIS` | Additional callback URIs. |
| `OAUTH_ALLOWED_ORIGINS` | Additional CORS origins for OAuth endpoints. |
| `WORKSPACE_MCP_HTTP_PORT` | Optional loopback HTTP sidecar for local legacy stdio plus `workspace-cli`. |
| `WORKSPACE_MCP_URL` | Default remote MCP endpoint for `workspace-cli`. |
| `WORKSPACE_ATTACHMENT_DIR` | Downloaded attachment directory. Defaults to `~/.workspace-mcp/attachments/`. |
| `ALLOWED_FILE_DIRS` | Path allowlist for local file reads. Sensitive paths remain blocked. |

### Tool Access

| Variable | Purpose |
| --- | --- |
| `WORKSPACE_MCP_TOOLS` | Comma-separated service list such as `gmail,drive,calendar`. |
| `WORKSPACE_MCP_TOOL_TIER` | `core`, `extended`, or `complete`. |
| `WORKSPACE_MCP_READ_ONLY` | `true`, `1`, or `yes` enables read-only scope mode. |
| `WORKSPACE_MCP_PERMISSIONS` | Space-separated `service:level` entries. |

### OAuth 2.1

| Variable | Purpose |
| --- | --- |
| `MCP_ENABLE_OAUTH21` | Enables OAuth 2.1 mode. |
| `WORKSPACE_MCP_STATELESS_MODE` | Enables stateless mode. Requires OAuth 2.1. |
| `EXTERNAL_OAUTH21_PROVIDER` | Uses external bearer-token auth. Requires OAuth 2.1. |
| `FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY` | Signing/encryption key material. Required for public OAuth 2.1 clients without a client secret. |
| `WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS` | Allowlist for dynamically registered client redirect URIs. |
| `WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND` | `memory`, `disk`, or `valkey`. |
| `WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY` | Disk OAuth proxy storage directory. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST` | Valkey/Redis host. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT` | Valkey/Redis port. Defaults to `6379`; port `6380` auto-enables TLS. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_DB` | Valkey/Redis database. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS` | Override TLS for Valkey/Redis. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USERNAME` | Valkey/Redis username. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PASSWORD` | Valkey/Redis password. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS` | Valkey request timeout. |
| `WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS` | Valkey connection timeout. |

### Credential Store

| Variable | Purpose |
| --- | --- |
| `WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND` | `local_directory` or `gcs`. Defaults to `local_directory`. |
| `WORKSPACE_MCP_CREDENTIALS_DIR` | Local credential directory. |
| `GOOGLE_MCP_CREDENTIALS_DIR` | Backward-compatible alias for `WORKSPACE_MCP_CREDENTIALS_DIR`. |
| `WORKSPACE_MCP_GCS_BUCKET` | Required bucket for the `gcs` backend. |
| `WORKSPACE_MCP_GCS_PREFIX` | Optional object prefix for the `gcs` backend. |
| `WORKSPACE_MCP_GCS_REQUIRE_CMEK` | Require default bucket CMEK at startup. |

### Service Account and Search

| Variable | Purpose |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_KEY_FILE` | Service account JSON key file. |
| `GOOGLE_SERVICE_ACCOUNT_KEY_JSON` | Inline service account JSON key. |
| `DWD_ALLOWED_DOMAINS` | Comma-separated allowlist for domain-wide delegation impersonation. |
| `GOOGLE_PSE_API_KEY` | Programmable Search Engine API key. |
| `GOOGLE_PSE_ENGINE_ID` | Programmable Search Engine ID. |

## Tool Tiers

Tiering is defined in `core/tool_tiers.yaml`. `complete` includes `extended`, and `extended` includes `core`.

Current tiered tool counts:

| Tier | Inclusive tool count | Use case |
| --- | ---: | --- |
| `core` | 43 | Essential search, read, create, and common modify operations. |
| `extended` | 89 | Core plus management, filtering, batch, and advanced operations. |
| `complete` | 119 | Full tiered tool surface. |

If you run without `--tool-tier`, the server imports every registered tool for the selected services. Tiered mode filters to the tools listed below.

### Calendar

- Core: `list_calendars`, `get_events`, `manage_event`
- Extended: `create_calendar`, `query_freebusy`, `manage_out_of_office`, `manage_focus_time`
- Complete: none

### Drive

- Core: `search_drive_files`, `get_drive_file_content`, `get_drive_file_download_url`, `create_drive_file`, `create_drive_folder`, `import_to_google_doc`, `get_drive_shareable_link`
- Extended: `list_drive_items`, `copy_drive_file`, `update_drive_file`, `manage_drive_access`, `set_drive_file_permissions`
- Complete: `get_drive_file_permissions`, `check_drive_file_public_access`

### Gmail

- Core: `search_gmail_messages`, `get_gmail_message_content`, `get_gmail_messages_content_batch`, `send_gmail_message`
- Extended: `get_gmail_attachment_content`, `get_gmail_thread_content`, `modify_gmail_message_labels`, `list_gmail_labels`, `manage_gmail_label`, `draft_gmail_message`, `list_gmail_filters`, `manage_gmail_filter`
- Complete: `get_gmail_threads_content_batch`, `batch_modify_gmail_message_labels`, `start_google_auth`

### Docs

- Core: `get_doc_content`, `create_doc`, `modify_doc_text`
- Extended: `export_doc_to_pdf`, `search_docs`, `find_and_replace_doc`, `list_docs_in_folder`, `insert_doc_elements`, `update_paragraph_style`, `get_doc_as_markdown`, `list_document_comments`, `manage_document_comment`
- Complete: `insert_doc_image`, `update_doc_headers_footers`, `batch_update_doc`, `inspect_doc_structure`, `create_table_with_data`, `debug_table_structure`, `manage_doc_tab`

### Sheets

- Core: `create_spreadsheet`, `read_sheet_values`, `modify_sheet_values`
- Extended: `list_spreadsheets`, `get_spreadsheet_info`, `format_sheet_range`, `list_sheet_tables`
- Complete: `create_sheet`, `append_table_rows`, `resize_sheet_dimensions`, `move_sheet_rows`, `list_spreadsheet_comments`, `manage_spreadsheet_comment`, `manage_conditional_formatting`

### Chat

- Core: `send_message`, `get_messages`, `search_messages`, `create_reaction`
- Extended: `list_spaces`, `download_chat_attachment`
- Complete: none

### Forms

- Core: `create_form`, `get_form`
- Extended: `list_form_responses`
- Complete: `set_publish_settings`, `get_form_response`, `batch_update_form`

### Slides

- Core: `create_presentation`, `get_presentation`
- Extended: `batch_update_presentation`, `get_page`, `get_page_thumbnail`
- Complete: `list_presentation_comments`, `manage_presentation_comment`

### Tasks

- Core: `get_task`, `list_tasks`, `manage_task`
- Extended: none
- Complete: `list_task_lists`, `get_task_list`, `manage_task_list`

### Contacts

- Core: `search_contacts`, `get_contact`, `list_contacts`, `manage_contact`
- Extended: `list_contact_groups`, `get_contact_group`
- Complete: `manage_contacts_batch`, `manage_contact_group`

### Custom Search

- Core: `search_custom`
- Extended: none
- Complete: `get_search_engine_info`

### Apps Script

- Core: `list_script_projects`, `get_script_project`, `get_script_content`, `create_script_project`, `update_script_content`, `run_script_function`, `generate_trigger_code`
- Extended: `manage_deployment`, `list_deployments`, `delete_script_project`, `list_versions`, `create_version`, `get_version`, `list_script_processes`, `get_script_metrics`
- Complete: none

## Client Examples

### Claude Desktop

Run a server instance and connect Claude Desktop through a Connector. See the quick start guide for the current UI flow:

<https://workspacemcp.com/quick-start>

Legacy stdio configuration:

```json
{
  "mcpServers": {
    "google_workspace": {
      "command": "uvx",
      "args": ["workspace-mcp", "--tool-tier", "core"],
      "env": {
        "GOOGLE_OAUTH_CLIENT_ID": "your-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "your-client-secret",
        "OAUTHLIB_INSECURE_TRANSPORT": "1"
      }
    }
  }
}
```

### Claude Code

```bash
uvx workspace-mcp --transport streamable-http --tool-tier core
claude mcp add --transport http workspace-mcp http://localhost:8000/mcp
```

### VS Code

```json
{
  "servers": {
    "google-workspace": {
      "url": "http://localhost:8000/mcp/",
      "type": "http"
    }
  }
}
```

### workspace-cli

`workspace-cli` calls a running HTTP MCP endpoint and caches OAuth tokens with encrypted disk-backed storage.

```bash
uv run workspace-cli list
uv run workspace-cli --url http://localhost:8000/mcp list
uv run workspace-cli call search_gmail_messages query="is:unread" max_results=5

workspace-cli list
workspace-cli --url https://your-server.example.com/mcp list
```

Default endpoint:

```text
http://localhost:8000/mcp
```

Override it with `--url` or `WORKSPACE_MCP_URL`.

## Development

```bash
git clone https://github.com/taylorwilsdon/google_workspace_mcp.git
cd google_workspace_mcp

uv sync --group dev
uv run main.py
uv run pytest
uv run ruff check .
```

Project layout:

```text
google_workspace_mcp/
├── auth/                 # OAuth, service accounts, credential stores, session auth
├── core/                 # FastMCP server, tool registry, config, storage
├── gcalendar/            # Google Calendar tools
├── gchat/                # Google Chat tools
├── gcontacts/            # Google Contacts tools
├── gdocs/                # Google Docs tools and managers
├── gdrive/               # Google Drive tools
├── gforms/               # Google Forms tools
├── gmail/                # Gmail tools
├── gsearch/              # Google Custom Search tools
├── gsheets/              # Google Sheets tools
├── gslides/              # Google Slides tools
├── gtasks/               # Google Tasks tools
├── gappsscript/          # Google Apps Script tools
├── main.py               # Main CLI/server entrypoint
└── fastmcp_server.py     # FastMCP Cloud entrypoint
```

Tool modules register functions with `@server.tool()`. Authentication is usually handled by `@require_google_service()` or `@require_multiple_services()` decorators, which inject Google API clients and enforce scopes.

## Security Notes

- Never commit `.env`, `client_secret.json`, service account keys, credential cache files, or `.credentials/`.
- Treat Workspace content as untrusted input. Emails, Docs, Sheets, and files can contain prompt injection.
- Local file access is restricted by `validate_file_path()`. The managed attachment directory is allowed by default, and sensitive paths such as `.env`, `.ssh/`, `.aws/`, and credential files remain blocked even if `ALLOWED_FILE_DIRS` is broadened.
- Use HTTPS and OAuth 2.1 for public deployments.
- Use `WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS` on public OAuth 2.1 deployments.
- Use least-privilege tool tiers, read-only mode, or granular permissions where possible.
- Service account domain-wide delegation is powerful. Restrict it with Google Admin scopes and `DWD_ALLOWED_DOMAINS`.

## License

MIT. See [LICENSE](LICENSE).
