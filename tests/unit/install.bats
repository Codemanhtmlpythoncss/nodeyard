#!/usr/bin/env bats
# install.sh and uninstall.sh, installing into a temporary prefix.

setup() {
    load ../helpers/common
    export NODEYARD_INSTALL_ANY_OS=1
    PREFIX="${BATS_TEST_TMPDIR}/prefix"
    mkdir -p "$PREFIX"
    export NODEYARD_ROOT="${BATS_TEST_TMPDIR}/root"
}

install_it() {
    bash "${NY_REPO_ROOT}/install.sh" --from-dir "$NY_REPO_ROOT" --prefix "$PREFIX" --no-deps --yes "$@"
}

@test "installs files and links, and the installed copy runs" {
    run install_it
    assert_success
    [ -L "${PREFIX}/bin/nodeyard" ]
    [ -L "${PREFIX}/bin/k3s-manager" ]
    [ -x "${PREFIX}/lib/nodeyard/bin/nodeyard" ]
    run "${PREFIX}/bin/nodeyard" --version
    assert_output --regexp '^nodeyard [0-9]'
    run "${PREFIX}/bin/k3s-manager" --version
    assert_output --regexp '^nodeyard [0-9]'
}

@test "the terminal AI agent (yardcode) is installed with nodeyard and works on its own" {
    command -v python3 >/dev/null || skip "python3 not installed"
    run install_it
    assert_success
    [ -L "${PREFIX}/bin/yardcode" ]
    [ -x "${PREFIX}/lib/nodeyard/yardcode/bin/yardcode" ]
    run "${PREFIX}/bin/yardcode" --version
    assert_output --regexp '^yardcode [0-9]'
    # removing nodeyard removes it too
    run bash "${NY_REPO_ROOT}/uninstall.sh" --prefix "$PREFIX" --yes --force
    assert_success
    [ ! -e "${PREFIX}/bin/yardcode" ]
}

@test "yardcode installs on its own (plain sh, no nodeyard needed) and uninstalls cleanly" {
    command -v python3 >/dev/null || skip "python3 not installed"
    run sh "${NY_REPO_ROOT}/yardcode/install.sh" --from-dir "$NY_REPO_ROOT" --prefix "${BATS_TEST_TMPDIR}/solo"
    assert_success
    run "${BATS_TEST_TMPDIR}/solo/bin/yardcode" --version
    assert_output --regexp '^yardcode [0-9]'
    run sh "${NY_REPO_ROOT}/yardcode/install.sh" --from-dir "$NY_REPO_ROOT" --prefix "${BATS_TEST_TMPDIR}/solo"
    assert_output --partial "already installed"
    run sh "${NY_REPO_ROOT}/yardcode/install.sh" --prefix "${BATS_TEST_TMPDIR}/solo" --uninstall
    assert_success
    [ ! -e "${BATS_TEST_TMPDIR}/solo/bin/yardcode" ]
}

@test "installing the same version again changes nothing" {
    install_it
    run install_it
    assert_success
    assert_output --partial "already installed"
}

@test "--dry-run installs nothing" {
    run install_it --dry-run
    assert_success
    [ ! -e "${PREFIX}/lib/nodeyard" ]
    [ ! -e "${PREFIX}/bin/nodeyard" ]
}

@test "an old k3s-manager script is kept as .legacy and aliased" {
    mkdir -p "${PREFIX}/bin"
    printf '#!/bin/bash\n# k3s-manager 3.2.0\n' >"${PREFIX}/bin/k3s-manager"
    run install_it
    assert_success
    [ -f "${PREFIX}/bin/k3s-manager.legacy" ]
    [ -L "${PREFIX}/bin/k3s-manager" ]
}

@test "uninstall removes what install created, and the config unless kept" {
    install_it
    mkdir -p "${NODEYARD_ROOT}/etc/nodeyard"
    : >"${NODEYARD_ROOT}/etc/nodeyard/cluster.conf"
    run bash "${NY_REPO_ROOT}/uninstall.sh" --prefix "$PREFIX" --yes --keep-config
    assert_success
    [ ! -e "${PREFIX}/lib/nodeyard" ]
    [ ! -e "${PREFIX}/bin/nodeyard" ]
    [ -f "${NODEYARD_ROOT}/etc/nodeyard/cluster.conf" ]
    install_it
    run bash "${NY_REPO_ROOT}/uninstall.sh" --prefix "$PREFIX" --yes
    [ ! -e "${NODEYARD_ROOT}/etc/nodeyard" ]
}

@test "uninstall refuses while nodeyard's system changes are still in place" {
    install_it
    mkdir -p "${NODEYARD_ROOT}/var/lib/nodeyard/journal"
    printf '%s\n' '{"txn":"t1","op":"write","path":"/etc/x","feature":"k3s"}' >"${NODEYARD_ROOT}/var/lib/nodeyard/journal/journal.jsonl"
    run bash "${NY_REPO_ROOT}/uninstall.sh" --prefix "$PREFIX" --yes
    assert_failure
    assert_output --partial "uninstall everything"
    [ -e "${PREFIX}/lib/nodeyard" ]
}
