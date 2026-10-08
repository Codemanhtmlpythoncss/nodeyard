# The web dashboard

A web page about your cluster, updating in real time: how big it is, how busy
it is, what is using the memory, where everything is, and a place to chat with
the AI models you run. It needs a password, loads nothing from the internet,
and works in dark and light themes, on a phone too.

## Opening it

On the server (the machine that runs the k3s control plane):

```bash
sudo nodeyard dashboard start      # a service that keeps running and starts at boot
```

or choose **Web dashboard** in the main menu (`sudo nodeyard`). Then browse to
the address it prints: **`http://<the server's Tailscale address>:9092`**, the
same way you'd open Nextcloud. It asks for the dashboard password on a sign-in
page.

- `start` makes a random password the first time and shows it once. Show it
  again with `sudo nodeyard dashboard password --show`, choose your own with
  `sudo nodeyard dashboard password` (it asks twice, hidden), or make a new
  random one with `--random`. Changing it signs everyone out. After 5 wrong
  tries an address is locked out for 5 minutes.
- By default it listens on this machine and on the Tailscale address (Tailscale
  already encrypts the traffic), and opens that port in ufw/firewalld for the
  Tailscale interface only. No Tailscale? It listens on this machine only: use
  an SSH tunnel, `ssh -L 9092:localhost:9092 you@your-server`, and browse to
  <http://localhost:9092>.
- `--listen` changes who can reach it: `local` (this machine only),
  `tailscale`, `all` (every interface, plain HTTP, avoid on shared networks) or
  addresses like `--listen local,192.168.1.10`.

| Command | |
|---|---|
| `dashboard start [--port N] [--listen WHERE] [--interval S]` | Install and start the service (port 9092; reads the cluster every 2 s) |
| `dashboard stop` | Stop it and remove the service |
| `dashboard status [--json]` | Running? On which addresses? |
| `dashboard password [--show\|--random\|--stdin]` | Set, show or randomise the sign-in password |
| `dashboard run [--port N]` | Run it in this terminal instead (Ctrl-C stops it) |
| `dashboard agent install\|remove\|status` | The per-node agents (below) |
| `--demo dashboard run` | A simulated four-node cluster, to look around first |
| `sudo nodeyard reboot-cluster [--timeout SECONDS]` | Drain and reboot each worker, verify it returns, then reboot the control server last |

It needs `python3` (3.8 or newer; nodeyard offers to install it).

## What's on it

- **Real time.** The server reads the cluster every 2 seconds and pushes each
  reading to the page the moment it has it; the page changes in place (bars
  and charts glide to their new values, nothing flashes or jumps). The top bar
  says "Live · real time"; if the stream drops, the page quietly polls instead.
- **Overview**: cluster health; nodes, pods, **total CPU, memory and disk with
  how much is used**, network throughput; CPU, memory, network and pod charts
  for the last 5 minutes to an hour with a hover tooltip per node; a card per
  node with its **IP address**, load, temperature and clock speed; the busiest
  pods; things that need attention; usage by namespace; recent events.
- **Nodes**: every machine with status, roles, IP address, CPU, memory, disk,
  clock speed, temperature, load, network, OS and age, sortable. Click one for
  its history charts (CPU, memory, temperature, clock), memory breakdown,
  addresses, conditions, taints, labels, its biggest programs and its pods.
- **Devices sidebar**: keep each node's temperature, load, CPU and memory in
  view while using other pages. Expand a node for sensor readings and short
  history charts, or open its full details. Short Ready/NotReady heartbeat
  flaps show as **Checking in** or **Recovering** for up to a minute; sustained
  outages remain critical. Press `d` or use the Devices button.
- **Processes**: what is running on each machine and **what is using the
  memory**: a bar of the biggest consumers (named by pod or service), a card per
  machine with CPU model, per-core **clock speeds**, **temperature**, load,
  uptime, memory pressure, OOM kills, low-voltage warnings, and a memory
  breakdown (programs, kernel, cache, buffers, free), then a sortable table of
  processes with their user, pod or service, memory, CPU, threads and state, or
  grouped by pod and service. Needs the node agents.
- **Pods**: filter by name, IP, node or image; by namespace, node and health.
  Click a pod for its containers, labels and its **logs** (choose the
  container and how many lines, filter, follow live, wrap, copy, save).
- **Workloads**, **Network** (the API server, pod and service address ranges;
  every node address; ready-to-copy addresses for each NodePort and
  LoadBalancer service on every node; ingress hostnames; and an **address
  book** of every node, service, ingress and pod IP in one searchable table),
  **Storage**, **Events** and **Alerts** (a node not ready, hot, out of memory
  or disk, a crashing or stuck pod, a workload with missing replicas...).
- **AI**: see below.

Settings also has a confirmed **Restart Kubernetes** action. Workers restart one
at a time before the control node; the dashboard waits for nodes to return.

Also: search everything with `Ctrl K` or `/` (names, IP addresses, ports),
numbers `1`-`9` and `0` switch sections, `r` refreshes, `Space` pauses live
updates, `t` switches theme, `?` lists the shortcuts. Click any address to
copy it. Download a JSON snapshot with the button in the top bar.

## The node agents (processes, clock speeds, temperatures)

The kubelets can't say which programs run on a machine or how fast its CPU is
going. `sudo nodeyard dashboard agent install` (or the button on the Processes
page) runs a tiny read-only program on every node as a DaemonSet (a
`python:3.12-alpine` pod with the host's process view, no extra privileges,
nothing written, 96 MB limit). It reads `/proc` and `/sys` and answers only the
dashboard, which holds the shared token. Command lines are cleaned of anything
that looks like a password, key or token before they leave the node.
`dashboard agent remove` takes it away again.

## AI: chat, models, search, API

The **AI** page has four tabs.

- **Chat**: a full chat window with the model running on your cluster. Replies
  stream in word by word with formatting and code blocks (with Copy), the speed
  (tokens per second) is shown under each answer, and there is a Stop button.
  Pick the model at the top (the split model, or any Ollama model on any
  machine), keep several conversations, set a system prompt, creativity and
  reply length under Settings. Attach, drop or paste up to 10 text/code/data
  files and images per message. PNG, JPEG, WebP, GIF and BMP images are resized
  in your browser and sent as visual input; the chat can transcribe them for
  OCR when you use a vision-capable model. Image data stays in the current
  browser session, so reattach images after a reload. When a model sends a
  reasoning stream, an expandable **Model reasoning** panel opens while it is
  responding and remains available afterward. Conversations are saved in your
  browser only.
- **Models**: the split model (Chat, **Unload** to free its memory on every
  machine while keeping the download, **Load** to bring it back, a progress
  check, a speed test, **Force stop** to immediately stop its servers and
  pending downloads while keeping saved files, and Remove) and, if you use Ollama, every model on every
  node with **Load** / **Unload** from memory, **Download** (a name like
  `llama3.2:3b` or `hf.co/owner/repo:Q4_K_M`) and a button that sets Ollama up
  across the cluster. Tasks show their live output in a window with a **Cancel**
  button while they are running.
- **Find models**: search Hugging Face for GGUF models (sort by downloads,
  likes, trending or recent), open one to see its files with size and whether
  each **fits your cluster's free memory right now** (shared across machines or
  on one), then **Run split** (pick the machines, context length and API name;
  it replaces the running model and keeps the old download), **Run on Ollama**
  or copy the command.
- **Already downloaded** models stay visible as **last seen** for up to 30 days
  if Kubernetes briefly omits a node during restart; the inventory refreshes
  when that node reconnects.
- **API**: your base URL, model name and key, and copy-ready examples: curl,
  streaming, Python (OpenAI library and plain requests), JavaScript, editor and
  chat-app settings, and Ollama's own API.

Chat goes from the dashboard server to the model's own API; the server API key
never reaches your browser unless you press *Reveal* in Settings.

### No API key on your own network

With the model gate (`sudo nodeyard ai gate install`, see [AI workloads](ai.md))
the model's OpenAI-compatible API on port 31435 needs **no key from your own
machines, LAN or Tailscale**, and still needs it from anywhere else, such as a
router port-forward from the internet. The gate decides by the address a
request really comes from and never trusts `X-Forwarded-For`.

## How it works and what it can see

`server.py` (Python standard library only) asks the Kubernetes API, using the
server's admin kubeconfig, for nodes, pods, workloads, services, ingresses,
volumes and events, and asks each node's kubelet for its CPU, memory, disk and
network usage (so the numbers are right even when a machine's host name differs
from its node name, which confuses the cluster's metrics service). It never
reads Secrets. Pod logs are shown as the containers write them, so a program
that logs a password will show it there.

## Security

- **Sign-in.** Anything beyond this machine needs the password, and
  `--listen` to a non-local address is refused without one. Sessions are
  12-hour `HttpOnly`, `SameSite=Strict` cookies; the password is compared in
  constant time; repeated wrong guesses lock the address out.
- **No cross-site tricks.** State-changing requests must come from the page
  itself (a custom header, `application/json`, and an `Origin` that matches),
  which another website can't send. No CORS headers, a strict
  Content-Security-Policy, `X-Frame-Options: DENY`. A loopback-only dashboard
  without a password rejects requests addressed to any other host name (DNS
  rebinding).
- **What a signed-in user can do.** Chat with the models, and run, unload or
  remove them: the page starts one of a short fixed list of nodeyard commands
  (arguments are validated against strict patterns; nothing is passed through
  a shell). That is why the service runs as root (the kubeconfig is
  root-only), under a locked-down systemd unit: no privilege escalation, no
  capabilities, private `/tmp`, read-only system, writes only to nodeyard's own
  state, 400 MB memory limit. See it with `sudo nodeyard dashboard start --dry-run`.
  Treat the password like a root password for the cluster's AI features.
- `sudo nodeyard dashboard stop` removes the service and its firewall rule.

## Troubleshooting

**The page won't open**: is the service running (`sudo nodeyard dashboard
status`; `journalctl -u nodeyard-dashboard -n 30`)? Over Tailscale, is
Tailscale connected on both machines? Without Tailscale, the SSH tunnel must
be open.

**"Too many wrong passwords"**: wait 5 minutes, or set a new password on the
server with `sudo nodeyard dashboard password`.

**Processes shows "Turn on the node agents"**: install them (button, or
`sudo nodeyard dashboard agent install`). The first start downloads a small
image on each node.

**A node's disk or network is empty**: the server couldn't reach that node's
kubelet through the API; check `sudo nodeyard doctor` on it.

**Chat says the model isn't ready**: see the AI page, Models tab: it may be
downloading, loading, or unloaded (press Load).

**Port 9092 is taken**: `sudo nodeyard dashboard start --port 9093`.

## Control API

Other programs can see and switch models with the server API key (Settings > Server API key). It works from any network the dashboard
answers on (also through public access) and the same key applies to yardcode, the control API and split-model deployments. If an older
deployment has a working model key that the dashboard refuses, use **Use the running model's key** in Settings > Server API key to adopt it.
The model is not restarted. Deleting models, running commands and changing settings are not
in it: those need the dashboard's password.

```sh
KEY=...   # the server API key
curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/status
curl -H "Authorization: Bearer $KEY" http://DASHBOARD:9092/api/v1/models
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"model":"qwen2.5-coder-7b.gguf"}' \
     http://DASHBOARD:9092/api/v1/models/load          # -> {"job": "ab12..."}
curl -H "Authorization: Bearer $KEY" 'http://DASHBOARD:9092/api/v1/jobs?id=ab12...&since=0'
```

| Endpoint | |
|---|---|
| `GET /api/v1/status` | what is loaded and whether it is ready |
| `GET /api/v1/targets` | model targets that can receive chat requests |
| `POST /api/v1/chat/completions` | streamed OpenAI-compatible chat through the selected target |
| `GET /api/v1/models` | downloaded models and Ollama's, which is loaded, running downloads |
| `POST /api/v1/models/load` `{"model": "file.gguf"}` | switch to a downloaded model (old files are kept) |
| `POST /api/v1/models/unload` | free the cluster's memory |
| `POST /api/v1/models/download` `{"repo": "owner/name", "file": "x.gguf"}` | download a GGUF file |
| `GET /api/v1/search?q=words`, `GET /api/v1/files?repo=owner/name` | find models on Hugging Face |
| `GET /api/v1/jobs?id=ID&since=N` | progress of a task |

`yardcode models ...` uses it ([yardcode/README.md](../yardcode/README.md)).

The native macOS chat app also uses this API and the shared server key. See [macOS app setup](macos.md).

## Chat settings

Each chat has Settings: the reply length (**No limit** removes the cap), **context compression** (when the conversation nears the
model's context length, older messages become a short summary the model writes; **Compress now** does it at once), and **Web search**
(each question is searched on the internet, the best pages are read, and the model gets the text with its sources; needs yardcode
next to nodeyard on the server and never fetches private network addresses).

The permanent **Settings → AI chat defaults** card sets the starting system prompt, creativity, reply length, model context length,
compression, web search, skill selection, code behavior and file preference for new chats. Changing these defaults does not alter an
existing chat; its own Settings remain in effect. The default context length is used when starting a downloaded model. A model that is
already running keeps its current context length until it is started again.

## Chat: commands, plugins and running code

- **`/` commands**: type `/` in the message box for a menu: `/help`, `/new`, `/compact`, `/model`, `/models`, `/unload`, `/max`
  (`/max none` = no reply limit), `/web`, `/skills`, `/plugins`, `/system`, `/temp`, `/run`, `/fix`, `/autofix`, `/retry`, `/stop`, `/copy`,
  `/export`, `/context`, `/files`. A message that really starts with a slash: type `//`.
- **AI skills** (chat Settings): **Choose automatically** lets the model select from every available skill when useful: search the web and read pages, look things up on Wikipedia or arXiv, get the
  weather, calculate, keep a task list, run Python (code interpreter) or use files and shell. Every tool call shows as a card;
  anything that runs code or changes things asks first (Allow / Allow for this chat / Deny). Python, files and shell run as the
  terminal user (never root), and never through public access. Needs yardcode next to nodeyard (the installer does that). Turn automatic selection off with `/skills off` to pick individual skills; use `/skills on` to turn it back on.
- **Run the AI's code**: python, bash and javascript blocks have a ▶ Run button. The code runs on the server as the terminal user
  (30 second limit, never through public access) and its output shows under the block; "Ask the AI to fix it" sends the error back.
  **Run and fix code automatically** (chat Settings, off by default) does that after every answer, up to 3 tries.

## Choosing the model in the AI tab

The **AI model** menu lists the models downloaded on your machines. Picking one only selects it: the page says it will be loaded when
you send your next message, and sending does the rest (switches to it, keeps the old model's files, shows the progress, and sends
your message once the model answers). If the running model was unloaded, sending loads it again. For the machines and context length,
use the Models tab.

## When the AI makes files

By default the AI only delivers a file (a card you can view, copy or download; a .zip for several) when you ask for one: "give me
that as a .py file", "make me a file with…", "download it as a zip". Ordinary requests ("write me a script") stay in the chat as code
blocks that you can run. Change it per chat in Settings > Files, or with `/files ask`, `/files always` (every script, page and
document becomes a file) or `/files never`.
