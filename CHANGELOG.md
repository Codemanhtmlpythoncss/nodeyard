# Changelog

All notable changes to nodeyard are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and nodeyard uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
each minor release completes one phase of the [roadmap](docs/STATUS.md).

## [Unreleased]

### Changed

- The installer and `nodeyard update` now use the HTTPS `main` source at a recorded commit, so updates no longer depend on GitHub releases or tags. `--version` remains available for tag-based installs.
- Model storage scans are bounded and include supported nodes even while they are NotReady. The dashboard keeps the last saved inventory during refreshes, retains last-known model locations for unreachable nodes, reports incomplete scans, retries after Kubernetes downloads finish, and shows Ollama downloads in Find models alongside GGUF files.
- **License**: nodeyard and yardcode are now "All rights reserved" instead of MIT. You can read the source and run it on your own machines; copying,
  redistributing, selling, hosting for others and making clones are not allowed without written permission. Copies already received under MIT stay MIT.
- **One server API key** for everything: yardcode, the control API and every model use the same key (dashboard Settings > Server API key, or
  `nodeyard ai key --show|--rotate|--stdin`). A model deployed with its own key now stores it as the server key (and a model deployed without one
  gets the server key), so the dashboard no longer says "refused the key" for a key the model accepts. yardcode now tells apart a wrong key (401),
  a control API that is off (403) and too many tries (429), and checks the key right after `yardcode login`.
- Settings can adopt the running split model's existing API key as the server key. This fixes older installs where yardcode's valid model key and
  the dashboard's control key drifted apart, without rotating the model's key.
- Dashboard Settings and the Devices sidebar can restart k3s across the cluster, workers one at a time and the control node last, with a confirmation.

### Added

- Dashboard **Find Models** now starts with an **Already downloaded** section showing saved models, the machines that contain them, and a direct Run/Load/Chat action. Its inventory refreshes while this tab is open.
- Dashboard **Devices sidebar**: per-node temperatures, load, CPU and memory use, sensor readings and short history charts, with the node details one click away.
- yardcode: **type `/` and the commands appear above the prompt** (filter as you type, Up/Down, Tab or Enter), with the input framed
  between two rules and the permission mode, model and context use on the line below. **Shell mode**: `!` on an empty prompt (or
  `/shell`) gives a real shell prompt: your own `$SHELL` with the terminal attached (vim, ssh, top, sudo, Ctrl-C all work), `cd` and
  exported variables carry over, Tab completes files, history is separate, and what a command printed is added to the conversation.
  `!command` runs one line the same way. New **`/reset`** forgets the conversation and starts the model fresh (the old one stays in
  `/resume`). Start-up no longer waits minutes when the server can't be reached: it says so after 4 seconds.
- yardcode: web search and page reading go **through the nodeyard server** (its internet connection, not the computer you type on);
  `/doctor` shows where it runs and `web.via` (`auto`, `server`, `local`) sets it. The Weather tool picks the most likely place and
  lists the others ("Jersey" is the island; add the country for another).
- yardcode chats are shared with the dashboard: they appear in the AI tab's chat list and can be continued there, and `/chats`
  (or `yardcode chats`) continues a dashboard chat in the terminal. `yardcode update` (and `/update`) updates from your server or GitHub.

- yardcode: **`/think live`** (or `--show-thinking`) streams the model's reasoning on screen as it is written; `show` keeps the short
  summary and `hide` removes it. While the model reads a long prompt the screen now says so, with progress and time left (llama.cpp),
  and the prompt is smaller and identical every time, so the model server can reuse what it already read: a context under 12k tokens
  gets 9 essential tools (about 1.3k tokens instead of 3.2k), the date and folder go in the first message, not the system prompt.
- Dashboard chat: **replies are only made into files when you ask for one** ("give me that as a .py file", "make me a zip",
  "download it"). "Write me a script" now stays a normal code block (with its Run button). The chat Settings have a Files menu: only
  when I ask (default), always, or never; `/files ask|always|never` does the same.

- AI tab: **the selected model loads when you send a message**. Picking a model in the AI model menu only selects it; sending
  switches to it, shows the loading progress ("Don't wait" gives up waiting) and sends your message when the model answers. The same
  happens when the running model was unloaded. `/model NAME` picks a downloaded model too.
- `ai gate install` remembers the gate (`ai.gate` in the config), and `ai split deploy` puts it back when it went missing (it
  went with the namespace when the AI namespace was deleted and recreated, which made the model ask for its key on your own network).
- yardcode: on macOS it also trusts the certificates in your keychains, and a web filter that re-signs HTTPS now gets a clear message.

- Dashboard chat **/ commands** (type `/` for a menu): /help, /new, /compact, /model, /models, /unload, /max, /web, /plugins,
  /system, /temp, /run, /fix, /autofix, /retry, /stop, /copy, /export, /context.
- **Run the AI's code**: every python, bash and javascript block has a ▶ Run button (runs on the server as your terminal user, 30 s
  limit, never through public access). The output shows under the block and "Ask the AI to fix it" sends the error back. With
  **Run and fix code automatically** on, the AI's last code block runs after each answer and failures go back to it, up to 3 tries.

- Dashboard chat **plugins** (Chat > Settings > Plugins): the AI can search the web and read pages, look things up on Wikipedia and
  arXiv, get the weather, calculate, keep a task list, run Python (code interpreter) and use files and shell in a work folder. Tool
  cards show in the conversation, and anything that runs code or changes things asks first (Allow / Allow for this chat / Deny); code
  and file plugins run as the terminal user, never root, and never through public access. Built on the same agent as yardcode.

- **yardcode**, a terminal AI agent (like Codex or Claude Code) for your own model API, on macOS and Linux (Python 3.8+, no
  packages): file, shell, Python and web tools (search, fetch, Wikipedia, arXiv, weather; no API keys), permissions with plan and
  accept-edits modes, sessions with `/rewind`, hooks, MCP servers, plugins, sub-agents, `YARDCODE.md` instructions, and automatic
  **context compression**. `--max-tokens none` removes the reply limit. Installs with nodeyard, or alone with
  `yardcode/install.sh` or the single-file `make yardcode`. See [yardcode/README.md](yardcode/README.md).
- **Control API** (`/api/v1` on the dashboard, protected by the server API key from any network): list, load, unload and download
  models remotely (`yardcode models load NAME`, or curl). Key-guessing is rate limited; deleting models and running commands stay
  behind the dashboard password.
- Dashboard chat: **No limit** for the reply length, **context compression** (automatic near the model's context length, or
  Compress now), a **Web search** switch (searches the internet and gives the model the results with sources), and a context meter.
- The model server now gets `--jinja`, so tool calls work with models that support them.

- **Speed-aware split planner**: `ai split plan|deploy` now tries every set
  of nodes, shares layers by speed (capped by free memory), counts a network
  hop per extra node and picks the fastest plan; it prints the estimated
  tokens/s and which nodes it left out. `--nodes auto` is the default;
  `--model auto` picks the biggest quant reaching `--min-speed` (default 10).
  The main node is the one with the most free space on its root partition.
- **Disk care**: `ai split clean [--models]` (free up space: old weight
  caches, abandoned downloads, unused models), `ai split models` (what is on
  every node's disk), `ai split rm FILE`, and `ai disk limit NODE GiB` (nodeyard
  never fills a node past it). Each model gets its own weight-cache folder and
  nodes delete other models' caches when a new model deploys.
- **Parallel downloads**: `ai split download`; one resumable, sha256-checked
  Job per file with progress lines. Deploys reuse a finished download.
- `ai split key --rotate|--stdin` and `ai hf token` (gated models).
- **Dashboard**: Doctor page (checks + one-click fixes), Settings page
  (password, sign out everywhere, server API key, Hugging Face token,
  listen/port/refresh, model gate networks, node agents, disk limits, cluster
  name, theme) and a custom background picture with blur and darken.
  AI > Models has a Downloaded models card with per-file download progress,
  delete buttons (split and Ollama) and Free up space. Downloads and plans no
  longer wait for the single-task lock.

- **Hardware page** (dashboard) and `nodeyard hw bench|show`: CPU model, cores,
  clock range, caches, instruction sets, board, disks (NVMe/SSD/HDD/SD, USB),
  network link speeds and GPUs from the node agents, plus a measured speed
  test per node (memory copy GB/s, CPU score, disk read/write MB/s and IOPS).
  Control-plane nodes skip the disk test (it stalls etcd on an SD card).
- Model downloads show their speed and an ETA (`ai split status` and the
  dashboard). `ai split plan --nodes all` uses every supported node.
- **Themes**: 13 in Settings > Themes (Midnight, Daylight, Nord, Dracula,
  Solarized dark/light, Forest, Sunset, Ocean, Rose, Paper, High contrast).

- **Public access** (`nodeyard public on|off|status`, Settings > Public
  access): the dashboard (https://NAME.ts.net:8443) and the model API
  (https://NAME.ts.net:10000/v1) through Tailscale Funnel, reachable from any
  device without Tailscale. The dashboard needs a strong password first;
  internet sign-in failures can't lock you out on your own network; the model
  gate has a loopback-only port where the API key is always required.
- **Terminal page**: a shell on the server in the dashboard (built-in
  terminal emulator), as the user who started the dashboard (never root), only
  over Tailscale/LAN, with the password asked again.
- **Commands page**: every nodeyard command with its help, runnable from the
  dashboard (`nodeyard commands --json` lists them), never through public access.
- **Model loading progress and ETA** (dashboard and `ai split status`): how much
  of each node's share has arrived, how much of the file has been read, time left.
  Download ETA also shows in the Split model card.
- The dashboard warns when a node's cluster network link runs slower than its
  hardware can (a cable or switch problem); the node agent now runs on the
  host network so it sees the real links.

- `dashboard.weak-password` setting (Settings > Sign-in, or `nodeyard config
  set dashboard.weak-password true`): no minimum length or strength for the
  dashboard password, also for `public on`. Off by default; the sign-in
  lockout still applies.
- GPUs: every graphics device on the PCI bus with its real name (NVIDIA's
  driver info, the PCI ID list) and driver; a GPU column on the Hardware page.
- **Model menu** on the dashboard's AI page and `ai split switch`: the menu
  lists exactly the model files downloaded on your nodes; picking one unloads
  the old model, deletes its file and weight caches on every node (unless you
  keep them) and runs the new one. `--model local:FILE.gguf` (or just the file
  name) runs a model already on a node's disk from there: no download, no
  Hugging Face lookup, and that node coordinates.
- **NVIDIA GPUs for models**: `ai gpu setup` (on the machine with the card:
  NVIDIA's container toolkit, also without gpg), `ai gpu enable NODE` (checks
  a container really sees the card, then marks the node with its video
  memory) and `ai gpu status`. The planner gives the main node's GPU the
  fastest share; the main server then runs llama.cpp's Vulkan build, which
  works with older NVIDIA drivers than its CUDA builds. GPU nodes get a node
  agent with NVIDIA's tools, so the dashboard shows GPU use, video memory,
  temperature and power, with charts.
- **Faster model loading** by default: the main server reads the model file
  with direct I/O (big sequential reads, `--load-mode dio`) instead of memory
  mapping, and weight caches are only kept on nodes whose disk is faster than
  gigabit ethernet (`nodeyard hw bench` measures it), never on the main node,
  where the cache fought the main server for the disk the model is on. A
  17.3 GiB model on a laptop hard drive went from about 22 to 7.5 minutes.
  The plan says which nodes have no cache and why. While loading, the GPU's
  share is measured on the card (its video memory in use).
- The Overview's **GPU card** is always there: live use, video memory,
  temperature and power for NVIDIA cards that are set up, otherwise which
  cards were found and that they aren't set up yet.
- **Chat files**: the model is asked to deliver scripts, pages and documents
  as files; each shows as a card to view, copy or download, and several (with
  folders) download as one .zip. Every code block has a Download button too.
  You can attach text files (button, drag and drop, or paste); they are sent
  as `<file>` blocks and kept with the chat. The page warns before sending
  more than the model's context length. "Make files" can be turned off per
  chat in its settings.

### Fixed

- The dashboard's status dot (and so its pulsing ring) was rebuilt on every update, so it kept restarting like a loading circle
  jumping back to the start. It is updated in place now, and the page still refreshes live with no polling.
- Chat: **Stop** works at any time. The page's regular refresh disabled it
  while an answer was coming; now it stays live (also after switching chats
  or pages), and it tells the dashboard to cut the model's request, so
  llama.cpp stops working on it even while it is still reading the prompt
  (a closed tab does the same).
- Split models with a GPU share put it on the wrong device: llama.cpp orders
  its devices RPC servers first and the local GPU last, but the GPU's share
  was listed first, so the GPU and an RPC server got each other's share (a
  4 GiB card was asked for 4.6 GiB and the load failed). The GPU's share is
  now last, and the dashboard labels it "<node> GPU" instead of "?".
- Disk scans (`ai split models`, `clean`, `rm`, the dashboard's Downloaded
  models) running at the same time deleted each other's helper pods, so one of
  them could see no files ("isn't downloaded on any node"). Each scan now has
  its own pods.
- `ai split undeploy` (and Remove on the dashboard) unloads the model first
  and waits until every server pod has gone (forcing any that hang), so no
  model server is left running; Remove can also delete the model's files.

- `ai gate install` hung when the model's Service still held the port as a
  NodePort; the Service now gives the port up before the gate is waited for.
- Changing the agent or gate program now restarts their pods (a checksum in
  the pod template); before, pods kept running the old program.
- The dashboard no longer jolts on every refresh: pages are patched in place,
  so scroll position, focus, typed filters and open menus survive updates.
- Cards were almost transparent, so dark-theme text could land on a light
  surface; every theme now paints its own background and solid cards.
- `ai split clean` said it freed space even when the helper pods were refused
  (the helper namespace was still being deleted); it now reports what the
  disks really freed and fails if a node couldn't be cleaned.
- `ai split deploy` failed with `namespaces "ai-split" not found` (printed
  once per object) after an undeploy: the namespace is now created first.
- The disk check said a 17 GiB model "needs about 129 GiB": it now shows the
  real download size and the kubelet's 10% headroom separately.
- `ai split undeploy --purge` did nothing once the namespace was gone, leaving
  model files and caches behind; it now cleans every node.
- `ai deploy` (Ollama) skips nodes whose root partition can't hold the image
  (`--min-disk-gb`, default 10) instead of failing with "no space left on device".
- The dashboard no longer shows replaced evicted pods as warnings, and the
  "Machines to use" checkboxes in the Run split dialog lined up wrong.

- **Web dashboard**: `nodeyard dashboard start|stop|status|run` and a
  **Web dashboard** entry in the main menu. A polished, read-only page on
  `localhost:9092` (reach it with an SSH tunnel) with total resources and
  usage over time, every node's IP address, load and details, pods with
  live usage and logs (filter, follow, save), workloads, an address book of
  every node/service/ingress/pod IP with copy-ready NodePort addresses,
  storage, AI model status, events, computed alerts, search (Ctrl K),
  keyboard shortcuts, dark/light themes, and a JSON snapshot download. It
  is Python 3 standard library only, loads nothing from the internet,
  refuses to listen anywhere but 127.0.0.1, rejects other `Host` names,
  and runs as a hardened systemd service. `--demo dashboard run` shows a
  simulated cluster. See docs/dashboard.md.
- `nodeyard worker-info` and a menu entry, **What a worker needs to join**
  (on servers): shows the join address and port (6443), this server's k3s
  version, where the join token is (hidden, with an offer to reveal it in
  the menu), every network port that must be open with whether each is
  open on this server's firewall, what the worker machine needs, and the
  exact commands to add it from here over SSH or on the worker itself.
  `--json` output included.
- When a k3s service won't start after `install` or `upgrade`, nodeyard
  now reads its log, shows the lines that matter and names the likely
  cause with a fix (cloud-setup service, memory cgroup, duplicate node
  name, wrong token, server unreachable, port in use, wrong interface,
  clock trouble, missing firewall tools). The k3s installer no longer
  starts the service itself, so a failed start is explained instead of
  ending in "the installer failed".
- `add-node --become auto|sudo|su`: become root on the new machine with
  sudo, or with su and the root password (for machines without sudo).
  Automatic by default.

### Fixed

- `ai split plan` skipped nodes whose metrics reading was `<unknown>` (a
  tab-separated read collapsed empty fields, shifting every column), so the
  largest machines could be left out of the plan. Fields are now separated
  by `|`, and a node without a metrics reading is asked for its free
  memory through its own kubelet.
- `add-node` skipped installing nodeyard on a machine that already had the
  same version number ("already installed; nothing to do"), so a leftover
  copy from an earlier attempt kept running, without any later fixes. It
  now always installs the server's exact copy.
- A worker whose first start fails (for example because the cluster still
  lists the node under its old address) is no longer reported as failed
  while systemd is still retrying it: nodeyard waits up to 90 seconds for
  the retry. The failure explanation now shows the decisive log lines
  first, no longer mistakes ordinary cgroup log lines for a missing memory
  cgroup, and recognises "failed to find interface with specified node ip".
- SSH host keys in `add-node`: a saved key is now checked against the key the
  machine presents *now*. A changed key (for example after a reinstall) is
  explained with the old and new fingerprints and needs a deliberate yes;
  an out-of-date key in your own `~/.ssh/known_hosts` is ignored instead of
  being copied back in; a new machine's fingerprint is confirmed by pressing
  Enter. `--yes` no longer skips this at a terminal, so the menu wizard now
  works for machines it hasn't seen (without a terminal, pass `--host-key`).
  `--dry-run` never blocks on it, and login failures get a plain next step
  instead of "check the user name and password".
- `remove-node` now clears the join password k3s stored for the node, so the
  same name can be added again (a reinstalled machine was refused as a
  "duplicate hostname"), and skips the drain for a node that is not Ready.
- The menu header named the default route's interface (e.g. wlan0) next to
  an address that is on another (eth0).
- `add-node` left root-owned files behind in `/tmp` on the new machine
  ("Permission denied" while cleaning up), because it unpacked as root but
  cleaned up as the SSH user. Everything now runs as root in one step in
  its own directory, which removes itself.
- `add-node` now checks that the `--interface` you gave exists on the new
  machine before installing anything, and lists the ones it has.

## [0.1.0] - 2026-10-06

The foundation release. nodeyard is the successor to the single-file
`k3s-manager` 3.2.0 script, rebuilt as a modular tool. Every k3s-manager
command still works (`k3s-manager` is installed as an alias), with the
changes listed under "Changed".

### Added

- **One command, many front ends.** Every action is a `nodeyard` command
  with `--help`, `--yes`, `--dry-run` and `--json`. The interactive menu
  and its guided wizards only ever run those commands.
- **Interactive menu** with a status header (host, address, role, cluster
  health), guided wizards with back/cancel, a summary of exactly what will
  change before anything does, and a first-run quick-start that detects the
  machine and recommends a setup. Uses gum, whiptail or dialog when
  available and plain prompts otherwise; respects `NO_COLOR`.
- **Change journal and undo.** Every system file nodeyard writes is backed
  up first and recorded. `nodeyard changes` lists them and `nodeyard undo`
  reverts one change, the last change, or everything a feature did.
- **`--dry-run` everywhere**, printing each file diff and command instead
  of making changes; with `--json` it returns the plan as data.
- **One cluster config file** (`/etc/nodeyard/cluster.conf`, an INI format
  readable by plain bash) with `config show|get|set|unset|edit|validate|
  export|import|drift`. Validation explains each problem with its line
  number, and checks duplicate addresses, overlapping IP ranges and more.
- **Runtime detection** of distribution, package manager, init system,
  architecture, Raspberry Pi model, boot disk (SD card, eMMC, NVMe, USB),
  network backend (NetworkManager, netplan, systemd-networkd, ifupdown,
  dhcpcd, wicked) and firewall: `nodeyard detect`.
- **Dependency checks** that offer to install what's missing before work
  starts (`nodeyard deps`), and checksum-verified downloads of pinned
  third-party tools (`share/nodeyard/versions.lock`).
- **`doctor` rebuilt**: each check reports ok/warning/problem with a plain
  explanation; problems can be fixed one by one or all at once with
  `--fix`, and every fix can be undone. New checks for required tools,
  config validity and SD-card servers. `--json` and `--strict` added.
- **Demo mode** (`nodeyard --demo`): the whole tool runs against a
  simulated four-node cluster, changing nothing on the computer.
- **Logging with secret redaction**: join tokens, passwords, API keys and
  bearer tokens never reach the log file or the screen.
- **Secrets store** (`/etc/nodeyard/secrets`, root-only) and
  `nodeyard secrets list`.
- **Self-update from GitHub releases** (`nodeyard update`), verifying the
  release checksum and showing the changelog before installing.
- `install.sh` (one-line installer) and `uninstall.sh`; bash and zsh
  completion; bats test suite; multi-distro container test harness; CI and
  release workflows.

### Changed

- The join token is no longer printed by `nodeyard token` unless you add
  `--reveal`, and is never put on a command line: use `--token-file` or
  `--token-stdin` (`--token` still works but warns).
- `install`, `upgrade`, `stop`, `remove-node`, `uninstall` and other
  changing commands now ask for confirmation; scripts should pass `--yes`.
  Without a terminal and without `--yes` they stop with exit code 10.
- `uninstall` asks what to remove; `uninstall k3s` does what
  k3s-manager's `uninstall` did, and `uninstall everything` also reverts
  every recorded change and removes nodeyard.
- `add-node` verifies the other machine's SSH host key (you confirm its
  fingerprint, or pass `--host-key`) instead of accepting it silently.
- The distro is detected automatically instead of asking every time.
- Per-node settings moved from `/etc/k3s-manager/config.env` to the
  cluster config.

### Fixed

- `--dry-run` no longer writes kernel-module, sysctl and config files.
- `upgrade` on a worker no longer reinstalls it as a server (the k3s
  installer is now given the node's existing settings).
- `kubeconfig` no longer writes the admin kubeconfig to a predictable
  file in `/tmp`.
- An external datastore password is passed through k3s's root-only
  environment file instead of the world-readable service file.
- The watchdog no longer counts an HTTP 401 from a healthy API server as
  "unreachable", and installing it twice no longer duplicates settings.

### Security

- Secrets are redacted from logs, `--dry-run` output and JSON plans.
- Join tokens are stored with mode 0600 and passed to k3s by file.
- SSH host keys are verified on first connection.

[Unreleased]: https://github.com/Codemanhtmlpythoncss/nodeyard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Codemanhtmlpythoncss/nodeyard/releases/tag/v0.1.0
