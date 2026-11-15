# App-per-agent lab (MCP)

No user login. **One Entra app (M2M `client_credentials`) per agent alias**; LiteLLM stores app secrets and refreshes Graph tokens. Hi Agent uses **Bearer virtual keys** only.

One Softeria container; N LiteLLM MCP configs (`m365_a`, `m365_b`, …).

| Path | Role |
|---|---|
| [`e2e/`](e2e/) | Automated isolation proof (`./run.sh`) |
| [`hiagent-demo/`](hiagent-demo/) | Manual tunnel + two Bearer keys for Hi Agent |

Ports (e2e): LiteLLM `4200`, Softeria `3200`, login `8281`, graph `8282`, Postgres `5435`.  
Demo: `4300` / `3300` / `8381` / `8382` / `5436`.
