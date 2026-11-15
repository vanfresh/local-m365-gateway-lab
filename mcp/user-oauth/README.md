# User OAuth lab (MCP)

Microsoft **user** OAuth (`authorization_code` + PKCE). LiteLLM stores access/refresh tokens. Hi Agent (or pytest) is the MCP client.

| Path | Role |
|---|---|
| [`e2e/`](e2e/) | Automated proof (`./run.sh`) — MCP OAuth client in pytest |
| [`hiagent-demo/`](hiagent-demo/) | Manual tunnel + LiteLLM UI + Hi Agent; **OAuth-first** when Integrate MCP supports it |

Ports (e2e): LiteLLM `4000`, Softeria `3000`, login `8081`, graph `8082`, Postgres `5433`.  
Demo uses `4100` / `3100` / `8181` / `8182` / `5434` so it can coexist with e2e if you stop the other stack first.

See each subdirectory README for details.
