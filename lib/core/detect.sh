# shellcheck shell=bash
# Runtime detection: distro, package manager, init system, architecture,
# hardware, boot disk, network backend and firewall. Nothing is assumed;
# every value comes from the machine (or a test/demo fixture).

NY_OS_ID="" NY_OS_LIKE="" NY_OS_VERSION="" NY_OS_CODENAME="" NY_OS_PRETTY=""
NY_OS_FAMILY="" NY_OS_SUPPORT="" NY_OS_LABEL="" NY_OS_NOTE=""
NY_PKG="" NY_INIT="" NY_ARCH="" NY_ARCH_RAW=""
NY_IS_PI=0 NY_HW_MODEL="" NY_RAM_MB=0 NY_CPUS=0 NY_CONTAINER="none"
NY_BOOT_DISK="unknown" NY_BOOT_DISK_MODEL=""
NY_DETECTED=0

# ny_os_release_value FILE KEY -- one value from an os-release file.
ny_os_release_value() {
    local file="$1" key="$2" line v
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" == "$key="* ]] || continue
        v="${line#*=}"
        v="${v%\"}"
        v="${v#\"}"
        v="${v%\'}"
        v="${v#\'}"
        printf '%s\n' "$v"
        return 0
    done <"$file"
    return 0
}

ny_detect_os() {
    local f
    f="$(ny_path /etc/os-release)"
    [[ -r "$f" ]] || f="$(ny_path /usr/lib/os-release)"
    NY_OS_ID="unknown" NY_OS_LIKE="" NY_OS_VERSION="" NY_OS_CODENAME="" NY_OS_PRETTY=""
    if [[ -r "$f" ]]; then
        NY_OS_ID="$(ny_os_release_value "$f" ID)"
        NY_OS_LIKE="$(ny_os_release_value "$f" ID_LIKE)"
        NY_OS_VERSION="$(ny_os_release_value "$f" VERSION_ID)"
        NY_OS_CODENAME="$(ny_os_release_value "$f" VERSION_CODENAME)"
        NY_OS_PRETTY="$(ny_os_release_value "$f" PRETTY_NAME)"
    fi
    NY_OS_ID="${NY_OS_ID,,}"
    NY_OS_ID="${NY_OS_ID:-unknown}"

    local all=" ${NY_OS_ID} ${NY_OS_LIKE,,} "
    case "$all" in
        *" fedora "*) NY_OS_FAMILY="fedora" ;;
        *" debian "* | *" ubuntu "* | *" raspbian "*) NY_OS_FAMILY="debian" ;;
        *" rhel "* | *" centos "*) NY_OS_FAMILY="rhel" ;;
        *" suse "* | *" opensuse "* | *" opensuse-leap "* | *" opensuse-tumbleweed "* | *" sles "*) NY_OS_FAMILY="suse" ;;
        *" arch "*) NY_OS_FAMILY="arch" ;;
        *" alpine "*) NY_OS_FAMILY="alpine" ;;
        *) NY_OS_FAMILY="other" ;;
    esac
    # RHEL rebuilds list "rhel centos fedora" in ID_LIKE; they are rhel.
    case "$NY_OS_ID" in
        rhel | rocky | almalinux | centos | ol | amzn) NY_OS_FAMILY="rhel" ;;
    esac

    ny_detect_pi
    local is_rpios=0
    if [[ "$NY_OS_ID" == raspbian ]] || { [[ "$NY_OS_ID" == debian ]] && [[ -e "$(ny_path /etc/rpi-issue)" ]]; }; then
        is_rpios=1
    fi

    local major="${NY_OS_VERSION%%.*}"
    NY_OS_NOTE=""
    case "$NY_OS_ID" in
        debian)
            if [[ "$major" =~ ^[0-9]+$ ]] && ((major >= 12)); then
                NY_OS_SUPPORT="supported"
            elif [[ -z "$major" ]]; then
                NY_OS_SUPPORT="best-effort"
                NY_OS_NOTE="Debian testing/unstable is not tested."
            else
                NY_OS_SUPPORT="best-effort"
                NY_OS_NOTE="Debian ${NY_OS_VERSION} is end-of-life; upgrade to Debian 12 or newer."
            fi
            ;;
        raspbian)
            NY_OS_SUPPORT="best-effort"
            NY_OS_NOTE="32-bit Raspberry Pi OS: k3s and AI features need the 64-bit edition."
            ;;
        ubuntu)
            case "$NY_OS_VERSION" in
                22.04 | 24.04 | 26.04) NY_OS_SUPPORT="supported" ;;
                *)
                    NY_OS_SUPPORT="best-effort"
                    NY_OS_NOTE="Only Ubuntu LTS releases (22.04, 24.04, 26.04) are tested."
                    ;;
            esac
            ;;
        fedora)
            if [[ "$major" =~ ^[0-9]+$ ]] && ((major >= 42)); then
                NY_OS_SUPPORT="supported"
            else
                NY_OS_SUPPORT="best-effort"
                NY_OS_NOTE="Fedora ${NY_OS_VERSION} is end-of-life or untested."
            fi
            ;;
        rhel | rocky | almalinux | centos)
            if [[ "$major" =~ ^[0-9]+$ ]] && ((major >= 8)); then
                NY_OS_SUPPORT="supported"
            else
                NY_OS_SUPPORT="unsupported"
                NY_OS_NOTE="Enterprise Linux 7 and older are end-of-life."
            fi
            ;;
        arch) NY_OS_SUPPORT="supported" ;;
        opensuse-leap)
            if [[ "$(ny_version_cmp "$NY_OS_VERSION" 15.6)" -ge 0 ]]; then
                NY_OS_SUPPORT="supported"
            else
                NY_OS_SUPPORT="best-effort"
            fi
            ;;
        opensuse-tumbleweed | opensuse-slowroll) NY_OS_SUPPORT="supported" ;;
        alpine)
            NY_OS_SUPPORT="unsupported"
            NY_OS_NOTE="Alpine uses OpenRC; nodeyard needs systemd. The basic k3s commands may still work."
            ;;
        *)
            if [[ "$NY_OS_FAMILY" != other ]]; then
                NY_OS_SUPPORT="best-effort"
                NY_OS_NOTE="${NY_OS_ID} is related to a supported distro (${NY_OS_FAMILY}) but is not tested."
            else
                NY_OS_SUPPORT="unsupported"
                NY_OS_NOTE="This distribution is not recognised."
            fi
            ;;
    esac

    if [[ "$is_rpios" -eq 1 ]]; then
        NY_OS_LABEL="Raspberry Pi OS ${NY_OS_VERSION}${NY_OS_CODENAME:+ (${NY_OS_CODENAME})}"
    else
        NY_OS_LABEL="${NY_OS_PRETTY:-${NY_OS_ID} ${NY_OS_VERSION}}"
    fi
    return 0
}

ny_detect_pi() {
    NY_IS_PI=0
    local f model=""
    for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
        f="$(ny_path "$f")"
        if [[ -r "$f" ]]; then
            model="$(tr -d '\0' <"$f" 2>/dev/null || true)"
            break
        fi
    done
    if [[ "$model" == *"Raspberry Pi"* ]]; then
        NY_IS_PI=1
    fi
    if [[ -n "$model" ]]; then
        NY_HW_MODEL="$model"
    else
        local vendor="" product=""
        [[ -r "$(ny_path /sys/class/dmi/id/sys_vendor)" ]] && vendor="$(head -n1 "$(ny_path /sys/class/dmi/id/sys_vendor)" 2>/dev/null || true)"
        [[ -r "$(ny_path /sys/class/dmi/id/product_name)" ]] && product="$(head -n1 "$(ny_path /sys/class/dmi/id/product_name)" 2>/dev/null || true)"
        NY_HW_MODEL="$(ny_trim "${vendor} ${product}")"
    fi
    NY_HW_MODEL="${NY_HW_MODEL:-unknown}"
}

ny_detect_pkg() {
    NY_PKG=""
    case "$NY_OS_FAMILY" in
        debian) have apt-get && NY_PKG="apt" ;;
        fedora | rhel)
            if have dnf; then
                NY_PKG="dnf"
            elif have yum; then
                NY_PKG="yum"
            fi
            ;;
        suse) have zypper && NY_PKG="zypper" ;;
        arch) have pacman && NY_PKG="pacman" ;;
        alpine) have apk && NY_PKG="apk" ;;
    esac
    if [[ -z "$NY_PKG" ]]; then
        local p
        for p in apt-get:apt dnf:dnf yum:yum zypper:zypper pacman:pacman apk:apk; do
            if have "${p%%:*}"; then
                NY_PKG="${p#*:}"
                break
            fi
        done
    fi
    return 0
}

ny_detect_init() {
    if [[ -d "$(ny_path /run/systemd/system)" ]]; then
        NY_INIT="systemd"
    elif have rc-service; then
        NY_INIT="openrc"
    else
        NY_INIT="unknown"
    fi
    return 0
}

ny_detect_arch() {
    NY_ARCH_RAW="$(uname -m 2>/dev/null || echo unknown)"
    case "$NY_ARCH_RAW" in
        x86_64 | amd64) NY_ARCH="amd64" ;;
        aarch64 | arm64) NY_ARCH="arm64" ;;
        armv7l | armv6l | armhf) NY_ARCH="armhf" ;;
        riscv64) NY_ARCH="riscv64" ;;
        *) NY_ARCH="$NY_ARCH_RAW" ;;
    esac
    return 0
}

ny_detect_resources() {
    local kb
    kb="$(awk '/^MemTotal:/{print $2; exit}' "$(ny_path /proc/meminfo)" 2>/dev/null || true)"
    [[ "$kb" =~ ^[0-9]+$ ]] && NY_RAM_MB=$((kb / 1024)) || NY_RAM_MB=0
    NY_CPUS="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"
    [[ "$NY_CPUS" =~ ^[0-9]+$ ]] || NY_CPUS=0

    NY_CONTAINER="none"
    if [[ -e "$(ny_path /.dockerenv)" ]]; then
        NY_CONTAINER="docker"
    elif [[ -e "$(ny_path /run/.containerenv)" ]]; then
        NY_CONTAINER="podman"
    elif have systemd-detect-virt; then
        local c
        c="$(systemd-detect-virt --container 2>/dev/null || true)"
        [[ -n "$c" && "$c" != none ]] && NY_CONTAINER="$c"
    fi
    return 0
}

# ny_detect_boot_disk -- what the root filesystem lives on: sd, emmc, nvme,
# usb, ssd, hdd, virtual, container or unknown. SD cards matter: they wear
# out, and etcd on an SD card is often unstable.
ny_detect_boot_disk() {
    NY_BOOT_DISK="unknown"
    NY_BOOT_DISK_MODEL=""
    if [[ "$NY_CONTAINER" != none ]]; then
        NY_BOOT_DISK="container"
        return 0
    fi
    local src dev parent
    src="$(findmnt -no SOURCE / 2>/dev/null || true)"
    src="${src%%\[*}"
    [[ "$src" == /dev/* ]] || return 0
    parent="$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 || true)"
    parent="$(ny_trim "$parent")"
    [[ -n "$parent" ]] || parent="${src#/dev/}"
    dev="$parent"
    NY_BOOT_DISK_MODEL="$(lsblk -dno MODEL "/dev/${dev}" 2>/dev/null | head -n1 || true)"
    NY_BOOT_DISK_MODEL="$(ny_trim "$NY_BOOT_DISK_MODEL")"
    case "$dev" in
        mmcblk*)
            local t=""
            t="$(cat "$(ny_path "/sys/block/${dev}/device/type")" 2>/dev/null || true)"
            if [[ "$t" == MMC ]]; then
                NY_BOOT_DISK="emmc"
            else
                NY_BOOT_DISK="sd"
            fi
            ;;
        nvme*) NY_BOOT_DISK="nvme" ;;
        vd* | xvd*) NY_BOOT_DISK="virtual" ;;
        *)
            local tran rota
            tran="$(lsblk -dno TRAN "/dev/${dev}" 2>/dev/null | head -n1 | tr -d ' ' || true)"
            rota="$(lsblk -dno ROTA "/dev/${dev}" 2>/dev/null | head -n1 | tr -d ' ' || true)"
            if [[ "$tran" == usb ]]; then
                NY_BOOT_DISK="usb"
            elif [[ "$rota" == 1 ]]; then
                NY_BOOT_DISK="hdd"
            elif [[ "$rota" == 0 ]]; then
                NY_BOOT_DISK="ssd"
            fi
            ;;
    esac
    return 0
}

# ny_detect_all -- run every detector once per process.
ny_detect_all() {
    [[ "$NY_DETECTED" -eq 1 ]] && return 0
    ny_detect_os
    ny_detect_pkg
    ny_detect_init
    ny_detect_arch
    ny_detect_resources
    ny_detect_boot_disk
    NY_DETECTED=1
}

# --- network -----------------------------------------------------------------

# ny_iface_kind IFACE -- ethernet | wifi | virtual | loopback
ny_iface_kind() {
    local i="$1" sys
    sys="$(ny_path "/sys/class/net/${i}")"
    [[ "$i" == lo ]] && {
        echo loopback
        return 0
    }
    if [[ -d "${sys}/wireless" || -d "${sys}/phy80211" ]]; then
        echo wifi
        return 0
    fi
    case "$i" in
        docker* | br-* | veth* | cni* | flannel* | cali* | vxlan* | tailscale* | wg* | tun* | tap* | virbr* | kube-* | lxc* | podman* | zt*)
            echo virtual
            return 0
            ;;
    esac
    if [[ -e "${sys}/device" ]]; then
        echo ethernet
    else
        echo virtual
    fi
    return 0
}

ny_iface_ipv4() {
    ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk 'NR==1 {split($4,a,"/"); print a[1]}'
}

ny_iface_cidr() {
    ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk 'NR==1 {print $4}'
}

ny_iface_state() {
    cat "$(ny_path "/sys/class/net/${1}/operstate")" 2>/dev/null || echo unknown
}

ny_iface_exists() {
    [[ -e "$(ny_path "/sys/class/net/${1}")" ]] || ip link show dev "$1" >/dev/null 2>&1
}

# ny_primary_route -- "IFACE SRC_IP GATEWAY" for the default route ('-' if none).
ny_primary_route() {
    local out dev src gw
    out="$(ip -4 route get 1.1.1.1 2>/dev/null | head -n1 || true)"
    dev="$(awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}' <<<"$out")"
    src="$(awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' <<<"$out")"
    gw="$(awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' <<<"$out")"
    printf '%s %s %s\n' "${dev:--}" "${src:--}" "${gw:--}"
}

# ny_best_ip -- this machine's main IPv4 address.
ny_best_ip() {
    local dev src gw
    read -r dev src gw <<<"$(ny_primary_route)"
    if [[ "$src" != "-" ]]; then
        printf '%s\n' "$src"
        return 0
    fi
    hostname -I 2>/dev/null | awk '{print $1}' || true
}

# ny_list_ifaces -- "NAME<TAB>KIND<TAB>STATE<TAB>CIDR" for each non-loopback interface.
ny_list_ifaces() {
    local name kind
    while IFS= read -r name; do
        [[ -n "$name" && "$name" != lo ]] || continue
        kind="$(ny_iface_kind "$name")"
        printf '%s\t%s\t%s\t%s\n' "$name" "$kind" "$(ny_iface_state "$name")" "$(ny_iface_cidr "$name")"
    done < <(ip -o link show 2>/dev/null | awk -F': ' '{sub(/@.*/, "", $2); print $2}')
    return 0
}

ny_unit_active() {
    systemctl is-active --quiet "$1" 2>/dev/null
}

# ny_detect_netbackend [IFACE] -- which tool owns network config:
# netplan | networkmanager | networkd | wicked | dhcpcd | ifupdown | unknown.
# Netplan is reported first because it generates NetworkManager/networkd
# config: editing the generated files directly would be overwritten.
ny_detect_netbackend() {
    local iface="${1:-}"
    local np
    np="$(ny_path /etc/netplan)"
    if have netplan && compgen -G "${np}/*.yaml" >/dev/null 2>&1; then
        echo netplan
        return 0
    fi
    if ny_unit_active NetworkManager; then
        if [[ -z "$iface" ]] || ! have nmcli; then
            echo networkmanager
            return 0
        fi
        local st
        st="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$iface" '$1==d{print $2}')"
        if [[ -n "$st" && "$st" != unmanaged ]]; then
            echo networkmanager
            return 0
        fi
    fi
    if ny_unit_active systemd-networkd; then
        echo networkd
        return 0
    fi
    if ny_unit_active wicked || ny_unit_active wickedd; then
        echo wicked
        return 0
    fi
    if ny_unit_active dhcpcd && [[ -f "$(ny_path /etc/dhcpcd.conf)" ]]; then
        echo dhcpcd
        return 0
    fi
    if [[ -f "$(ny_path /etc/network/interfaces)" ]] && { have ifup || ny_unit_active networking; }; then
        echo ifupdown
        return 0
    fi
    echo unknown
}

# ny_detect_firewall -- ufw | firewalld | nftables | iptables | none
ny_detect_firewall() {
    local out=""
    if have ufw && grep -qi '^Status: active' <<<"$(ufw status 2>/dev/null || true)"; then
        echo ufw
    elif have firewall-cmd && ny_unit_active firewalld; then
        echo firewalld
    elif have nft && [[ -n "$(nft list ruleset 2>/dev/null || true)" ]]; then
        echo nftables
    elif have iptables && out="$(iptables -S 2>/dev/null || true)" && [[ -n "$out" ]] && grep -qv '^-P .* ACCEPT$' <<<"$out"; then
        echo iptables
    else
        echo none
    fi
    return 0
}
