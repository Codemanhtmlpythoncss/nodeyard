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
nodeyard ai split undeploy [--purge]
```

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
