# Microsoft 365 gateway labs

Labs for Hi Agent → Microsoft Graph (stand-ins in the lab). **Not** Google A2A — “Agent A / Agent B” means two Hi Agent agents with separate plugins.

| Lab | Auth model | Automated | Manual Hi Agent |
|---|---|---|---|
| [`mcp/user-oauth/`](mcp/user-oauth/) | User Microsoft OAuth (`authorization_code`); LiteLLM stores access/refresh | [`e2e/`](mcp/user-oauth/e2e/) | [`hiagent-demo/`](mcp/user-oauth/hiagent-demo/) |
| [`mcp/app-per-agent/`](mcp/app-per-agent/) | No user login; one Entra app (M2M `client_credentials`) per agent alias; LiteLLM mints/refreshes app tokens | [`e2e/`](mcp/app-per-agent/e2e/) | [`hiagent-demo/`](mcp/app-per-agent/hiagent-demo/) |
| [`rest/nango-oauth-proxy/`](rest/nango-oauth-proxy/) | Create plugin OAuth → Keycloak (broker → stand-in Microsoft); Nango holds Graph tokens; thin policy proxy | [`e2e/`](rest/nango-oauth-proxy/e2e/) | [`hiagent-demo/`](rest/nango-oauth-proxy/hiagent-demo/) |

MCP labs use **one Softeria container** per stack and multiple LiteLLM MCP aliases when isolating agents. The REST lab has no MCP / LiteLLM / Softeria.

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
```

Do not run stacks at once without checking ports (`user-oauth` e2e uses 4000/3000/…; demos and `app-per-agent` use other offsets; REST uses 4400/4500/8085/3003 — see each folder’s README).
