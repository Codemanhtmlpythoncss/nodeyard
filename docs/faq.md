# FAQ

**Why Bash?**
It's on every Linux machine already, it's what most homelab setup guides
use, and it's readable by the people who run it. Each command is an
ordinary shell command you could type yourself, and `--dry-run` shows
exactly which. The dashboard's agent (0.5) is a small Go binary because a
web server and TLS don't belong in Bash.

**Is it safe to run twice?**
Yes. Every action checks the current state first; writing a file with the
same content does nothing, and re-running an install reconciles settings.

**What exactly will it change?**
Add `--dry-run` to any command: it prints every file it would write (as a
diff) and every command it would run, and changes nothing. Afterwards,
`sudo nodeyard changes` lists what it did and `sudo nodeyard undo` reverts it.

**Can I try it without hardware?**
Yes: `nodeyard --demo`. See [demo mode](demo-mode.md).

**Does it work with two machines?**
Yes, as one server plus one worker. It won't call that "highly
available": with embedded etcd, two servers are less reliable than one,
because losing either stops the cluster. Three servers survive one
failing. See [k3s](k3s.md#how-many-servers).

**Can I use it on a Mac or Windows machine?**
nodeyard manages Linux machines. A remote installer that sets them up from
a Mac or Linux laptop is coming in 0.3, and demo mode runs on a Mac with
Homebrew's bash.

**Where does it keep things?**
Program: `/usr/local/lib/nodeyard`. Config: `/etc/nodeyard/cluster.conf`.
Secrets: `/etc/nodeyard/secrets/` (root-only). State, journal and backups:
`/var/lib/nodeyard/`. Log: `/var/log/nodeyard/nodeyard.log`.

**Does it phone home?**
No. It downloads only what you ask it to install (k3s, Ollama, gum, its
own updates), from their official sources over HTTPS.

**I used k3s-manager. Do I need to change anything?**
Your commands still work (`k3s-manager` is an alias). See
[upgrading](upgrading.md#from-k3s-manager).

**Will it host my website / give me a dashboard / set static IPs?**
Those are on the [roadmap](STATUS.md): static IPs and a network device view
(0.2), the remote installer (0.3), a floating virtual IP (0.4),
dashboards (0.5), website hosting with HTTPS (0.6), an AI router (0.7),
backups, alerts and power management (0.8).

**Does it need a paid service?**
No. Everything works with free software and free tiers. For public
websites (0.6), Tailscale Funnel or a free subdomain are the defaults.
