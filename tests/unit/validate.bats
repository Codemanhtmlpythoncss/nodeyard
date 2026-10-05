#!/usr/bin/env bats
# Input validation.

setup() {
    load ../helpers/common
    ny_lib_setup
}

@test "IPv4 addresses" {
    ny_valid_ipv4 192.168.1.10
    ny_valid_ipv4 0.0.0.0
    run ! ny_valid_ipv4 192.168.1.256
    run ! ny_valid_ipv4 192.168.1
    run ! ny_valid_ipv4 192.168.01.1
    run ! ny_valid_ipv4 "a.b.c.d"
    ny_valid_ipv4 300.1.1.1 || [[ "$NY_VALID_MSG" == *"e.g. 192.168.1.10"* ]]
}

@test "CIDR and ranges" {
    ny_valid_cidr4 192.168.1.10/24
    run ! ny_valid_cidr4 192.168.1.10
    run ! ny_valid_cidr4 192.168.1.10/33
    ny_valid_ipv4_range 192.168.1.100-192.168.1.199
    if ny_valid_ipv4_range 192.168.1.199-192.168.1.100; then fail "reversed range accepted"; fi
    [[ "$NY_VALID_MSG" == *"lower address first"* ]]
}

@test "hostnames" {
    ny_valid_hostname pi-node-1
    ny_valid_hostname yard-1.home.arpa
    run ! ny_valid_hostname -bad
    run ! ny_valid_hostname "has space"
    run ! ny_valid_hostname "trailing."
    run ! ny_valid_hostname "$(printf 'a%.0s' {1..64})"
}

@test "ports, ints, bools, macs" {
    ny_valid_port 22
    run ! ny_valid_port 0
    run ! ny_valid_port 70000
    ny_valid_int 5 1 10
    run ! ny_valid_int 11 1 10
    ny_valid_bool yes
    run ! ny_valid_bool maybe
    ny_valid_mac dc:a6:32:12:34:56
    run ! ny_valid_mac dc:a6:32:12:34
}

@test "kubernetes labels, taints and k3s versions" {
    ny_valid_label nodeyard.io/group=gpu
    ny_valid_label role=
    run ! ny_valid_label "no-equals"
    ny_valid_taint dedicated=gpu:NoSchedule
    run ! ny_valid_taint dedicated=gpu:Sometimes
    ny_valid_k3s_version v1.33.4+k3s1
    run ! ny_valid_k3s_version 1.33.4
}

@test "ny_validate dispatches on type specs" {
    ny_validate "enum:a,b" b
    run ! ny_validate "enum:a,b" c
    ny_validate "int:1:5" 3
    run ! ny_validate "int:1:5" 9
    ny_validate "list:ipv4" "10.0.0.1, 10.0.0.2"
    run ! ny_validate "list:ipv4" "10.0.0.1,nope"
    ny_validate string "anything"
    run ! ny_validate bogus-type x
}

@test "IPv4 arithmetic" {
    run ny_ip_to_int 192.168.1.10
    assert_output 3232235786
    run ny_int_to_ip 3232235786
    assert_output 192.168.1.10
    ny_cidr_contains 192.168.1.0/24 192.168.1.200
    run ! ny_cidr_contains 192.168.1.0/24 192.168.2.1
    ny_cidr_contains 0.0.0.0/0 8.8.8.8
}
