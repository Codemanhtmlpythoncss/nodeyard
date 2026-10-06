#!/usr/bin/env bats
# Explaining why a k3s service would not start.

setup() {
    load ../helpers/common
    ny_lib_setup --demo-fs
    export NODEYARD_WAIT_INTERVAL=0
}

diagnose_with() { # LOG_TEXT -> runs k3s_diagnose with that journal
    ny_rule "journalctl *" 0 "$1"
    run k3s_diagnose k3s-agent
}

@test "shows the log lines that matter, redacted" {
    diagnose_with 'Oct 06 systemd[1]: Starting k3s-agent\nOct 06 k3s[1]: level=fatal msg="failed with token=Sup3rS3cretT0ken"'
    assert_success
    assert_output --partial "k3s-agent.service would not start"
    assert_output --partial "level=fatal"
    refute_output --partial Sup3rS3cretT0ken
    assert_output --partial "[REDACTED]"
}

@test "nm-cloud-setup" {
    diagnose_with 'ExecStartPre: ! /usr/bin/systemctl is-enabled --quiet nm-cloud-setup.service failed'
    assert_output --partial "Likely cause: NetworkManager's cloud-setup"
    assert_output --partial "disable --now nm-cloud-setup"
}

@test "memory cgroup (Raspberry Pi)" {
    diagnose_with 'level=fatal msg="failed to find memory cgroup, you may need to add \\"cgroup_memory=1 cgroup_enable=memory\\" to your linux cmdline"'
    assert_output --partial "memory cgroup is switched off"
    assert_output --partial "nodeyard doctor --fix"
}

@test "duplicate hostname" {
    diagnose_with 'level=fatal msg="Node password rejected, duplicate hostname or contents of /etc/rancher/node/password may not match server node-passwd entry"'
    assert_output --partial "already has a node with this machine's name"
    assert_output --partial "hostnamectl set-hostname"
}

@test "cannot reach the server names the server and a test command" {
    K3S_J_SERVER="https://10.50.0.1:6443"
    diagnose_with 'level=error msg="failed to get CA certs: Get \\"https://10.50.0.1:6443/cacerts\\": dial tcp 10.50.0.1:6443: connect: no route to host"'
    assert_output --partial "can't reach the k3s server at https://10.50.0.1:6443"
    assert_output --partial "curl -k https://10.50.0.1:6443/ping"
    assert_output --partial "firewall status"
}

@test "wrong token" {
    diagnose_with 'level=error msg="Unauthorized"'
    assert_output --partial "did not accept the join token"
}

@test "an unknown failure says so, and always ends with where to look next" {
    diagnose_with 'level=error msg="something nobody has seen"'
    assert_output --partial "No known cause matched"
    assert_output --partial "journalctl -u k3s-agent"
    assert_output --partial "nodeyard doctor"
}

@test "ordinary iptables lines in a log do not trigger the firewall hint" {
    diagnose_with 'level=info msg="Running kube-proxy with iptables"\nlevel=error msg="boom"'
    refute_output --partial "firewall tool or kernel module"
}

@test "the installer is told not to start the service itself" {
    ny_lib_setup --demo-fs
    NY_DEMO=1
    NY_DRY_RUN=1
    run k3s_run_installer INSTALL_K3S_EXEC=agent
    assert_output --partial "INSTALL_K3S_SKIP_START=true"
}

@test "a service that fails to start is diagnosed and the command stops" {
    ny_rule "systemctl restart k3s-agent" 1
    ny_rule "journalctl *" 0 'level=error msg="Unauthorized"'
    NY_INIT=systemd
    run k3s_start_service k3s-agent
    assert_failure 1
    assert_output --partial "did not accept the join token"
    assert_output --partial "k3s-agent.service failed to start"
}

@test "an address mismatch (the node's IP changed) is explained and says it usually fixes itself" {
    diagnose_with 'level=error msg="Shutdown request received: \\"failed to start networking: unable to initialize network policy controller: error getting node subnet: failed to find interface with specified node ip\\""'
    assert_output --partial "registered under a different address"
    assert_output --partial "fixes itself within a minute"
    assert_output --partial "nodeyard remove-node NAME"
}

@test "ordinary cgroup lines in a log do not trigger the memory-cgroup hint" {
    diagnose_with 'I1006 container_manager_linux.go: Creating Container Manager object nodeConfig={"CgroupRoot":"/","CgroupDriver":"systemd","MemoryManagerPolicy":"None","CgroupVersion":2}\nlevel=error msg="boom"'
    refute_output --partial "memory cgroup is switched off"
}

@test "the decisive error line is shown before the noise" {
    diagnose_with 'I1006 factory.go: Registration of the crio container factory failed: dial unix /var/run/crio/crio.sock: no such file\nlevel=error msg="Shutdown request received: something decisive"'
    assert_output --partial "Shutdown request received: something decisive"
    refute_output --partial "crio container factory"
}

@test "a first start that fails but recovers on systemd's retry counts as success" {
    ny_rule_first "systemctl is-active --quiet k3s-agent" 0
    ny_rule_first "systemctl restart k3s-agent" 1
    NY_INIT=systemd
    run k3s_start_service k3s-agent
    assert_success
    assert_output --partial "did not start on the first try"
    assert_output --partial "came up on a retry"
    refute_output --partial "would not start"
}
