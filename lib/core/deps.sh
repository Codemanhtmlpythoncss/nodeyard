# shellcheck shell=bash
# Dependencies: find missing commands, map them to each distro's package, and
# offer to install them before starting work (never halfway through).
# Also verified downloads of pinned third-party binaries (versions.lock).

# Command -> package per package manager: "apt dnf zypper pacman apk".
# "-" means "not packaged there" (installed another way or not needed).
declare -gA NY_DEP_PKG=(
    [jq]="jq jq jq jq jq"
    [curl]="curl curl curl curl curl"
    [ip]="iproute2 iproute iproute2 iproute2 iproute2"
    [ss]="iproute2 iproute iproute2 iproute2 iproute2"
    [flock]="util-linux util-linux util-linux util-linux util-linux"
    [findmnt]="util-linux util-linux util-linux util-linux util-linux-misc"
    [lsblk]="util-linux util-linux util-linux util-linux lsblk"
    [openssl]="openssl openssl openssl openssl openssl"
    [sha256sum]="coreutils coreutils coreutils coreutils coreutils"
    [tar]="tar tar tar tar tar"
    [gzip]="gzip gzip gzip gzip gzip"
    [diff]="diffutils diffutils diffutils diffutils diffutils"
    [ssh]="openssh-client openssh-clients openssh-clients openssh openssh-client"
    [scp]="openssh-client openssh-clients openssh-clients openssh openssh-client"
    [ssh-keyscan]="openssh-client openssh-clients openssh-clients openssh openssh-client"
    [ssh-keygen]="openssh-client openssh-clients openssh-clients openssh openssh-client"
    [whiptail]="whiptail newt newt libnewt newt"
    [dialog]="dialog dialog dialog dialog dialog"
    [iptables]="iptables iptables iptables - iptables"
    [nft]="nftables nftables nftables nftables nftables"
    [ping]="iputils-ping iputils iputils iputils iputils"
)

# Commands each feature needs.
declare -gA NY_DEP_FEATURE=(
    [core]="jq curl ip flock openssl sha256sum tar gzip"
    [k3s]="curl ip ss findmnt lsblk"
    [ssh]="ssh scp ssh-keyscan ssh-keygen"
    [dialog]="whiptail"
)

NY_PKG_INDEX_REFRESHED=0

ny_pkg_index() {
    case "$NY_PKG" in
        apt) echo 0 ;;
        dnf | yum) echo 1 ;;
        zypper) echo 2 ;;
        pacman) echo 3 ;;
        apk) echo 4 ;;
        *) echo -1 ;;
    esac
}

# ny_pkg_for CMD -- the package that provides CMD here (empty if unknown).
ny_pkg_for() {
    local entry="${NY_DEP_PKG[$1]:-}" idx
    [[ -n "$entry" ]] || return 0
    idx="$(ny_pkg_index)"
    ((idx >= 0)) || return 0
    local -a pkgs=()
    read -r -a pkgs <<<"$entry"
    [[ "${pkgs[idx]:-}" != "-" ]] && printf '%s\n' "${pkgs[idx]:-}"
    return 0
}

# ny_pkg_install_cmd PKG... -- the command line that installs packages here.
ny_pkg_install_cmd() {
    case "$NY_PKG" in
        apt) echo "apt-get install -y $*" ;;
        dnf) echo "dnf install -y $*" ;;
        yum) echo "yum install -y $*" ;;
        zypper) echo "zypper --non-interactive install $*" ;;
        pacman) echo "pacman -S --needed --noconfirm $*" ;;
        apk) echo "apk add $*" ;;
        *) echo "(install $* with your package manager)" ;;
    esac
}

# ny_pkg_install PKG... -- install packages (retries, refreshes the index once).
ny_pkg_install() {
    [[ $# -gt 0 ]] || return 0
    ny_detect_all
    case "$NY_PKG" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            if [[ "$NY_PKG_INDEX_REFRESHED" -eq 0 ]]; then
                ny_retry 3 5 ny_run apt-get update -q || ny_warn "apt-get update failed; using the cached package lists."
                NY_PKG_INDEX_REFRESHED=1
            fi
            ny_retry 3 5 ny_run apt-get install -y -q "$@"
            ;;
        dnf) ny_retry 3 5 ny_run dnf install -y -q "$@" ;;
        yum) ny_retry 3 5 ny_run yum install -y -q "$@" ;;
        zypper) ny_retry 3 5 ny_run zypper --non-interactive --quiet install "$@" ;;
        pacman)
            # No -Sy: syncing without upgrading is an Arch partial upgrade.
            ny_retry 3 5 ny_run pacman -S --needed --noconfirm "$@" || {
                ny_warn "pacman could not install $*; if the package database is stale, run 'sudo pacman -Syu' first."
                return 1
            }
            ;;
        apk) ny_retry 3 5 ny_run apk add --no-cache "$@" ;;
        *)
            ny_warn "No supported package manager found; install these yourself: $*"
            return 1
            ;;
    esac
}

# ny_deps_missing CMD... -- print the commands that are not installed.
ny_deps_missing() {
    local c
    for c in "$@"; do
        have "$c" || printf '%s\n' "$c"
    done
}

# ny_deps_ensure REASON CMD... -- make sure commands exist, offering to install
# what is missing. Dies with the exact install command if it can't.
ny_deps_ensure() {
    local reason="$1"
    shift
    local -a missing=()
    mapfile -t missing < <(ny_deps_missing "$@")
    [[ "${#missing[@]}" -gt 0 ]] || return 0

    ny_detect_all
    local -a pkgs=() unknown=()
    local c p
    for c in "${missing[@]}"; do
        p="$(ny_pkg_for "$c")"
        if [[ -n "$p" ]]; then
            ny_in_list "$p" "${pkgs[@]+"${pkgs[@]}"}" || pkgs+=("$p")
        else
            unknown+=("$c")
        fi
    done

    if [[ "${#unknown[@]}" -gt 0 ]]; then
        ny_die "Missing required commands for ${reason}: $(ny_join ', ' "${unknown[@]}")." \
            "Install them with your package manager, then try again." "$NY_E_PRECONDITION"
    fi

    local install_cmd
    install_cmd="$(ny_pkg_install_cmd "${pkgs[@]}")"
    ny_warn "nodeyard needs $(ny_join ', ' "${missing[@]}") for ${reason}."
    if ! ny_is_root; then
        ny_die "Missing: $(ny_join ', ' "${missing[@]}")." "Install with: sudo ${install_cmd}" "$NY_E_PRECONDITION"
    fi
    if ny_offer "Install $(ny_join ' ' "${pkgs[@]}") now (${install_cmd})?" y; then
        ny_pkg_install "${pkgs[@]}" || ny_die "Installing $(ny_join ' ' "${pkgs[@]}") failed." "Run '${install_cmd}' yourself to see why, then try again." "$NY_E_PRECONDITION"
        ny_simulating && return 0
        mapfile -t missing < <(ny_deps_missing "$@")
        [[ "${#missing[@]}" -eq 0 ]] || ny_die "Still missing after install: $(ny_join ', ' "${missing[@]}")." "Check your package sources, then run: ${install_cmd}" "$NY_E_PRECONDITION"
        ny_ok "Installed $(ny_join ' ' "${pkgs[@]}")."
    else
        ny_die "Cannot continue without $(ny_join ', ' "${missing[@]}")." "Install with: sudo ${install_cmd}" "$NY_E_PRECONDITION"
    fi
}

# ny_deps_ensure_feature FEATURE... -- ensure every command a feature needs.
ny_deps_ensure_feature() {
    local f
    local -a cmds=()
    for f in "$@"; do
        local -a more=()
        read -r -a more <<<"${NY_DEP_FEATURE[$f]:-}"
        cmds+=("${more[@]+"${more[@]}"}")
    done
    ny_deps_ensure "$(ny_join ', ' "$@")" "${cmds[@]+"${cmds[@]}"}"
}

# --- pinned downloads ----------------------------------------------------------

NY_LOCK_VERSION="" NY_LOCK_SHA="" NY_LOCK_URL=""

# ny_platform -- linux-amd64 / linux-arm64 for versions.lock lookups.
ny_platform() {
    ny_detect_arch
    printf 'linux-%s\n' "$NY_ARCH"
}

# ny_lock_lookup NAME [PLATFORM] -- read a pinned entry from versions.lock.
ny_lock_lookup() {
    local name="$1" platform="${2:-$(ny_platform)}" n v p s u
    NY_LOCK_VERSION="" NY_LOCK_SHA="" NY_LOCK_URL=""
    while read -r n v p s u; do
        [[ -z "$n" || "$n" == \#* ]] && continue
        if [[ "$n" == "$name" && ("$p" == "$platform" || "$p" == any) ]]; then
            NY_LOCK_VERSION="$v" NY_LOCK_SHA="$s" NY_LOCK_URL="$u"
            return 0
        fi
    done <"${NY_SHARE}/versions.lock"
    return 1
}

# ny_download URL DEST [SHA256] -- HTTPS-only download, checksum-verified when
# a checksum is given.
ny_download() {
    local url="$1" dest="$2" sha="${3:-}"
    [[ "$url" == https://* ]] || ny_die "Refusing to download over plain HTTP: ${url}" "Use an https:// URL." "$NY_E_PRECONDITION"
    if ny_simulating; then
        ny_plan_add download "Download ${url}" "url=$url" "sha256?=$sha"
        [[ "$NY_DRY_RUN" -eq 1 ]] && printf '%s %s\n' "$(ny_color cyan "[dry-run] would download")" "$url" >&2
        return 0
    fi
    ny_vlog "downloading ${url}"
    if ! ny_retry 3 5 curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 -o "$dest" "$url"; then
        ny_die "Could not download ${url}." "Check this machine's internet connection (try: curl -I https://github.com), then try again."
    fi
    if [[ -n "$sha" ]]; then
        local got
        got="$(ny_sha256 "$dest")"
        if [[ "$got" != "$sha" ]]; then
            rm -f -- "$dest"
            ny_die "Checksum mismatch for ${url} (expected ${sha}, got ${got}). The download was deleted." \
                "Try again; if it keeps failing, the file may have been tampered with: report it at https://github.com/${NY_REPO}/issues"
        fi
        ny_vlog "checksum ok: ${url}"
    fi
}

# ny_fetch_pinned NAME DEST -- download NAME for this platform as pinned in
# versions.lock, verifying its checksum.
ny_fetch_pinned() {
    local name="$1" dest="$2"
    ny_lock_lookup "$name" || ny_die "No pinned download for '${name}' on $(ny_platform)." \
        "This platform may not be supported for ${name}; see docs/distros-and-hardware.md." "$NY_E_PRECONDITION"
    ny_download "$NY_LOCK_URL" "$dest" "$NY_LOCK_SHA"
}

# ny_install_gum -- install the pinned gum release to /usr/local/bin/gum.
ny_install_gum() {
    ny_need_root
    ny_lock_lookup gum || ny_die "gum is not available for $(ny_platform)." "nodeyard will use whiptail, dialog or plain prompts instead." "$NY_E_PRECONDITION"
    local tmp tarball
    tmp="$(ny_mktemp -d)"
    tarball="${tmp}/gum.tar.gz"
    ny_step "Installing gum ${NY_LOCK_VERSION} (checksum-verified download from github.com/charmbracelet/gum)"
    ny_fetch_pinned gum "$tarball"
    if ny_simulating; then
        ny_write_file /usr/local/bin/gum 0755 </dev/null
        return 0
    fi
    tar -xzf "$tarball" -C "$tmp"
    local bin
    bin="$(find "$tmp" -type f -name gum -perm -u+x | head -n1)"
    [[ -n "$bin" ]] || ny_die "The gum download did not contain a gum binary." "Report this at https://github.com/${NY_REPO}/issues"
    ny_write_file /usr/local/bin/gum 0755 <"$bin"
    ny_ok "gum ${NY_LOCK_VERSION} installed."
}
