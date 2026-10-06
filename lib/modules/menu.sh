# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# The interactive terminal menu, its status header, and the first-run
# quick-start wizard. Every menu entry runs a registered command or a wizard
# for one, so the menu can never behave differently from the command line.

ny_cmd "menu" menu_cmd "Start here" "Open the interactive menu (the default with no command)" menu
ny_cmd "quickstart" menu_quickstart_cmd "Start here" "Detect this machine and recommend a setup" menu
ny_cmd "wizard" menu_wizard_cmd "Start here" "Run a guided wizard by name (e.g. install-master)" menu

MENU_QUICKSTART_MARKER="quickstart-done"

# menu_header -- one status line: version, host, address, role, cluster health.
menu_header() {
    k3s_load_state
    local dev src gw addr role health="no cluster"
    read -r dev src gw <<<"$(ny_primary_route)"
    addr="${K3S_NODE_IP:-${src#-}}"
    role="${K3S_ROLE:-not set up}"
    if ny_k3s_installed; then
        local svc
        svc="$(k3s_service_name)"
        if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
            health="$(ny_color red "k3s stopped")"
        elif [[ "$K3S_ROLE" == server ]] && kctl_available; then
            local raw total ready
            raw="$(kctl get nodes --no-headers --request-timeout=4s 2>/dev/null || true)"
            total="$(grep -c . <<<"$raw" || true)"
            ready="$(awk '$2=="Ready"{n++} END{print n+0}' <<<"$raw")"
            if [[ "$total" -gt 0 && "$ready" -eq "$total" ]]; then
                health="$(ny_color green "${ready}/${total} nodes Ready")"
            elif [[ "$total" -gt 0 ]]; then
                health="$(ny_color yellow "${ready}/${total} nodes Ready")"
            else
                health="$(ny_color yellow "API not answering")"
            fi
        else
            health="$(ny_color green "k3s running")"
        fi
    fi
    local sep
    sep="$(ny_color dim " | ")"
    printf '\n%s%s%s%s%s%s%s%s%s\n' "$(ny_color bold "nodeyard ${NY_VERSION}")" "$sep" "$(hostname 2>/dev/null || uname -n)" "$sep" \
        "${addr:-no address}${dev:+$([[ $dev != - ]] && printf ' (%s)' "$dev")}" "$sep" "role: ${role}" "$sep" "cluster: ${health}" >&2
    [[ "$NY_DEMO" -eq 1 ]] && printf '%s\n' "$(ny_color yellow "DEMO MODE - simulated machines; nothing on this computer is changed")" >&2
    return 0
}

menu_run() {
    # A failed or cancelled action returns to the menu instead of exiting it.
    local rc=0
    ny_nodeyard "$@" || rc=$?
    [[ "$rc" -eq 0 || "$rc" -eq "$NY_E_CANCELLED" ]] || ny_warn "That ended with an error (exit ${rc}); see the message above."
    ny_ui_pause
}

menu_wizard() {
    ny_wizard_run "$@" || true
}

menu_cmd_help() {
    cat <<'HELP'
Usage: nodeyard menu

The interactive menu. It runs the same commands you can type, through
guided wizards that show a summary and ask before changing anything.
Works with gum, whiptail/dialog, or plain prompts (NODEYARD_UI=plain).
HELP
}

menu_cmd() {
    ny_need_root
    ny_ui_interactive || ny_die "The menu needs an interactive terminal." "Run 'nodeyard help' to see the commands to use instead." "$NY_E_PRECONDITION"
    ny_deps_ensure "the interactive menu" jq
    if [[ ! -f "$NY_CONFIG" && ! -f "${NY_STATE}/${MENU_QUICKSTART_MARKER}" ]]; then
        menu_header
        if ny_ui_yesno "This looks like the first run. Start the quick-start (detects this machine and recommends a setup)?" y; then
            menu_quickstart_cmd || true
        else
            menu_mark_quickstart_done
        fi
    fi
    while true; do
        k3s_load_state
        menu_header
        local -a items=()
        if [[ -z "$K3S_ROLE" ]]; then
            items+=($'create\tCreate a cluster (this machine becomes the first server)')
            items+=($'join\tJoin this machine to an existing cluster')
        fi
        if [[ "$K3S_ROLE" == server ]]; then
            items+=($'workerinfo\tWhat a worker needs to join (address, ports, commands)')
            items+=($'add\tInstall on other machines (add a node over SSH)')
        fi
        [[ -n "$K3S_ROLE" ]] && items+=($'status\tCluster status')
        items+=($'ai\tAI workloads' $'health\tHealth check (doctor)' $'backups\tBackups' $'updates\tUpdates' $'settings\tSettings')
        items+=($'uninstall\tUninstall' $'quit\tExit')
        local choice
        NY_UI_BACK=0
        NY_UI_TITLE="nodeyard"
        choice="$(ny_ui_choose "What do you want to do?" "" "${items[@]}")" || choice="quit"
        case "$choice" in
            create) menu_wizard install-master ;;
            join) menu_join ;;
            workerinfo) menu_worker_info ;;
            add) menu_wizard add-node ;;
            status) menu_run status ;;
            ai) menu_ai ;;
            health) menu_run doctor ;;
            backups) menu_backups ;;
            updates) menu_updates ;;
            settings) menu_settings ;;
            uninstall) menu_run uninstall ;;
            quit | *) return 0 ;;
        esac
    done
    return 0
}

menu_worker_info() {
    ny_nodeyard worker-info || true
    if ny_ui_yesno "Show the join token too? (anyone who has it can add machines to your cluster)" n; then
        ny_nodeyard token --reveal || true
    fi
    ny_ui_pause
}

menu_join() {
    local c
    c="$(ny_ui_choose "Join as" "worker" \
        $'worker\tA worker (runs workloads)' \
        $'server\tAn additional server (control plane; the cluster must have been created with HA)' \
        $'back\tBack')" || return 0
    case "$c" in
        worker) menu_wizard install-worker ;;
        server) menu_wizard install-join-master ;;
    esac
    return 0
}

menu_ai() {
    while true; do
        local c
        c="$(ny_ui_choose "AI workloads" "status" \
            $'status\tShow AI deployments and models' \
            $'nodes\tShow which nodes can run AI' \
            $'deploy\tRun Ollama across the cluster' \
            $'install\tInstall Ollama on this machine only' \
            $'pull\tDownload a model everywhere' \
            $'list\tList models on each node' \
            $'split\tExperimental: one big model split across nodes' \
            $'back\tBack')" || return 0
        case "$c" in
            status) menu_run ai status ;;
            nodes) menu_run ai nodes ;;
            deploy) menu_wizard ai-deploy ;;
            install) menu_run ai install ;;
            pull) menu_wizard ai-model-install ;;
            list) menu_run ai model list ;;
            split)
                ny_warn "Splitting one model across machines is experimental and slow over ethernet: every token crosses the network."
                menu_run ai split plan
                ;;
            *) return 0 ;;
        esac
    done
    return 0
}

menu_backups() {
    while true; do
        local c
        c="$(ny_ui_choose "Backups" "save" \
            $'save\tSnapshot the cluster state now' \
            $'list\tList snapshots' \
            $'restore\tRestore from a snapshot (destructive)' \
            $'back\tBack')" || return 0
        case "$c" in
            save) menu_run snapshot save ;;
            list) menu_run snapshot list ;;
            restore) menu_wizard snapshot-restore ;;
            *) return 0 ;;
        esac
    done
    return 0
}

menu_updates() {
    while true; do
        local c
        c="$(ny_ui_choose "Updates" "check" \
            $'check\tCheck for a new nodeyard version' \
            $'update\tUpdate nodeyard' \
            $'k3s\tUpgrade k3s on this node' \
            $'back\tBack')" || return 0
        case "$c" in
            check) menu_run update --check ;;
            update) menu_run update ;;
            k3s) menu_wizard k3s-upgrade ;;
            *) return 0 ;;
        esac
    done
    return 0
}

menu_settings() {
    while true; do
        local c
        c="$(ny_ui_choose "Settings" "machine" \
            $'machine\tWhat nodeyard detected about this machine' \
            $'network\tNetwork interfaces' \
            $'config\tShow the cluster config' \
            $'validate\tCheck the cluster config' \
            $'edit\tEdit the cluster config' \
            $'drift\tCompare this node with the config' \
            $'changes\tChanges nodeyard made (and undo)' \
            $'kubeconfig\tSet up kubectl access' \
            $'firewall\tFirewall status' \
            $'deps\tRequired tools' \
            $'ui\tInterface style' \
            $'quickstart\tRun the quick-start again' \
            $'back\tBack')" || return 0
        case "$c" in
            machine) menu_run detect ;;
            network) menu_run network-info ;;
            config) menu_run config show ;;
            validate) menu_run config validate ;;
            edit) ny_nodeyard config edit || true ;;
            drift) menu_run config drift ;;
            changes) menu_changes ;;
            kubeconfig) menu_run kubeconfig ;;
            firewall) menu_run firewall status ;;
            deps) menu_run deps ;;
            ui) menu_ui_style ;;
            quickstart) menu_quickstart_cmd || true ;;
            *) return 0 ;;
        esac
    done
    return 0
}

menu_changes() {
    ny_nodeyard changes || true
    local c
    c="$(ny_ui_choose "Undo something?" "no" $'no\tNo' $'last\tUndo the most recent change' $'pick\tUndo a specific change')" || return 0
    case "$c" in
        last) menu_run undo --last ;;
        pick)
            local id
            id="$(ny_ui_input "Change ID (from the list above)" "" string)" || return 0
            [[ -n "$id" ]] && menu_run undo "$id"
            ;;
    esac
    return 0
}

menu_ui_style() {
    local -a items=($'auto\tAutomatic (best available)')
    have gum && items+=($'gum\tgum')
    have gum || items+=($'install-gum\tInstall gum (nicest; downloads ~5 MB, checksum-verified)')
    have whiptail && items+=($'whiptail\twhiptail boxes')
    have dialog && items+=($'dialog\tdialog boxes')
    items+=($'plain\tPlain prompts (best for slow SSH or screen readers)')
    local c
    c="$(ny_ui_choose "Interface style" "$(ny_cfg_get ui "" backend auto)" "${items[@]}")" || return 0
    if [[ "$c" == install-gum ]]; then
        menu_run ui install-gum
        return 0
    fi
    menu_run config set ui.backend "$c"
    NY_UI=""
}

# --- quick-start -------------------------------------------------------------

menu_mark_quickstart_done() {
    mkdir -p -- "$NY_STATE" 2>/dev/null || true
    : >"${NY_STATE}/${MENU_QUICKSTART_MARKER}" 2>/dev/null || true
}

menu_quickstart_cmd_help() {
    cat <<'HELP'
Usage: nodeyard quickstart

Detects this machine's hardware, OS and network, explains what that means
for a cluster, recommends a setup, and starts the matching wizard.
HELP
}

menu_quickstart_cmd() {
    ny_need_root
    ny_ui_interactive || ny_die "The quick-start needs an interactive terminal." "Run 'nodeyard detect' to see what was detected." "$NY_E_PRECONDITION"
    ny_detect_all
    local dev src gw backend
    read -r dev src gw <<<"$(ny_primary_route)"
    backend="$(ny_detect_netbackend "${dev#-}")"
    local -a eth=() wifi=()
    local name kind state cidr
    while IFS=$'\t' read -r name kind state cidr; do
        [[ "$kind" == ethernet ]] && eth+=("${name}${cidr:+ ${cidr}} (${state})")
        [[ "$kind" == wifi ]] && wifi+=("${name}${cidr:+ ${cidr}} (${state})")
    done < <(ny_list_ifaces)

    printf '\n%s\n' "$(ny_color bold "Quick-start: what this machine is")" >&2
    printf '  %-12s %s\n' "Machine:" "$NY_HW_MODEL" "OS:" "${NY_OS_LABEL} ($(ny_status_color "$NY_OS_SUPPORT"))" \
        "CPU / RAM:" "${NY_CPUS} cores, $(awk -v m="$NY_RAM_MB" 'BEGIN{printf "%.1f", m/1024}') GiB" \
        "Boot disk:" "$NY_BOOT_DISK" "Ethernet:" "$( ((${#eth[@]})) && ny_join ', ' "${eth[@]}" || echo none)" \
        "Wi-Fi:" "$( ((${#wifi[@]})) && ny_join ', ' "${wifi[@]}" || echo none)" \
        "Network:" "${src#-} via ${gw#-}, managed by ${backend}" >&2

    printf '\n%s\n' "$(ny_color bold "What that means")" >&2
    local notes=0
    if [[ "$NY_OS_SUPPORT" != supported ]]; then
        ny_warn "${NY_OS_LABEL} is ${NY_OS_SUPPORT}. ${NY_OS_NOTE}"
        notes=1
    fi
    if ((NY_RAM_MB > 0 && NY_RAM_MB < 1800)); then
        ny_warn "With $((NY_RAM_MB)) MiB of RAM this machine should be a worker, not a server (k3s servers need about 2 GiB)."
        notes=1
    fi
    if [[ "$NY_BOOT_DISK" == sd ]]; then
        ny_warn "It runs from an SD card. That's fine for a worker; for a server, an SSD is much more reliable (etcd writes constantly)."
        notes=1
    fi
    if [[ "${#eth[@]}" -eq 0 ]]; then
        ny_warn "No wired ethernet interface was found. A cluster is far more reliable over the switch than over Wi-Fi."
        notes=1
    fi
    [[ "$notes" -eq 1 ]] || ny_ok "This machine looks well suited to run k3s."
    local is_worker_only=0
    ((NY_RAM_MB > 0 && NY_RAM_MB < 1800)) && is_worker_only=1

    local existing count
    existing="$(ny_ui_choose "Is there already a cluster on your network?" "no" \
        $'no\tNo - this is the first machine' \
        $'yes\tYes - this machine should join it')" || {
        menu_mark_quickstart_done
        return 0
    }
    if [[ "$existing" == yes ]]; then
        menu_mark_quickstart_done
        if [[ "$is_worker_only" -eq 1 ]]; then
            ny_info "Recommended: join as a worker."
            menu_wizard install-worker
        else
            menu_join
        fi
        return 0
    fi
    count="$(ny_ui_choose "How many machines will the cluster have in total?" "3" \
        $'1\tJust this one' $'2\tTwo' $'3\tThree or more')" || {
        menu_mark_quickstart_done
        return 0
    }
    menu_mark_quickstart_done
    case "$count" in
        1)
            ny_info "Recommended: a single server that also runs your workloads. You can add machines later."
            menu_wizard install-master ha=no worker=yes
            ;;
        2)
            ny_info "Recommended: this machine as the server (also running workloads) and the other as a worker."
            ny_hint "Two servers would be LESS reliable than one: etcd needs a majority, so losing either would stop the cluster."
            ny_hint "Take regular snapshots (Backups menu) so the cluster can be restored if the server dies."
            menu_wizard install-master ha=no worker=yes
            ;;
        *)
            ny_info "Recommended: three servers with embedded etcd (high availability: any one can fail)."
            ny_hint "This machine creates the cluster; join the next two as additional servers, any more as workers."
            [[ "$is_worker_only" -eq 1 ]] && ny_warn "This machine is small for a server; consider creating the cluster on a bigger one."
            menu_wizard install-master ha=yes worker=yes
            ;;
    esac
    return 0
}

menu_wizard_cmd() {
    [[ $# -ge 1 ]] || ny_usage_error "Say which wizard." "nodeyard wizard NAME [step=value]..."
    ny_ui_interactive || ny_die "Wizards need an interactive terminal." "Run the underlying command with flags instead (see its --help)." "$NY_E_PRECONDITION"
    ny_wizard_run "$@"
}
