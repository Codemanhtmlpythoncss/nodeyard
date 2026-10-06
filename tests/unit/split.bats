#!/usr/bin/env bats
# The ai split planner's view of the nodes.

setup() {
    load ../helpers/common
    ny_lib_setup --demo-fs
    # the k3s binary the code calls (demo fs has none): point it at the shim
    mkdir -p "${NODEYARD_ROOT}/usr/local/bin"
    ln -sfn "${NY_REPO_ROOT}/share/nodeyard/demo/shim.sh" "${NODEYARD_ROOT}/usr/local/bin/k3s"
    NODES='n-a\t8038648Ki\t4\tamd64\tTrue\tFalseFalse\t\nn-b\t16244588Ki\t8\tamd64\tTrue\tFalseFalse\t\nn-c\t3930704Ki\t4\tamd64\tTrue\tFalseFalse\t\nn-cp\t8145728Ki\t4\tarm64\tTrue\tFalseFalse\ttrue'
    ny_rule "k3s kubectl get nodes -o jsonpath=*capacity.memory*" 0 "$NODES"
    # metrics: only two nodes have a reading; n-a and n-b are "<unknown>"
    ny_rule "k3s kubectl top nodes --no-headers" 0 'n-c   23m   0%   402Mi   10%\nn-cp   1026m   25%   2604Mi   32%\nn-a   <unknown>   <unknown>   <unknown>   <unknown>\nn-b   <unknown>   <unknown>   <unknown>   <unknown>'
    ny_rule "k3s kubectl -n ai-split *" 0 ""
}

@test "a node with no metrics reading keeps its Ready state (no shifted columns)" {
    run split_node_table
    assert_success
    # name|memMiB|usedMiB|cpus|controlplane|arch|ready
    assert_line --regexp '^n-a\|7850\|[0-9]*\|4\|false\|amd64\|True$'
    assert_line --regexp '^n-b\|15863\|[0-9]*\|8\|false\|amd64\|True$'
    assert_line --regexp '^n-cp\|7954\|2604\|4\|true\|arm64\|True$'
}

@test "the node's own kubelet supplies free memory when metrics are missing" {
    ny_rule "k3s kubectl get --raw /api/v1/nodes/n-b/proxy/stats/summary" 0 '{"node":{"memory":{"availableBytes":14000000000}}}'
    run split_node_table
    # 15863 MiB total, 14000000000 B (= 13351 MiB) available -> 2512 MiB used
    assert_line --regexp '^n-b\|15863\|2512\|8\|false\|amd64\|True$'
}

@test "the planner uses every Ready node, including those without metrics" {
    split_parse_flags
    SPLIT_SIZE=$((8 * 1024 * 1024 * 1024))
    SPLIT_NODE_CACHE="$(split_node_table)"
    split_plan
    local joined
    joined="${PLAN_NAMES[*]}"
    [[ "$joined" == *n-a* && "$joined" == *n-b* && "$joined" == *n-c* ]] || fail "plan skipped nodes: $joined"
}

@test "an unusable node is still skipped and named" {
    ny_rule_first "k3s kubectl get nodes -o jsonpath=*capacity.memory*" 0 'n-a\t8038648Ki\t4\tamd64\tFalse\tFalseFalse\t\nn-b\t16244588Ki\t8\tamd64\tTrue\tFalseFalse\t'
    split_parse_flags
    SPLIT_SIZE=$((2 * 1024 * 1024 * 1024))
    SPLIT_NODE_CACHE="$(split_node_table)"
    run split_plan
    assert_output --partial "Skipping n-a: not Ready"
}
