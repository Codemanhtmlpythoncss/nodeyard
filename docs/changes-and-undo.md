# Changes and undo

Before nodeyard changes a system file it copies the original, and it
records every change it makes. So you can always see what nodeyard did,
and put it back.

```bash
sudo nodeyard changes                  # one line per command that changed something
sudo nodeyard changes --feature k3s    # only one feature's changes
sudo nodeyard changes --all            # include changes already undone
sudo nodeyard changes show ID          # every file and command in one change
sudo nodeyard undo ID                  # revert one change
sudo nodeyard undo --last              # revert the most recent change
sudo nodeyard undo --feature doctor    # revert everything one feature did
sudo nodeyard undo --last --dry-run    # see what undo would do
```

Undo:

- restores files nodeyard changed from their backups,
- removes files and empty directories nodeyard created,
- restores files nodeyard removed,
- reverses recorded commands, such as enabling a service or adding a
  firewall rule.

If something else edited a file after nodeyard wrote it, undo leaves it
alone and tells you; add `--force` to restore the backup anyway.

Changes to the cluster config are their own feature (`config`), so undoing
a feature (for example when uninstalling k3s) never rolls back your config
edits.

## Where it lives

`/var/lib/nodeyard/journal/` (root-only): `journal.jsonl` lists the changes
and `files/` holds the backups. Changes made by programs nodeyard runs
(k3s's own installer, your package manager) aren't backed up file by file;
those are reversed by their own uninstallers (`nodeyard uninstall k3s`).

## Removing everything

```bash
sudo nodeyard uninstall everything
```

removes k3s, undoes every remaining change in the journal (newest first),
then removes nodeyard itself.
