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
  reply length under Settings. Conversations are saved in your browser only.
- **Models**: the split model (Chat, **Unload** to free its memory on every
  machine while keeping the download, **Load** to bring it back, a progress
  check, a speed test, and Remove) and, if you use Ollama, every model on every
  node with **Load** / **Unload** from memory, **Download** (a name like
  `llama3.2:3b` or `hf.co/owner/repo:Q4_K_M`) and a button that sets Ollama up
  across the cluster. Tasks show their live output in a window.
- **Find models**: search Hugging Face for GGUF models (sort by downloads,
  likes, trending or recent), open one to see its files with size and whether
  each **fits your cluster's free memory right now** (shared across machines or
  on one), then **Run split** (pick the machines, context length and API name;
  it replaces the running model and keeps the old download), **Run on Ollama**
  or copy the command.
- **API**: your base URL, model name and key, and copy-ready examples: curl,
  streaming, Python (OpenAI library and plain requests), JavaScript, editor and
  chat-app settings, and Ollama's own API.

Chat goes from the dashboard server to the model's own API; the model's API
key never reaches your browser unless you press *Reveal the key* on the API tab.

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
