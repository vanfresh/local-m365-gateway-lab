# App-per-agent e2e (M2M)

One Softeria; two LiteLLM MCP aliases (`m365_a`, `m365_b`) each with a distinct Entra app stand-in (`client_credentials`). Hi Agent / pytest auth is **Bearer virtual key** only — no browser OAuth in this suite.

## Ports

| Service | Host |
|---|---|
| LiteLLM | `4200` |
| Softeria | `3200` |
| Postgres | `5435` |
| Login stand-in | `8281` |
| Graph stand-in | `8282` |

Offset from [`../../user-oauth/e2e`](../../user-oauth/e2e) so both labs can run without clashing.

## Run

```sh
cd mcp/app-per-agent/e2e
chmod +x run.sh
./run.sh
```

## What it proves

- Key A `tools/list` / read on `m365_a` → Graph `sub=user-a` (app-agent-a)
- Key A cannot use `m365_b` (grant denial)
- Key B can send; cannot use `m365_a`
- Login log shows `client_credentials` only (no authorization_code)
