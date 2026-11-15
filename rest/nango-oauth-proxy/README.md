# REST lab: `rest/nango-oauth-proxy/`

## What you will prove

| Check | Pass signal |
|---|---|
| One Create plugin OAuth config in Hi Agent | Keycloak URLs + client_id only |
| One browser login | Stand-in Microsoft page inside Hi Agent OAuth redirect chain |
| Graph tokens not in Hi Agent | Nango has connection; Graph log shows `sub=user-a` / `user-b` |
| Agent isolation | A read-only folder-x; B send allowlist |

## Prerequisites

- Docker / Docker Compose
- `curl`, Python 3 for seed/e2e
- Hosted Hi Agent that can reach a Cloudflare quick tunnel URL
- Stop other labs if ports clash (see Ports)

## Ports

| Service | e2e | hiagent-demo |
|---|---|---|
| Policy proxy | `4400` | `4500` |
| Keycloak | `8085` | `8185` |
| Nango API | `3003` | `3103` |
| Nango Connect UI | `3009` | `3109` (internal; not used on happy path) |
| Login stand-in | `8481` | `8581` |
| Graph stand-in | `8482` | `8582` |
| Caddy | — | `8090` |
| Tunnel | — | printed by `up.sh` |

Compose project names `rest-nango-e2e` / `rest-nango-hiagent-demo`.

## Automated proof

```sh
cd rest/nango-oauth-proxy/e2e
chmod +x run.sh
./run.sh
```

Pytest: Keycloak OAuth (with broker) per agent → OpenAPI via policy proxy → Graph via Nango; isolation assertions.

## Manual Hi Agent demo

```sh
cd rest/nango-oauth-proxy/hiagent-demo
chmod +x up.sh down.sh
./up.sh
```

`up.sh` will:

1. Start Keycloak, Nango, stand-ins, policy proxy, Caddy
2. Start cloudflared; write `.tunnel-url`
3. Recreate Keycloak/Nango/proxy with public URLs (redirects/broker)
4. Seed Keycloak realm (clients + IdP) and Nango integration
5. Print a **Hi Agent setup banner** (copy-paste fields)

Stop: `./down.sh`

## Hi Agent — Create plugin + tools

This lab has **two** plugins only (**Agent A** read, **Agent B** send). There is **no “full” / unrestricted plugin** (unlike the MCP `m365` ceiling lab). Isolation is enforced by `connection_policy.yaml` on the policy proxy.

### URLs (not the same thing)

| Role | URL |
|---|---|
| **Plugin base** (prefix on Create tool) | `https://<tunnel>` |
| **OpenAPI import** (plugin setup only) | `https://<tunnel>/openapi.json` |
| **Full tool URL** | plugin base + tool path, e.g. `https://<tunnel>/v1.0/me/mailFolders/{mailFolderId}/messages` |

Do **not** put `/openapi.json` in the tool path.

### 1. Create plugin (once per agent)

1. Open banner / `.tunnel-url` → `PUBLIC=https://<tunnel>`
2. **Create plugin** (not Integrate MCP)
3. Plugin / server URL: `https://<tunnel>` (root). Import OpenAPI from `https://<tunnel>/openapi.json` if the UI asks for a schema URL.
4. Auth mode: **OAuth** / Authorization Code

Paste each URL on its own (no trailing space — `…/auth%20` makes Keycloak show **Page not found**).

| Field | Agent A | Agent B |
|---|---|---|
| auth_url | `https://<tunnel>/realms/hiagent-lab/protocol/openid-connect/auth` | same URL (exact) |
| access_token_url | `https://<tunnel>/oauth/token` (shim; not Keycloak `/token`) | same |
| refresh_token_url | `https://<tunnel>/oauth/token` | same |
| client_id | `agent-a` | `agent-b` |
| client_secret | from banner / seed | from banner / seed |
| redirect_url | Hi Agent default callback | same |
| scope | `openid` | `openid` |
| auth_content_type (if shown) | `application/x-www-form-urlencoded` | same |
| UI grant_type / auth mode (if shown) | Authorization Code | same |

**Scope:** use exactly `openid`. Do not put `Mail.Read` / `Mail.Send` on the Hi Agent→Keycloak plugin (those are Graph scopes on the IdP/Nango side).

**UI “grant_type” = Authorization Code is normal** — that picks the OAuth flow. The error `Missing form parameter: grant_type` is a different thing: Hi Agent’s token HTTP request omitted the form field `grant_type=authorization_code`. The `/oauth/token` shim adds it and form-posts to Keycloak.

Exact Keycloak paths: banner always wins.

5. Save → complete browser login (Keycloak → login stand-in). No Nango Connect step.
6. Create tools below, then attach the plugin to the matching agent.

**You do not** paste Nango secret, Microsoft app id, or Graph tokens into Hi Agent.

### 2. Create tools (after the plugin)

Grey prefix = plugin base. **Tool path** = path only (starts with `/`).

**Not SSE.** Leave **Streaming output** unchecked. Responses are normal `application/json` (one shot), not Server-Sent Events.

**Agent A plugin — list messages**

| Field | Value |
|---|---|
| Name | `listMailFolderMessages` |
| Description | List messages in a mail folder (lab Graph stand-in). |
| Tool path | `/v1.0/me/mailFolders/folder-x/messages` (literal — Hi Agent does not expand `{mailFolderId}`) |
| Method | `GET` |
| Streaming | **off** |
| Input | none if path is literal; or `mailFolderId=folder-x` if you keep `{mailFolderId}` (proxy rewrites) |
| Output | Root object with required `value` (array). Message fields (`id`, `subject`, …) are **inside** `value[]`, not at the root. |

Example **200** body (`mailFolderId=folder-x`, user-a):

```json
{
  "value": [
    {
      "id": "msg-user-a",
      "subject": "hello-user-a",
      "parentFolderId": "folder-x",
      "from": { "emailAddress": { "address": "user-a@example.com" } }
    }
  ]
}
```

If Hi Agent says `Error at "/id": property "id" is missing` while the Response panel already shows `value[0].id`, the **output schema** is wrong (it expects a single message at root). Fix the schema or use raw JSON output.

**Agent B plugin — send mail**

| Field | Value |
|---|---|
| Name | `sendMail` |
| Description | Send mail via Graph stand-in (allowlisted recipients only). |
| Tool path | `/v1.0/me/sendMail` |
| Method | `POST` |
| Streaming | **off** |
| Body (JSON) | see below |
| Output | JSON object; lab returns `id`, `subject` (see below). |

Request body:

```json
{
  "message": {
    "subject": "agent-b",
    "body": { "contentType": "Text", "content": "ok" },
    "toRecipients": [{ "emailAddress": { "address": "allowed@example.com" } }]
  },
  "saveToSentItems": false
}
```

Example **200** body:

```json
{
  "id": "created-1",
  "subject": "hello-user-b",
  "parentFolderId": ""
}
```

If Hi Agent’s output step is optional / “raw JSON”, you can skip defining every field and still debug.

## Test prompts

**Agent A (read):** call list folder messages with `mailFolderId=folder-x` → expect subject `hello-user-a`.

**Agent B (send):** send mail to `allowed@example.com` → success; refuse read.

## Troubleshooting

| Symptom | Fix |
|---|---|
| OAuth redirect error | Re-run `up.sh` after tunnel change; Keycloak frontend URL / IdP redirect must match tunnel |
| Cookie not found | Keycloak redirected to `127.0.0.1` instead of tunnel — re-run `./up.sh` (hostname must be the tunnel). Retry authorize in a fresh browser tab |
| `policy_denied` … `%7BmailFolderId%7D` | Hi Agent sent literal `{mailFolderId}` in the URL. Set tool path to `/v1.0/me/mailFolders/folder-x/messages` |
| 401 on tools | Finish Create plugin OAuth; token must be Keycloak access_token |
| 403 policy | Wrong agent plugin / client_id; check `connection_policy.yaml` |
| Graph unauthorized | Policy proxy sync to Nango failed — check proxy + Nango logs |
| Port in use | `docker compose down` on other labs |

## Production mapping (for stakeholders)

| Lab | Production |
|---|---|
| Keycloak | Same or enterprise IdP (Entra ID as Keycloak IdP, etc.) |
| Login stand-in | Real `login.microsoftonline.com` |
| Graph stand-in | Real Microsoft Graph |
| Nango | Same (OSS/cloud) as Graph token vault + proxy |
| Policy proxy | Same thin service (or API gateway + small sync job) |

Hi Agent still has **one** Create plugin OAuth config pointing at your IdP; mailbox tokens stay in Nango.

## Architecture (short)

```text
Hi Agent — one Create plugin
  Auth: OAuth → Keycloak (client_id/secret from Keycloak)
  API:  OpenAPI → policy proxy

Browser (started by Hi Agent OAuth only):
  Keycloak authorize → IdP broker → login stand-in → back → Hi Agent
  Hi Agent stores Keycloak access_token (NOT Graph token)

Tool call:
  Hi Agent → policy proxy (Keycloak access_token)
           → sync Microsoft tokens from Keycloak IdP store into Nango
           → guardrail → Nango proxy → Graph stand-in
```

### Why Nango if Keycloak already stores Microsoft tokens?

Keycloak **can** hold IdP (Microsoft) access/refresh after broker login (`storeToken`). That is a **side effect of login**, not a Graph integration layer.

| | Keycloak | Nango |
|---|---|---|
| Job | Log the user in; issue tokens to **Hi Agent** | Be **system of record** for Graph tokens + call Graph |
| Microsoft tokens | Temporary / account-linked IdP copy after broker | Vault: refresh, rotate, attach on each Graph call |
| Graph HTTP | No (not an API proxy) | Yes (`/proxy/...` → Graph) |
| Built for | OIDC/SAML for apps (Hi Agent plugin OAuth) | SaaS API connections (Graph, HubSpot, …) |

In this lab the policy proxy **copies** IdP tokens from Keycloak → Nango on each tool call, then uses Nango to talk to Graph. You could skip Nango and have the proxy call Graph with Keycloak’s stored IdP token — but then **you** own refresh, retries, multi-connection mapping, and Graph proxying. Nango is that productized piece; Keycloak stays the login/AS for Hi Agent.

**Mental model:** Keycloak = “who is this agent/user?” · Nango = “call Microsoft Graph with the right mailbox token.”
