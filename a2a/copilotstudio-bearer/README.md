# A2A lab: `a2a/copilotstudio-bearer/`

Hi Agent **Create A2A service** (SSE URL + short Bearer) → gateway → Nango → Copilot A2A **stand-in**.

**No Keycloak.** Hi Agent cannot run OAuth for A2A (rejects oauth2; bearer max 128 chars). Entra-shaped Copilot tokens are minted by the gateway (`/_lab/issue`) and vaulted in Nango.

```text
Hi Agent: SSE URL + bearer lab-agent-a
  → Gateway: verify lab key → mint IdP tokens → Nango upsert
  → Copilot A2A stand-in SSE
```

| Folder | Purpose |
|---|---|
| [`e2e/`](e2e/) | Automated proof (`./run.sh`) |
| [`hiagent-demo/`](hiagent-demo/) | Tunnel + banner for Hi Agent |

## Secrets (same pattern as other labs)

| File | Committed? |
|---|---|
| `.env.example`, `.nango-secret.example`, `.nango-encryption-key.example` | Yes |
| `.env`, `.nango-secret`, `.nango-encryption-key` | No (gitignored; filled by `seed` / `up.sh`) |

```sh
cp .env.example .env   # or let ./run.sh / ./up.sh copy it
```

## Ports

| Service | e2e | hiagent-demo |
|---|---|---|
| Gateway | 4800 | 4900 |
| Nango API / Connect | 3403 / 3409 | 3503 / 3509 |
| Login stand-in | 8881 | 8981 |
| Copilot A2A stand-in | 8882 | 8982 |
| Caddy | — | 8490 |

## Run

```sh
cd a2a/copilotstudio-bearer/e2e && ./run.sh

cd a2a/copilotstudio-bearer/hiagent-demo && ./up.sh
# Create A2A → SSE URL from banner, bearer: lab-agent-a
./down.sh
```

## Hi Agent

1. Create A2A service → paste SSE URL  
2. **bearer** = `lab-agent-a` (or `lab-agent-b`) — not a JWT  

## Troubleshooting

- **oauth2 not supported** → card uses HTTP Bearer only  
- **bearer 128 limit** → use `lab-agent-a`, not Keycloak/Entra JWT  
- **Parsing failed / missing_bearer** → GET on SSE URL must return agent card (no auth)
