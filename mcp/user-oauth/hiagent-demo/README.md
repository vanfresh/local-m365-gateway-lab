# Hi Agent demo — user OAuth (MCP)

Manual walkthrough with Microsoft **stand-ins** and a Cloudflare quick tunnel. Auth model: **MCP OAuth** (`authorization_code`). LiteLLM stores Graph access/refresh. Automated proof: [`../e2e`](../e2e).

## What this proves

| Check | Pass signal |
|---|---|
| LiteLLM UI shows three MCPs | `m365`, `agent_a`, `agent_b` |
| MCP OAuth challenge | Seed smoke: `initialize` → HTTP 401 + `WWW-Authenticate` |
| Full OAuth + tool call | [`../e2e`](../e2e) pytest (MCP client login) |
| Hi Agent OAuth (when UI supports it) | Sync/tools after Authorization Code login using URLs below |
| Agent A / Agent B isolation | Separate plugins; A read-only, B send-only |

## Two auth layers (intended)

| Layer | What | When |
|---|---|---|
| Gateway | LiteLLM virtual key (optional; prefer `x-litellm-api-key` when the client supports it) | Admission |
| Upstream | User OAuth → Graph token stored in LiteLLM | Sync / tools after login |

**Hi Agent Integrate MCP today** often only offers No Auth / Basic / Bearer / API Key (no OAuth). Until OAuth exists on that form, use **`../e2e`** to prove login. Do **not** treat Bearer + primed tokens as this lab’s design.

## Start

```sh
cd mcp/user-oauth/hiagent-demo
chmod +x up.sh down.sh
./up.sh
```

Stop e2e first if ports clash: `cd ../e2e && docker compose down`.

`up.sh` starts the stack, tunnel, recreates LiteLLM with `PROXY_BASE_URL`, seeds MCP servers (**no Graph token priming**), and prints OAuth URLs.

## Integrate with Hi Agent (OAuth-first)

1. Run `./up.sh`; copy tunnel URLs from the banner / `.tunnel-url`.
2. LiteLLM UI: [http://127.0.0.1:4100/ui](http://127.0.0.1:4100/ui) — master key `sk-local-master` — confirm `m365`, `agent_a`, `agent_b`.
3. **Integrate MCP Plugin** → Streamable HTTP → URL e.g. `https://<tunnel>/agent_a/mcp`.
4. When the product supports it, set **OAuth2 / Authorization Code**:

| Field | Value |
|---|---|
| grant_type | Authentication Code |
| client_id | `litellm-gateway` |
| client_secret | `litellm-gateway-secret` |
| redirect_url | Hi Agent callback (leave default) |
| auth_url | `https://<tunnel>/login/authorize` |
| access_token_url | `https://<tunnel>/login/token` |
| refresh_token_url | same as access_token_url (or blank if optional) |
| scope | `openid offline_access Mail.Read Mail.Send Sites.Read.All` |
| auth_content_type | `application/x-www-form-urlencoded` |

5. Synchronize tools → complete browser login on the stand-in if prompted.
6. Create agents; attach **only** that agent’s plugin (`agent_a` vs `agent_b`).

If Integrate MCP has no OAuth: wait for product support, or run `cd ../e2e && ./run.sh`.

## Agent instructions + test prompts

### Agent A (read only)

Attach **only** `agent_a`. Instruction:

```text
You are Agent A, a mail-read assistant. You may only use MCP tools from the agent_a plugin.
For reading mail, call list-mail-folder-messages with arguments exactly:
  {"mailFolderId": "folder-x"}
You have no send-mail tool. Refuse send requests.
```

User test:

```text
Call list-mail-folder-messages with mailFolderId = "folder-x". Prefer no other parameters.
Return the message subject from the tool result.
```

**Pass:** `hello-user-a` (after OAuth completed).

### Agent B (send only)

Attach **only** `agent_b`. Instruction:

```text
You are Agent B, a mail-send assistant. Use only send-mail from the agent_b plugin.
Recipient must be allowed@example.com. Refuse list/read mail requests.
```

User test:

```text
Use send-mail to allowed@example.com with subject agent-b-demo. Confirm success.
```

## Troubleshooting

| Symptom | What to try |
|---|---|
| Sync fails without OAuth UI | Expected until Hi Agent adds OAuth; use `../e2e` |
| OAuth redirect error | Re-run seed after tunnel change; `PROXY_BASE_URL` must match tunnel |
| Port in use | `cd ../e2e && docker compose down` |
| Tunnel stale | Re-run `./up.sh` |

## Layout

| Path | Role |
|---|---|
| `compose.yaml` | Stack + Caddy + cloudflared |
| `connection_policy.yaml` | Guardrail pins |
| `seed.py` | Registers MCP + OAuth challenge smoke (no priming) |
| `up.sh` / `down.sh` | Start/stop |
