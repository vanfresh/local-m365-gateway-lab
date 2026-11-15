# Hi Agent demo — app-per-agent (M2M)

One Softeria; **two LiteLLM MCP rows** (`m365_a`, `m365_b`) each with a distinct Entra app stand-in (`client_credentials`). Hi Agent uses **Bearer virtual keys** only. LiteLLM mints and refreshes Graph app tokens.

Automated proof: [`../e2e`](../e2e).

## What this proves

| Check | Pass signal |
|---|---|
| LiteLLM UI shows two M2M MCPs | `m365_a`, `m365_b` |
| Seed smoke | `tools/list` with each key on its alias; cross-alias grant denial |
| Agent A read | Graph log `sub=user-a` / mail subject `hello-user-a` |
| Agent B send | Graph authorized for `user-b` |
| Isolation | Key A cannot call `m365_b`; key B cannot call `m365_a` |

## Auth model

| Layer | Who holds it | Role |
|---|---|---|
| Gateway | LiteLLM virtual key (Bearer) | Hi Agent plugin auth |
| Upstream | Entra app client_id/secret in LiteLLM MCP config | Graph M2M token |

Hi Agent never sees the Entra secret. Production: restrict each app with Exchange application access policies (or equivalent); this lab approximates mailbox identity via JWT `sub` / `azp`.

## Start

```sh
cd mcp/app-per-agent/hiagent-demo
chmod +x up.sh down.sh
./up.sh
```

Stop sibling stacks first if ports clash (`../e2e` uses 4200; this demo uses **4300** / Softeria **3300**).

## Configure in LiteLLM UI (optional manual path)

1. [http://127.0.0.1:4300/ui](http://127.0.0.1:4300/ui) — master key `sk-local-master`
2. MCP Servers → each alias → OAuth → **Machine-to-Machine**
3. Seed already sets:

| Alias | client_id | client_secret | token_url |
|---|---|---|---|
| `m365_a` | `app-agent-a` | `secret-a` | `http://microsoft-login-stand-in:8080/token` |
| `m365_b` | `app-agent-b` | `secret-b` | same |

## Integrate with Hi Agent

1. Run `./up.sh`; copy URLs and keys from the banner (or `.agent-a-virtual-key` / `.agent-b-virtual-key`).
2. **Integrate MCP Plugin** → Streamable HTTP → URL `https://<tunnel>/m365_a/mcp` (or `m365_b`).
3. Auth: **Bearer** → paste that agent’s virtual key.
4. Synchronize tools → create agents; attach **only** that agent’s plugin.

## Agent instructions + test prompts

### Agent A (read only)

Attach **only** `m365_a`. Instruction:

```text
You are Agent A, a mail-read assistant. You may only use MCP tools from the m365_a plugin.
For reading mail, call list-mail-folder-messages with arguments exactly:
  {"mailFolderId": "folder-x"}
You have no send-mail tool. Refuse send requests.
```

User test:

```text
Call list-mail-folder-messages with mailFolderId = "folder-x". Prefer no other parameters.
Return the message subject from the tool result.
```

**Pass:** `hello-user-a`.

### Agent B (send only)

Attach **only** `m365_b`. Instruction:

```text
You are Agent B, a mail-send assistant. Use only send-mail from the m365_b plugin.
Recipient must be allowed@example.com. Refuse list/read mail requests.
```

User test:

```text
Use send-mail to allowed@example.com with subject agent-b-demo. Confirm success.
```

## Troubleshooting

| Symptom | What to try |
|---|---|
| 401/403 on wrong alias | Expected — key grants are per MCP alias |
| Graph `sub` wrong | Confirm LiteLLM MCP row uses the matching client_id |
| Port in use | `cd ../e2e && docker compose down` |
| Tunnel stale | Re-run `./up.sh` |

## Layout

| Path | Role |
|---|---|
| `compose.yaml` | Stack + Caddy + cloudflared |
| `connection_policy.yaml` | Guardrail pins |
| `seed.py` | M2M MCP register + two keys + smoke |
| `up.sh` / `down.sh` | Start/stop |
