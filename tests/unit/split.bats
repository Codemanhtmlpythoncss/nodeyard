#!/usr/bin/env bats
# ai split: the speed-aware planner, disk limits, deploy without a namespace,
# per-file downloads and purge. kctl is faked per test; nothing touches a cluster.

setup() {
    load ../helpers/common
    ny_lib_setup
    ny_is_root() { return 0; }
    kctl_available() { return 0; } # (these tests fake kctl; there is no k3s on a dev machine or CI runner)
    KLOG="${BATS_TEST_TMPDIR}/kctl.log"
    : >"$KLOG"
    SPLIT_SIZE=$((17700000000))
    SPLIT_FILE="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
    SPLIT_REPO="someone/Qwen3-Coder-30B-A3B-Instruct-GGUF"
    SPLIT_NODES=()
    SPLIT_MAIN=""
    SPLIT_MAIN_ONLY=0
    SPLIT_NO_CACHE=()
    declare -gA SPLIT_RESERVE=() SPLIT_THREADS=()
    SPLIT_DISK_SPEEDS="" # (no `hw bench` results unless a test sets them)
}

# name|capMiB|usedMiB|cpus|controlPlane|arch|ready|diskFreeBytes
cluster() {
    SPLIT_NODE_CACHE="$(printf '%s\n' \
        "debian-1|16000|2000|8|false|amd64|True|860000000000" \
        "archlinux-2|8000|800|4|false|amd64|True|32000000000" \
        "debian-worker|4000|850|4|false|amd64|True|4000000000" \
        "k8s-control|8000|2000|4|true|arm64|True|50000000000" \
        "Dead_channel-1|900|400|4|false|arm|True|56000000000")"
}

@test "a mixture-of-experts model only reads its active experts per token" {
    run split_active_fraction
    assert_output "0.117"
    SPLIT_FILE="Qwen3.8-27B-Q4_K_P.gguf" SPLIT_REPO="x/Qwen3.8-27B-GGUF"
    run split_active_fraction
    assert_output "1"
}

@test "the planner prefers one fast node over a split when the model fits there" {
    run bash -c "$(declare -f split_best_subset); SPLIT_HOP_MS=40; printf '0 13000 12\n1 6000 6\n2 3000 6\n' | split_best_subset 11500 0 0.117 11200 0"
    assert_success
    [[ "$output" == *"|1 node, "*"|0:"* ]]
    [[ "$output" != *" 1:"* ]]
}

@test "the planner splits by speed and fails cleanly when nothing fits" {
    run bash -c "$(declare -f split_best_subset); SPLIT_HOP_MS=40; printf '0 9500 12\n1 5600 6\n2 3000 6\n' | split_best_subset 17388 0 0.117 16880 1"
    assert_success
    [[ "$output" == 3.*"|3 nodes"* ]]
    run bash -c "$(declare -f split_best_subset); SPLIT_HOP_MS=40; printf '0 1000 5\n1 1000 5\n' | split_best_subset 5000 0 1 4800 0"
    assert_output "none 2000"
}

@test "the main node is the one with the most free disk, and slow extras are left out" {
    cluster
    split_plan
    [[ "${PLAN_NAMES[0]}" == debian-1 ]]
    [[ -n "$PLAN_TOKS" ]]
    [[ " ${PLAN_LEFT_OUT[*]} " == *" k8s-control "* || " ${PLAN_NAMES[*]} " == *" k8s-control "* ]]
    [[ " ${PLAN_NAMES[*]} " != *" Dead_channel-1 "* ]]
}

@test "a node without disk room (ai disk limit) is skipped and gets no weight cache" {
    SPLIT_NODE_CACHE="$(printf '%s\n' \
        "debian-1|16000|2000|8|false|amd64|True|860000000000" \
        "archlinux-2|8000|800|4|false|amd64|True|2000000000" \
        "debian-worker|4000|850|4|false|amd64|True|500000000")"
    SPLIT_NODES=(debian-1 archlinux-2 debian-worker)
    run split_plan
    assert_output --partial "Skipping debian-worker: under 1 GiB of disk left"
    split_plan 2>/dev/null
    [[ " ${SPLIT_NO_CACHE[*]} " == *" archlinux-2 "* ]]
}

@test "--nodes auto means any subset; --nodes a,b means exactly those" {
    split_parse_flags --nodes auto --model a/b:c.gguf
    [[ "${#SPLIT_NODES[@]}" -eq 0 ]]
    split_parse_flags --nodes debian-1,archlinux-2 --model a/b:c.gguf
    [[ "${SPLIT_NODES[*]}" == "debian-1 archlinux-2" ]]
}

@test "each model file gets its own valid, stable download Job name" {
    a="$(split_job_name "Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated.i1-Q4_K_M.gguf")"
    b="$(split_job_name "Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated.i1-IQ3_XXS.gguf")"
    [[ "$a" != "$b" ]]
    [[ "$a" == "$(split_job_name "Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated.i1-Q4_K_M.gguf")" ]]
    [[ "$a" =~ ^dl-[a-z0-9-]+$ && ${#a} -le 63 ]]
}

@test "the manifest gives each model its own weight cache and prunes the others" {
    cluster
    split_plan
    SPLIT_URL="https://huggingface.co/x/y/resolve/main/${SPLIT_FILE}" SPLIT_SHA="" SPLIT_ALIAS=x SPLIT_THINK=off
    SPLIT_API_KEY="" SPLIT_NODEPORT=31435 SPLIT_CTX=4096 SPLIT_MODEL_DIR=/var/lib/nodeyard/models SPLIT_LLAMA_BUILD=b1
    ny_simulating() { return 0; }
    out="${BATS_TEST_TMPDIR}/m.yaml"
    split_manifest "$out"
    grep -q "name: prune-old-caches" "$out"
    grep -q 'LLAMA_CACHE, value: "/cache/qwen3-coder-30b-a3b-instruct-q4-k-m"' "$out"
    grep -q "name: $(split_job_name "$SPLIT_FILE")" "$out"
    run grep -c "name: model-download$" "$out"
    assert_output "0"
    grep -q 'echo "progress' "$out"
}

@test "deploy creates the namespace before the server-side check when it is missing" {
    split_parse_flags() { NY_YES=1; }
    split_pick_model() { :; }
    split_print_plan() { :; }
    split_disk_check() { :; }
    split_manifest() { echo "kind: Namespace" >"$1"; }
    PLAN_NAMES=(debian-1)
    SPLIT_NODEPORT=0
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            "get namespace ai-split") grep -q "^create namespace" "$KLOG" ;;
            "create namespace ai-split") return 0 ;;
            "apply --dry-run=server"*) grep -q "^create namespace" "$KLOG" || {
                echo 'namespaces "ai-split" not found' >&2
                return 1
            } ;;
            *) return 0 ;;
        esac
    }
    run ai_split_deploy
    assert_success
    run grep -n "create namespace\|dry-run=server" "$KLOG"
    [[ "${lines[0]}" == *"create namespace ai-split"* ]]
    [[ "${lines[1]}" == *"dry-run=server"* ]]
}

@test "a rejected manifest prints the reason once, not once per object" {
    split_parse_flags() { NY_YES=1; }
    split_pick_model() { :; }
    split_print_plan() { :; }
    split_disk_check() { :; }
    split_manifest() { echo "x" >"$1"; }
    PLAN_NAMES=(debian-1)
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            "get namespace ai-split") return 1 ;;
            "apply --dry-run=server"*) for _ in 1 2 3 4; do echo 'Error: bad thing' >&2; done; return 1 ;;
            *) return 0 ;;
        esac
    }
    run ai_split_deploy
    assert_failure
    run grep -c "Error: bad thing" <<<"$output"
    assert_output "1"
    grep -q "delete namespace ai-split" "$KLOG"
}

@test "undeploy --purge cleans every node even when nothing is deployed" {
    NY_YES=1
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in "get namespace ai-split") return 1 ;; *) return 0 ;; esac
    }
    split_ai_nodes() { printf '%s\n' debian-1 archlinux-2; }
    split_on_nodes() {
        echo "on-nodes $1 ${*:3}" >>"$KLOG"
        printf '== debian-1\ncleaned\n== archlinux-2\ncleaned\n'
    }
    run ai_split_undeploy --purge --yes
    assert_success
    grep -q "^on-nodes rw debian-1 archlinux-2" "$KLOG"
    assert_output --partial "debian-1 archlinux-2"
}

@test "clean keeps the running model's cache and active downloads" {
    NY_YES=1
    split_ai_nodes() { printf '%s\n' debian-1; }
    split_in_use() { echo "run.gguf|run|busy.gguf "; }
    split_on_nodes() {
        if [[ "$1" == ro ]]; then
            printf '== debian-1\nDISK 900 800\nMODEL 100 run.gguf\nMODEL 200 old.gguf\nPARTIAL 50 busy.gguf.part1\nPARTIAL 60 dead.gguf.part0\nCACHE 300 run\nCACHE 400 old\n'
        else
            echo "rw $2" >>"$KLOG"
            echo "FREED 1000 2000"
        fi
    }
    kctl() { return 0; }
    run ai_split_clean --yes
    assert_success
    run cat "$KLOG"
    assert_output --partial "/d/rpc-cache/old"
    assert_output --partial "/m/dead.gguf.part0"
    refute_output --partial "/d/rpc-cache/run'"
    refute_output --partial "busy.gguf"
    refute_output --partial "old.gguf"
}

@test "clean --models also deletes downloaded models that aren't running" {
    NY_YES=1
    split_ai_nodes() { printf '%s\n' debian-1; }
    split_in_use() { echo "run.gguf|run|"; }
    split_on_nodes() {
        if [[ "$1" == ro ]]; then printf '== debian-1\nMODEL 100 run.gguf\nMODEL 200 old.gguf\n'; else echo "rw $2" >>"$KLOG"; echo "FREED 200 900"; fi
    }
    kctl() { return 0; }
    run ai_split_clean --models --yes
    assert_success
    grep -q "/m/old.gguf" "$KLOG"
    run grep -c "run.gguf" "$KLOG"
    assert_output "0"
}

@test "split rm refuses the running model and bad names" {
    split_in_use() { echo "run.gguf|run|"; }
    run ai_split_rm run.gguf --yes
    assert_failure
    assert_output --partial "running now"
    run ai_split_rm "../etc/passwd" --yes
    assert_failure
}

@test "split rm says Deleted only when every Ready node confirmed the delete" {
    split_in_use() { echo "run.gguf|run|"; }
    split_scan_nodes() { printf '%s\n' a b c; }
    kctl() { return 0; }
    split_on_nodes() { printf '== a\nFREED 2147483648\n== b\nERROR could not run on this node (is it under disk pressure?)\n== c\nFREED 0\n'; }
    run ai_split_rm old.gguf --yes
    assert_failure
    assert_output --partial "b: delete failed (could not run on this node"
    assert_output --partial "couldn't be deleted on every node"
    refute_output --partial "Deleted old.gguf."
    [[ "$output" == *"a                freed 2.0 GiB"* ]]
}

@test "split rm skips NotReady nodes, says so, and keeps their copy" {
    split_in_use() { echo "run.gguf|run|"; }
    split_scan_nodes() { printf '%s\n' a down; }
    kctl() { return 0; }
    split_on_nodes() {
        [[ "$1" == rw && "$*" == *"/m/old.gguf"* && "$*" == *" a down" ]] || return 1
        printf '== a\nFREED 0\n== down\nERROR node is NotReady or missing; kept its last saved disk inventory\n'
    }
    run ai_split_rm old.gguf --yes
    assert_success
    assert_output --partial "down: not reached (NotReady or offline)"
    assert_output --partial "Deleted old.gguf where the nodes could be reached."
}

@test "switch still runs the new model when the old files can't all be deleted" {
    switch_stubs
    split_delete_files() { echo "delete $1" >>"$KLOG"; return 1; }
    run ai_split_switch --model a/b:new.gguf --ctx 8192 --yes
    assert_success
    assert_output --partial "couldn't be deleted"
    grep -q '^apply$' "$KLOG"
}

@test "the main model server tolerates a slow /health while it is busy answering" {
    SPLIT_FILE="m.gguf" SPLIT_SIZE=$((4500000000)) SPLIT_URL=u SPLIT_SHA="" SPLIT_ALIAS=x SPLIT_THINK=off SPLIT_API_KEY="" SPLIT_NODEPORT=0 SPLIT_CTX=4096
    SPLIT_MODEL_DIR=/var/lib/nodeyard/models SPLIT_LLAMA_BUILD=b1
    cluster
    split_plan 2>/dev/null
    ny_simulating() { return 0; }
    out="${BATS_TEST_TMPDIR}/m.yaml"
    split_manifest "$out"
    grep -A4 "readinessProbe:" "$out" | grep -q "timeoutSeconds: 5"
    run grep -c "livenessProbe" "$out"
    assert_output "0"
}

@test "clean reports what the disks freed, and fails when a node couldn't be cleaned" {
    NY_YES=1
    split_ai_nodes() { printf '%s\n' a b; }
    split_in_use() { echo "||"; }
    split_on_nodes() {
        if [[ "$1" == ro ]]; then printf '== a\nCACHE 400 old\n== b\nCACHE 500 old\n'; elif [[ "$3" == a ]]; then echo "FREED 1073741824 9"; else echo "ERROR could not run"; fi
    }
    kctl() { return 0; }
    run ai_split_clean --yes
    assert_failure
    assert_output --partial "Freed 1.0 GiB; 1 node(s) couldn't be cleaned"
}

@test "--nodes all uses every supported node, even slow ones" {
    cluster
    split_parse_flags --nodes all --model a/b:Qwen3-Coder-30B-A3B-Q4_K_M.gguf
    SPLIT_SIZE=$((17700000000)) SPLIT_FILE="Qwen3-Coder-30B-A3B-Q4_K_M.gguf"
    cluster
    split_plan 2>/dev/null
    [[ "${#PLAN_LEFT_OUT[@]}" -eq 0 ]]
    [[ " ${PLAN_NAMES[*]} " == *" k8s-control "* && " ${PLAN_NAMES[*]} " == *" archlinux-2 "* ]]
}

@test "the main node's NVIDIA GPU takes the fastest share and raises the estimate" {
    SPLIT_FILE="Qwen3-Coder-30B-A3B-Instruct-IQ3_XXS.gguf" SPLIT_SIZE=$((12500000000))
    SPLIT_NODE_CACHE="$(printf '%s\n' \
        "debian-1|16000|2000|8|false|amd64|True|860000000000|4096" \
        "archlinux-2|8000|800|4|false|amd64|True|32000000000|0")"
    split_plan 2>/dev/null
    [[ "${PLAN_NAMES[0]}" == debian-1 && "$PLAN_GPU_SHARE" -gt 2000 ]]
    with_gpu="$PLAN_TOKS"
    [[ "$PLAN_WHY" == *"+ GPU"* ]]
    SPLIT_NO_GPU=1 split_plan 2>/dev/null
    [[ "$PLAN_GPU_SHARE" -eq 0 ]]
    awk -v a="$with_gpu" -v b="$PLAN_TOKS" 'BEGIN{exit !(a > b)}'
}

@test "with a GPU share the main server uses the Vulkan build and NVIDIA's runtime" {
    SPLIT_FILE="m.gguf" SPLIT_SIZE=$((12500000000)) SPLIT_URL=u SPLIT_SHA="" SPLIT_ALIAS=x SPLIT_THINK=off SPLIT_API_KEY="" SPLIT_NODEPORT=0 SPLIT_CTX=4096
    SPLIT_MODEL_DIR=/var/lib/nodeyard/models SPLIT_LLAMA_BUILD=b1
    SPLIT_NODE_CACHE="debian-1|16000|2000|8|false|amd64|True|860000000000|4096"
    split_plan 2>/dev/null
    ny_simulating() { return 0; }
    out="${BATS_TEST_TMPDIR}/m.yaml"
    split_manifest "$out"
    grep -q "runtimeClassName: nvidia" "$out"
    grep -q "bin-ubuntu-vulkan-x64" "$out"
    grep -q "llama.cpp/b1-vulkan" "$out"
    grep -q "image: ghcr.io/ggml-org/llama.cpp:server-vulkan" "$out"
    grep -q 'NVIDIA_DRIVER_CAPABILITIES, value: "compute,utility,graphics"' "$out"
    run grep -c "cuda" "$out"
    assert_output "0"
    # llama.cpp's device order is RPC servers first, then the local GPU: its share is last
    grep -A1 -- "- -ts" "$out" | grep -q ",${PLAN_GPU_SHARE}\"$"
}

switch_stubs() {
    split_parse_flags() { NY_YES=1; echo "flags $*" >>"$KLOG"; }
    split_pick_model() { :; }
    split_print_plan() { :; }
    split_disk_check() { :; }
    split_in_use() { echo "old.gguf|old|"; }
    split_stop_servers() { echo "stop" >>"$KLOG"; }
    split_delete_files() { echo "delete $1" >>"$KLOG"; }
    split_apply() { echo "apply" >>"$KLOG"; }
    PLAN_NAMES=(debian-1)
    SPLIT_FILE="new.gguf"
}

@test "switch unloads the old model, deletes its files, then runs the new one" {
    switch_stubs
    run ai_split_switch --model a/b:new.gguf --ctx 8192 --yes
    assert_success
    run grep -v '^flags' "$KLOG"
    assert_output "$(printf 'stop\ndelete old.gguf\napply')"
    grep -q '^flags --model a/b:new.gguf --ctx 8192 --yes$' "$KLOG"
}

@test "switch --keep-old unloads but keeps the old files" {
    switch_stubs
    run ai_split_switch --model a/b:new.gguf --keep-old --yes
    assert_success
    run grep -v '^flags' "$KLOG"
    assert_output "$(printf 'stop\napply')"
    run grep -c -- "--keep-old" "$KLOG"
    assert_output "0"
}

@test "switching to the running model restarts it without deleting it" {
    switch_stubs
    SPLIT_FILE="old.gguf"
    run ai_split_switch --model a/b:old.gguf --yes
    assert_success
    run grep -v '^flags' "$KLOG"
    assert_output "$(printf 'stop\napply')"
}

@test "switch with nothing running is a plain deploy" {
    switch_stubs
    split_in_use() { echo "||"; }
    run ai_split_switch --model a/b:new.gguf --yes
    assert_success
    run grep -v '^flags' "$KLOG"
    assert_output "apply"
}

@test "undeploy unloads the model before it removes anything" {
    NY_YES=1
    kctl() {
        echo "$*" >>"$KLOG"
        return 0
    }
    split_stop_servers() { echo "stop" >>"$KLOG"; }
    run ai_split_undeploy --yes
    assert_success
    run grep -n "^stop\|delete namespace ai-split" "$KLOG"
    [[ "${lines[0]}" == *":stop" ]]
    [[ "${lines[1]}" == *"delete namespace ai-split"* ]]
}

@test "undeploy --force kills model servers and download jobs but keeps saved files" {
    NY_YES=1
    kctl() {
        echo "$*" >>"$KLOG"
        return 0
    }
    run ai_split_undeploy --force --yes
    assert_success
    assert_output --partial "Force stopping model servers and downloads"
    grep -q "delete deployments,daemonsets,statefulsets,jobs --all --ignore-not-found --wait=false" "$KLOG"
    grep -q "delete pods --all --grace-period=0 --force --wait=false --ignore-not-found" "$KLOG"
    grep -q "delete namespace ai-split --wait=true --timeout=300s" "$KLOG"
    run grep -c "on-nodes rw" "$KLOG"
    assert_output "0"
}

@test "unloading waits for the server pods and forces any that hang" {
    sleep() { :; }
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            "get namespace ai-split") return 0 ;;
            *"get pods"*) printf 'llama-main-abc ReplicaSet\ndl-model-x Job\n' ;;
            *) return 0 ;;
        esac
    }
    run split_stop_servers
    assert_success
    assert_output --partial "forcing them: llama-main-abc"
    grep -q "scale deployment --all --replicas=0" "$KLOG"
    grep -q "delete pod llama-main-abc --grace-period=0 --force" "$KLOG"
    run grep -c "dl-model-x" "$KLOG"
    assert_output "0"
}

@test "a model file name (or local:NAME) means a model already on a node's disk" {
    split_parse_model "local:Ornith-1.5-9B-Q4_K_M.gguf"
    [[ "$SPLIT_LOCAL" == 1 && "$SPLIT_FILE" == "Ornith-1.5-9B-Q4_K_M.gguf" && -z "$SPLIT_URL" && -z "$SPLIT_REPO" ]]
    split_parse_model "Ornith-1.5-9B-Q4_K_M.gguf"
    [[ "$SPLIT_LOCAL" == 1 ]]
    split_parse_model "a/b:c.gguf"
    [[ "$SPLIT_LOCAL" == 0 && "$SPLIT_URL" == "https://huggingface.co/a/b/resolve/main/c.gguf" ]]
}

@test "a downloaded model runs from the node it is on, with its size from the disk" {
    split_ai_nodes() { printf '%s\n' debian-1 archlinux-2; }
    split_on_nodes() { printf '== debian-1\nDISK 1 1\nMODEL 5777777777 Ornith-1.5-9B-Q4_K_M.gguf\n== archlinux-2\nCACHE 5 ornith\n'; }
    split_parse_model "local:Ornith-1.5-9B-Q4_K_M.gguf"
    split_local_model_info >/dev/null
    [[ "$SPLIT_MAIN" == debian-1 && "$SPLIT_SIZE" == 5777777777 && -z "$SPLIT_SHA" ]]
    SPLIT_MAIN=archlinux-2
    run split_local_model_info
    assert_failure
    assert_output --partial "is on debian-1"
    split_parse_model "local:Missing-Q4_K_M.gguf"
    SPLIT_MAIN=""
    run split_local_model_info
    assert_failure
    assert_output --partial "isn't downloaded on any node"
}

@test "a model already on disk gets no download Job" {
    cluster
    split_plan
    SPLIT_LOCAL=1 SPLIT_URL="" SPLIT_SHA="" SPLIT_ALIAS=x SPLIT_THINK=off
    SPLIT_API_KEY="" SPLIT_NODEPORT=31435 SPLIT_CTX=4096 SPLIT_MODEL_DIR=/var/lib/nodeyard/models SPLIT_LLAMA_BUILD=b1
    ny_simulating() { return 0; }
    out="${BATS_TEST_TMPDIR}/m.yaml"
    split_manifest "$out"
    run grep -c "kind: Job" "$out"
    assert_output "0"
    grep -q "name: llama-model" "$out"
    grep -q "until \[ -f '/models/${SPLIT_FILE}' \]" "$out"
    run split_disk_check
    assert_success
    assert_output ""
}

@test "weight caches only on disks faster than the network, never on the main node" {
    cluster
    SPLIT_DISK_SPEEDS="$(printf '%s\n' "debian-1 38" "archlinux-2 77" "k8s-control 400")"
    SPLIT_NODES=(debian-1 archlinux-2 k8s-control)
    split_plan >/dev/null 2>&1
    [[ "${PLAN_NAMES[0]}" == debian-1 ]]
    ny_in_list debian-1 "${SPLIT_NO_CACHE[@]}"
    ny_in_list archlinux-2 "${SPLIT_NO_CACHE[@]}"
    ! ny_in_list k8s-control "${SPLIT_NO_CACHE[@]}"
    run split_print_plan
    assert_output --partial "No weight cache on: debian-1 (main node), archlinux-2 (disk 77 MB/s)"
}

@test "the main server reads the model with direct I/O" {
    cluster
    split_plan 2>/dev/null
    SPLIT_URL=u SPLIT_SHA="" SPLIT_ALIAS=x SPLIT_THINK=off SPLIT_API_KEY="" SPLIT_NODEPORT=0 SPLIT_CTX=4096
    SPLIT_MODEL_DIR=/var/lib/nodeyard/models SPLIT_LLAMA_BUILD=b1
    ny_simulating() { return 0; }
    out="${BATS_TEST_TMPDIR}/m.yaml"
    split_manifest "$out"
    grep -A1 -- "- --load-mode" "$out" | grep -q -- "- dio"
}

@test "deploy puts the model gate back when it was installed before and went with the namespace" {
    split_parse_flags() { NY_YES=1; }
    split_pick_model() { :; }
    split_print_plan() { :; }
    split_disk_check() { :; }
    split_manifest() { echo "kind: Namespace" >"$1"; }
    PLAN_NAMES=(debian-1)
    SPLIT_NODEPORT=0
    ny_cfg_get() { if [[ "$1" == ai && "$3" == gate ]]; then echo true; elif [[ "$3" == gate-trusted ]]; then echo "10.0.0.0/8,100.64.0.0/10"; else echo "${4:-}"; fi; }
    ai_gate_install() { echo "GATE $*" >>"$KLOG"; }
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            "get namespace ai-split") return 0 ;;
            *"get daemonset llama-gate"*) return 1 ;;
            *) return 0 ;;
        esac
    }
    run ai_split_deploy
    assert_success
    run grep "^GATE" "$KLOG"
    assert_output "GATE --trusted 10.0.0.0/8,100.64.0.0/10"
}

@test "deploy leaves the gate alone when you never installed one" {
    split_parse_flags() { NY_YES=1; }
    split_pick_model() { :; }
    split_print_plan() { :; }
    split_disk_check() { :; }
    split_manifest() { echo "kind: Namespace" >"$1"; }
    PLAN_NAMES=(debian-1)
    SPLIT_NODEPORT=0
    ny_cfg_get() { echo "${4:-}"; }
    ai_gate_install() { echo "GATE $*" >>"$KLOG"; }
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            *"get daemonset llama-gate"*) return 1 ;;
            *) return 0 ;;
        esac
    }
    run ai_split_deploy
    assert_success
    run grep -c "^GATE" "$KLOG"
    assert_output "0"
}

@test "new split deployments inherit the server key, and an explicit key is staged as the shared key" {
    printf '%s' "server-key-0123456789abcdef" | ny_secret_set "$SPLIT_KEY_SECRET"
    split_parse_flags
    [ "$SPLIT_API_KEY" = "server-key-0123456789abcdef" ]
    printf '%s' "my-own-key-0123456789abcdef" >"${BATS_TEST_TMPDIR}/k"
    split_parse_flags --api-key-file "${BATS_TEST_TMPDIR}/k"
    [ "$SPLIT_API_KEY" = "my-own-key-0123456789abcdef" ]
}

@test "deploying with an explicit model key makes it the single server key before apply" {
    printf '%s' "old-server-key-0123456789" | ny_secret_set "$SPLIT_KEY_SECRET"
    SPLIT_API_KEY="model-key-0123456789abcdef"
    PLAN_NAMES=(debian-1)
    split_manifest() { printf 'kind: Secret\n' >"$1"; }
    split_hf_secret_sync() { :; }
    ny_simulating() { return 1; }
    kctl() {
        echo "$*" >>"$KLOG"
        case "$*" in
            "get namespace ai-split") return 0 ;;
            *"get job"* | *"get daemonset llama-gate"*) return 1 ;;
            *) return 0 ;;
        esac
    }

    run split_apply
    assert_success
    [ "$(ny_secret_get "$SPLIT_KEY_SECRET")" = "$SPLIT_API_KEY" ]
    run grep -F "apply -f" "$KLOG"
    assert_success
}

@test "ai key --show prints the server API key, and says how to make one when there is none" {
    run ai_split_key --show
    assert_failure
    assert_output --partial "no server API key"
    printf '%s' "server-key-0123456789abcdef" | ny_secret_set "$SPLIT_KEY_SECRET"
    run ai_split_key --show
    assert_success
    assert_output "server-key-0123456789abcdef"
}

@test "ai key is a command next to ai split key" {
    [ "${NY_CMD_FN["ai key"]}" = "ai_split_key" ]
    [ "${NY_CMD_FN["ai split key"]}" = "ai_split_key" ]
}

@test "model inventory includes supported offline nodes and marks incomplete scans" {
    NY_JSON=1
    split_scan_nodes() { printf '%s\n' debian-1 debian-offline; }
    split_on_nodes() {
        printf '== debian-1\nDISK 1000 500\nMODEL 42 one.gguf\n== debian-offline\nERROR could not run on this node\n'
    }
    split_in_use() { printf '|\n'; }

    run ai_split_models

    assert_success
    assert_output --partial '"node":"debian-1"'
    assert_output --partial '"name":"one.gguf"'
    assert_output --partial '"node":"debian-offline"'
    assert_output --partial '"scan_error":"Disk scan did not finish on this node"'
}

@test "disk scans include supported architectures even when their nodes are not ready" {
    kctl() {
        printf '%s\n' 'debian-1 amd64 True' 'debian-offline arm64 False' 'legacy-node arm False'
    }

    run split_scan_nodes

    assert_success
    assert_output $'debian-1\ndebian-offline'
}
