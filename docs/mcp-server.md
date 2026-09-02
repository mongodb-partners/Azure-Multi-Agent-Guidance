# Self-hosted MongoDB MCP Server (Azure Container Apps)

The agents retrieve data from MongoDB through a **self-hosted** MongoDB MCP server — the official
[`mongodb/mongodb-mcp-server`](https://github.com/mongodb-js/mongodb-mcp-server) image running as an
**Azure Container App** (`deploy/mcp.bicep`). This replaces the previous **Atlas Managed Remote MCP**
(`https://mcp.mongodb.com`, OAuth) — there is no OAuth token to mint/refresh and no
`remote-atlas-connect` handshake.

No MCP URL or credential is hardcoded — all values come from `.env` / `.env.live` (see `.env.sample`).

## How it works

- The MCP container is started with the **MongoDB connection string baked in**
  (`MDB_MCP_CONNECTION_STRING`, sourced from Key Vault). It therefore holds a single **preconfigured
  connection** to the cluster, and the agents **never handle DB credentials**.
- Transport is **Streamable HTTP** (`MDB_MCP_TRANSPORT=http`) on `<fqdn>/mcp`.
- The agents attach the MCP as a Foundry Responses `mcp` tool (`src/mcp_client.py`) with
  `require_approval="never"` and an `allowed_tools` allow-list. Because the connection is
  preconfigured, tool calls (`find`, `aggregate`, `count`, `list-collections`, `collection-schema`)
  run directly with a `database` + `collection` argument — no connect step.

```
agent turn ─▶ mcp_tool_dict() ─▶ { server_url: <fqdn>/mcp, headers: { X-MCP-Api-Key: … } }
                                        │  Foundry / Responses runtime calls the MCP over HTTPS
                                        ▼
              self-hosted mongodb/mongodb-mcp-server (Azure Container Apps)
                                        │  preconfigured MDB_MCP_CONNECTION_STRING
                                        ▼
                                 MongoDB Atlas cluster
```

## Authentication (API-key header)

The MCP endpoint is exposed with **external ingress** so both the in-cluster API and any
**out-of-environment Foundry hosted agents** can reach it. Access is gated by a shared **API-key
header**:

- The server validates an inbound header via `MDB_MCP_HTTP_HEADERS` (a JSON object of required
  header name → value). `deploy/mcp.bicep` sets this to `{"<MCP_API_KEY_HEADER>":"<MCP_API_KEY>"}`.
- The header **name is not `Authorization`** (`X-MCP-Api-Key` by default). This matters: the Foundry
  runtime **rejects** registering an MCP tool that carries an `Authorization` header, but accepts a
  custom header — so the same tool works on inline *and* registered/hosted agents.
- The key is a **static, long-lived** value (unlike the old ~1h OAuth token), so the registered
  Foundry path no longer breaks with a `424 Failed Dependency` when a token expires.
- `deploy.sh` **generates** the key (`openssl rand -hex 24`) if you don't supply one, stores it in
  **Key Vault** (`mcp-api-key`), injects it into the MCP container (`MDB_MCP_HTTP_HEADERS`) and the
  API container (`MCP_API_KEY`), and writes the header name to `.env.live`.

> Leave `MCP_API_KEY` empty for an **unauthenticated** MCP (only appropriate for local dev or an
> internal-ingress deployment). See the **Security** section in the top-level README for the
> hardening trade-offs.

## Safety defaults

`deploy/mcp.bicep` runs the server hardened:

| Setting | Value | Effect |
|---|---|---|
| `MDB_MCP_READ_ONLY` | `true` | No write tools exposed (mirrors `MONGODB_ALLOW_WRITE=false`). |
| `MDB_MCP_DISABLE_SERVER_SIDE_JS` | `true` | Blocks `$where` / `$function` / `$accumulator` injection. |
| `MDB_MCP_CONNECTION_SCOPE` | `session` | Concurrent HTTP clients get isolated connections. |

## Local development

Run the MCP in Docker and point the app at it:

```bash
docker run --rm -p 3000:3000 \
  -e MDB_MCP_CONNECTION_STRING="$MONGODB_URI" \
  -e MDB_MCP_TRANSPORT=http -e MDB_MCP_HTTP_HOST=0.0.0.0 -e MDB_MCP_HTTP_PORT=3000 \
  -e MDB_MCP_READ_ONLY=true \
  mongodb/mongodb-mcp-server:latest
# .env:
#   MONGODB_MCP_URL=http://localhost:3000/mcp
#   MCP_API_KEY=            (empty → no header required locally)
```

## In deployment

- **API / UI stack (`deploy.sh` / `main.bicep` / `mcp.bicep`)** — the MCP Container App reads
  `MDB_MCP_CONNECTION_STRING` from Key Vault via the user-assigned identity; the API reads the MCP
  URL + `MCP_API_KEY` (from Key Vault). All stack-owned (torn down by `destroy.sh`).
- **Foundry hosted agents (`provision_hosted.sh` / `provision_agents_hosted.sh`)** — `MONGODB_MCP_URL`
  + `MCP_API_KEY` + `MCP_API_KEY_HEADER` are passed into each hosted container via
  `scripts/provision_hosted.py`.

## Config reference

| Setting (`.env`) | Default | Purpose |
|---|---|---|
| `MONGODB_MCP_URL` | `http://localhost:3000/mcp` (local) | Self-hosted MCP endpoint (Streamable HTTP). Cloud value is written to `.env.live`. |
| `MCP_API_KEY` | — | Shared API-key value the MCP validates (empty = unauthenticated). Stored in Key Vault in the cloud. |
| `MCP_API_KEY_HEADER` | `X-MCP-Api-Key` | Header NAME the MCP validates (non-`Authorization`). |
| `MONGODB_ALLOW_WRITE` | `false` | Exposes write tools in the agent tool surface (the MCP itself is also read-only by default). |

## Files

| Path | Role |
|------|------|
| `deploy/mcp.bicep` | The MongoDB MCP Container App (image, ingress, env, API-key header, read-only). |
| `src/mcp_client.py` | Builds the Responses `mcp` tool spec with the API-key header. |
| `src/settings.py` | `mongodb_mcp_url`, `mcp_api_key`, `mcp_api_key_header`. |

## Troubleshooting

- **`/health` shows `mcp_configured: false`** → set `MONGODB_MCP_URL` (or run `deploy.sh` and use
  `.env.live`).
- **`424 Failed Dependency` / MCP tool rejected** → the MCP endpoint is unreachable or the API-key
  header is missing/incorrect. Check `MONGODB_MCP_URL` points at the running Container App and that
  `MCP_API_KEY` / `MCP_API_KEY_HEADER` match what the MCP was deployed with.
- **Agent replies "no relevant information in the MongoDB database"** → the grounding contract is
  working; verify the MCP's connection string targets the seeded `multi-agent` database.
