#!/usr/bin/env bats
# Distro, hardware, network backend and firewall detection.

setup() {
    load ../helpers/common
    ny_lib_setup
    FIX="${NY_REPO_ROOT}/tests/fixtures/os-release"
}

os() { # os FIXTURE -> detect with that os-release
    mkdir -p "${NODEYARD_ROOT}/etc"
    cp "${FIX}/$1" "${NODEYARD_ROOT}/etc/os-release"
    ny_detect_os
    ny_detect_pkg
}

@test "Debian 12 and 13 are supported; 11 is end-of-life" {
    os debian-12
    [ "$NY_OS_FAMILY" = debian ] && [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_PKG" = apt ]
    os debian-13
    [ "$NY_OS_SUPPORT" = supported ]
    os debian-11
    [ "$NY_OS_SUPPORT" = best-effort ] && [[ "$NY_OS_NOTE" == *end-of-life* ]]
}

@test "Ubuntu LTS releases are supported; others best-effort" {
    os ubuntu-22.04
    [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_OS_FAMILY" = debian ]
    os ubuntu-24.04
    [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_OS_LABEL" = "Ubuntu 24.04.3 LTS" ]
    os ubuntu-25.04
    [ "$NY_OS_SUPPORT" = best-effort ]
}

@test "Raspberry Pi OS is recognised from Debian plus the Pi markers" {
    mkdir -p "${NODEYARD_ROOT}/proc/device-tree"
    printf 'Raspberry Pi 4 Model B Rev 1.5\0' >"${NODEYARD_ROOT}/proc/device-tree/model"
    : >"${NODEYARD_ROOT}/etc/rpi-issue" 2>/dev/null || { mkdir -p "${NODEYARD_ROOT}/etc"; : >"${NODEYARD_ROOT}/etc/rpi-issue"; }
    os raspios-12
    [ "$NY_IS_PI" -eq 1 ]
    [ "$NY_OS_LABEL" = "Raspberry Pi OS 12 (bookworm)" ]
    [ "$NY_HW_MODEL" = "Raspberry Pi 4 Model B Rev 1.5" ]
    [ "$NY_OS_SUPPORT" = supported ]
    os raspbian-12
    [ "$NY_OS_SUPPORT" = best-effort ] && [[ "$NY_OS_NOTE" == *64-bit* ]]
}

@test "Fedora and the RHEL family use dnf" {
    os fedora-42
    [ "$NY_OS_FAMILY" = fedora ] && [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_PKG" = dnf ]
    os fedora-40
    [ "$NY_OS_SUPPORT" = best-effort ]
    for f in rhel-9 rocky-9 almalinux-10; do
        os "$f"
        [ "$NY_OS_FAMILY" = rhel ] || fail "$f family $NY_OS_FAMILY"
        [ "$NY_OS_SUPPORT" = supported ] || fail "$f support $NY_OS_SUPPORT"
        [ "$NY_PKG" = dnf ]
    done
    os centos-7
    [ "$NY_OS_SUPPORT" = unsupported ]
}

@test "Arch, openSUSE and others" {
    os arch
    [ "$NY_OS_FAMILY" = arch ] && [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_PKG" = pacman ]
    os manjaro
    [ "$NY_OS_FAMILY" = arch ] && [ "$NY_OS_SUPPORT" = best-effort ]
    os opensuse-leap-15.6
    [ "$NY_OS_FAMILY" = suse ] && [ "$NY_OS_SUPPORT" = supported ] && [ "$NY_PKG" = zypper ]
    os opensuse-leap-16.0
    [ "$NY_OS_SUPPORT" = supported ]
    os opensuse-tumbleweed
    [ "$NY_OS_SUPPORT" = supported ]
    os linuxmint-22
    [ "$NY_OS_FAMILY" = debian ] && [ "$NY_OS_SUPPORT" = best-effort ]
    os alpine-3.22
    [ "$NY_OS_SUPPORT" = unsupported ] && [[ "$NY_OS_NOTE" == *systemd* ]]
    os nixos
    [ "$NY_OS_FAMILY" = other ] && [ "$NY_OS_SUPPORT" = unsupported ]
}

@test "architecture names are normalised" {
    ny_rule "uname -m" 0 x86_64
    ny_detect_arch
    [ "$NY_ARCH" = amd64 ]
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "uname -m" 0 aarch64
    ny_detect_arch
    [ "$NY_ARCH" = arm64 ]
}

@test "init system comes from /run/systemd/system" {
    ny_detect_init
    [ "$NY_INIT" = systemd ]
    rmdir "${NODEYARD_ROOT}/run/systemd/system"
    ny_rule "rc-service*" 0
    ny_detect_init
    [ "$NY_INIT" = openrc ]
}

@test "boot disk: SD card, eMMC, NVMe, USB" {
    NY_CONTAINER=none
    ny_rule "findmnt -no SOURCE /" 0 /dev/mmcblk0p2
    ny_rule "lsblk -no PKNAME /dev/mmcblk0p2" 0 mmcblk0
    mkdir -p "${NODEYARD_ROOT}/sys/block/mmcblk0/device"
    printf 'SD\n' >"${NODEYARD_ROOT}/sys/block/mmcblk0/device/type"
    ny_detect_boot_disk
    [ "$NY_BOOT_DISK" = sd ]
    printf 'MMC\n' >"${NODEYARD_ROOT}/sys/block/mmcblk0/device/type"
    ny_detect_boot_disk
    [ "$NY_BOOT_DISK" = emmc ]
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "findmnt -no SOURCE /" 0 /dev/sda2
    ny_rule "lsblk -no PKNAME /dev/sda2" 0 sda
    ny_rule "lsblk -dno TRAN /dev/sda" 0 usb
    ny_detect_boot_disk
    [ "$NY_BOOT_DISK" = usb ]
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "findmnt -no SOURCE /" 0 /dev/nvme0n1p2
    ny_rule "lsblk -no PKNAME /dev/nvme0n1p2" 0 nvme0n1
    ny_detect_boot_disk
    [ "$NY_BOOT_DISK" = nvme ]
}

@test "network backend: NetworkManager, networkd, ifupdown, dhcpcd, wicked" {
    # default demo rules: NetworkManager active and managing eth0
    run ny_detect_netbackend eth0
    assert_output networkmanager
    ny_rule "systemctl is-active --quiet NetworkManager" 3
    ny_rule "systemctl is-active --quiet systemd-networkd" 0
    run ny_detect_netbackend eth0
    assert_output networkd
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "systemctl is-active --quiet NetworkManager" 3
    ny_rule "systemctl is-active --quiet wicked" 0
    run ny_detect_netbackend eth0
    assert_output wicked
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "systemctl is-active --quiet NetworkManager" 3
    ny_rule "systemctl is-active --quiet dhcpcd" 0
    mkdir -p "${NODEYARD_ROOT}/etc/network"
    : >"${NODEYARD_ROOT}/etc/dhcpcd.conf"
    run ny_detect_netbackend eth0
    assert_output dhcpcd
    rm "${NODEYARD_ROOT}/etc/dhcpcd.conf"
    printf 'auto eth0\niface eth0 inet dhcp\n' >"${NODEYARD_ROOT}/etc/network/interfaces"
    run ny_detect_netbackend eth0
    assert_output ifupdown
}

@test "netplan wins when it has config (it generates the others)" {
    mkdir -p "${NODEYARD_ROOT}/etc/netplan"
    : >"${NODEYARD_ROOT}/etc/netplan/50-cloud-init.yaml"
    run ny_detect_netbackend eth0
    assert_output netplan
}

@test "an interface NetworkManager doesn't manage falls through" {
    ny_rule "nmcli -t -f DEVICE,STATE device status" 0 'eth0:unmanaged'
    ny_rule "systemctl is-active --quiet systemd-networkd" 0
    run ny_detect_netbackend eth0
    assert_output networkd
}

@test "firewall detection" {
    run ny_detect_firewall
    assert_output none
    ny_rule "ufw status*" 0 "Status: active"
    run ny_detect_firewall
    assert_output ufw
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "systemctl is-active --quiet firewalld" 0
    run ny_detect_firewall
    assert_output firewalld
    : >"${BATS_TEST_TMPDIR}/rules"
    ny_rule "nft list ruleset" 0 "table inet filter {"
    run ny_detect_firewall
    assert_output nftables
}

@test "interface kinds" {
    ny_lib_setup --demo-fs
    mkdir -p "${NODEYARD_ROOT}/sys/class/net/eth0/device" "${NODEYARD_ROOT}/sys/class/net/wlan0/wireless"
    [ "$(ny_iface_kind eth0)" = ethernet ]
    [ "$(ny_iface_kind wlan0)" = wifi ]
    [ "$(ny_iface_kind cni0)" = virtual ]
    [ "$(ny_iface_kind lo)" = loopback ]
}
