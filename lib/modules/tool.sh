# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# The tool itself: dependencies, the terminal UI, secrets, demo mode,
# self-update and uninstalling.

ny_cmd "deps" tool_deps_cmd "Tool" "Check (and offer to install) the tools nodeyard needs" tool json
ny_cmd "ui install-gum" tool_ui_install_gum_cmd "Settings" "Install gum for a nicer terminal interface (checksum-verified)" tool
ny_cmd "secrets list" tool_secrets_list_cmd "Settings" "List stored secrets (names and dates only, never values)" secrets json
ny_cmd "demo reset" tool_demo_reset_cmd "Tool" "Reset the demo sandbox to its starting state" tool
ny_cmd "update" tool_update_cmd "Updates" "Update nodeyard itself (shows what changed first)" tool
ny_cmd "uninstall" tool_uninstall_cmd "Settings" "Remove k3s, or everything nodeyard set up" uninstall
ny_cmd "uninstall everything" tool_uninstall_everything_cmd "Settings" "Undo every change nodeyard made and remove nodeyard" uninstall

tool_deps_cmd_help() {
    cat <<'HELP'
Usage: nodeyard deps [--feature NAME]... [--install] [--json]

Lists the commands nodeyard and its features need and whether each is
installed. With --install, installs what's missing using this machine's
package manager.

Features: core, k3s, ssh, dialog
HELP
}

tool_deps_cmd() {
    local install=0
    local -a features=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --feature)
                ny_need_value "$1" $#
                [[ -n "${NY_DEP_FEATURE[$2]:-}" ]] || ny_usage_error "Unknown feature '$2'." "Features: $(printf '%s ' "${!NY_DEP_FEATURE[@]}")"
                features+=("$2")
                shift 2
                ;;
            --install)
                install=1
                shift
                ;;
            *) ny_usage_error "Unknown option for 'deps': $1" ;;
        esac
    done
    [[ "${#features[@]}" -gt 0 ]] || features=(core k3s ssh)
    ny_detect_all
    local f c status pkg
    local -a items=() rows=()
    for f in "${features[@]}"; do
        for c in ${NY_DEP_FEATURE[$f]}; do
            status="installed"
            have "$c" || status="missing"
            pkg="$(ny_pkg_for "$c")"
            items+=("$(ny_json_obj "feature=$f" "command=$c" "status=$status" "package?=$pkg")")
            rows+=("${f}"$'\t'"${c}"$'\t'"${status}"$'\t'"${pkg:--}")
        done
    done
    if [[ "$install" -eq 1 ]]; then
        ny_need_root
        ny_deps_ensure_feature "${features[@]}"
    fi
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(ny_json_obj ok:=true "package_manager?=$NY_PKG" "dependencies:=$(ny_json_arr "${items[@]}")")"
        return 0
    fi
    {
        printf 'FEATURE\tCOMMAND\tSTATUS\tPACKAGE\n'
        printf '%s\n' "${rows[@]}"
    } | ny_table --status STATUS
    if printf '%s\n' "${rows[@]}" | grep -q $'\tmissing\t' && [[ "$install" -eq 0 ]]; then
        ny_hint "Install what's missing: sudo nodeyard deps --install"
    fi
    printf '\n%s %s\n' "Terminal UI:" "$(
        ny_ui_init
        printf '%s' "$NY_UI"
    )$(have gum || printf ' (sudo nodeyard ui install-gum for the nicest interface)')"
}

tool_ui_install_gum_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if have gum; then
        ny_ok "gum is already installed ($(gum --version 2>/dev/null | head -n1))."
        return 0
    fi
    ny_confirm "Download and install gum (a terminal UI toolkit from charm.sh, checksum-verified)?" y || return 0
    ny_install_gum
}

tool_secrets_list_cmd() {
    ny_need_root
    local -a rows=()
    mapfile -t rows < <(ny_secret_list)
    if [[ "$NY_JSON" -eq 1 ]]; then
        local -a items=()
        local r
        for r in "${rows[@]+"${rows[@]}"}"; do
            items+=("$(ny_json_obj "name=${r%%$'\t'*}" "modified=${r#*$'\t'}")")
        done
        ny_json_out "$(ny_json_obj ok:=true "secrets:=$(ny_json_arr "${items[@]+"${items[@]}"}")")"
        return 0
    fi
    if [[ "${#rows[@]}" -eq 0 ]]; then
        ny_info "No secrets are stored on this node."
        return 0
    fi
    {
        printf 'SECRET\tLAST CHANGED\n'
        printf '%s\n' "${rows[@]}"
    } | ny_table
    ny_hint "Values are never shown. They live in $(ny_unroot "$NY_SECRETS_DIR") (root only)."
}

tool_demo_reset_cmd() {
    [[ "$NY_DEMO" -eq 1 ]] || ny_die "This only works in demo mode." "Run: nodeyard --demo demo reset" "$NY_E_USAGE"
    ny_demo_reset || ny_die "Refusing to delete $(ny_demo_dir)." "Delete it yourself if it is the demo sandbox."
    ny_ok "Demo sandbox reset; it is rebuilt next time you run nodeyard --demo."
}

# --- self-update -------------------------------------------------------------

tool_update_cmd_help() {
    cat <<'HELP'
Usage: nodeyard update [--check] [--version X.Y.Z] [--force]

Updates nodeyard from its GitHub releases: downloads the release for this
machine, verifies its SHA-256 checksum, shows the changelog entries you
are about to get, and installs it after you confirm. It never installs an
older version unless --force is given. A git checkout is updated with
'git pull' instead.

Options:
  --check        Only report whether an update is available
  --version V    Install this release instead of the latest
  --force        Reinstall or downgrade
HELP
}

tool_latest_version() {
    local url
    url="$(curl -fsSIL --proto '=https' -o /dev/null -w '%{url_effective}' "https://github.com/${NY_REPO}/releases/latest" 2>/dev/null || true)"
    url="${url%/}"
    [[ "$url" == */tag/v* ]] || return 1
    printf '%s\n' "${url##*/tag/v}"
}

# tool_changelog_between FILE FROM TO -- the changelog entries after FROM up to TO.
tool_changelog_between() {
    awk -v from="$2" '
        /^## \[/ { v = $2; gsub(/[\[\]]/, "", v); if (v == from) exit; show = (v != "Unreleased") }
        show { print }' "$1"
}

tool_update_cmd() {
    local check=0 version="" force=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check)
                check=1
                shift
                ;;
            --version)
                ny_need_value "$1" $#
                [[ "${2#v}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || ny_usage_error "--version must look like 1.2.3"
                version="${2#v}"
                shift 2
                ;;
            --force)
                force=1
                shift
                ;;
            *) ny_usage_error "Unknown option for 'update': $1" ;;
        esac
    done
    if [[ -d "${NY_HOME}/.git" ]] && have git; then
        ny_info "nodeyard is running from a git checkout (${NY_HOME}); updating with git."
        git -C "$NY_HOME" fetch --quiet origin || ny_die "git fetch failed." "Check your connection and the 'origin' remote."
        local behind
        behind="$(git -C "$NY_HOME" log --oneline 'HEAD..@{u}' 2>/dev/null || true)"
        if [[ -z "$behind" ]]; then
            ny_ok "Already up to date."
            return 0
        fi
        printf '%s\n' "$behind"
        [[ "$check" -eq 1 ]] && return 0
        ny_confirm "Pull these commits?" y || return 0
        ny_run git -C "$NY_HOME" pull --ff-only --quiet
        ny_ok "Updated."
        return 0
    fi

    ny_deps_ensure "updating" curl tar gzip find
    if [[ -z "$version" ]]; then
        version="$(tool_latest_version)" || ny_die "Could not find the latest release of ${NY_REPO}." \
            "Check this machine's internet connection, or see https://github.com/${NY_REPO}/releases"
    fi
    local cmp
    cmp="$(ny_version_cmp "$version" "$NY_VERSION")"
    ny_info "Installed: ${NY_VERSION}   Available: ${version}"
    if [[ "$cmp" -le 0 && "$force" -eq 0 ]]; then
        if [[ "$cmp" -eq 0 ]]; then ny_ok "Already up to date."; else ny_ok "The installed version is newer than ${version}; not downgrading (use --force to)."; fi
        return 0
    fi
    if [[ "$check" -eq 1 ]]; then
        ny_info "An update is available: sudo nodeyard update"
        return 0
    fi
    ny_need_root
    ny_detect_arch
    local base="https://github.com/${NY_REPO}/releases/download/v${version}"
    local name="nodeyard-${version}-linux-${NY_ARCH}.tar.gz"
    local tmp sha
    tmp="$(ny_mktemp -d)"
    ny_download "${base}/SHA256SUMS" "${tmp}/SHA256SUMS"
    if ny_simulating; then
        ny_download "${base}/${name}" "${tmp}/${name}"
        ny_plan_add run "Install nodeyard ${version} with its install.sh"
        return 0
    fi
    sha="$(awk -v n="$name" '$2 == n || $2 == "*"n {print $1}' "${tmp}/SHA256SUMS")"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || ny_die "The release ${version} has no checksum for ${name}." "This architecture may not be published for that release."
    ny_download "${base}/${name}" "${tmp}/${name}" "$sha"
    mkdir -p "${tmp}/src"
    tar -xzf "${tmp}/${name}" -C "${tmp}/src"
    local src="${tmp}/src"
    [[ -f "${src}/install.sh" ]] || src="$(find "${tmp}/src" -maxdepth 2 -name install.sh -exec dirname {} \; | head -n1)"
    [[ -n "$src" && -f "${src}/install.sh" ]] || ny_die "The downloaded release looks incomplete (no install.sh)." "Report it at https://github.com/${NY_REPO}/issues"
    if [[ -f "${src}/CHANGELOG.md" ]]; then
        printf '\n%s\n' "$(ny_color bold "What's new since ${NY_VERSION}")"
        tool_changelog_between "${src}/CHANGELOG.md" "$NY_VERSION" | head -n 80
        printf '\n'
    fi
    ny_confirm "Install nodeyard ${version}?" y || return 0
    bash "${src}/install.sh" --from-dir "$src" --yes || ny_die "Installing ${version} failed; the previous version is still in place." "See the output above."
    ny_ok "nodeyard updated: ${NY_VERSION} -> ${version}"
}

# --- uninstall ---------------------------------------------------------------

tool_uninstall_cmd_help() {
    cat <<'HELP'
Usage: nodeyard uninstall k3s | everything

  k3s          Remove k3s from this node (see: nodeyard uninstall k3s --help)
  everything   Undo every change nodeyard made on this machine (network,
               kernel and firewall settings, services), remove k3s, and
               remove nodeyard itself

To undo a single change instead: nodeyard changes / nodeyard undo ID
HELP
}

# shellcheck disable=SC2119 # the picker passes no options
tool_uninstall_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unknown thing to uninstall: $1" "nodeyard uninstall k3s|everything"
    if ! ny_ui_interactive; then
        ny_usage_error "Say what to uninstall." "nodeyard uninstall k3s|everything"
    fi
    local what
    what="$(ny_ui_choose "What do you want to remove?" "" \
        $'k3s\tk3s on this node (keeps nodeyard)' \
        $'everything\tEverything nodeyard set up, and nodeyard itself' \
        $'cancel\tNothing, go back')" || return 0
    case "$what" in
        k3s) k3s_uninstall_cmd ;;
        everything) tool_uninstall_everything_cmd ;;
        *) return 0 ;;
    esac
    return 0
}

tool_uninstall_everything_cmd_help() {
    cat <<'HELP'
Usage: nodeyard uninstall everything [--keep-config] [--yes]

Removes k3s (if installed), undoes every change nodeyard recorded on this
machine (restoring the original files from its backups), then removes
nodeyard's own files. With --keep-config, /etc/nodeyard stays.
HELP
}

# shellcheck disable=SC2120 # options come from the command line, not callers
tool_uninstall_everything_cmd() {
    ny_need_root
    local keep=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --keep-config)
                keep=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" ;;
        esac
    done
    ny_warn "This removes k3s and all its data from this machine, reverts every change nodeyard made, and removes nodeyard."
    ny_confirm "Remove everything nodeyard set up on this machine?" n || return 0
    if ny_k3s_installed; then
        local NY_YES=1
        k3s_uninstall_cmd
    fi
    local -a txns=()
    mapfile -t txns < <(ny_journal_txns | jq -r 'select(.undone | not) | .txn' | sort -r)
    local t failed=0
    for t in "${txns[@]+"${txns[@]}"}"; do
        ny_journal_undo_txn "$t" 0 || failed=1
    done
    [[ "$failed" -eq 0 ]] || ny_warn "Some changes could not be reverted automatically (listed above); check those files by hand."
    local -a args=(--yes)
    [[ "$keep" -eq 1 ]] && args+=(--keep-config)
    [[ "$NY_DRY_RUN" -eq 1 ]] && args+=(--dry-run)
    if [[ -x "${NY_HOME}/uninstall.sh" ]]; then
        ny_simulating && [[ "$NY_DRY_RUN" -eq 0 ]] && return 0
        bash "${NY_HOME}/uninstall.sh" "${args[@]}"
    else
        ny_warn "uninstall.sh was not found next to nodeyard; remove ${NY_HOME} yourself."
    fi
    return 0
}
