# Connect Hermes Desktop to nodeyard AI

nodeyard exposes an **OpenAI-compatible** API, so **Hermes Desktop** (and any other OpenAI
client) can use the models running on your cluster as its backend. Set it up once. The
dashboard shows these same steps with your real values filled in, under **AI → API →
Connect Hermes Desktop**.

## 1 · Get your base URL and key

Open the dashboard → **AI → API** tab. It shows both, filled in for your install:

- **Base URL** — your dashboard's address followed by `/api/v1`. It's the same address you
  open the dashboard on:
  - over Tailscale, from anywhere on your tailnet: `https://<node>.<tailnet>.ts.net/api/v1`
  - on the local network: `http://<dashboard-lan-ip>:9092/api/v1`
- **Server API key** — one key that works for the control API and every model. Send it as
  `Authorization: Bearer <key>`. (Copy it from the same tab.)

> Keep the key private. It can load/unload models, which changes what the whole cluster runs.

## 2 · Point Hermes at it

Hermes talks OpenAI `chat_completions`. Add nodeyard as a provider in `~/.hermes/config.yaml`:

```yaml
model:
  provider: nodeyard
  api_mode: chat_completions
  base_url: https://<your-dashboard-host>/api/v1
  key_env: HERMES_NODEYARD_API_KEY        # the env var that holds your key
  default: split                           # the active split model (or a real name, below)

platform_toolsets:
  nodeyard:
    name: nodeyard ai
    base_url: https://<your-dashboard-host>/api/v1
    key_env: HERMES_NODEYARD_API_KEY
    discover_models: true                  # list every model the cluster offers
    model: split
    # Do NOT hard-code `models: { split: {} }` here — that pins a single entry called
    # "split" and hides the real model names. Let discover_models fill the list.
```

Put the key in `~/.hermes/.env` (never commit it):

```
HERMES_NODEYARD_API_KEY=<paste your server API key>
```

Restart Hermes, then use **Refresh models** in the model picker.

## 3 · Choosing a model

`discover_models: true` lists everything from `GET /api/v1/models`:

- **Split (clustered) models** show by their **real name** — the deployment's alias, or the
  downloaded file's base name, e.g. `Ornith-1.5-9B-Q4_K_M`. The special id `split` always
  means whichever split model is **currently deployed**.
- **Ollama models** show as `ollama:<pod>:<name>`.

Set `default` / `model` to `split` to always ride the active deployment, or to a specific real
name to pin one. Any name the picker shows can be sent as the `model` field — nodeyard maps it
back to the right target and loads it on demand.

## 4 · Verify from a terminal

```bash
curl -H "Authorization: Bearer $HERMES_NODEYARD_API_KEY" https://<your-dashboard-host>/api/v1/models
```

Each installed model should appear under `data[]` by its real name. A chat:

```bash
curl https://<your-dashboard-host>/api/v1/chat/completions \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $HERMES_NODEYARD_API_KEY" \
  -d '{"model":"split","messages":[{"role":"user","content":"Hello!"}]}'
```

## Troubleshooting

- **The picker only shows "split", not the real model names.** The dashboard is running an
  older build. Update it (deploy the latest to the cluster), then hit **Refresh models** in
  Hermes. Also remove any hard-coded `models: { split: {} }` from the config so discovery can
  list the real names.
- **"This chat is too long" / won't fit the context window.** Split models run with an 8192-token
  context by default. Compress the conversation or start a new chat; raise the context when you
  load the model if you need more (bigger context uses more memory per node).
- **401 Unauthorized.** Missing or wrong `Authorization: Bearer` key — copy it again from
  **AI → API**.
- **Can't reach it off the LAN.** Use the Tailscale address (the device must be on your tailnet),
  or set up outside access with [the friend VPN over Tailscale Funnel](pwnforge-friend-vpn.md).
- **First reply is slow, or replies go cold after idle.** Turn on **Keep the model loaded**
  (dashboard → AI → Models → Model lifecycle): it preloads the split model and keeps it ready.
