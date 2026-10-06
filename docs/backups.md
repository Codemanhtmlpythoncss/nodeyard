# Snapshots and backups

Today nodeyard can snapshot the cluster's state on a server:

```bash
sudo nodeyard snapshot save
sudo nodeyard snapshot list
sudo nodeyard snapshot restore NAME_OR_PATH    # destructive; asks first
```

- On an HA cluster (embedded etcd), `save` takes an etcd snapshot into
  `/var/lib/rancher/k3s/server/db/snapshots/`.
- On a single server with the default SQLite datastore, it saves a
  compressed copy of the database to `/var/backups/k3s-sqlite-*.tar.gz`
  (root-only).

`restore` stops k3s and resets the cluster to the snapshot. Anything
created after the snapshot is lost. It works on a single server; to
restore a multi-server cluster, follow
[k3s's etcd restore steps](https://docs.k3s.io/datastore/backup-restore):
restore on one server, then reset and rejoin the others.

Copy snapshots off the machine: a snapshot on a disk that dies is no
backup.

## Coming in 0.8

Scheduled backups of etcd, the cluster config, site data and volumes to
another node, a NAS or S3-compatible storage, with retention rules; a
restore wizard; and a command that proves a backup can actually be
restored.

## Also worth knowing

- `nodeyard config export` saves the cluster description, which (together
  with your workloads' manifests) is enough to rebuild the cluster.
- nodeyard backs up every system file before changing it, see
  [changes and undo](changes-and-undo.md). That's for undoing nodeyard,
  not a backup of your data.
