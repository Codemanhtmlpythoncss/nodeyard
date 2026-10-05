#!/usr/bin/env bats
# Semantic version comparison.

setup() {
    load ../helpers/common
    ny_lib_setup
}

cmp() {
    run ny_version_cmp "$1" "$2"
    assert_output "$3"
}

@test "numeric parts compare numerically" {
    cmp 1.10.0 1.9.0 1
    cmp 1.9.0 1.10.0 -1
    cmp 2.0.0 2.0.0 0
    cmp v1.2.3 1.2.3 0
    cmp 1.2 1.2.0 0
}

@test "pre-releases sort before the release" {
    cmp 1.0.0-alpha.1 1.0.0 -1
    cmp 1.0.0 1.0.0-rc.1 1
    cmp 1.0.0-alpha.2 1.0.0-alpha.10 -1
    cmp 1.0.0-alpha 1.0.0-beta -1
    cmp 1.0.0-alpha.1 1.0.0-alpha 1
}

@test "build metadata is ignored" {
    cmp v1.33.4+k3s1 v1.33.4+k3s2 0
    cmp v1.33.5+k3s1 v1.33.4+k3s9 1
}
