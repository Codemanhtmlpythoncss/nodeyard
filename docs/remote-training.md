# Remote model training

nodeyard can train one model on several machines at once and keep the best copy. The
machines talk to each other over Tailscale. Each training copy runs as a separate
process, starts on the machine with the most free capacity, and a new copy starts as
soon as a slot frees up. When every copy has finished, the copy with the lowest
validation loss is kept. The dashboard's Kubernetes inventory is the device list;
offline or unsupported nodes are shown but never scheduled.

Two pieces make this work:

- **The master** runs a second SSH service on its Tailscale address (port 2222 by
  default). Passwords are refused, root cannot log in, only the training user can,
  and only keys from the tailnet (100.64.0.0/10) are accepted. The normal SSH service
  on port 22 is not changed.
- **This Mac** (or any other client) uses a key pair to connect, and the runner
  (`nyjobs`) places the copies.

## Set up from the Mac

The Nodeyard AI app's **AI → Remote training → Set up remote training** does these steps
for you. From a terminal, the equivalent one-time setup is:

```sh
nodeyard jobs setup-master --address MASTER_TAILSCALE_IP --user TRAINING_USER
```

It creates a dedicated app key, uses the password only for the initial login (if needed),
sets up the key-only training service on the master, installs CPU PyTorch, and uses the
master's Kubernetes API to set up every Ready x86_64/arm64 worker. It does not write the
password to disk or change port 22. The helper skips offline nodes and 32-bit Arm nodes
that do not have a compatible PyTorch CPU wheel. In the app, enter the password in its
one-time field; it is discarded immediately after setup.

To preview the master service config without changing it:

```sh
nodeyard jobs access setup --print --public-key "..." --tailscale-ip 100.x.y.z --user YOUR_USER
```

Install the training runtime (PyTorch and numpy) as the training user:

```sh
nodeyard jobs access runtime --venv ~/nodeyard-train
```

## Connect this Mac

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519        # set a passphrase and use ssh-agent
nodeyard jobs access-key                           # paste this into the master setup above
nodeyard jobs add-host master barnaby.taile31da3.ts.net --user YOUR_USER --port 2222 \
    --python "$HOME/nodeyard-train/bin/python"     # the Python on the master
nodeyard jobs probe                                # read-only: architecture, CPUs, memory, torch
nodeyard jobs hosts
```

## Train

The same runner is in Nodeyard AI: open AI > Remote training.

```sh
nodeyard jobs discover                           # every Kubernetes node and readiness
nodeyard jobs plan --copies 6                     # where copies would go
nodeyard jobs run --data corpus.txt --copies 6 --steps 3000 --assistant-only
nodeyard jobs run --data corpus.txt --device debian-1:3 --device k8s-control:2 --steps 3000
nodeyard jobs run --data corpus.txt --copies 2 --local  # this Mac only
nodeyard jobs run --data corpus.txt --device debian-1:2 --resume-from ~/.../BPEFromScratch \
    --install-best-to ~/.../BPEFromScratch --steps 1000  # continue the Mac's BPE weights remotely

`NODEYARD_JOBS_PYTHON` picks the Python with torch and numpy, for example
`NODEYARD_JOBS_PYTHON=$HOME/nodeyard-train/bin/python nodeyard jobs run ...`.
```

Each job writes to `~/nodeyard-jobs/<job>/`: one folder per copy with its log and
model, `results.json` with every copy's validation loss, and `best/` with the copy that
won.

When more than one copy runs, the runner evaluates them every ten steps by default,
pauses them at the same step, and broadcasts the lowest-validation-loss weights to
every copy before continuing. This keeps the copies on the best trajectory, but transfers
checkpoint weights between devices and therefore adds network overhead. Set
`--sync-every 0` to disable these barriers, or choose another interval with
`--sync-every N`. All selected copies must fit in the devices' current parallel slots so
they can meet at each barrier.

## Security notes

- The service listens only on the master's Tailscale address, so it is not reachable
  from the LAN or the internet.
- Keys are installed with `restrict` and `from="100.64.0.0/10,..."`, so each key can
  only run commands from the tailnet, with no forwarding and no pseudo-terminal.
- `sudo nodeyard jobs access remove` removes the service, its firewall rule, and the
  key entries, and leaves port 22 alone.
- Nothing is installed on a machine except the runtime you choose to install.

## Limits

- Each copy uses one thread per free CPU, so copies on CPU-only machines are slower
  than on this Mac's GPU. More copies give a better best-of-N, not a faster single run.
- Remote continuation shares BPE model weights and tokenizer exactly; AdamW moment state
  starts fresh on the worker. The best returned weights replace the local current model,
  and the prior local checkpoint is archived.
- The remote trainer is the PyTorch BPE GPT (`train_bpe_gpt_torch.py`). It uses the
  same recipe and log format as the app's MLX trainer, and the two share a tokenizer.
- Only text files are supported; SQLite sources are not.
