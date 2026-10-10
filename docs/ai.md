# AI workloads

nodeyard runs [Ollama](https://ollama.com) on your machines, either on one
node or across the cluster, and can split one large model across several
nodes (experimental).

> Coming in 0.7: GPU driver setup, a single OpenAI-compatible endpoint that
> sends each request to a node that has the model loaded and has capacity,
> API keys with usage limits, model placement, a chat web interface, and
> benchmarks. What's below works today.

## One machine

```bash
sudo nodeyard ai install          # Ollama as a service on this node (port 11434)
sudo nodeyard ai model install llama3.2
sudo nodeyard ai uninstall [--purge-models]
```

## Across the cluster

```bash
sudo nodeyard ai deploy [--only NODE]... [--exclude NODE]... [--min-memory-gb N]
                        [--memory-limit 6Gi] [--nodeport 31434]
sudo nodeyard ai model install llama3.2 [--node NODE]   # onto every AI node
nodeyard ai model list [--node NODE]
sudo nodeyard ai model rm MODEL [--node NODE]
nodeyard ai status
nodeyard ai nodes                 # architecture, memory and AI label per node
sudo nodeyard ai undeploy [--keep-labels] [--keep-data]
```

`ai deploy` runs Ollama as a DaemonSet on the chosen nodes behind one
Kubernetes Service. Concurrent requests are spread across machines, but
every node keeps its **own full copy** of each model, so a model has to fit
on the smallest node. Rough guide for CPU-only nodes:

| Node RAM | Comfortable model size |
|---|---|
| 4 GB | 1-3B parameters (e.g. `llama3.2:1b`, `qwen2.5:1.5b`) |
| 8 GB | up to ~7-8B at 4-bit (e.g. `llama3.1:8b`, `qwen2.5:7b`) |
| 16 GB | up to ~14B at 4-bit |

The API is in-cluster by default; `--nodeport 31434` also exposes it on
every node's address (inside your network only).

## One big model across several machines (experimental)

**Experimental and slow over ethernet.** Every generated token passes
through every participating node, so speed is set by the slowest machine
and the network, and all nodes must stay on. It lets you run a model that
no single machine could.

```bash
nodeyard ai split plan [--model auto|owner/repo:file.gguf] [--main NODE] [--nodes a,b,c]
                       [--reserve NODE=GiB]... [--threads NODE=N]... [--main-only]
                       [--model-dir /path] [--no-cache a,b]
nodeyard ai split deploy [same options] [--ctx 16384] [--think on|off]
                         [--nodeport 31435 | --nodeport 0] [--api-key-file PATH] [--alias NAME]
nodeyard ai split status
nodeyard ai split test [--prompt TEXT] [--api-key-file PATH]
nodeyard ai split switch --model owner/repo:file.gguf [--keep-old] [deploy options]
nodeyard ai split unload | load
nodeyard ai split undeploy [--force] [--purge]
```

**NVIDIA graphics cards** speed it up. On the machine with the card (its
NVIDIA driver must already work: `nvidia-smi`), run `sudo nodeyard ai gpu
setup`; then on the server `sudo nodeyard ai gpu enable NODE`. When that node
is the main node, the plan puts the fastest share of the model on its card
(the card's video memory minus 1 GiB), and the main server runs llama.cpp's
Vulkan build. `--no-gpu` leaves the card out.

`switch` changes the running model and cleans up after the old one: it
unloads it (every node gets its memory back), deletes its file and weight
caches from every node (`--keep-old` keeps them), then downloads the new
model if needed and loads it. `--model local:FILE.gguf` (or just the file
name) runs a model that is already downloaded on a node, from that node.
`undeploy` always unloads first, so nothing is left running while it removes
the model. `undeploy --force` immediately removes model-server and download
pods before removing the split namespace; completed model files and weight
caches stay on disk unless you also pass `--purge`. The dashboard's **Force
stop** button does this after confirmation. Running dashboard tasks can also
be cancelled from their progress window.

The dashboard saves its last disk inventory locally. If a Kubernetes restart
temporarily hides a node, its models remain listed as **last seen** for up to
30 days and are refreshed when the node rejoins.

Dashboard chat accepts several text/code/data attachments and PNG, JPEG, WebP,
GIF or BMP images in one message. Images are resized in the browser and sent
to the selected model as visual input; OCR needs a vision-capable model. Image
data is not stored in browser history, so attach images again after a reload.

The dashboard's **AI model** menu (top of the AI page) lists the models
downloaded on your nodes and switches between them the same way.

In the dashboard's Ollama section, **Load** keeps a model in memory until you
choose **Unload** or Ollama restarts (unless automatic unloading, below, is on).
Chats through Nodeyard keep it that way: Ollama's OpenAI-compatible endpoint
ignores `keep_alive`, so each chat used to reset a loaded model to Ollama's
5-minute default and it reloaded mid-task. The dashboard now sets the intended
keep-alive again after every request it forwards. The disk inventory is saved by the
dashboard and in the browser, so recent model locations remain visible while
unavailable nodes recover; a complete disk scan refreshes that saved list.

It uses [llama.cpp RPC](https://github.com/ggml-org/llama.cpp/tree/master/tools/rpc):
each node runs an RPC server holding a share of the model's layers, sized
from its live free memory minus a reserve; one **main** node runs
`llama-server`, which downloads the model (resumable, checksum-verified,
disk-space checked), sends each node its share, and serves a chat page and
an OpenAI-compatible API (`/v1/chat/completions`) on port 31435.

By default (`--model auto`) it picks the biggest quantization of
Qwen3.6-35B-A3B (a mixture-of-experts model that activates ~3B parameters
per token) that fits in the cluster's free memory right now.

Choosing the main node matters most: give it a real disk (not an SD card
or USB stick; use `--model-dir` to put the model on a bigger partition)
and gigabit ethernet. A main node that's short on memory can just
coordinate (`--main-only`). Keep more memory free on a machine you use
day to day with `--reserve laptop=4`.

Protect the API with a key: `--api-key-file PATH` (the key is read from a
file and stored as a Kubernetes Secret).

### Automatic model unloading

Models > **Automatic model unloading** (also in the Nodeyard AI Mac app's Settings) frees memory from models
nobody is using. It is **off** until you turn it on; the idle time defaults to 30 minutes (presets 5 min to
2 hours, or a custom 1 minute to 7 days). The setting is stored once, in the dashboard's
`/var/lib/nodeyard/dashboard/prefs.json`, so the page, the control API (`GET`/`POST /api/v1/lifecycle`) and
the Mac app always show the same value, and it survives restarts.

| Backend | How idle time is measured | What "unload" does |
|---|---|---|
| Split model (llama.cpp across nodes) | llama.cpp's own `/slots` (`id_task`, `is_processing`), checked every 15 s, plus chats the dashboard forwards. Requests that go straight to the model (yardcode, the dashboard's skills, other programs) count as use. | `nodeyard ai split unload`: every server of the model (main and RPC nodes) stops together; files and weight caches stay on disk. |
| Ollama | Ollama's own keep-alive timer, which every request resets. | The dashboard gives each model the idle time as its keep-alive after every request through Nodeyard and when you **Load** it; Ollama unloads it from that node when the time runs out. |

Safety rules, whether the setting is on or off:

- A model that is answering, or has a request queued, is never unloaded; the dashboard checks `/slots` again
  right before it acts. If it can't read `/slots` it shows **activity unknown** and never unloads that model.
- It never runs while another model task (load, switch, removal, download) is running, and it never loads a
  model back by itself. Sending a chat or choosing the model loads it again.
- A failed unload is shown with its error and retried after 5, 10, 20... minutes (at most hourly); monitoring
  carries on.
- Turning it on starts every model's idle time from that moment, so nothing is unloaded the instant you enable
  it. Turning it off stops all automatic unloads of the split model. Ollama models loaded from the page go back to
  staying loaded; other Ollama models keep the timer they were last given until their next use.
- Requests sent directly to an Ollama pod's own port (not through Nodeyard) use Ollama's default timer.

Diagnosing: the card shows each model's state (checking, idle, processing, unload pending, unloading,
unloaded automatically, unload failed, activity unknown) and the last 30 actions. The dashboard's service log
has the same lines (`journalctl -u nodeyard-dashboard | grep lifecycle`). If a model server keeps restarting,
Alerts now say when Kubernetes killed it for memory (**OOMKilled**): a model server that restarts reloads the
model and interrupts answers, so use a smaller context or quant, or spread the model over more nodes.

## Research Mode

The dashboard's AI page has a **Research** tab (also `POST /api/v1/research` with the server API key, for the Mac app and
scripts). Ask a question, pick the model that writes the report and a depth:

| Depth | Searches | Pages read |
|---|---|---|
| Quick | 1 (the question itself) | 3 |
| Standard | up to 3, planned by the model | 6 |
| Deep | up to 4, planned by the model | 10 |

It runs on the server, so it keeps going if you close the page, and every session is saved
(`/var/lib/nodeyard/dashboard/research/`, the last 60). The steps:

1. **Plan**: the model turns the question into a few searches. If it can't, the question itself is searched.
2. **Search**: from the server's internet connection (the same engines as yardcode's web tools; private network
   addresses are never fetched). Duplicate pages and more than two pages per site are dropped.
3. **Read**: the pages are fetched and the paragraphs that match the question are kept, with the time each page was
   read. Pages that can't be read stay listed as search results with the reason.
4. **Write**: the model writes the report from those passages only, citing them as [n], in four sections: Answer, Key
   findings, Where sources disagree, Not verified.
5. **Check**: every [n] is checked against the sources really read. The report warns when it cites nothing, cites a
   number that matches no source, or cites a page that couldn't be read. The source list is written by Nodeyard, not by
   the model, so it never contains made-up links.

Click a citation to open its source and the passage that was read. **Export Markdown** saves the report with its
sources and citation check; **Discuss in chat** starts a chat that has the report and sources. On a large model on CPU
nodes, writing the report can take several minutes; the steps list shows what it is doing, and **Cancel** stops it
(including the model's work).

Limits: research reads public web pages only. It doesn't sign in to sites, fill forms or run JavaScript-heavy pages
(that needs browser automation). A page behind a cookie wall or paywall usually shows as "couldn't read".

## Terminal AI agent

[yardcode](../yardcode/README.md) is a coding agent for your terminal that uses this model API (and any OpenAI-compatible one). It can also load models on the cluster: `yardcode models load NAME`. It installs with nodeyard.
