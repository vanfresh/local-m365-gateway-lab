# Microsoft 365 gateway labs

Labs for Hi Agent → Microsoft Graph / Copilot (stand-ins in the lab).

In the Graph labs, “Agent A / Agent B” means two Hi Agent agents with separate plugins — **not** the A2A protocol. The A2A lab below **is** Agent2Agent (SSE).

| Lab | Auth model | Automated | Manual Hi Agent |
|---|---|---|---|
| [`mcp/user-oauth/`](mcp/user-oauth/) | User Microsoft OAuth (`authorization_code`); LiteLLM stores access/refresh | [`e2e/`](mcp/user-oauth/e2e/) | [`hiagent-demo/`](mcp/user-oauth/hiagent-demo/) |
| [`mcp/app-per-agent/`](mcp/app-per-agent/) | No user login; one Entra app (M2M `client_credentials`) per agent alias; LiteLLM mints/refreshes app tokens | [`e2e/`](mcp/app-per-agent/e2e/) | [`hiagent-demo/`](mcp/app-per-agent/hiagent-demo/) |
| [`rest/nango-oauth-proxy/`](rest/nango-oauth-proxy/) | Create plugin OAuth → Keycloak (broker → stand-in Microsoft); Nango holds Graph tokens; thin policy proxy | [`e2e/`](rest/nango-oauth-proxy/e2e/) | [`hiagent-demo/`](rest/nango-oauth-proxy/hiagent-demo/) |
| [`a2a/copilotstudio-bearer/`](a2a/copilotstudio-bearer/) | Create A2A (SSE URL + short Bearer); gateway mints Copilot tokens into Nango (no Keycloak) | [`e2e/`](a2a/copilotstudio-bearer/e2e/) | [`hiagent-demo/`](a2a/copilotstudio-bearer/hiagent-demo/) |

MCP labs use **one Softeria container** per stack and multiple LiteLLM MCP aliases when isolating agents. The REST and A2A labs have no MCP / LiteLLM / Softeria.

```sh
# User OAuth proof (pytest)
cd mcp/user-oauth/e2e && ./run.sh

# User OAuth manual demo (tunnel + Hi Agent; OAuth-first when the product supports it)
cd mcp/user-oauth/hiagent-demo && ./up.sh

# App-per-agent proof (pytest)
cd mcp/app-per-agent/e2e && ./run.sh

# App-per-agent manual demo (Bearer virtual keys per agent)
cd mcp/app-per-agent/hiagent-demo && ./up.sh

# REST Create-plugin OAuth → Keycloak + Nango (pytest)
cd rest/nango-oauth-proxy/e2e && ./run.sh

# REST manual demo (tunnel + Hi Agent Create plugin OAuth)
cd rest/nango-oauth-proxy/hiagent-demo && ./up.sh

# A2A Copilot stand-in → short Bearer + Nango SSE (pytest)
cd a2a/copilotstudio-bearer/e2e && ./run.sh

# A2A manual demo (tunnel + Hi Agent Create A2A SSE URL + lab-agent-a)
cd a2a/copilotstudio-bearer/hiagent-demo && ./up.sh
```

Do not run stacks at once without checking ports (see each folder’s README). A2A bearer uses 4800/4900/3403/3503.
