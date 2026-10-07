# shellcheck shell=bash
# Snapshots of the cluster state (ported from k3s-manager): etcd snapshots on
# HA servers, a tarball of the SQLite datastore otherwise. Scheduled backups,
# remote targets and restore verification come in a later phase.

ny_cmd "snapshot save" backup_snapshot_save_cmd "Backups" "Take a snapshot of the cluster state now" backup
ny_cmd "snapshot list" backup_snapshot_list_cmd "Backups" "List cluster-state snapshots on this server" backup
ny_cmd "snapshot restore" backup_snapshot_restore_cmd "Backups" "Restore the cluster state from a snapshot (destructive)" backup

BACKUP_SQLITE_DIR="/var/backups"

backup_need_k3s() {
    ny_need_root
    ny_k3s_installed || ny_die "k3s is not installed on this machine." "Run this on a k3s server." "$NY_E_PRECONDITION"
}

backup_snapshot_save_cmd() {
    backup_need_k3s
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local name err
    name="nodeyard-snapshot-$(printf '%(%Y%m%d-%H%M%S)T' -1)"
    err="$(ny_mktemp)"
    if ny_run "$(ny_path "$NY_K3S_BIN")" etcd-snapshot save --name "$name" 2>"$err"; then
        ny_ok "etcd snapshot saved: ${name} (in /var/lib/rancher/k3s/server/db/snapshots/)"
        return 0
    fi
    if grep -qiE 'etcd (is not running|datastore disabled)|not supported|sqlite' "$err" 2>/dev/null; then
        ny_info "This server uses the SQLite datastore (not etcd), so a copy of its database is saved instead."
        local dest
        dest="${BACKUP_SQLITE_DIR}/k3s-sqlite-$(printf '%(%Y%m%d-%H%M%S)T' -1).tar.gz"
        ny_ensure_dir "$BACKUP_SQLITE_DIR" 0700
        ny_run tar -czf "$(ny_path "$dest")" -C "$(ny_path /var/lib/rancher/k3s/server)" db ||
            ny_die "Saving the SQLite datastore failed." "Check free space in ${BACKUP_SQLITE_DIR} (df -h ${BACKUP_SQLITE_DIR})."
        ny_simulating || chmod 600 "$(ny_path "$dest")"
        ny_ok "SQLite datastore saved to ${dest}"
        return 0
    fi
    sed 's/^/    /' "$err" >&2
    ny_die "The etcd snapshot failed (output above)." "Check that k3s is running: sudo nodeyard status"
}

backup_snapshot_list_cmd() {
    backup_need_k3s
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if ! "$(ny_path "$NY_K3S_BIN")" etcd-snapshot ls 2>/dev/null; then
        printf 'No etcd snapshots (this server uses SQLite). SQLite copies:\n'
        ls -lh "$(ny_path "$BACKUP_SQLITE_DIR")"/k3s-sqlite-*.tar.gz 2>/dev/null || printf '  (none)\n'
    fi
    return 0
}

backup_snapshot_restore_cmd_help() {
    cat <<'HELP'
Usage: nodeyard snapshot restore SNAPSHOT [--yes]

Stops k3s and resets the cluster state to SNAPSHOT (a name from
'nodeyard snapshot list', or a path). This is destructive: anything created
after the snapshot is lost. Works on a single server; restoring a
multi-server (HA) cluster also needs the other servers reset and rejoined,
see docs/backups.md.
HELP
}

backup_snapshot_restore_cmd() {
    backup_need_k3s
    local snap="${1:-}"
    [[ -n "$snap" ]] || ny_usage_error "Say which snapshot to restore." "nodeyard snapshot restore SNAPSHOT_NAME_OR_PATH"
    [[ $# -le 1 ]] || ny_usage_error "Unexpected argument: $2"
    local path="$snap"
    if [[ ! -f "$(ny_path "$snap")" ]]; then
        [[ "$snap" =~ ^[A-Za-z0-9._-]+$ ]] || ny_usage_error "'${snap}' is not a snapshot name or an existing file."
        path="/var/lib/rancher/k3s/server/db/snapshots/${snap}"
    fi
    ny_warn "Restoring stops k3s and replaces the cluster state with the snapshot. Changes made since it was taken are lost."
    ny_confirm "Restore the cluster from '${snap}'?" n || {
        ny_info "Cancelled."
        return 0
    }
    ny_run systemctl stop k3s
    ny_run "$(ny_path "$NY_K3S_BIN")" server --cluster-reset --cluster-reset-restore-path="$path"
    ny_run systemctl start k3s
    ny_wait_service k3s || ny_die "k3s did not start after the restore." "Check: sudo journalctl -u k3s -n 100 --no-pager"
    ny_ok "Restore complete; k3s restarted."
}
