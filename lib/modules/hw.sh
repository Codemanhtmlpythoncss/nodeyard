# shellcheck shell=bash
# shellcheck disable=SC2034 # NY_YES is read by the core
# Hardware speed tests: memory copy speed, CPU speed and disk speed on every
# node, measured by a short Job each (share/nodeyard/bench/bench.py). Results
# go into the ConfigMap nodeyard-bench (namespace nodeyard-system), which the
# dashboard's Hardware page shows next to what the node agents report.

ny_cmd "hw bench" hw_bench_cmd "Nodes" "Measure every node's memory, CPU and disk speed (about a minute)" hw json
ny_cmd "hw show" hw_show_cmd "Nodes" "Show the last speed-test results" hw json

HW_NS="nodeyard-system"
HW_CM="nodeyard-bench"
HW_IMAGE="python:3.12-alpine"

hw_bench_cmd_help() {
    cat <<'HELP'
Usage: nodeyard hw bench [--node NODE]... [--json]
       nodeyard hw show [--json]

Measures, on every Ready node (or the ones you name):
  memory   how fast one core and all cores copy memory (GB/s)
  CPU      a simple integer score for one core and all cores (higher is faster)
  disk     sequential write and read speed (MB/s) and random 4 KiB reads (IOPS)
           of the root partition, with a temporary file of at most 1 GiB in
           /var/lib/nodeyard/bench that is deleted again (it stays within
           'nodeyard ai disk limit'). Skipped on control-plane nodes: the
           cluster's database lives on that disk and would stall
Each node runs one short Job (about 15 seconds) at the lowest CPU priority,
one node at a time.
HELP
}
hw_show_cmd_help() { hw_bench_cmd_help; }

# hw_bench_script -- the benchmark, base64 (passed on the command line: no ConfigMap needed)
hw_bench_script() {
    base64 <"${NY_HOME}/share/nodeyard/bench/bench.py" | tr -d '\n'
    return 0
}

hw_bench_cmd() {
    ny_need_kube
    local -a only=() nodes=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --node)
                ny_need_value "$1" $#
                only+=("$2")
                shift 2
                ;;
            --yes | -y)
                NY_YES=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard hw bench [--node NODE]..." ;;
        esac
    done
    local name ready room disk cp skip
    while IFS='|' read -r name _ _ _ cp _ ready disk _; do
        [[ -n "$name" && "$ready" == "True" ]] || continue
        if [[ "${#only[@]}" -gt 0 ]] && ! ny_in_list "$name" "${only[@]}"; then continue; fi
        nodes+=("${name}|${disk}|${cp}")
    done <<<"$(split_node_table)"
    # split_node_table leaves out what it can't read; the Pi 3 (armv7) still gets tested
    if [[ "${#only[@]}" -eq 0 ]]; then
        while read -r name; do
            [[ -n "$name" ]] || continue
            printf '%s\n' "${nodes[@]}" | grep -q "^${name}|" || nodes+=("${name}||false")
        done < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
    fi
    [[ "${#nodes[@]}" -gt 0 ]] || ny_die "No Ready nodes to test."
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add apply "Run a speed-test Job on: $(printf '%s ' "${nodes[@]%%|*}")"
        return 0
    fi
    local script entry job manifest
    script="$(hw_bench_script)"
    kctl get namespace "$HW_NS" >/dev/null 2>&1 || kctl create namespace "$HW_NS" >/dev/null
    kctl -n "$HW_NS" get configmap "$HW_CM" >/dev/null 2>&1 || kctl -n "$HW_NS" create configmap "$HW_CM" >/dev/null
    ny_step "Testing ${#nodes[@]} node(s), one at a time so they don't slow each other down"
    for entry in "${nodes[@]}"; do
        name="${entry%%|*}"
        room="${entry#*|}"
        cp="${room#*|}"
        room="${room%%|*}"
        # The cluster's database (etcd) lives on a control-plane node's disk:
        # a heavy write test there stalls it and the API server stops answering.
        skip=0
        [[ "$cp" == "true" ]] && skip=1
        job="bench-$(ny_k8s_name "$name")"
        kctl -n "$HW_NS" delete job "$job" --ignore-not-found --wait=true >/dev/null 2>&1 || true
        manifest="$(ny_mktemp)"
        cat >"$manifest" <<YAML
apiVersion: batch/v1
kind: Job
metadata: {name: ${job}, namespace: ${HW_NS}, labels: {app.kubernetes.io/managed-by: nodeyard, app.kubernetes.io/component: bench}}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata: {labels: {app.kubernetes.io/component: bench}}
    spec:
      restartPolicy: Never
      nodeSelector: {kubernetes.io/hostname: ${name}}
      tolerations: [{operator: Exists}]
      containers:
      - name: bench
        image: ${HW_IMAGE}
        imagePullPolicy: IfNotPresent
        env: [{name: BENCH_DIR, value: /bench}, {name: ROOM, value: "${room}"}, {name: SKIP_DISK, value: "${skip}"}]
        command: ["python3", "-c", "import base64; exec(compile(base64.b64decode('${script}'), 'bench.py', 'exec'))"]
        volumeMounts: [{name: b, mountPath: /bench}]
      volumes: [{name: b, hostPath: {path: /var/lib/nodeyard/bench, type: DirectoryOrCreate}}]
YAML
        kctl apply -f "$manifest" >/dev/null || {
            rm -f "$manifest"
            ny_warn "Couldn't start the test on ${name}."
            continue
        }
        rm -f "$manifest"
        kctl -n "$HW_NS" wait --for=condition=complete "job/${job}" --timeout=240s >/dev/null 2>&1 ||
            kctl -n "$HW_NS" wait --for=condition=failed "job/${job}" --timeout=5s >/dev/null 2>&1 || true
        local line patch
        line="$(kctl -n "$HW_NS" logs "job/${job}" 2>/dev/null | grep '^BENCH ' | tail -1 || true)"
        if [[ -z "$line" ]]; then
            ny_warn "${name}: the test didn't finish (is the node busy or under disk pressure?)."
            continue
        fi
        patch="$(jq -cn --arg n "$name" --arg v "${line#BENCH }" '{data: {($n): $v}}')"
        kctl -n "$HW_NS" patch configmap "$HW_CM" --type merge -p "$patch" >/dev/null || ny_warn "Couldn't save the result for ${name}."
        [[ "$NY_JSON" -eq 1 ]] || hw_print_one "$name" "${line#BENCH }"
    done
    if [[ "$NY_JSON" -eq 1 ]]; then hw_show_cmd; else ny_ok "Saved. See them any time: nodeyard hw show (or the dashboard's Hardware page)"; fi
    return 0
}

# hw_print_one NODE JSON -- one node's results as a line of text
hw_print_one() {
    jq -r --arg n "$1" '"  \($n | . + "                " | .[0:16]) memory \(.mem_copy_1core_gbs) GB/s (1 core), \(.mem_copy_all_gbs) GB/s (all)   CPU \(.cpu_1core) / \(.cpu_all)   disk " +
        (if .disk.write_mbs then "write \(.disk.write_mbs) MB/s, read \(.disk.read_mbs) MB/s, \(.disk.rand_read_iops) IOPS" else (.disk.skipped // .disk.error // "-") end)' <<<"$2" 2>/dev/null ||
        printf '  %s: %s\n' "$1" "$2"
    return 0
}

hw_show_cmd() {
    ny_need_kube
    local data
    data="$(kctl -n "$HW_NS" get configmap "$HW_CM" -o json 2>/dev/null | jq -c '.data // {}' 2>/dev/null || true)"
    if [[ -z "$data" || "$data" == "{}" ]]; then
        if [[ "$NY_JSON" -eq 1 ]]; then ny_json_out '{"ok":true,"nodes":{}}'; else echo "No speed tests yet. Run one: nodeyard hw bench"; fi
        return 0
    fi
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(jq -c '{ok: true, nodes: (with_entries(.value |= (fromjson? // null)))}' <<<"$data")"
        return 0
    fi
    local n
    while read -r n; do
        hw_print_one "$n" "$(jq -r --arg n "$n" '.[$n]' <<<"$data")"
    done < <(jq -r 'keys[]' <<<"$data")
    return 0
}
