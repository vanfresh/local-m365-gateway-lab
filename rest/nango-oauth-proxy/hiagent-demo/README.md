# Hi Agent demo — REST Nango OAuth proxy

Manual demo for **Create plugin** OAuth against Keycloak (broker → Microsoft stand-in). Nango holds Graph tokens; the thin policy proxy enforces per-agent guardrails.

Full setup, ports, troubleshooting, and production mapping: **[parent README](../README.md)**.

```sh
chmod +x up.sh down.sh
./up.sh
```

`up.sh` prints the **full** Hi Agent guide to the terminal (URLs, OAuth, Create tool steps, SSE=off, input/output JSON) and saves it as `.hiagent-banner` (`cat .hiagent-banner` to re-print). Stop with `./down.sh`.

Host ports are offset from e2e (`8185` / `3103` / `8581` / `8582` / `4500`) so both stacks can run.
