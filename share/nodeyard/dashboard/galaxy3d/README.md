# The 3D galaxy screens

A copy of the front end of [homelab-galaxy-dashboard](https://github.com/NetworkBound/homelab-galaxy-dashboard) by
NetworkBound, under the MIT licence (see `LICENSE` in this folder), fed from k3s by `../galaxy3d.py` instead of
Proxmox, UniFi and Zabbix.

What changed from the original, mechanically (so it can be refreshed from upstream the same way):

- every absolute path moved under `/g3/` (`/api/` -> `/g3/api/`, `/static/` -> `/g3/static/`, `/nexus` -> `/g3/nexus`);
- the pages' inline scripts moved into `static/page-*.js`, and the chat button's inline `onclick` became a listener,
  because nodeyard's pages only run scripts from files;
- `fetch` calls that send JSON also send nodeyard's `X-Nodeyard: 1` header (its cross-site request guard);
- a small "nodeyard" link at the bottom left of each page leads back to the rest of the dashboard;
- the setup wizard, the demo fixtures and the camera still are not included (nodeyard has its own setup and data).

`../galaxy3d.py` answers the same JSON API: k3s nodes are the galaxies, pods the stars (by category), nodeyard's alerts
the problems, the tailnet the clients, each node's measured network rate its link, the split model's RPC traffic a flow,
and the chat box asks the model with a summary of the cluster.
