# The menu and wizards

Run `sudo nodeyard` with no command for the interactive menu.

![The nodeyard menu in demo mode](media/menu.svg)

The status line at the top shows this machine's hostname, address,
cluster role and the cluster's health. The menu lists only what makes sense
on this machine: a machine that isn't in a cluster yet is offered
**Create a cluster** and **Join a cluster**; a server is offered
**Install on other machines**.

## The first run

The first time, nodeyard offers a quick-start. It detects the machine
(model, OS and whether it's supported, CPU, memory, boot disk, wired and
wireless interfaces, which service manages the network) and explains what
that means: for example that a Raspberry Pi running from an SD card is
fine as a worker but better on an SSD as a server. Then it asks whether a
cluster already exists and how many machines yours will have, recommends a
layout (one server; one server plus workers; or three HA servers), and
opens the matching wizard. Run it again any time with `sudo nodeyard quickstart`.

## Wizards

Each wizard asks for one thing at a time, with a sensible default and a
short explanation, and checks your answer before moving on.

- **Back** goes to the previous question (`b` in plain prompts, the Back
  button in whiptail/dialog, Esc in gum).
- **Cancel** stops without changing anything (`q`, Esc, or Ctrl-C).
- Before anything changes you get a **summary**: the exact command, and
  every file it would write and command it would run (its `--dry-run`).
  Choose **Apply**, go back to change answers, or cancel.

Every wizard runs one ordinary command, and the summary shows it, so you
can copy it into a script. Wizards available today: `install-master`,
`install-worker`, `install-join-master`, `add-node`, `ai-deploy`,
`ai-model-install`, `snapshot-restore` and `k3s-upgrade`. You can run one
directly: `sudo nodeyard wizard install-worker`.

## Interface styles

nodeyard uses the best interface available:

1. [gum](https://github.com/charmbracelet/gum), the nicest. Install it
   with `sudo nodeyard ui install-gum` (a pinned, checksum-verified
   download).
2. whiptail or dialog: boxed menus that work over any SSH session.
3. Plain numbered prompts, which work everywhere, including slow
   connections and screen readers.

Pick one in **Settings > Interface style**, with
`nodeyard config set ui.backend plain`, or for one run with
`NODEYARD_UI=plain sudo -E nodeyard`. Colour follows `NO_COLOR`,
`--no-color` and `ui.color`. Narrow terminals switch boxed dialogs to
plain prompts, and tables shrink to fit.
