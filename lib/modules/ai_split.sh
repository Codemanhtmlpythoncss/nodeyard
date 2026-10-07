# shellcheck shell=bash
# shellcheck disable=SC2034 # globals here are read by other files
# EXPERIMENTAL: one model too big for any single node, split across several
# with llama.cpp RPC (ported unchanged in behaviour from k3s-manager 3.2).
# Every generated token passes through every node over the network, so it
# is slow over ethernet and every node must stay on while it runs.

ny_cmd "ai split plan" ai_split_plan "AI" "Experimental: plan how one big model would be split across nodes" ai
ny_cmd "ai split deploy" ai_split_deploy "AI" "Experimental: deploy one model split across several nodes" ai
ny_cmd "ai split status" ai_split_status "AI" "Experimental: download/load progress of the split model" ai
ny_cmd "ai split test" ai_split_test "AI" "Experimental: send the split model a prompt and time it" ai
ny_cmd "ai gate install" ai_gate_install "AI" "Experimental: no API key needed from your own network (Tailscale, LAN); outside addresses still need it" ai
ny_cmd "ai gate remove" ai_gate_remove "AI" "Experimental: remove the model gate (the API key is needed everywhere again)" ai
ny_cmd "ai gate status" ai_gate_status "AI" "Experimental: show the model gate" ai
ny_cmd "ai split unload" ai_split_unload "AI" "Experimental: free the split model's memory on every node (keeps the download)" ai
ny_cmd "ai split load" ai_split_load "AI" "Experimental: load the split model again after an unload" ai
ny_cmd "ai split undeploy" ai_split_undeploy "AI" "Experimental: remove the split model (unloads it first)" ai
ny_cmd "ai split switch" ai_split_switch "AI" "Change the running split model: unload the old one, delete its files, run the new one" ai
ny_cmd_alias "ai split remove" "ai split undeploy"
ny_cmd "ai split clean" ai_split_clean "AI" "Free disk space: delete old weight caches and unfinished downloads on every node" ai json
ny_cmd "ai split models" ai_split_models "AI" "List downloaded model files, caches and free disk on every node" ai json
ny_cmd "ai split rm" ai_split_rm "AI" "Delete a downloaded split model (file + caches) from every node" ai
ny_cmd "ai split download" ai_split_download "AI" "Download a model file in the background (several can run at once)" ai
ny_cmd "ai split key" ai_split_key "AI" "Change the split model's API key (rotate or set) and apply it" ai
ny_cmd "ai hf token" ai_hf_token "AI" "Store a Hugging Face token for gated models" ai
ny_cmd "ai gpu setup" ai_gpu_setup "AI" "Let containers on this machine use its NVIDIA card (run on that machine)" ai
ny_cmd "ai gpu enable" ai_gpu_enable "AI" "Check a node's NVIDIA card works in containers and use it for models" ai
ny_cmd "ai gpu status" ai_gpu_status "AI" "Show the nodes whose NVIDIA card models can use" ai
ny_cmd "ai disk limit" ai_disk_limit "AI" "Cap how full nodeyard lets a node's disk get (e.g. debian-worker 8)" ai

ai_split_plan_help() {
    cat <<'HELP'
Usage: nodeyard ai split plan|deploy [options]

EXPERIMENTAL. Runs ONE model whose weights don't fit on any single node by
giving each node a share of its layers (llama.cpp RPC). Every token passes
through every node over the network, so it is much slower than one big
machine, and all the nodes must stay on.

Options:
  --model auto|owner/repo:file.gguf   Model (default: the biggest Qwen3.6-35B-A3B quant that fits)
  --main NODE              Node that stores the model and serves the API
                           (default: most free space on its root partition)
  --nodes auto|all|a,b,c   auto (default): the fastest set of nodes that
                           fits; all: every supported node; or exactly these
  --min-speed TOK/S        With --model auto: the biggest quant that reaches
                           this estimated speed (default 10)
  --reserve NODE=GiB       Keep extra memory free on a node (repeatable)
  --threads NODE=N         CPU threads on a node (repeatable)
  --main-only              The main node coordinates and holds no layers
  --no-gpu                 Don't use the main node's NVIDIA GPU (see: nodeyard ai gpu)
  --model-dir /path        Where the main node keeps the model file
  --no-cache a,b           Don't cache weights on these nodes (slow disks)
  --ctx N                  Context length (default 16384)       [deploy]
  --think on|off           Allow reasoning output (default off) [deploy]
  --nodeport PORT|0        Chat page + API NodePort (default 31435; 0 = cluster only) [deploy]
  --api-key-file PATH      Require this API key (read from a file) [deploy]
  --alias NAME             Model name the API reports              [deploy]
HELP
}
ai_split_deploy_help() { ai_split_plan_help; }

ai_gate_install_help() {
    cat <<'HELP'
Usage: nodeyard ai gate install [--trusted NET,NET,...] [--port PORT]
       nodeyard ai gate remove
       nodeyard ai gate status

EXPERIMENTAL. By default the split model's API needs its key from everywhere.
The gate changes that: requests from your own network need no key, and requests
from anywhere else (for example a router port-forward from the internet) still
do. It runs a tiny proxy on every node, on the model's port (default 31435),
and decides by the address a request really comes from. It never trusts
X-Forwarded-For or similar headers.

Trusted by default (no key needed): this machine, the private ranges 10/8,
172.16/12, 192.168/16 (your LAN and the cluster) and Tailscale (100.64/10 and
its IPv6 range).

Options:
  --trusted NETS   Replace that list (comma-separated networks, e.g. 100.64.0.0/10,192.168.1.0/24)
  --port PORT      The port to answer on (default: the model's current NodePort, or 31435)

If the model has no API key, outside addresses are refused outright.
`ai split deploy` keeps the gate in place when you replace the model.
Other nodes' own firewalls (if any) must allow the port.
HELP
}
ai_gate_remove_help() { ai_gate_install_help; }
ai_gate_status_help() { ai_gate_install_help; }

# ---------- AI: one big model split across nodes (llama.cpp RPC) ----------
#
# Unlike `ai deploy` (a full copy of a small model on every node, requests
# load-balanced), `ai split` runs ONE model too big for any single node by
# giving each node a share of its layers. Every node (including the main one)
# runs llama.cpp's ggml-rpc-server holding its share; the main node also runs
# llama-server, which drives the pipeline and serves a chat UI + OpenAI API.
# Shares are sized from each node's live free memory, so no layer counts are
# needed. The RPC servers are reachable only inside the cluster network, and a
# NetworkPolicy only lets the main server talk to them (ggml RPC has no auth).

SPLIT_NS="ai-split"
SPLIT_GATE_DS="llama-gate"
SPLIT_GATE_IMAGE="python:3.12-alpine"
SPLIT_GATE_PUBLIC_PORT=31436 # on 127.0.0.1 only; the key is always needed there (Tailscale Funnel's target)
ny_cfg_section_add ai single
ny_cfg_schema_add ai.gate bool "" "Keep the model gate installed: nodeyard puts it back after the model is redeployed"
ny_cfg_schema_add ai.gate-trusted string "" "Networks that need no API key (set by 'nodeyard ai gate install')"
SPLIT_GATE_TRUSTED="127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7,fd7a:115c:a1e0::/48"
SPLIT_LLAMA_BUILD="${NODEYARD_LLAMA_BUILD:-b11160}"
# NVIDIA GPUs run llama.cpp's Vulkan build, not its CUDA one: the CUDA builds need
# a newer driver than many machines have (driver 550 can't run the CUDA 12.8 build's
# kernels: "PTX was compiled with an unsupported toolchain"), Vulkan works with any.
SPLIT_GPU_IMAGE="ghcr.io/ggml-org/llama.cpp:server-vulkan" # (has the Vulkan loader)
# Default (--model auto): the biggest of these Qwen3.6-35B-A3B quants that fits
# in the cluster's free memory right now, best first.
SPLIT_AUTO_REPO="unsloth/Qwen3.6-35B-A3B-GGUF"
SPLIT_AUTO_PREFIX="Qwen3.6-35B-A3B-"
SPLIT_AUTO_QUANTS=(Q8_0 UD-Q6_K_XL UD-Q5_K_XL UD-Q4_K_XL UD-IQ4_XS UD-Q3_K_XL UD-IQ3_S UD-Q2_K_XL)
SPLIT_DEFAULT_MODEL="auto"
SPLIT_DEFAULT_MODEL_DIR="/var/lib/nodeyard/models"

# split_parse_model SPEC -> SPLIT_REPO SPLIT_REV SPLIT_FILE SPLIT_URL SPLIT_LOCAL
# SPEC is "owner/repo:file.gguf", a huggingface.co .../resolve|blob/<rev>/<file>
# URL, or "local:file.gguf" (or just file.gguf): a model already downloaded on
# one of the nodes, run from there with no download.
split_parse_model() {
    local spec="$1"
    SPLIT_LOCAL=0
    if [[ "$spec" =~ ^(local:)?([A-Za-z0-9][A-Za-z0-9._+-]*\.gguf)$ ]]; then
        SPLIT_LOCAL=1
        SPLIT_REPO=""
        SPLIT_REV=""
        SPLIT_FILE="${BASH_REMATCH[2]}"
        SPLIT_URL=""
        return 0
    fi
    if [[ "$spec" =~ ^https?://huggingface\.co/([^/]+/[^/]+)/(resolve|blob)/([^/]+)/(.+)$ ]]; then
        SPLIT_REPO="${BASH_REMATCH[1]}"
        SPLIT_REV="${BASH_REMATCH[3]}"
        SPLIT_FILE="${BASH_REMATCH[4]}"
    elif [[ "$spec" =~ ^([^/:]+/[^/:]+):(.+)$ ]]; then
        SPLIT_REPO="${BASH_REMATCH[1]}"
        SPLIT_REV="main"
        SPLIT_FILE="${BASH_REMATCH[2]}"
    else
        ny_die "--model must be 'owner/repo:file.gguf' or a huggingface.co file URL (got: $spec)"
    fi
    [[ "$SPLIT_FILE" == *.gguf ]] || ny_die "The model file must be a single .gguf (got: $SPLIT_FILE)"
    [[ "$SPLIT_FILE" =~ -[0-9]{5}-of-[0-9]{5}\.gguf$ ]] && ny_die "Multi-part GGUFs aren't supported yet; pick a single-file quant."
    SPLIT_URL="https://huggingface.co/${SPLIT_REPO}/resolve/${SPLIT_REV}/${SPLIT_FILE}"
    return 0
}

# split_model_info -> SPLIT_SIZE (bytes) and SPLIT_SHA (sha256), from Hugging Face's headers
split_model_info() {
    if [[ "${SPLIT_LOCAL:-0}" -eq 1 ]]; then
        split_local_model_info
        return 0
    fi
    local hdr
    local -a auth=()
    mapfile -t auth < <(split_hf_curl_args)
    hdr="$(curl -sI --max-time 30 "${auth[@]}" "$SPLIT_URL" 2>/dev/null || true)"
    SPLIT_SIZE="$(grep -i '^x-linked-size:' <<<"$hdr" | tr -dc '0-9' || true)"
    SPLIT_SHA="$(grep -i '^x-linked-etag:' <<<"$hdr" | grep -oE '[0-9a-f]{64}' || true)"
    [[ -n "$SPLIT_SIZE" ]] || ny_die "Couldn't get the size of $SPLIT_URL -- check the repo/file name and that this machine is online."
    if [[ -z "$SPLIT_SHA" ]]; then
        ny_warn "Hugging Face didn't return a sha256 for this file; the download won't be verified."
    fi
    return 0
}

# split_local_model_info -- a model already on a node's disk: its size and
# node come from the nodes themselves, and that node becomes the main node
# (no download, so no Hugging Face lookup and no checksum).
split_local_model_info() {
    local -a nodes=()
    local scan found node
    mapfile -t nodes < <(split_ai_nodes)
    scan="$(split_on_nodes ro "$SPLIT_SCAN_SCRIPT" "${nodes[@]}")"
    found="$(awk -v f="$SPLIT_FILE" '/^== / {n = $2} $1 == "MODEL" && $3 == f && $2 > best {best = $2; at = n} END {if (at != "") print at, best}' <<<"$scan")"
    [[ -n "$found" ]] || ny_die "${SPLIT_FILE} isn't downloaded on any node." "See what is there: nodeyard ai split models" "$NY_E_PRECONDITION"
    node="${found% *}"
    SPLIT_SIZE="${found#* }"
    SPLIT_SHA=""
    if [[ -n "$SPLIT_MAIN" && "$SPLIT_MAIN" != "$node" ]]; then
        ny_die "${SPLIT_FILE} is on ${node}, so ${node} has to be the main node (not ${SPLIT_MAIN})."
    fi
    SPLIT_MAIN="$node"
    ny_ok "${SPLIT_FILE} is already on ${node} ($(split_gib "$SPLIT_SIZE") GiB): no download needed."
    return 0
}

# split_node_table -> "name|capMiB|usedMiB|cpus|controlPlane|arch|ready|diskFreeBytes|gpuVramMiB" per node
split_node_table() {
    local tops name cap cpu arch ready cp pressure used mine podnodes podtops avail_b summary disk dcap dlim room limits gpus gpu
    tops="$(kctl top nodes --no-headers 2>/dev/null || true)"
    gpus="$(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels.nodeyard\.io/gpu-vram-mib}{"\n"}{end}' 2>/dev/null || true)"
    limits="$(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.nodeyard/disk-limit-gib}{"\n"}{end}' 2>/dev/null || true)"
    # Memory held by an existing split deployment is freed when it is
    # replaced, so it doesn't count as "in use" (MiB per node).
    podnodes="$(kctl -n "$SPLIT_NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\n"}{end}' 2>/dev/null || true)"
    podtops="$(kctl -n "$SPLIT_NS" top pods --no-headers 2>/dev/null || true)"
    # (the possibly-empty control-plane label must stay last: read merges empty tab fields)
    while IFS=$'\t' read -r name cap cpu arch ready pressure cp; do
        [[ -n "$name" ]] || continue
        cap="${cap%Ki}"
        [[ "$cap" =~ ^[0-9]+$ ]] || cap=0
        # a node under disk/memory pressure rejects new pods: report it as not ready
        if [[ "$pressure" == *True* ]]; then ready="Pressure"; fi
        used="$(awk -v n="$name" '$1 == n {print $4}' <<<"$tops")"
        case "$used" in
            *Mi) used="${used%Mi}" ;;
            *Gi) used=$((${used%Gi} * 1024)) ;;
            *Ki) used=$((${used%Ki} / 1024)) ;;
            *) used="" ;;
        esac
        # The node's own kubelet: free space on its root partition (where
        # /var/lib/nodeyard lives) and, when the metrics service has no
        # reading yet, how much memory is really free.
        summary="$(kctl get --raw "/api/v1/nodes/${name}/proxy/stats/summary" 2>/dev/null | jq -r '"\(.node.memory.availableBytes // "") \(.node.fs.availableBytes // "") \(.node.fs.capacityBytes // "")"' 2>/dev/null || true)"
        read -r avail_b disk dcap <<<"$summary"
        [[ "$disk" =~ ^[0-9]+$ ]] || disk=""
        # `ai disk limit`: only the room left under the node's limit counts
        dlim="$(awk -v n="$name" '$1 == n {print $2}' <<<"$limits")"
        if [[ -n "$disk" && "$dcap" =~ ^[0-9]+$ && "$dlim" =~ ^[0-9]+$ ]]; then
            room=$((dlim * 1073741824 - (dcap - disk)))
            ((room > 0)) || room=0
            ((room < disk)) && disk=$room
        fi
        if [[ -z "$used" ]]; then
            if [[ "$avail_b" =~ ^[0-9]+$ ]]; then
                used=$((cap / 1024 - avail_b / 1048576))
                ((used >= 0)) || used=0
            fi
        fi
        mine="$(awk -v n="$name" 'NR == FNR { if ($2 == n) on[$1] = 1; next }
            ($1 in on) { v = $3; m = 0
                if (v ~ /Gi$/) m = v * 1024; else if (v ~ /Mi$/) m = v + 0; else if (v ~ /Ki$/) m = v / 1024
                s += m }
            END { printf "%d", s }' <(printf '%s\n' "$podnodes") <(printf '%s\n' "$podtops"))"
        if [[ -n "$used" && "$mine" =~ ^[0-9]+$ ]] && ((mine > 0 && mine < used)); then
            used=$((used - mine))
        fi
        [[ "$cp" == "true" ]] || cp="false"
        # '|' not tab: read collapses empty tab-separated fields, which shifted
        # every column left when "used" was empty and made nodes look not Ready.
        gpu="$(awk -v n="$name" '$1 == n {print $2}' <<<"$gpus")"
        printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$name" "$((cap / 1024))" "$used" "$cpu" "$cp" "$arch" "$ready" "$disk" "${gpu:-0}"
    done < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.capacity.memory}{"\t"}{.status.capacity.cpu}{"\t"}{.status.nodeInfo.architecture}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.conditions[?(@.type=="DiskPressure")].status}{.status.conditions[?(@.type=="MemoryPressure")].status}{"\t"}{.metadata.labels.node-role\.kubernetes\.io/control-plane}{"\n"}{end}' 2>/dev/null)
    return 0
}

# split_plan -- fills PLAN_* arrays (main node first) from SPLIT_SIZE,
# SPLIT_NODES (optional filter), SPLIT_MAIN (optional) and SPLIT_RESERVE
# (per-node overrides). Dies, saying by how much, if the model won't fit.
SPLIT_RES_WORKER=1024 # MiB kept free on every node for the OS and k3s
SPLIT_RES_CP=1536     # control-plane nodes also run the API server & co.
SPLIT_RES_MAIN=1024   # extra on the main node: KV cache + prompt cache
SPLIT_WORK=256        # compute buffers each RPC server allocates
split_plan() {
    local size_mib=$((SPLIT_SIZE / 1048576))
    local -a names=() avails=() cpus=() cps=() reserves=() frees=() disks=() bws=() vrams=()
    local name cap used cpu cp arch ready disk vram reserve avail ov

    while IFS='|' read -r name cap used cpu cp arch ready disk vram; do
        [[ -n "$name" ]] || continue
        if [[ "${#SPLIT_NODES[@]}" -gt 0 ]] && ! ny_in_list "$name" "${SPLIT_NODES[@]}"; then
            continue
        fi
        if [[ "$ready" == "Pressure" ]]; then
            ny_warn "Skipping ${name}: it is under disk or memory pressure, so it rejects new pods ('nodeyard doctor' explains)."
            continue
        fi
        if [[ "$ready" != "True" ]]; then
            ny_warn "Skipping ${name}: not Ready."
            continue
        fi
        case "$arch" in amd64 | arm64) ;; *)
            ny_warn "Skipping ${name}: architecture ${arch} isn't supported."
            continue
            ;;
        esac
        if [[ "$disk" =~ ^[0-9]+$ ]] && ((disk < 1073741824)); then
            ny_warn "Skipping ${name}: under 1 GiB of disk left (or at its 'ai disk limit')."
            continue
        fi
        if [[ -z "$used" ]]; then
            used=$((cap * 30 / 100))
            ny_warn "No live memory stats for ${name} (is metrics-server working? try 'nodeyard nettest'); assuming ${used} MiB in use."
        fi
        reserve=$SPLIT_RES_WORKER
        if [[ "$cp" == "true" ]]; then reserve=$SPLIT_RES_CP; fi
        ov="${SPLIT_RESERVE[$name]:-}"
        if [[ -n "$ov" ]]; then reserve="$ov"; fi
        avail=$((cap - used - reserve - SPLIT_WORK))
        names+=("$name")
        avails+=("$avail")
        cpus+=("${cpu%%m*}")
        cps+=("$cp")
        reserves+=("$reserve")
        frees+=("$((cap - used))")
        disks+=("${disk:-0}")
        bws+=("$(split_node_speed "${cpu%%m*}" "$arch")")
        vrams+=("${vram:-0}")
    done <<<"${SPLIT_NODE_CACHE:-$(split_node_table)}"

    [[ "${#names[@]}" -gt 0 ]] || ny_die "No usable nodes."

    # Main node: as given, else the node with the most free space on its root
    # partition (it stores the model file), as long as it has the memory to
    # coordinate. Control-plane nodes only if nothing else qualifies.
    local i best=-1
    if [[ -n "${SPLIT_MAIN:-}" ]]; then
        for i in "${!names[@]}"; do
            if [[ "${names[i]}" == "$SPLIT_MAIN" ]]; then best=$i; fi
        done
        [[ $best -ge 0 ]] || ny_die "--main ${SPLIT_MAIN} isn't one of the usable nodes."
    else
        local pass
        for pass in noncp any; do
            for i in "${!names[@]}"; do
                [[ "$pass" == noncp && "${cps[i]}" == "true" ]] && continue
                ((frees[i] >= SPLIT_RES_MAIN + 256)) || continue
                if [[ $best -lt 0 ]] || ((disks[i] > disks[best])) || ((disks[i] == disks[best] && avails[i] > avails[best])); then best=$i; fi
            done
            [[ $best -ge 0 ]] && break
        done
        [[ $best -ge 0 ]] || best=0
        SPLIT_MAIN="${names[best]}"
    fi
    reserves[best]=$((reserves[best] + SPLIT_RES_MAIN))
    avails[best]=$((avails[best] - SPLIT_RES_MAIN))
    if ((frees[best] < SPLIT_RES_MAIN + 256)); then
        ny_die "The main node ${names[best]} doesn't have enough free memory even to coordinate (${frees[best]} MiB free)."
    fi
    # A main node short on memory (but with a good disk/network) can just
    # coordinate: it reads the model and sends every layer to the others.
    # (Under 1 GiB a slice isn't worth the extra network hop per token.)
    if [[ "$SPLIT_MAIN_ONLY" -ne 1 ]] && ((avails[best] < 1024)); then
        ny_warn "${names[best]} is low on memory, so it will only coordinate and hold no part of the model."
        SPLIT_MAIN_ONLY=1
    fi
    if [[ "$SPLIT_MAIN_ONLY" -eq 1 ]]; then avails[best]=0; fi
    for i in "${!names[@]}"; do
        if ((avails[i] < 512)); then
            if [[ $i -eq $best && "$SPLIT_MAIN_ONLY" -ne 1 ]]; then ny_die "The main node ${names[i]} doesn't have enough free memory (${frees[i]} MiB free)."; fi
            avails[i]=0
        fi
    done

    local need=$((size_mib * 103 / 100)) forced=0 result
    # --nodes a,b,c means exactly those; otherwise any subset may be picked.
    if [[ "${#SPLIT_NODES[@]}" -gt 0 || "${SPLIT_ALL_NODES:-0}" -eq 1 ]]; then forced=1; fi
    # The main node's NVIDIA GPU (set up with `nodeyard ai gpu enable`) is one more
    # place for layers: much faster than any CPU, and no network hop (it is local).
    local gi=${#names[@]} gpu_avail=0
    PLAN_GPU_SHARE=0
    if [[ "${SPLIT_NO_GPU:-0}" -ne 1 && "${vrams[best]:-0}" =~ ^[0-9]+$ ]] && ((vrams[best] > SPLIT_GPU_RESERVE + 512)); then
        gpu_avail=$((vrams[best] - SPLIT_GPU_RESERVE))
    fi
    result="$(
        {
            for i in "${!names[@]}"; do printf '%s %s %s 0\n' "$i" "${avails[i]}" "${bws[i]}"; done
            if ((gpu_avail > 0)); then printf '%s %s %s 1\n' "$gi" "$gpu_avail" "$SPLIT_GPU_BW"; fi
        } | split_best_subset "$need" "$best" "$(split_active_fraction)" "$size_mib" "$forced"
    )"

    if [[ "$result" == none* ]]; then
        [[ "${SPLIT_PROBE:-0}" -eq 1 ]] && return 1
        local total="${result#none }"
        ny_die "This model needs ~$(awk -v m="$need" 'BEGIN{printf "%.1f", m/1024}') GiB but only ~$(awk -v m="$total" 'BEGIN{printf "%.1f", m/1024}') GiB is free across the nodes after reserves. Pick a smaller quant, add nodes, or lower a node's reserve (--reserve NODE=GiB)."
    fi

    # result: "TOKS|WHY|idx:shareMiB idx:shareMiB ..."
    PLAN_TOKS="${result%%|*}"
    result="${result#*|}"
    PLAN_WHY="${result%%|*}"
    result="${result#*|}"
    PLAN_NAMES=()
    PLAN_SHARE=()
    PLAN_CPU=()
    PLAN_CP=()
    PLAN_FREE=()
    PLAN_RESERVE=()
    PLAN_LEFT_OUT=()
    PLAN_CACHE_OFF=()
    local -A picked=()
    local pair
    for pair in $result; do picked[${pair%%:*}]="${pair#*:}"; done
    if [[ -n "${picked[$gi]:-}" ]]; then
        PLAN_GPU_SHARE="${picked[$gi]}"
        unset "picked[$gi]"
    fi
    picked[$best]="${picked[$best]:-0}"
    local order=("$best")
    for i in "${!names[@]}"; do
        [[ $i -eq $best ]] && continue
        if [[ -n "${picked[$i]:-}" ]]; then order+=("$i"); else PLAN_LEFT_OUT+=("${names[i]}"); fi
    done
    for i in "${order[@]}"; do
        PLAN_NAMES+=("${names[i]}")
        PLAN_SHARE+=("${picked[$i]}")
        PLAN_CPU+=("${cpus[i]}")
        PLAN_CP+=("${cps[i]}")
        PLAN_FREE+=("${frees[i]}")
        PLAN_RESERVE+=("${reserves[i]}")
    done
    PLAN_MAIN_DISK="${disks[best]}"
    # The weight cache keeps a copy of each node's share on its disk: off on
    # nodes without room for it (so `ai disk limit` holds).
    local k=0
    for i in "${order[@]}"; do
        if [[ "${disks[i]}" -gt 0 ]] && ((PLAN_SHARE[k] > 0 && disks[i] / 1048576 < PLAN_SHARE[k] + 1024)) &&
            ! ny_in_list "${names[i]}" "${SPLIT_NO_CACHE[@]+"${SPLIT_NO_CACHE[@]}"}"; then
            SPLIT_NO_CACHE+=("${names[i]}")
            ny_warn "No weight cache on ${names[i]}: not enough disk room for it (loading takes longer there)."
        fi
        k=$((k + 1))
    done
    # It only speeds loading up where the node's disk reads its share faster
    # than the network brings it (gigabit: ~110 MB/s): a hard drive or SD card
    # is slower. Never on the main node: its share comes over loopback anyway,
    # and the cache would fight the main server for the disk the model is on.
    local speeds rd
    speeds="${SPLIT_DISK_SPEEDS-$(split_disk_speeds)}"
    PLAN_CACHE_OFF=()
    k=0
    for i in "${order[@]}"; do
        if ((PLAN_SHARE[k] > 0)) && ! ny_in_list "${names[i]}" "${SPLIT_NO_CACHE[@]+"${SPLIT_NO_CACHE[@]}"}"; then
            rd="$(awk -v n="${names[i]}" '$1 == n {printf "%d", $2}' <<<"$speeds")"
            if ((k == 0)); then
                SPLIT_NO_CACHE+=("${names[i]}")
                PLAN_CACHE_OFF+=("${names[i]} (main node)")
            elif [[ "$rd" =~ ^[0-9]+$ ]] && ((rd < SPLIT_CACHE_MIN_MBS)); then
                SPLIT_NO_CACHE+=("${names[i]}")
                PLAN_CACHE_OFF+=("${names[i]} (disk ${rd} MB/s)")
            fi
        fi
        k=$((k + 1))
    done
    return 0
}

# split_disk_speeds -> "NODE MB/s" per node: disk read speed measured by
# `nodeyard hw bench` (nothing for nodes it hasn't measured)
SPLIT_CACHE_MIN_MBS=150 # a weight cache only pays off on a disk faster than gigabit ethernet
split_disk_speeds() {
    kctl -n nodeyard-system get configmap nodeyard-bench -o json 2>/dev/null |
        jq -r '.data // {} | to_entries[] | "\(.key) \((.value | fromjson? | .disk.read_mbs) // "")"' 2>/dev/null || true
    return 0
}

# split_node_speed CPUS ARCH -- rough effective memory bandwidth (GB/s) the
# node's CPU reaches when generating tokens. Decode is memory-bound, and only
# physical cores help: laptop x86 chips have 2 threads per core, Arm boards 1.
# Calibrated on the real cluster: Qwen3-Coder-30B-A3B Q4_K_M over an i7-7700HQ,
# an i3-7020U and an i5-7200U measured 3.3 tok/s, and this model says 3.0.
split_node_speed() {
    local ncpu="${1:-1}" arch="$2"
    awk -v c="$ncpu" -v a="$arch" 'BEGIN {
        if (a == "amd64") { p = int(c / 2); per = 3.0 } else { p = c; per = 1.2 }
        if (p < 1) p = 1
        bw = p * per; if (bw > 20) bw = 20
        printf "%.2f", bw }'
    return 0
}

# split_active_fraction -- share of the weights read for each token: 1 for a
# dense model; for a mixture-of-experts named like "30B-A3B" only the active
# experts are read (3/30), which is why those models are so much faster.
split_active_fraction() {
    local f="${SPLIT_FILE:-} ${SPLIT_REPO:-}"
    if [[ "$f" =~ ([0-9]+(\.[0-9]+)?)[Bb]-[Aa]([0-9]+(\.[0-9]+)?)[Bb] ]]; then
        awk -v t="${BASH_REMATCH[1]}" -v a="${BASH_REMATCH[3]}" 'BEGIN { r = (a + 0.5) / t; if (r > 1) r = 1; printf "%.3f", r }'
    elif [[ "$f" =~ [0-9]+x[0-9]+(\.[0-9]+)?[Bb] ]]; then
        printf '0.300\n' # Mixtral-style 8x7B: 2 of 8 experts plus shared layers
    else
        printf '1\n'
    fi
    return 0
}

# split_best_subset NEED_MiB MAIN_IDX ACTIVE_FRACTION SIZE_MiB FORCED
#   stdin: "idx availMiB bandwidthGBs" per usable node
#   stdout: "TOKS|WHY|idx:shareMiB ..." for the fastest plan that fits, or "none TOTAL_MiB"
# Every subset that includes the main node is tried (FORCED=1: only the full
# set). Layers go to each node in proportion to its speed, capped at its free
# memory. Time per token = the bytes each node reads / its bandwidth, plus a
# network round trip for every node besides the main one.
SPLIT_HOP_MS="${NODEYARD_SPLIT_HOP_MS:-40}"
SPLIT_GPU_BW="${NODEYARD_SPLIT_GPU_BW:-80}" # GB/s a small NVIDIA card reaches (GTX 1050 Ti: 112 on paper)
SPLIT_GPU_RESERVE=1024                      # MiB of video memory kept for the context and work buffers
split_best_subset() {
    awk -v need="$1" -v main="$2" -v act="$3" -v size="$4" -v forced="$5" -v hop="$SPLIT_HOP_MS" '
    BEGIN { n = 0; mi = -1 }
    { id[n] = $1; av[n] = $2; bw[n] = $3; loc[n] = ($4 == 1); if ($1 == main) mi = n; n++ }
    END {
        total = 0; for (i = 0; i < n; i++) total += av[i]
        bt = -1; full = 2 ^ n - 1
        for (mask = 1; mask <= full; mask++) {
            if (int(mask / 2 ^ mi) % 2 == 0) continue
            if (forced && mask != full) continue
            # water-fill: shares in proportion to speed, capped at free memory
            m = 0; R = need
            for (i = 0; i < n; i++) { sh[i] = 0; cap_[i] = 0; on[i] = (int(mask / 2 ^ i) % 2 == 1 && av[i] > 0); if (on[i]) m++ }
            if (m == 0) continue
            ok = 0
            for (iter = 0; iter <= n; iter++) {
                S = 0; for (i = 0; i < n; i++) if (on[i] && !cap_[i]) S += bw[i]
                if (S <= 0) break
                capped = 0
                for (i = 0; i < n; i++) if (on[i] && !cap_[i] && R * bw[i] / S > av[i]) { sh[i] = av[i]; cap_[i] = 1; R -= av[i]; capped = 1 }
                if (!capped) { for (i = 0; i < n; i++) if (on[i] && !cap_[i]) sh[i] = R * bw[i] / S; R = 0; ok = 1; break }
            }
            if (!ok && R <= 0.5) ok = 1
            if (!ok) continue
            t = 0; hops = 0; used = 0
            for (i = 0; i < n; i++) if (sh[i] > 0) {
                if (!loc[i]) used++
                t += (sh[i] / need) * act * (size / 1024) * 1.073741824 / bw[i]
                if (i != mi && !loc[i]) hops++
            }
            t += hops * hop / 1000 + 0.005
            if (bt < 0 || t < bt - 0.000001 || (t < bt + 0.000001 && used < bu)) {
                bt = t; bu = used; bh = hops; out = ""; gpu_used = 0
                for (i = 0; i < n; i++) if (loc[i] && sh[i] > 0) gpu_used = 1
                for (i = 0; i < n; i++) if (sh[i] > 0 || i == mi) {
                    s = int(sh[i] * size / need); if (sh[i] > 0 && s < 1) s = 1
                    out = out (out == "" ? "" : " ") id[i] ":" s
                }
            }
        }
        if (bt < 0) { printf "none %d\n", total; exit }
        why = sprintf("%d node%s%s, %.1f GB read per token%s", bu, bu == 1 ? "" : "s", gpu_used ? " + GPU" : "", act * size / 1024 * 1.073741824, bh ? sprintf(", %d network hop%s", bh, bh == 1 ? "" : "s") : ", no network hops")
        printf "%.1f|%s|%s\n", 1 / bt, why, out
    }'
    return 0
}

# split_threads INDEX -- threads for a plan node: physical-ish cores (big SMT
# CPUs gain nothing from hyperthreads on memory-bound decode, and a laptop stays
# responsive), one fewer on control-plane nodes, or the --threads override.
split_threads() {
    local i="$1" t="${PLAN_CPU[$1]}" ov
    ov="${SPLIT_THREADS[${PLAN_NAMES[i]}]:-}"
    if [[ -n "$ov" ]]; then
        printf '%s\n' "$ov"
        return 0
    fi
    if ((t >= 8)); then t=$((t / 2)); fi
    if [[ "${PLAN_CP[i]}" == "true" ]]; then t=$((t > 1 ? t - 1 : 1)); fi
    printf '%s\n' "$t"
    return 0
}

split_print_plan() {
    local size_gib i threads left extra
    size_gib=$(awk -v b="$SPLIT_SIZE" 'BEGIN{printf "%.1f", b/1073741824}')
    if [[ "${SPLIT_LOCAL:-0}" -eq 1 ]]; then
        echo "Model: ${SPLIT_FILE}  (${size_gib} GiB, already on ${PLAN_NAMES[0]})"
    else
        echo "Model: ${SPLIT_REPO} : ${SPLIT_FILE}  (${size_gib} GiB)"
    fi
    echo
    printf '  %-18s %-13s %9s %12s %10s %8s\n' "NODE" "ROLE" "FREE NOW" "MODEL SHARE" "LEFT FREE" "THREADS"
    for i in "${!PLAN_NAMES[@]}"; do
        threads="$(split_threads "$i")"
        extra=$SPLIT_WORK
        if [[ "${PLAN_SHARE[i]}" -eq 0 ]]; then extra=0; fi
        if [[ $i -eq 0 ]]; then extra=$((extra + SPLIT_RES_MAIN)); fi
        left=$((PLAN_FREE[i] - PLAN_SHARE[i] - extra))
        printf '  %-18s %-13s %8.1fG %7.1fG %2d%% %9.1fG %8s\n' "${PLAN_NAMES[i]}" \
            "$(if [[ $i -eq 0 && "${PLAN_SHARE[i]}" -eq 0 ]]; then echo 'main only'; elif [[ $i -eq 0 ]]; then echo 'main + share'; else echo 'share'; fi)" \
            "$(awk -v m="${PLAN_FREE[i]}" 'BEGIN{print m/1024}')" \
            "$(awk -v m="${PLAN_SHARE[i]}" 'BEGIN{print m/1024}')" \
            "$((PLAN_SHARE[i] * 100 * 1048576 / SPLIT_SIZE))" \
            "$(awk -v m="$left" 'BEGIN{print m/1024}')" "$threads"
    done
    echo
    if [[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]]; then
        echo "Model file on ${PLAN_NAMES[0]}: ${SPLIT_MODEL_DIR}/${SPLIT_FILE}"
    fi
    if [[ "${#PLAN_LEFT_OUT[@]}" -gt 0 ]]; then
        echo "Left out (faster without them): $(ny_join ", " "${PLAN_LEFT_OUT[@]}")"
    fi
    if [[ "${#PLAN_CACHE_OFF[@]}" -gt 0 ]]; then
        echo "No weight cache on: $(ny_join ", " "${PLAN_CACHE_OFF[@]}"): slower than sending it over the network."
    fi
    if [[ "${PLAN_GPU_SHARE:-0}" -gt 0 ]]; then
        echo "GPU: $(awk -v m="$PLAN_GPU_SHARE" 'BEGIN{printf "%.1f", m/1024}') GiB of the model on ${PLAN_NAMES[0]}'s NVIDIA GPU (the fastest part)."
    fi
    if [[ -n "${PLAN_TOKS:-}" ]]; then
        echo "Estimated speed: about ${PLAN_TOKS} tokens/s (${PLAN_WHY})"
    fi
    if [[ "${#PLAN_NAMES[@]}" -gt 1 ]]; then
        echo "All ${#PLAN_NAMES[@]} nodes take part in every token, so keep them all switched on while the model runs."
    fi
    echo "To keep extra memory free on a node you also use day to day: --reserve NODE=GiB"
    return 0
}

# split_job_name FILE -- the download Job for one model file. One Job per file,
# so several downloads can run at the same time and none blocks a deploy.
split_job_name() {
    local base sum
    base="$(ny_k8s_name "${1%.gguf}")"
    base="${base:0:34}"
    base="${base%-}"
    sum="$(printf '%s' "$1" | cksum | cut -d' ' -f1)"
    printf 'dl-%s-%s\n' "$base" "$sum"
    return 0
}

# split_cache_key FILE -- the weight-cache folder for one model. Each RPC
# server only keeps its current model's folder, so caches of models you've
# moved on from don't pile up on every node.
split_cache_key() {
    local k
    k="$(ny_k8s_name "${1%.gguf}")"
    printf '%s\n' "${k:-model}"
    return 0
}

# split_download_yaml NODE -- the resumable, sha256-checked download Job for
# SPLIT_FILE into SPLIT_MODEL_DIR on NODE. Prints "progress BYTES SIZE" every
# 10 s for `ai split status` and the dashboard.
split_download_yaml() {
    local node="$1"
    cat <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $(split_job_name "$SPLIT_FILE")
  namespace: ${SPLIT_NS}
  labels: {app.kubernetes.io/managed-by: nodeyard, app.kubernetes.io/component: model-download}
  annotations: {nodeyard/file: "${SPLIT_FILE}", nodeyard/repo: "${SPLIT_REPO}", nodeyard/size: "${SPLIT_SIZE}", nodeyard/node: "${node}", nodeyard/model-dir: "${SPLIT_MODEL_DIR}"}
spec:
  backoffLimit: 30
  ttlSecondsAfterFinished: 604800
  template:
    metadata:
      labels: {app.kubernetes.io/component: model-download}
    spec:
      restartPolicy: OnFailure
      nodeSelector: {kubernetes.io/hostname: ${node}}
      tolerations: [{operator: Exists}]
      securityContext: {runAsUser: 0}
      containers:
      - name: dl
        image: curlimages/curl:8.15.0
        env:
        - {name: F, value: "${SPLIT_FILE}"}
        - {name: URL, value: "${SPLIT_URL}"}
        - {name: SIZE, value: "${SPLIT_SIZE}"}
        - {name: SHA, value: "${SPLIT_SHA}"}
        - {name: HF_TOKEN, valueFrom: {secretKeyRef: {name: hf-token, key: token, optional: true}}}
        command:
        - sh
        - -c
        - |
          set -e
          cd /models
          if [ -f "\$F" ]; then echo "already downloaded"; exit 0; fi
          N=4; CH=\$(( (SIZE + N - 1) / N )); i=0; pids=""
          # Disk check: the parts are joined in place, so the peak is the model
          # plus one part. The kubelet starts evicting pods (DiskPressure) when
          # less than 10% of the disk is free, so stay above that. Failing here
          # retries later, so freeing space lets it carry on by itself.
          have=0; for p in "\$F".part* "\$F.joining"; do [ -f "\$p" ] && have=\$(( have + \$(stat -c %s "\$p") )); done
          set -- \$(df -Pk /models | tail -1); cap=\$(( \$2 * 1024 )); free=\$(( \$4 * 1024 ))
          need=\$(( SIZE - have + CH + cap / 10 ))
          if [ "\$free" -lt "\$need" ]; then
            echo "NOT ENOUGH DISK: \$(( free / 1073741824 )) GiB free, need \$(( need / 1073741824 )) GiB (model + one part + the 10% the kubelet keeps free). Run 'nodeyard ai split clean' or pick another --main node."
            exit 1
          fi
          ( while :; do sleep 10; n=0; for p in "\$F".part* "\$F.joining"; do [ -f "\$p" ] && n=\$(( n + \$(stat -c %s "\$p") )); done; echo "progress \$n \$SIZE \$(date +%s)"; done ) &
          PROG=\$!
          trap 'kill \$PROG 2>/dev/null || true' EXIT
          # Once joining has started the parts are being consumed: never refetch.
          [ -f "\$F.joining" ] && i=\$N
          while [ \$i -lt \$N ]; do
            (
              start=\$(( i * CH )); end=\$(( start + CH - 1 )); [ \$end -ge \$SIZE ] && end=\$(( SIZE - 1 ))
              want=\$(( end - start + 1 ))
              while :; do
                have=\$(stat -c %s "\$F.part\$i" 2>/dev/null || echo 0)
                [ "\$have" -ge "\$want" ] && break
                if [ -n "\$HF_TOKEN" ]; then set -- -H "Authorization: Bearer \$HF_TOKEN"; else set --; fi
                curl -fsL --connect-timeout 20 "\$@" -r \$(( start + have ))-\$end "\$URL" >> "\$F.part\$i" || sleep 5
              done
            ) &
            pids="\$pids \$!"
            i=\$(( i + 1 ))
          done
          # only the downloads: a bare wait would also wait on the progress loop
          for p in \$pids; do wait "\$p"; done
          kill \$PROG 2>/dev/null || true
          # Join IN PLACE: part0 becomes the file and each later part is
          # appended then deleted. Restart-safe: a half-done append is cut
          # back to where it started and redone.
          [ -f "\$F.joining" ] || mv "\$F.part0" "\$F.joining"
          i=1
          while [ \$i -lt \$N ]; do
            if [ -f "\$F.part\$i" ]; then
              want=\$(( i * CH ))
              if [ "\$(stat -c %s "\$F.joining")" -gt "\$want" ]; then
                dd if=/dev/null of="\$F.joining" bs=1 seek="\$want" count=0 2>/dev/null
              fi
              echo "\$(date +%T) joining part \$(( i + 1 )) of \$N"
              cat "\$F.part\$i" >> "\$F.joining"; rm -f "\$F.part\$i"
            fi
            i=\$(( i + 1 ))
          done
          if [ -n "\$SHA" ]; then
            echo "\$(date +%T) verifying sha256"
            if ! echo "\$SHA  \$F.joining" | sha256sum -c -; then
              echo "checksum mismatch: deleting the file so the next retry downloads it again"
              rm -f "\$F.joining"; exit 1
            fi
          fi
          mv "\$F.joining" "\$F"
          echo "\$(date +%T) downloaded: \$(ls -l "\$F")"
        volumeMounts: [{name: models, mountPath: /models}]
      volumes:
      - {name: models, hostPath: {path: ${SPLIT_MODEL_DIR}, type: DirectoryOrCreate}}
YAML
    return 0
}

# split_manifest FILE -- writes the full set of Kubernetes objects
split_manifest() {
    local out="$1" i n share mem_req mem_lim threads cache_arg rpc_list="" ts_list="" cache_key
    local fetch_script main_fetch main_bin="${SPLIT_LLAMA_BUILD}" main_rt="" main_env="" main_image="ghcr.io/ggml-org/llama.cpp:server"
    cache_key="$(split_cache_key "$SPLIT_FILE")"
    fetch_script="$(
        cat <<FETCH
          set -e
          [ -x /opt/llama/ggml-rpc-server ] && [ -x /opt/llama/llama-server ] && exit 0
          case "\$(uname -m)" in x86_64) P=x64;; aarch64) P=arm64;; *) echo "unsupported arch"; exit 1;; esac
          curl -fsSL --retry 5 -o /tmp/l.tgz "https://github.com/ggml-org/llama.cpp/releases/download/${SPLIT_LLAMA_BUILD}/llama-${SPLIT_LLAMA_BUILD}-bin-ubuntu-\$P.tar.gz"
          tar -xzf /tmp/l.tgz -C /opt/llama --strip-components=1
FETCH
    )"

    main_fetch="$fetch_script"
    if [[ "${PLAN_GPU_SHARE:-0}" -gt 0 ]]; then
        # NVIDIA: llama.cpp's Vulkan build in a folder of its own, NVIDIA's runtime
        # (graphics = its Vulkan driver) and an image with the Vulkan loader.
        # (llama.cpp puts RPC servers first and local GPUs last, so in -ts the GPU's share comes last)
        main_bin="${SPLIT_LLAMA_BUILD}-vulkan"
        main_image="$SPLIT_GPU_IMAGE"
        main_rt=$'\n      runtimeClassName: nvidia'
        main_env=', {name: NVIDIA_VISIBLE_DEVICES, value: all}, {name: NVIDIA_DRIVER_CAPABILITIES, value: "compute,utility,graphics"}'
        main_fetch="$(
            cat <<FETCH
          set -e
          [ -x /opt/llama/llama-server ] && [ -e /opt/llama/libggml-vulkan.so ] && exit 0
          curl -fsSL --retry 5 -o /tmp/l.tgz "https://github.com/ggml-org/llama.cpp/releases/download/${SPLIT_LLAMA_BUILD}/llama-${SPLIT_LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"
          tar -xzf /tmp/l.tgz -C /opt/llama --strip-components=1
          ls /opt/llama
FETCH
        )"
    fi

    {
        cat <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${SPLIT_NS}
  labels: {app.kubernetes.io/managed-by: nodeyard}
  annotations: {nodeyard/main: "${PLAN_NAMES[0]}", nodeyard/model-dir: "${SPLIT_MODEL_DIR}"}
---
YAML
        if [[ "${SPLIT_LOCAL:-0}" -eq 1 ]]; then
            # already on the main node's disk: nothing to download
            cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata: {name: llama-model, namespace: ${SPLIT_NS}, labels: {app.kubernetes.io/managed-by: nodeyard}}
data: {file: "${SPLIT_FILE}", source: "already on ${PLAN_NAMES[0]}"}
YAML
        else
            split_download_yaml "${PLAN_NAMES[0]}"
        fi
        cat <<YAML
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: rpc-only-from-main, namespace: ${SPLIT_NS}}
spec:
  podSelector: {matchLabels: {role: rpc}}
  policyTypes: [Ingress]
  ingress:
  - from: [{podSelector: {matchLabels: {app: llama-main}}}]
    ports: [{port: 50052, protocol: TCP}]
YAML

        for i in "${!PLAN_NAMES[@]}"; do
            n="rpc-$(ny_k8s_name "${PLAN_NAMES[i]}")"
            share="${PLAN_SHARE[i]}"
            [[ "$share" -gt 0 ]] || continue # a main-only coordinator runs no RPC server
            cache_arg=', "-c"'
            if [[ "${#SPLIT_NO_CACHE[@]}" -gt 0 ]] && ny_in_list "${PLAN_NAMES[i]}" "${SPLIT_NO_CACHE[@]}"; then cache_arg=""; fi
            mem_req=$((share + 256))
            mem_lim=$((share + share * 15 / 100 + 512))
            threads="$(split_threads "$i")"
            rpc_list+="${rpc_list:+,}${n}.${SPLIT_NS}.svc.cluster.local:50052"
            ts_list+="${ts_list:+,}${share}"
            cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: ${n}, namespace: ${SPLIT_NS}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: ${n}}}
  template:
    metadata: {labels: {app: ${n}, role: rpc}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${PLAN_NAMES[i]}}
      tolerations: [{operator: Exists}]
      initContainers:
      - name: fetch-llama-cpp
        image: ghcr.io/ggml-org/llama.cpp:server
        command:
        - sh
        - -c
        - |
${fetch_script}
        volumeMounts: [{name: bin, mountPath: /opt/llama}]
      # Weight caches of models this node no longer serves are deleted, so
      # switching models doesn't leave gigabytes behind on every node.
      - name: prune-old-caches
        image: busybox:1.36
        command:
        - sh
        - -c
        - |
          cd /cache || exit 0
          for d in * .[!.]*; do
            [ -e "\$d" ] && [ "\$d" != "${cache_key}" ] && echo "removing old weight cache \$d" && rm -rf "\$d"
          done
          mkdir -p "${cache_key}"
        volumeMounts: [{name: cache, mountPath: /cache}]
      containers:
      - name: rpc
        image: ghcr.io/ggml-org/llama.cpp:server
        command: ["/opt/llama/ggml-rpc-server", "-H", "0.0.0.0", "-p", "50052", "-t", "${threads}"${cache_arg}]
        env: [{name: LD_LIBRARY_PATH, value: /opt/llama}, {name: HOME, value: "/cache/${cache_key}"}, {name: LLAMA_CACHE, value: "/cache/${cache_key}"}]
        ports: [{containerPort: 50052}]
        resources: {requests: {memory: ${mem_req}Mi}, limits: {memory: ${mem_lim}Mi}}
        volumeMounts: [{name: bin, mountPath: /opt/llama}, {name: cache, mountPath: /cache}]
      volumes:
      - {name: bin, hostPath: {path: /var/lib/nodeyard/llama.cpp/${SPLIT_LLAMA_BUILD}, type: DirectoryOrCreate}}
      - {name: cache, hostPath: {path: /var/lib/nodeyard/rpc-cache, type: DirectoryOrCreate}}
---
apiVersion: v1
kind: Service
metadata: {name: ${n}, namespace: ${SPLIT_NS}}
spec: {selector: {app: ${n}}, ports: [{port: 50052, targetPort: 50052}]}
YAML
        done

        local think_args="" key_args="" svc_type="NodePort" np_line="    nodePort: ${SPLIT_NODEPORT}"
        if [[ "$SPLIT_THINK" != "on" ]]; then think_args=$'\n        - --reasoning-budget\n        - "0"'; fi
        if [[ -n "$SPLIT_API_KEY" ]]; then key_args=$'\n        - --api-key-file\n        - /secrets/api-key'; fi
        if [[ "$SPLIT_NODEPORT" == "0" ]]; then
            svc_type="ClusterIP"
            np_line=""
        fi
        # The gate (ai gate install) owns the port on every node, so the Service stays cluster-only.
        if ! ny_simulating && kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" >/dev/null 2>&1; then
            svc_type="ClusterIP"
            np_line=""
        fi
        if [[ -n "$SPLIT_API_KEY" ]]; then
            cat <<YAML
---
apiVersion: v1
kind: Secret
metadata: {name: llama-api-key, namespace: ${SPLIT_NS}}
stringData: {api-key: "${SPLIT_API_KEY}"}
YAML
        fi
        cat <<YAML
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llama-main
  namespace: ${SPLIT_NS}
  annotations: {nodeyard/repo: "${SPLIT_REPO}", nodeyard/file: "${SPLIT_FILE}", nodeyard/size: "${SPLIT_SIZE}"}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: llama-main}}
  template:
    metadata: {labels: {app: llama-main}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${PLAN_NAMES[0]}}
      tolerations: [{operator: Exists}]${main_rt}
      initContainers:
      - name: fetch-llama-cpp
        image: ghcr.io/ggml-org/llama.cpp:server
        command:
        - sh
        - -c
        - |
${main_fetch}
        volumeMounts: [{name: bin, mountPath: /opt/llama}]
      - name: wait-for-model
        image: busybox:1.36
        command: ["sh", "-c", "until [ -f '/models/${SPLIT_FILE}' ]; do echo 'waiting for the model download...'; sleep 30; done"]
        volumeMounts: [{name: models, mountPath: /models}]
      containers:
      - name: server
        image: ${main_image}
        command: ["/opt/llama/llama-server"]
        args:
        - -m
        - "/models/${SPLIT_FILE}"
        # big direct reads: on a hard drive memory-mapped loading (small page
        # faults) is several times slower
        - --load-mode
        - dio
        - --alias
        - "${SPLIT_ALIAS}"
        - --rpc
        - "${rpc_list}"
        - -ngl
        - "999"
        - -sm
        - layer
        - -ts
        - "${ts_list}$([[ "${PLAN_GPU_SHARE:-0}" -gt 0 ]] && printf ',%s' "$PLAN_GPU_SHARE" || true)"
        - -c
        - "${SPLIT_CTX}"
        - -np
        - "1"
        - -t
        - "2"
        - -cram
        - "512"
        # lets the model call tools (OpenAI-style tool calls: yardcode and other agents need it)
        - --jinja${think_args}${key_args}
        - --host
        - 0.0.0.0
        - --port
        - "8080"
        env: [{name: LD_LIBRARY_PATH, value: /opt/llama}${main_env}]
        ports: [{containerPort: 8080}]
        readinessProbe:
          httpGet: {path: /health, port: 8080}
          periodSeconds: 10
        volumeMounts:
        - {name: bin, mountPath: /opt/llama}
        - {name: models, mountPath: /models}$([[ -n "$SPLIT_API_KEY" ]] && printf '\n        - {name: api-key, mountPath: /secrets, readOnly: true}' || true)
      volumes:
      - {name: bin, hostPath: {path: /var/lib/nodeyard/llama.cpp/${main_bin}, type: DirectoryOrCreate}}
      - {name: models, hostPath: {path: ${SPLIT_MODEL_DIR}, type: DirectoryOrCreate}}$([[ -n "$SPLIT_API_KEY" ]] && printf '\n      - {name: api-key, secret: {secretName: llama-api-key}}' || true)
---
apiVersion: v1
kind: Service
metadata: {name: llama, namespace: ${SPLIT_NS}}
spec:
  type: ${svc_type}
  selector: {app: llama-main}
  ports:
  - port: 8080
    targetPort: 8080
${np_line}
YAML
    } >"$out"
    return 0
}

split_parse_flags() {
    SPLIT_MODEL_SPEC="$SPLIT_DEFAULT_MODEL"
    SPLIT_MAIN=""
    SPLIT_NODES=()
    SPLIT_NODEPORT=31435
    SPLIT_CTX=16384
    SPLIT_THINK="off"
    SPLIT_API_KEY=""
    SPLIT_ALIAS=""
    SPLIT_MODEL_DIR="$SPLIT_DEFAULT_MODEL_DIR"
    SPLIT_MAIN_ONLY=0
    SPLIT_NO_GPU=0
    SPLIT_MIN_TOKS=10
    SPLIT_ALL_NODES=0
    SPLIT_NO_CACHE=()
    declare -gA SPLIT_RESERVE=()
    declare -gA SPLIT_THREADS=()
    local rn rg
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reserve)
                [[ $# -ge 2 && "$2" == *=* ]] || ny_usage_error "--reserve needs NODE=GiB (e.g. --reserve archlinux=4)"
                rn="${2%%=*}"
                rg="${2#*=}"
                [[ "$rg" =~ ^[0-9]+([.][0-9]+)?$ ]] || ny_usage_error "--reserve ${2}: the amount must be a number of GiB"
                SPLIT_RESERVE[$rn]="$(awk -v g="$rg" 'BEGIN{printf "%d", g*1024}')"
                shift 2
                ;;
            --threads)
                [[ $# -ge 2 && "$2" =~ ^[^=]+=[0-9]+$ ]] || ny_usage_error "--threads needs NODE=N (e.g. --threads archlinux=6)"
                SPLIT_THREADS[${2%%=*}]="${2#*=}"
                shift 2
                ;;
            --model)
                [[ $# -ge 2 ]] || ny_usage_error "--model needs owner/repo:file.gguf"
                SPLIT_MODEL_SPEC="$2"
                shift 2
                ;;
            --main)
                [[ $# -ge 2 ]] || ny_usage_error "--main needs a node name"
                SPLIT_MAIN="$2"
                shift 2
                ;;
            --nodes)
                [[ $# -ge 2 ]] || ny_usage_error "--nodes needs auto or a comma-separated list"
                SPLIT_ALL_NODES=0
                if [[ "$2" == "auto" ]]; then
                    SPLIT_NODES=()
                elif [[ "$2" == "all" ]]; then
                    SPLIT_NODES=()
                    SPLIT_ALL_NODES=1
                else IFS=',' read -r -a SPLIT_NODES <<<"$2"; fi
                shift 2
                ;;
            --min-speed)
                [[ $# -ge 2 && "$2" =~ ^[0-9]+([.][0-9]+)?$ ]] || ny_usage_error "--min-speed needs a number of tokens per second"
                SPLIT_MIN_TOKS="$2"
                shift 2
                ;;
            --nodeport)
                [[ $# -ge 2 ]] || ny_usage_error "--nodeport needs a port (0 = cluster-only)"
                SPLIT_NODEPORT="$2"
                shift 2
                ;;
            --ctx)
                [[ $# -ge 2 ]] || ny_usage_error "--ctx needs a number of tokens"
                SPLIT_CTX="$2"
                shift 2
                ;;
            --think)
                [[ $# -ge 2 ]] || ny_usage_error "--think needs on|off"
                SPLIT_THINK="$2"
                shift 2
                ;;
            --api-key)
                [[ $# -ge 2 ]] || ny_usage_error "--api-key needs a value"
                SPLIT_API_KEY="$2"
                ny_secret_register "$2"
                ny_warn "--api-key puts the key in your shell history; next time use --api-key-file."
                shift 2
                ;;
            --api-key-file)
                [[ $# -ge 2 && -r "$2" ]] || ny_usage_error "--api-key-file needs a readable file"
                SPLIT_API_KEY="$(ny_trim "$(<"$2")")"
                ny_secret_register "$SPLIT_API_KEY"
                shift 2
                ;;
            --alias)
                [[ $# -ge 2 ]] || ny_usage_error "--alias needs a name"
                SPLIT_ALIAS="$2"
                shift 2
                ;;
            --model-dir)
                [[ $# -ge 2 && "$2" =~ ^/[A-Za-z0-9._/-]+$ && "$2" != *..* ]] || ny_usage_error "--model-dir needs an absolute path on the main node (e.g. /srv/models)"
                SPLIT_MODEL_DIR="${2%/}"
                shift 2
                ;;
            --main-only)
                SPLIT_MAIN_ONLY=1
                shift
                ;;
            --no-gpu)
                SPLIT_NO_GPU=1
                shift
                ;;
            --no-cache)
                [[ $# -ge 2 ]] || ny_usage_error "--no-cache needs a comma-separated list of nodes"
                IFS=',' read -r -a SPLIT_NO_CACHE <<<"$2"
                shift 2
                ;;
            --yes | -y)
                NY_YES=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" ;;
        esac
    done
    [[ "$SPLIT_NODEPORT" == 0 ]] || ny_valid_int "$SPLIT_NODEPORT" 30000 32767 || ny_usage_error "--nodeport must be 0 or between 30000 and 32767."
    ny_valid_int "$SPLIT_CTX" 512 1048576 || ny_usage_error "--ctx: ${NY_VALID_MSG}"
    ny_valid_enum "$SPLIT_THINK" on off || ny_usage_error "--think: ${NY_VALID_MSG}"
    [[ -z "$SPLIT_ALIAS" || "$SPLIT_ALIAS" =~ ^[A-Za-z0-9._:-]+$ ]] || ny_usage_error "--alias may only contain letters, digits, '.', '_', ':' and '-'."
    SPLIT_MODEL_AUTO=0
    if [[ "$SPLIT_MODEL_SPEC" == "auto" ]]; then
        SPLIT_MODEL_AUTO=1
    else
        split_parse_model "$SPLIT_MODEL_SPEC"
    fi
    return 0
}

# split_pick_model -- sets the model (SPLIT_FILE/URL/SIZE/SHA) and the plan.
# With --model auto, tries each SPLIT_AUTO_QUANTS entry (best first) against
# the live free memory and keeps the first that fits.
split_pick_model() {
    if [[ "$SPLIT_MODEL_AUTO" -eq 1 ]]; then
        local q picked="" skipped="" toks fastest="" fastest_toks=0 why=""
        SPLIT_NODE_CACHE="$(split_node_table)"
        for q in "${SPLIT_AUTO_QUANTS[@]}"; do
            split_parse_model "${SPLIT_AUTO_REPO}:${SPLIT_AUTO_PREFIX}${q}.gguf"
            split_model_info
            toks="$( (SPLIT_PROBE=1 split_plan >/dev/null 2>&1 && printf '%s' "$PLAN_TOKS") || true)"
            if [[ -z "$toks" ]]; then
                skipped+="${skipped:+, }${q} (too big)"
                continue
            fi
            if awk -v t="$toks" -v m="$SPLIT_MIN_TOKS" 'BEGIN{exit !(t >= m)}'; then
                picked="$q"
                why="the biggest version that reaches ${SPLIT_MIN_TOKS} tokens/s (about ${toks})"
                break
            fi
            skipped+="${skipped:+, }${q} (~${toks} tok/s)"
            if awk -v t="$toks" -v f="$fastest_toks" 'BEGIN{exit !(t > f)}'; then
                fastest="$q"
                fastest_toks="$toks"
            fi
        done
        if [[ -z "$picked" && -n "$fastest" ]]; then
            picked="$fastest"
            why="nothing reaches ${SPLIT_MIN_TOKS} tokens/s here, so the fastest one that fits (about ${fastest_toks})"
            split_parse_model "${SPLIT_AUTO_REPO}:${SPLIT_AUTO_PREFIX}${picked}.gguf"
            split_model_info
        fi
        [[ -n "$picked" ]] || {
            split_plan
            ny_die "Not even the smallest version fits."
        }
        ny_ok "Picked ${SPLIT_FILE}: ${why}."
        [[ -z "$skipped" ]] || echo "   Skipped: ${skipped}"
        SPLIT_MODEL_SPEC="${SPLIT_REPO}:${SPLIT_FILE}"
    else
        split_model_info
    fi
    split_plan
    if [[ -z "$SPLIT_ALIAS" ]]; then SPLIT_ALIAS="${SPLIT_FILE%.gguf}"; fi
    return 0
}

ai_split_plan() {
    ny_need_kube
    split_parse_flags "$@"
    split_pick_model
    split_print_plan
    echo "Deploy it with: nodeyard ai split deploy --model ${SPLIT_MODEL_SPEC} --main ${SPLIT_MAIN}$([[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]] && printf ' --model-dir %q' "$SPLIT_MODEL_DIR" || true)"
    return 0
}

# split_duration SECONDS -> "45 s", "12 min", "1 h 20 min"
split_duration() {
    local t="${1:-0}"
    if ((t < 90)); then
        printf '%d s\n' "$t"
    elif ((t < 3600)); then
        printf '%d min\n' $(((t + 30) / 60))
    else
        printf '%d h %d min\n' $((t / 3600)) $(((t % 3600) / 60))
    fi
    return 0
}

split_gib() { awk -v b="$1" 'BEGIN{printf "%.1f", b/1073741824}'; }

# Early warning from the kubelet's own numbers for the main node's root
# partition (where /var/lib/nodeyard/models lives). The download needs the
# model plus one of its 4 parts at the peak (they're joined in place), and the
# kubelet starts evicting pods once less than 10% of the disk is free.
split_disk_check() {
    local node="${PLAN_NAMES[0]}" fs avail cap need floor gib
    [[ "${SPLIT_LOCAL:-0}" -eq 1 ]] && return 0 # (already on that disk)
    if [[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]]; then
        echo "(Disk space in ${SPLIT_MODEL_DIR} is checked by the download itself.)"
        return 0
    fi
    fs="$(kctl get --raw "/api/v1/nodes/${node}/proxy/stats/summary" 2>/dev/null | jq -r '"\(.node.fs.availableBytes // "") \(.node.fs.capacityBytes // "")"' 2>/dev/null || true)"
    read -r avail cap <<<"$fs"
    [[ "$avail" =~ ^[0-9]+$ && "$cap" =~ ^[0-9]+$ ]] || return 0
    # `ai disk limit` on the main node: only the room under the limit counts
    if [[ "${PLAN_MAIN_DISK:-}" =~ ^[0-9]+$ ]] && ((PLAN_MAIN_DISK < avail)); then avail=$PLAN_MAIN_DISK; fi
    need=$((SPLIT_SIZE + SPLIT_SIZE / 4))
    floor=$((cap / 10))
    if ((avail - need < floor)); then
        ny_warn "${node} has $(split_gib "$avail") GiB free on its root partition; the download needs $(split_gib "$need") GiB and the kubelet wants $(split_gib "$floor") GiB left over."
        ny_warn "Fine if this model is already downloaded there; otherwise run 'nodeyard ai split clean' or pick --main <node with more room>."
    else
        ny_ok "Disk on ${node}: $(split_gib "$avail") GiB free on its root partition; the download needs $(split_gib "$need") GiB."
    fi
    return 0
}

ai_split_deploy() {
    ny_need_kube
    split_parse_flags "$@"
    split_pick_model
    split_print_plan
    split_disk_check
    if kctl -n "$SPLIT_NS" get deployment llama-main >/dev/null 2>&1; then
        ny_warn "A split model is already deployed; this replaces it (its downloaded file stays; 'nodeyard ai split clean' frees old caches)."
    fi
    ny_confirm "Deploy this?" y || {
        echo "Cancelled."
        return 0
    }
    split_apply
}

# split_apply -- writes and applies the manifest for the planned model (after
# the plan, the disk check and the confirmation), replacing what runs now.
split_apply() {
    local manifest
    manifest="$(ny_mktemp)"
    split_manifest "$manifest"
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add apply "Apply the split-model deployment (main node ${PLAN_NAMES[0]})"
        ny_info "[dry-run] would apply:"
        ny_redact "$(cat "$manifest")" >&2
        printf '\n' >&2
        return 0
    fi
    local made_ns=0
    if kctl get namespace "$SPLIT_NS" >/dev/null 2>&1; then
        kctl -n "$SPLIT_NS" delete deployment,service,networkpolicy,secret --all --wait=true >/dev/null 2>&1 || true
        # Downloads of OTHER files carry on. This file's Job is replaced only
        # if it failed or was for another node (Jobs can't be changed); the
        # download resumes from the parts already on disk.
        local job jnode jfail
        job="$(split_job_name "$SPLIT_FILE")"
        jnode="$(kctl -n "$SPLIT_NS" get job "$job" -o jsonpath='{.metadata.annotations.nodeyard/node}' 2>/dev/null || true)"
        jfail="$(kctl -n "$SPLIT_NS" get job "$job" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)"
        if [[ -n "$jnode" && ("$jnode" != "${PLAN_NAMES[0]}" || "$jfail" == "True") ]]; then
            kctl -n "$SPLIT_NS" delete job "$job" --wait=true >/dev/null 2>&1 || true
        fi
        # the single Job older versions used
        kctl -n "$SPLIT_NS" delete job model-download --ignore-not-found --wait=true >/dev/null 2>&1 || true
    else
        # The server-side dry run checks every object against the live
        # cluster, so the namespace they go in has to exist first (after an
        # undeploy it doesn't, and every object failed with "not found").
        kctl create namespace "$SPLIT_NS" >/dev/null || {
            rm -f "$manifest"
            ny_die "Couldn't create the ${SPLIT_NS} namespace."
        }
        made_ns=1
    fi
    split_hf_secret_sync
    local check_err
    check_err="$(kctl apply --dry-run=server -f "$manifest" 2>&1 >/dev/null)" || {
        rm -f "$manifest"
        if [[ $made_ns -eq 1 ]]; then kctl delete namespace "$SPLIT_NS" --wait=false >/dev/null 2>&1 || true; fi
        printf '%s\n' "$check_err" | sort -u | head -5 >&2
        ny_die "Kubernetes rejected the generated manifest."
    }
    kctl apply -f "$manifest" || {
        rm -f "$manifest"
        ny_die "Applying the manifest failed."
    }
    rm -f "$manifest"
    if kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" >/dev/null 2>&1; then
        kctl -n "$SPLIT_NS" rollout restart "daemonset/${SPLIT_GATE_DS}" >/dev/null 2>&1 || true
    elif ! ny_simulating && ny_bool "$(ny_cfg_get ai "" gate false)"; then
        # the gate was installed before but went with the namespace: without it the model needs its key even on your own network
        ny_step "Putting the model gate back (you had installed it before)"
        ai_gate_install --trusted "$(ny_cfg_get ai "" gate-trusted "$SPLIT_GATE_TRUSTED")" ||
            ny_warn "Couldn't put the gate back. Run: sudo nodeyard ai gate install"
    fi

    ny_ok "Deployed. The main node (${PLAN_NAMES[0]}) downloads the model, then loads it and sends each node its share."
    echo "Watch progress:  nodeyard ai split status"
    echo "Try it:          nodeyard ai split test"
    if [[ "$SPLIT_NODEPORT" != "0" ]]; then
        echo "Chat page + API: http://<any node IP>:${SPLIT_NODEPORT}   (OpenAI-compatible API under /v1)"
    fi
    return 0
}

# split_load_progress -- while the model loads: how much of each node's share has
# arrived (each RPC server's memory grows towards its share as the weights come in)
split_load_progress() {
    local phase args shares rpcs tops
    phase="$(kctl -n "$SPLIT_NS" get pods -l app=llama-main -o jsonpath='{.items[0].status.phase} {.items[0].status.containerStatuses[0].ready}' 2>/dev/null || true)"
    [[ "$phase" == "Running false" ]] || return 0
    args="$(kctl -n "$SPLIT_NS" get deploy llama-main -o json 2>/dev/null | jq -r '.spec.template.spec.containers[0].args as $a | ($a | index("-ts")) as $t | ($a | index("--rpc")) as $r | "\($a[$t+1])|\($a[$r+1])"' 2>/dev/null || true)"
    shares="${args%%|*}"
    rpcs="${args#*|}"
    [[ -n "$shares" && -n "$rpcs" ]] || return 0
    tops="$(kctl -n "$SPLIT_NS" top pods --no-headers 2>/dev/null || true)"
    local started now main
    started="$(kctl -n "$SPLIT_NS" get pods -l app=llama-main -o jsonpath='{.items[0].status.containerStatuses[0].state.running.startedAt}' 2>/dev/null || true)"
    main="$(kctl -n "$SPLIT_NS" get deploy llama-main -o jsonpath='{.spec.template.spec.nodeSelector.kubernetes\.io/hostname}' 2>/dev/null || true)"
    # A GPU share: the main server reads with direct I/O (its memory doesn't show
    # the file), so ask the main node's agent how much video memory is in use.
    local vram=0 ip tok
    if (($(tr ',' '\n' <<<"$shares" | grep -c .) > $(tr ',' '\n' <<<"$rpcs" | grep -c .))) && [[ -n "$main" ]]; then
        ip="$(kctl get node "$main" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
        tok="$(ny_secret_get "$DASH_AGENT_TOKEN" 2>/dev/null || true)"
        if [[ -n "$ip" && -n "$tok" ]]; then
            vram="$(curl -s --max-time 3 -H "X-Agent-Token: ${tok}" "http://${ip}:9093/metrics.json" 2>/dev/null |
                jq -r '[.gpu_live[]?.mem_used // 0] | add // 0 | . / 1048576 | floor' 2>/dev/null || echo 0)"
        fi
        [[ "$vram" =~ ^[0-9]+$ ]] || vram=0
    fi
    started="$(date -d "$started" +%s 2>/dev/null || echo 0)"
    now="$(date +%s)"
    # (a share after the RPC servers' is the main node's GPU: llama.cpp's device order)
    awk -v shares="$shares" -v rpcs="$rpcs" -v main="$main" -v vram="$vram" -v el=$((started > 0 ? now - started : 0)) '
        { m = $3; v = m + 0; if (m ~ /Gi$/) v *= 1024; else if (m ~ /Ki$/) v /= 1024; mem[$1] = v }
        END {
            n = split(shares, s, ","); nr = split(rpcs, r, ",")
            for (i = 1; i <= n; i++) {
                got = 0
                if (i > nr) {
                    node = main " GPU"
                    got = vram - 64
                } else {
                    node = r[i]; sub(/\..*/, "", node); sub(/^rpc-/, "", node)
                    for (p in mem) if (index(p, "rpc-" node "-") == 1) got = mem[p] - 48
                }
                if (got < 0) got = 0; if (got > s[i] || got >= 0.93 * s[i]) got = s[i]
                all += got; tot += s[i]; line = line sprintf("%s %.1f/%.1f GiB  ", node, got / 1024, s[i] / 1024)
            }
            if (tot > 0) {
                pct = 100 * all / tot
                eta = ""
                if (el > 30 && pct > 3 && pct < 100) { left = el * (100 - pct) / pct
                    eta = left < 90 ? sprintf(", roughly %d s left", left) : sprintf(", roughly %d min left", (left + 30) / 60) }
                printf "Loading the model into memory: %d%%%s  (%s)\n", pct, eta, line
            }
        }' <<<"$tops"
    return 0
}

ai_split_status() {
    ny_need_kube
    if ! kctl get namespace "$SPLIT_NS" >/dev/null 2>&1; then
        echo "No split model is deployed. Plan one with: nodeyard ai split plan"
        return 0
    fi
    kctl -n "$SPLIT_NS" get pods -o wide
    echo

    # one line per download Job (several can run at once)
    local job file node size step got
    while IFS='|' read -r job file node size; do
        [[ -n "$job" ]] || continue
        local recent rate eta first last
        recent="$(kctl -n "$SPLIT_NS" logs "job/${job}" --tail=8 2>/dev/null || true)"
        step="$(tail -1 <<<"$recent")"
        # speed and time left from the progress lines of the last ~70 s
        rate="$(awk '$1 == "progress" && NF >= 4 {if (!t0) {b0 = $2; t0 = $4} b1 = $2; t1 = $4} END {if (t1 > t0) printf "%d", (b1 - b0) / (t1 - t0)}' <<<"$recent")"
        if [[ -n "$(kctl -n "$SPLIT_NS" get job "$job" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)" ]]; then
            echo "Download ${file}: done"
        elif [[ "$step" == progress* ]]; then
            read -r _ got size _ <<<"$step"
            eta=""
            if [[ "$rate" =~ ^[0-9]+$ ]] && ((rate > 0)); then
                eta="  $((rate / 1048576)) MiB/s, about $(split_duration $(((size - got) / rate))) left"
            fi
            echo "Download ${file} on ${node}: $((got * 100 / (size > 0 ? size : 1)))%  ($((got / 1048576)) of $((size / 1048576)) MiB)${eta}"
        elif [[ "$step" == *"NOT ENOUGH DISK"* ]]; then
            ny_warn "Download ${file}: ${step}"
        elif [[ "$step" == *joining* || "$step" == *verifying* ]]; then
            echo "Download ${file}: finished; now ${step#* }"
        else
            echo "Download ${file} on ${node}: starting"
        fi
    done < <(kctl -n "$SPLIT_NS" get jobs -l app.kubernetes.io/component=model-download -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.annotations.nodeyard/file}{"|"}{.metadata.annotations.nodeyard/node}{"|"}{.metadata.annotations.nodeyard/size}{"\n"}{end}' 2>/dev/null)

    split_load_progress

    local ready cip
    ready="$(kctl -n "$SPLIT_NS" get deploy llama-main -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    if [[ "$ready" == "1" ]]; then
        ny_ok "The model is loaded and serving."
        cip="$(kctl -n "$SPLIT_NS" get svc llama -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
        [[ -n "$cip" ]] && echo "In-cluster: http://${cip}:8080"
        local np
        np="$(kctl -n "$SPLIT_NS" get svc llama -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
        if [[ -z "$np" ]]; then
            np="$(kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PORT")].value}' 2>/dev/null || true)"
            [[ -n "$np" ]] && echo "From your network: http://<any node IP>:${np}  (no API key needed from your own network; outside it, the key is required)"
        else
            echo "From your network: http://<any node IP>:${np}  (chat page; OpenAI API under /v1)"
        fi
    else
        echo "Main server: not ready yet (it waits for the download, then loads and distributes the model)."
        echo "Its latest log lines:"
        kctl -n "$SPLIT_NS" logs deploy/llama-main --all-containers --tail=5 2>/dev/null | sed 's/^/  /' || true
    fi
    return 0
}

ai_split_test() {
    ny_need_kube
    local prompt="Explain in two sentences what a Raspberry Pi is." key=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prompt)
                [[ $# -ge 2 ]] || ny_usage_error "--prompt needs text"
                prompt="$2"
                shift 2
                ;;
            --api-key)
                [[ $# -ge 2 ]] || ny_usage_error "--api-key needs a value"
                key="$2"
                ny_secret_register "$2"
                shift 2
                ;;
            --api-key-file)
                [[ $# -ge 2 && -r "$2" ]] || ny_usage_error "--api-key-file needs a readable file"
                key="$(ny_trim "$(<"$2")")"
                ny_secret_register "$key"
                shift 2
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai split test [--prompt TEXT] [--api-key-file PATH]" ;;
        esac
    done
    local cip body out
    cip="$(kctl -n "$SPLIT_NS" get svc llama -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
    [[ -n "$cip" ]] || ny_die "No split model is deployed."
    body="$(printf '{"messages":[{"role":"user","content":%s}],"max_tokens":200}' "$(printf '%s' "$prompt" | sed 's/\\/\\\\/g; s/"/\\"/g; s/.*/"&"/')")"
    ny_info "Asking the split model: $prompt"
    local -a hdrs=(-H 'Content-Type: application/json')
    if [[ -n "$key" ]]; then hdrs+=(-H "Authorization: Bearer ${key}"); fi
    out="$(curl -s --max-time 900 "http://${cip}:8080/v1/chat/completions" "${hdrs[@]}" -d "$body" || true)"
    [[ -n "$out" ]] || ny_die "No answer (is it ready? check: nodeyard ai split status)"
    if have python3; then
        python3 -c 'import sys,json
d=json.loads(sys.argv[1]); t=d.get("timings",{})
if "error" in d: print("ERROR:", d["error"]); sys.exit(1)
print(d["choices"][0]["message"]["content"].strip()); print()
print("speed: %.1f words(tokens)/s generating, %.1f tokens/s reading the prompt" % (t.get("predicted_per_second",0), t.get("prompt_per_second",0)))' "$out"
    else
        printf '%s\n' "$out"
    fi
    return 0
}

# ---------- the model gate: no API key from your own network ----------

ai_gate_manifest() {
    local port="$1" trusted="$2" script
    if [[ "${GATE_ELIDE:-0}" == 1 ]]; then
        script="    # (the gate program, $(wc -l <"${NY_SHARE}/gate/gate.py" | tr -d ' ') lines, is stored here)"
    else
        script="$(sed 's/^/    /' "${NY_SHARE}/gate/gate.py")"
    fi
    cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata: {name: llama-gate-script, namespace: ${SPLIT_NS}}
data:
  gate.py: |
${script}
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ${SPLIT_GATE_DS}
  namespace: ${SPLIT_NS}
  labels: {app: ${SPLIT_GATE_DS}}
spec:
  selector:
    matchLabels: {app: ${SPLIT_GATE_DS}}
  updateStrategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 1}}
  template:
    metadata:
      labels: {app: ${SPLIT_GATE_DS}}
      # a new gate program restarts the pods (a ConfigMap change alone doesn't)
      annotations: {nodeyard/program-sha256: "$(ny_sha256 "${NY_HOME}/share/nodeyard/gate/gate.py" 2>/dev/null || echo unknown)"}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      tolerations: [{operator: Exists}]
      terminationGracePeriodSeconds: 2
      containers:
      - name: gate
        image: ${SPLIT_GATE_IMAGE}
        command: ["python", "-u", "/gate/gate.py"]
        env:
        - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
        - {name: PORT, value: "${port}"}
        - {name: PUBLIC_PORT, value: "${SPLIT_GATE_PUBLIC_PORT}"}
        - {name: UPSTREAM, value: "http://llama.${SPLIT_NS}.svc.cluster.local:8080"}
        - {name: TRUSTED, value: "${trusted}"}
        - name: API_KEY
          valueFrom: {secretKeyRef: {name: llama-api-key, key: api-key, optional: true}}
        resources:
          requests: {cpu: 10m, memory: 24Mi}
          limits: {cpu: 500m, memory: 128Mi}
        securityContext:
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: {drop: [ALL]}
          runAsUser: 65534
          runAsNonRoot: true
        readinessProbe: {httpGet: {path: /gate-health, port: ${port}}, periodSeconds: 10}
        volumeMounts:
        - {name: script, mountPath: /gate, readOnly: true}
      volumes:
      - {name: script, configMap: {name: llama-gate-script}}
YAML
}

ai_gate_install() {
    ny_need_root
    ny_need_kube
    local trusted="$SPLIT_GATE_TRUSTED" port="" item
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --trusted)
                [[ $# -ge 2 ]] || ny_usage_error "--trusted needs a list of networks"
                trusted="$2"
                shift 2
                ;;
            --port)
                [[ $# -ge 2 ]] || ny_usage_error "--port needs a number"
                ny_valid_port "$2" || ny_usage_error "$NY_VALID_MSG"
                port="$((10#$2))"
                shift 2
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai gate install [--trusted NETS] [--port PORT]" ;;
        esac
    done
    local -a nets
    IFS=, read -r -a nets <<<"$trusted"
    [[ "${#nets[@]}" -gt 0 ]] || ny_usage_error "--trusted needs at least one network"
    for item in "${nets[@]}"; do
        [[ "$item" =~ ^[0-9a-fA-F:.]+/[0-9]{1,3}$ ]] || ny_usage_error "'${item}' isn't a network like 192.168.1.0/24 or 100.64.0.0/10."
    done
    [[ -r "${NY_SHARE}/gate/gate.py" ]] || ny_die "The gate program is missing from this nodeyard install." "Update nodeyard: sudo nodeyard update" "$NY_E_PRECONDITION"
    kctl -n "$SPLIT_NS" get service llama >/dev/null 2>&1 ||
        ny_die "No split model is deployed, so there is nothing to put a gate in front of." "Deploy one first: nodeyard ai split plan" "$NY_E_PRECONDITION"
    if [[ -z "$port" ]]; then
        port="$(kctl -n "$SPLIT_NS" get service llama -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
        [[ "$port" =~ ^[0-9]+$ ]] || port="$(kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PORT")].value}' 2>/dev/null || true)"
        [[ "$port" =~ ^[0-9]+$ ]] || port=31435
    fi
    if ! kctl -n "$SPLIT_NS" get secret llama-api-key >/dev/null 2>&1; then
        ny_warn "The model has no API key. Your own network will be able to use it; every other address will be refused."
    fi

    ny_step "Putting the gate in front of the model on port ${port}"
    ny_info "Every node runs one small proxy pod (${SPLIT_GATE_IMAGE}). No key is needed from: ${trusted//,/, }"
    ny_confirm "Install the gate?" y || return 0
    if ny_simulating; then
        ny_plan_add apply "Apply the model gate DaemonSet and make the model's Service cluster-only"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            GATE_ELIDE=1 ai_gate_manifest "$port" "$trusted" >&2
            printf '%s\n' "[dry-run] would then switch Service llama to ClusterIP so the gate owns port ${port}" >&2
        fi
    else
        ai_gate_manifest "$port" "$trusted" | kctl apply -f - >/dev/null || ny_die "Applying the gate failed." "Check: nodeyard ai gate status"
    fi
    # The Service must give the port up BEFORE waiting: while it is a NodePort,
    # Kubernetes' own rules catch the port first and the gate never looks ready.
    local type
    type="$(kctl -n "$SPLIT_NS" get service llama -o jsonpath='{.spec.type}' 2>/dev/null || true)"
    if [[ "$type" == NodePort ]] || ny_simulating; then
        ny_run "$(ny_path "$NY_K3S_BIN")" kubectl -n "$SPLIT_NS" patch service llama --type=json \
            -p '[{"op":"remove","path":"/spec/ports/0/nodePort"},{"op":"replace","path":"/spec/type","value":"ClusterIP"}]' >/dev/null ||
            ny_die "Couldn't hand the port over to the gate." "Undo with: sudo nodeyard ai gate remove"
    fi
    if ! ny_simulating; then
        ny_step "Waiting for the gate to start on every node (the first run downloads the image)"
        kctl -n "$SPLIT_NS" rollout status "daemonset/${SPLIT_GATE_DS}" --timeout=300s || ny_warn "Not finished yet; check: nodeyard ai gate status"
    fi
    firewall_open_ports "model gate" "${port}/tcp"
    if ny_simulating; then
        ny_info "Nothing was installed (dry run or demo)."
    else
        ny_cfg_set ai "" gate true
        ny_cfg_set ai "" gate-trusted "$trusted"
        ny_ok "The gate is running. From your own network: http://<node address>:${port}/v1 with no API key; from anywhere else the key is required."
        ny_hint "Machines running their own firewall (ufw, firewalld) need TCP port ${port} open to be reachable on that node."
    fi
    return 0
}

ai_gate_remove() {
    ny_need_root
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" >/dev/null 2>&1 || {
        ny_info "The gate isn't installed; nothing to remove."
        return 0
    }
    ny_confirm "Remove the gate? The model's API key will be needed from everywhere again." y || return 0
    local port
    port="$(kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PORT")].value}' 2>/dev/null || true)"
    [[ "$port" =~ ^[0-9]+$ ]] || port=31435
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl -n "$SPLIT_NS" delete "daemonset/${SPLIT_GATE_DS}" configmap/llama-gate-script --ignore-not-found >/dev/null || true
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl -n "$SPLIT_NS" patch service llama --type=json \
        -p '[{"op":"replace","path":"/spec/type","value":"NodePort"},{"op":"add","path":"/spec/ports/0/nodePort","value":'"${port}"'}]' >/dev/null 2>&1 || true
    ny_cfg_set ai "" gate false
    ny_ok "The gate is removed; the model is on NodePort ${port} again and needs its API key from everywhere."
    return 0
}

ai_gate_status() {
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    if ! kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" >/dev/null 2>&1; then
        ny_info "No gate: the model needs its API key from everywhere. Install one with: sudo nodeyard ai gate install"
        return 0
    fi
    kctl -n "$SPLIT_NS" get pods -l "app=${SPLIT_GATE_DS}" -o wide
    printf '\nNo key needed from: %s\n' "$(kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="TRUSTED")].value}' 2>/dev/null | sed 's/,/, /g')"
    return 0
}

# ---------- helper pods on each node: scan / clean nodeyard's own data ----------
#
# A plain (unprivileged) busybox pod per node that only sees /var/lib/nodeyard
# (and the model folder). Used by `ai split clean`, `undeploy --purge` and
# `ai split models`. The pods and their namespace are always deleted again.
SPLIT_HELPER_NS="nodeyard-cleanup"

# split_ai_nodes -- Ready amd64/arm64 nodes (the ones split models can use)
split_ai_nodes() {
    kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.nodeInfo.architecture}{" "}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' 2>/dev/null |
        awk '($2 == "amd64" || $2 == "arm64") && $3 == "True" {print $1}'
    return 0
}

# split_on_nodes MODE SCRIPT NODE... -- runs SCRIPT (busybox sh) on each node,
# all at once, with /var/lib/nodeyard at /d and the model folder at /m
# (read-only unless MODE is rw). Prints "== NODE" then that node's output.
split_on_nodes() {
    local mode="$1" script="$2" n pod ro="true" mdir main
    shift 2
    [[ "$mode" == rw ]] && ro="false"
    main="$(kctl get namespace "$SPLIT_NS" -o jsonpath='{.metadata.annotations.nodeyard/main}' 2>/dev/null || true)"
    mdir="$(kctl get namespace "$SPLIT_NS" -o jsonpath='{.metadata.annotations.nodeyard/model-dir}' 2>/dev/null || true)"
    [[ "$mdir" =~ ^/[A-Za-z0-9._/-]+$ && "$mdir" != *..* ]] || mdir="$SPLIT_DEFAULT_MODEL_DIR"
    # A namespace still being deleted refuses new pods: wait for it to go.
    local w=0
    while ((w < 60)) && [[ "$(kctl get namespace "$SPLIT_HELPER_NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)" == Terminating ]]; do
        sleep 2
        w=$((w + 1))
    done
    kctl create namespace "$SPLIT_HELPER_NS" >/dev/null 2>&1 || true
    # Each call's pods have their own names and label: the dashboard scans the
    # disks in the background, and two scans must not delete each other's pods.
    local run
    run="$(printf '%s' "$$ ${RANDOM} $(date +%s%N)" | cksum | cut -d' ' -f1)"
    for n in "$@"; do
        pod="$(ny_k8s_name "${mode}-${run}-${n}")"
        kctl -n "$SPLIT_HELPER_NS" apply -f - >/dev/null <<YAML || ny_warn "Couldn't start the helper on ${n}."
apiVersion: v1
kind: Pod
metadata: {name: ${pod}, namespace: ${SPLIT_HELPER_NS}, labels: {app.kubernetes.io/managed-by: nodeyard, nodeyard/run: "${run}"}}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: ${n}}
  tolerations: [{operator: Exists}]
  containers:
  - name: c
    image: busybox:1.36
    imagePullPolicy: IfNotPresent
    command: ["sh", "-c", $(printf '%s' "$script" | jq -Rs .)]
    volumeMounts: [{name: d, mountPath: /d, readOnly: ${ro}}, {name: m, mountPath: /m, readOnly: ${ro}}]
  volumes:
  - {name: d, hostPath: {path: /var/lib/nodeyard, type: DirectoryOrCreate}}
  - {name: m, hostPath: {path: $([[ "$n" == "$main" ]] && echo "$mdir" || echo "$SPLIT_DEFAULT_MODEL_DIR"), type: DirectoryOrCreate}}
YAML
    done
    local tries=0 pending
    while ((tries < 90)); do
        pending="$(kctl -n "$SPLIT_HELPER_NS" get pods -l "nodeyard/run=${run}" -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null | grep -cvE '^(Succeeded|Failed)$' || true)"
        [[ "$pending" == 0 ]] && break
        sleep 2
        tries=$((tries + 1))
    done
    for n in "$@"; do
        printf '== %s\n' "$n"
        kctl -n "$SPLIT_HELPER_NS" logs "$(ny_k8s_name "${mode}-${run}-${n}")" 2>/dev/null || printf 'ERROR could not run on this node (is it under disk pressure?)\n'
    done
    # only this call's pods go; the namespace stays (empty) so the next call can reuse it
    kctl -n "$SPLIT_HELPER_NS" delete pods -l "nodeyard/run=${run}" --wait=false >/dev/null 2>&1 || true
    return 0
}

# What the scan prints per node (sizes in bytes):
#   DISK capacity free
#   MODEL size name        a finished .gguf in the model folder
#   PARTIAL size name      an unfinished download (.part*, .joining, .copying)
#   CACHE size name        a weight cache folder
# shellcheck disable=SC2016 # runs in the helper pod's shell
SPLIT_SCAN_SCRIPT='set -- $(df -Pk /m | tail -1); echo "DISK $(( $2 * 1024 )) $(( $4 * 1024 ))"
for f in /m/*.gguf; do [ -f "$f" ] && echo "MODEL $(stat -c %s "$f") ${f#/m/}"; done
for f in /m/*.gguf.part* /m/*.gguf.joining /m/*.gguf.copying; do [ -f "$f" ] && echo "PARTIAL $(stat -c %s "$f") ${f#/m/}"; done
for d in /d/rpc-cache/* /d/rpc-cache/.[!.]*; do [ -e "$d" ] && echo "CACHE $(( $(du -sk "$d" | cut -f1) * 1024 )) ${d#/d/rpc-cache/}"; done
true'

# split_in_use -> "file|cachekey|downloading-file ..." for what must be kept
split_in_use() {
    local args file="" dl
    args="$(kctl -n "$SPLIT_NS" get deploy llama-main -o jsonpath='{.spec.template.spec.containers[0].args[1]}' 2>/dev/null || true)"
    [[ -n "$args" ]] && file="${args##*/}"
    dl="$(kctl -n "$SPLIT_NS" get jobs -l app.kubernetes.io/component=model-download -o jsonpath='{range .items[?(@.status.active)]}{.metadata.annotations.nodeyard/file}{" "}{end}' 2>/dev/null || true)"
    printf '%s|%s|%s\n' "$file" "$([[ -n "$file" ]] && split_cache_key "$file" || true)" "$dl"
    return 0
}

ai_split_clean_help() {
    cat <<'HELP'
Usage: nodeyard ai split clean [--models] [--json] [--dry-run] [--yes]

Frees disk space the split model left behind on every node:
  - weight caches of models that aren't running (each node keeps a copy of
    its share of every model it ever served: often many GiB)
  - unfinished downloads that nothing is downloading any more
  - with --models: downloaded model files the running model doesn't use

The running model, its cache and any download in progress are always kept.
--dry-run (or --json) only shows what would go, with sizes.
HELP
}

ai_split_clean() {
    ny_need_kube
    local models=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --models)
                models=1
                shift
                ;;
            --yes | -y)
                NY_YES=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai split clean [--models]" ;;
        esac
    done
    local -a nodes=()
    mapfile -t nodes < <(split_ai_nodes)
    [[ "${#nodes[@]}" -gt 0 ]] || ny_die "No usable nodes."
    ny_step "Looking at what the split model keeps on ${#nodes[@]} node(s)"
    local scan keep file cache dls
    scan="$(split_on_nodes ro "$SPLIT_SCAN_SCRIPT" "${nodes[@]}")"
    keep="$(split_in_use)"
    file="${keep%%|*}"
    keep="${keep#*|}"
    cache="${keep%%|*}"
    dls="${keep#*|}"

    # decide per item: "node kind size name"
    local plan
    plan="$(awk -v file="$file" -v cache="$cache" -v dls=" $dls " -v models="$models" '
        /^== / { node = $2; next }
        $1 == "CACHE" && $3 != cache { print node, "cache", $2, $3 }
        $1 == "PARTIAL" { base = $3; sub(/\.(part[0-9]*|joining|copying)$/, "", base); if (index(dls, " " base " ") == 0) print node, "partial", $2, $3 }
        $1 == "MODEL" && models == 1 && $3 != file { print node, "model", $2, $3 }
    ' <<<"$scan")"
    local total
    total="$(awk '{s += $3} END {printf "%d", s}' <<<"$plan")"

    if [[ "$NY_JSON" -eq 1 ]]; then
        local items
        items="$(awk 'BEGIN{printf "["} NF==4 {printf "%s{\"node\":\"%s\",\"kind\":\"%s\",\"bytes\":%s,\"name\":\"%s\"}", (n++ ? "," : ""), $1, $2, $3, $4} END{printf "]"}' <<<"$plan")"
        ny_json_out "$(ny_json_obj ok:=true "dry_run:=$(ny_json_bool "$NY_DRY_RUN")" "bytes:=${total:-0}" "items:=${items}" "in_use=${file}")"
        [[ "$NY_DRY_RUN" -eq 1 ]] && return 0
    fi
    if [[ -z "$plan" ]]; then
        ny_ok "Nothing to free: no old caches or unfinished downloads$([[ $models -eq 0 ]] && echo ' (downloaded models are kept; add --models to remove the ones not in use)' || true)."
        return 0
    fi
    [[ "$NY_JSON" -eq 1 ]] || {
        printf '  %-16s %-8s %10s  %s\n' NODE KIND SIZE NAME
        awk '{printf "  %-16s %-8s %9.1fG  %s\n", $1, $2, $3 / 1073741824, $4}' <<<"$plan"
        printf '  %-16s %-8s %9.1fG\n' "" total "$(awk -v b="$total" 'BEGIN{print b/1073741824}')"
        [[ -n "$file" ]] && echo "  Kept: the running model ${file} and its caches."
    }
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add delete "Delete $(awk -v b="$total" 'BEGIN{printf "%.1f", b/1073741824}') GiB of old caches/downloads"
        return 0
    fi
    ny_confirm "Delete these ($(awk -v b="$total" 'BEGIN{printf "%.1f", b/1073741824}') GiB)?" y || {
        echo "Cancelled."
        return 0
    }
    local n script name kind size out
    local -a touched=()
    local -A per=()
    while read -r n kind size name; do
        [[ -n "$n" ]] || continue
        # names come from the scan; refuse anything that could leave the folder
        [[ "$name" =~ ^[A-Za-z0-9._+-]+$ ]] || continue
        case "$kind" in
            cache) per[$n]+="rm -rf '/d/rpc-cache/${name}'; " ;;
            partial | model) per[$n]+="rm -f '/m/${name}'; " ;;
        esac
    done <<<"$plan"
    for n in "${!per[@]}"; do touched+=("$n"); done
    # one helper per node, each with its own list
    local freed=0 failed=0 got
    for n in "${touched[@]}"; do
        script="set -- \$(df -Pk /m | tail -1); b=\$4; ${per[$n]} sync; set -- \$(df -Pk /m | tail -1); echo \"FREED \$(( (\$4 - b) * 1024 )) \$(( \$4 * 1024 ))\""
        out="$(split_on_nodes rw "$script" "$n")"
        awk -v n="$n" '/^FREED/ {printf "  %-16s freed %.1f GiB, now %.1f GiB free\n", n, $2 / 1073741824, $3 / 1073741824} /^ERROR/ {print "  " n ": " $0}' <<<"$out"
        got="$(awk '/^FREED/ {print $2}' <<<"$out")"
        if [[ "$got" =~ ^-?[0-9]+$ ]]; then freed=$((freed + (got > 0 ? got : 0))); else failed=$((failed + 1)); fi
    done
    # report what the disks say, not what was planned
    if ((failed > 0)); then
        ny_warn "Freed $(split_gib "$freed") GiB; ${failed} node(s) couldn't be cleaned (run it again)."
        return 1
    fi
    ny_ok "Freed $(split_gib "$freed") GiB."
    return 0
}

ai_split_models_help() {
    cat <<'HELP'
Usage: nodeyard ai split models [--json]

Lists the downloaded model files, unfinished downloads and weight caches on
every node, with sizes and free disk, and which ones the running model uses.
HELP
}

ai_split_models() {
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local -a nodes=()
    mapfile -t nodes < <(split_ai_nodes)
    local scan keep file
    scan="$(split_on_nodes ro "$SPLIT_SCAN_SCRIPT" "${nodes[@]}")"
    keep="$(split_in_use)"
    file="${keep%%|*}"
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(awk -v file="$file" '
            BEGIN { printf "{\"ok\":true,\"in_use\":\"%s\",\"nodes\":[", file }
            /^== / { if (open) printf "]}"; printf "%s{\"node\":\"%s\",\"items\":[", (nn++ ? "," : ""), $2; open = 1; ni = 0; next }
            $1 == "DISK" { printf "%s{\"kind\":\"disk\",\"capacity\":%s,\"free\":%s}", (ni++ ? "," : ""), $2, $3; next }
            $1 ~ /^(MODEL|PARTIAL|CACHE)$/ { printf "%s{\"kind\":\"%s\",\"bytes\":%s,\"name\":\"%s\"}", (ni++ ? "," : ""), tolower($1), $2, $3 }
            END { if (open) printf "]}"; printf "]}" }' <<<"$scan")"
        return 0
    fi
    awk -v file="$file" '
        /^== / { printf "\n%s\n", $2; next }
        $1 == "DISK" { printf "  disk: %.1f GiB free of %.1f GiB (root partition)\n", $3 / 1073741824, $2 / 1073741824 }
        $1 == "MODEL" { printf "  model    %8.1fG  %s%s\n", $2 / 1073741824, $3, ($3 == file ? "  (in use)" : "") }
        $1 == "PARTIAL" { printf "  partial  %8.1fG  %s\n", $2 / 1073741824, $3 }
        $1 == "CACHE" { printf "  cache    %8.1fG  %s\n", $2 / 1073741824, $3 }
        /^ERROR/ { print "  " $0 }' <<<"$scan"
    echo
    echo "Free space: nodeyard ai split clean   (add --models to also delete models not in use)"
    return 0
}

ai_split_download_help() {
    cat <<'HELP'
Usage: nodeyard ai split download --model owner/repo:file.gguf [--main NODE] [--model-dir PATH]

Downloads one model file (resumable, sha256-checked) without deploying it.
Several downloads can run at once, each in its own Job. By default it goes to
the node with the most free space on its root partition. `ai split deploy`
later uses the file straight away. Progress: nodeyard ai split status
HELP
}

ai_split_download() {
    ny_need_kube
    split_parse_flags "$@"
    [[ "$SPLIT_MODEL_AUTO" -eq 0 ]] || ny_usage_error "--model owner/repo:file.gguf is required"
    split_model_info
    local node="$SPLIT_MAIN" best=-1 name cap used cpu cp arch ready disk
    if [[ -z "$node" ]]; then
        while IFS='|' read -r name cap used cpu cp arch ready disk _; do
            [[ "$ready" == "True" && ("$arch" == amd64 || "$arch" == arm64) && "$disk" =~ ^[0-9]+$ ]] || continue
            if ((disk > best)); then
                best=$disk
                node="$name"
            fi
        done <<<"$(split_node_table)"
        [[ -n "$node" ]] || ny_die "No usable node to download to."
    fi
    if kctl get namespace "$SPLIT_NS" >/dev/null 2>&1; then
        local cur
        cur="$(kctl get namespace "$SPLIT_NS" -o jsonpath='{.metadata.annotations.nodeyard/main}' 2>/dev/null || true)"
        if [[ -n "$cur" && "$cur" != "$node" && -z "$SPLIT_MAIN" ]]; then
            node="$cur" # keep models together on the main node of the running model
        fi
    fi
    ny_info "Downloading ${SPLIT_FILE} ($(awk -v b="$SPLIT_SIZE" 'BEGIN{printf "%.1f", b/1073741824}') GiB) to ${node}:${SPLIT_MODEL_DIR}"
    local job manifest
    job="$(split_job_name "$SPLIT_FILE")"
    manifest="$(ny_mktemp)"
    {
        printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n  labels: {app.kubernetes.io/managed-by: nodeyard}\n---\n' "$SPLIT_NS"
        split_download_yaml "$node"
    } >"$manifest"
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add apply "Start download Job ${job} on ${node}"
        rm -f "$manifest"
        return 0
    fi
    # a finished or failed Job of the same file is replaced (it resumes)
    if kctl -n "$SPLIT_NS" get job "$job" >/dev/null 2>&1; then
        if [[ -z "$(kctl -n "$SPLIT_NS" get job "$job" -o jsonpath='{.status.active}' 2>/dev/null || true)" ]]; then
            kctl -n "$SPLIT_NS" delete job "$job" --wait=true >/dev/null 2>&1 || true
        else
            rm -f "$manifest"
            ny_ok "Already downloading ${SPLIT_FILE}. Progress: nodeyard ai split status"
            return 0
        fi
    fi
    kctl apply -f "$manifest" >/dev/null || {
        rm -f "$manifest"
        ny_die "Couldn't start the download."
    }
    split_hf_secret_sync
    rm -f "$manifest"
    ny_ok "Downloading in the background (Job ${job}). Progress: nodeyard ai split status"
    return 0
}

# ---------- per-node disk limits ----------
#
# `ai disk limit NODE GiB` stores a node annotation; nodeyard then never lets
# its own data (model files, weight caches, Ollama) push that node's root
# partition past the limit. Read by split_node_table and `ai deploy`.
SPLIT_DISK_LIMIT_ANN="nodeyard/disk-limit-gib"

ai_disk_limit_help() {
    cat <<'HELP'
Usage: nodeyard ai disk limit NODE GiB|off
       nodeyard ai disk limit            (show the limits)

Caps how full nodeyard lets a node's root partition get. A node at its limit
gets no model files, weight caches or Ollama; a node close to it still holds
a share of a split model but without the weight cache.
Example: nodeyard ai disk limit debian-worker 8
HELP
}

ai_disk_limit() {
    ny_need_kube
    if [[ $# -eq 0 ]]; then
        kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.nodeyard/disk-limit-gib}{"\n"}{end}' 2>/dev/null |
            awk -F'\t' 'BEGIN {printf "%-18s %s\n", "NODE", "DISK LIMIT"} {printf "%-18s %s\n", $1, ($2 == "" ? "none" : $2 " GiB")}'
        return 0
    fi
    [[ $# -eq 2 ]] || ny_usage_error "Usage: nodeyard ai disk limit NODE GiB|off"
    local node="$1" gib="$2"
    kctl get node "$node" >/dev/null 2>&1 || ny_die "No node called ${node}."
    if [[ "$gib" == off ]]; then
        ny_run "$(ny_path "$NY_K3S_BIN")" kubectl annotate node "$node" "${SPLIT_DISK_LIMIT_ANN}-" >/dev/null 2>&1 || true
        ny_ok "${node} has no disk limit now."
        return 0
    fi
    ny_valid_int "$gib" 1 100000 || ny_usage_error "GiB: ${NY_VALID_MSG}"
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl annotate node "$node" "${SPLIT_DISK_LIMIT_ANN}=${gib}" --overwrite >/dev/null ||
        ny_die "Couldn't set the limit."
    ny_ok "nodeyard will keep ${node}'s root partition under ${gib} GiB used."
    return 0
}

ai_split_rm_help() {
    cat <<'HELP'
Usage: nodeyard ai split rm FILE.gguf [--yes]

Deletes one downloaded split model from every node: the file, any unfinished
parts of it, and its weight caches. A download of it that is still running is
stopped. The model that is running now can't be deleted: remove it first
(nodeyard ai split undeploy). See what is there: nodeyard ai split models
HELP
}

ai_split_rm() {
    ny_need_kube
    local file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --yes | -y)
                NY_YES=1
                shift
                ;;
            -*) ny_usage_error "Unknown option: $1" ;;
            *)
                [[ -z "$file" ]] || ny_usage_error "One file at a time."
                file="$1"
                shift
                ;;
        esac
    done
    [[ "$file" =~ ^[A-Za-z0-9._+-]+\.gguf$ ]] || ny_usage_error "Give the model's file name, e.g. Qwen3-8B-Q4_K_M.gguf (see: nodeyard ai split models)"
    local keep
    keep="$(split_in_use)"
    [[ "${keep%%|*}" != "$file" ]] || ny_die "${file} is the model that is running now." "Remove it first: sudo nodeyard ai split undeploy" "$NY_E_PRECONDITION"
    ny_confirm "Delete ${file} (and its caches) from every node?" n || {
        echo "Cancelled."
        return 0
    }
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add delete "Delete ${file}, its parts and its weight caches on every node"
        return 0
    fi
    split_delete_files "$file"
    ny_ok "Deleted ${file}."
    return 0
}

# split_delete_files FILE -- deletes one model's file, unfinished parts and
# weight caches on every node (and stops its download), saying what it freed.
split_delete_files() {
    local file="$1"
    [[ "$file" =~ ^[A-Za-z0-9._+-]+\.gguf$ ]] || return 1
    kctl -n "$SPLIT_NS" delete job "$(split_job_name "$file")" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    local -a nodes=()
    mapfile -t nodes < <(split_ai_nodes)
    local key out
    key="$(split_cache_key "$file")"
    out="$(split_on_nodes rw "set -- \$(df -Pk /m | tail -1); b=\$4; rm -f '/m/${file}' '/m/${file}'.part* '/m/${file}.joining' '/m/${file}.copying'; rm -rf '/d/rpc-cache/${key}'; sync; set -- \$(df -Pk /m | tail -1); echo \"FREED \$(( (\$4 - b) * 1024 ))\"" "${nodes[@]}")"
    awk '/^== / {n = $2} /^FREED/ && $2 > 1048576 {printf "  %-16s freed %.1f GiB\n", n, $2 / 1073741824} /^ERROR/ {print "  " n ": " $0}' <<<"$out"
    return 0
}

# ---------- the model's API key and the Hugging Face token ----------

SPLIT_KEY_SECRET="ai-split-api-key"
SPLIT_HF_SECRET="hf-token"

ai_split_key_help() {
    cat <<'HELP'
Usage: nodeyard ai split key --rotate | --stdin

Changes the split model's API key (stored in /etc/nodeyard/secrets/ai-split-api-key,
which is NOT the dashboard password) and gives the running model the new one:
  --rotate   make a new random key
  --stdin    read the new key from standard input (at least 16 characters)
Apps that use the old key need the new one afterwards. The dashboard's chat
picks it up by itself.
HELP
}

ai_split_key() {
    ny_need_root
    local mode=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --rotate) mode=rotate ;;
            --stdin) mode=stdin ;;
            --yes | -y) NY_YES=1 ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai split key --rotate | --stdin" ;;
        esac
        shift
    done
    [[ -n "$mode" ]] || ny_usage_error "Say --rotate or --stdin." "nodeyard ai split key --rotate | --stdin"
    local key
    if [[ "$mode" == rotate ]]; then
        key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    else
        IFS= read -r key || true
        key="$(ny_trim "$key")"
        [[ ${#key} -ge 16 && "$key" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || ny_usage_error "The key needs at least 16 characters: letters, digits and . _ ~ + / = -"
    fi
    ny_secret_register "$key"
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add write "Store a new model API key and restart the model's front end"
        return 0
    fi
    printf '%s' "$key" | ny_secret_set "$SPLIT_KEY_SECRET"
    if kctl -n "$SPLIT_NS" get secret llama-api-key >/dev/null 2>&1; then
        kctl -n "$SPLIT_NS" create secret generic llama-api-key --from-file="api-key=$(ny_path "$(ny_secret_path "$SPLIT_KEY_SECRET")")" \
            --dry-run=client -o yaml | kctl apply -f - >/dev/null || ny_die "Couldn't update the key in the cluster."
        kctl -n "$SPLIT_NS" rollout restart deployment/llama-main >/dev/null 2>&1 || true
        ny_ok "New API key stored; the model restarts with it (it reloads in a minute or two)."
    else
        ny_ok "New API key stored. The next 'ai split deploy' uses it."
    fi
    return 0
}

ai_hf_token_help() {
    cat <<'HELP'
Usage: nodeyard ai hf token --stdin | --remove | --status

A Hugging Face access token, for models you have to accept terms for (gated
models). Searches, file lists and downloads send it to huggingface.co only.
Make one at https://huggingface.co/settings/tokens (read access is enough).
HELP
}

ai_hf_token() {
    ny_need_root
    local mode="status" tok
    case "${1:-}" in
        --stdin) mode=stdin ;;
        --remove) mode=remove ;;
        --status | "") mode=status ;;
        *) ny_usage_error "Unknown option: $1" "nodeyard ai hf token --stdin | --remove | --status" ;;
    esac
    case "$mode" in
        status)
            if ny_secret_exists "$SPLIT_HF_SECRET"; then echo "A Hugging Face token is set."; else echo "No Hugging Face token is set."; fi
            ;;
        remove)
            ny_run rm -f "$(ny_path "$(ny_secret_path "$SPLIT_HF_SECRET")")"
            kctl -n "$SPLIT_NS" delete secret hf-token --ignore-not-found >/dev/null 2>&1 || true
            ny_ok "The Hugging Face token is removed."
            ;;
        stdin)
            IFS= read -r tok || true
            tok="$(ny_trim "$tok")"
            [[ "$tok" =~ ^hf_[A-Za-z0-9]{20,100}$ ]] || ny_usage_error "That doesn't look like a Hugging Face token (they start with hf_)."
            ny_secret_register "$tok"
            printf '%s' "$tok" | ny_secret_set "$SPLIT_HF_SECRET"
            ny_ok "Hugging Face token stored."
            ;;
    esac
    return 0
}

# split_hf_header -- curl arguments that send the Hugging Face token, if one is set
split_hf_curl_args() {
    if ny_secret_exists "$SPLIT_HF_SECRET" 2>/dev/null; then
        printf '%s\n' "-H" "Authorization: Bearer $(ny_secret_get "$SPLIT_HF_SECRET")"
    fi
    return 0
}

# split_hf_secret_sync -- copy the token into the cluster for the download Jobs
split_hf_secret_sync() {
    ny_secret_exists "$SPLIT_HF_SECRET" 2>/dev/null || return 0
    ny_simulating && return 0
    kctl -n "$SPLIT_NS" create secret generic hf-token --from-file="token=$(ny_path "$(ny_secret_path "$SPLIT_HF_SECRET")")" \
        --dry-run=client -o yaml | kctl apply -f - >/dev/null 2>&1 || true
    return 0
}

# ---------- NVIDIA GPUs for models ----------

ai_gpu_setup_help() {
    cat <<'HELP'
Usage: nodeyard ai gpu setup          (run ON the machine with the NVIDIA card)
       nodeyard ai gpu enable NODE    (run on the server afterwards)
       nodeyard ai gpu status

Lets models (and the dashboard's GPU numbers) use an NVIDIA graphics card.
  setup    installs NVIDIA's container toolkit on this machine (Debian/Ubuntu)
           and restarts k3s here so containers can use the card. The NVIDIA
           driver itself must already work (nvidia-smi).
  enable   checks from the server that a container on NODE really sees the
           card, then marks NODE as a GPU node (with its video memory), so
           'ai split' puts the fastest layers on it and the dashboard shows
           its usage.
HELP
}
ai_gpu_enable_help() { ai_gpu_setup_help; }
ai_gpu_status_help() { ai_gpu_setup_help; }

ai_gpu_setup() {
    ny_need_root
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --yes | -y)
                NY_YES=1
                shift
                ;;
            *) ny_usage_error "Unexpected argument: $1" ;;
        esac
    done
    local d found=0
    for d in /sys/bus/pci/devices/*; do
        [[ "$(cat "$d/vendor" 2>/dev/null)" == 0x10de && "$(cat "$d/class" 2>/dev/null)" == 0x03* ]] && found=1
    done
    [[ $found -eq 1 ]] || ny_die "This machine has no NVIDIA graphics card." "" "$NY_E_PRECONDITION"
    if [[ ! -r /proc/driver/nvidia/version ]] || ! command -v nvidia-smi >/dev/null 2>&1; then
        ny_die "The NVIDIA driver isn't working on this machine (nvidia-smi)." "Install NVIDIA's driver first (Debian: apt install nvidia-driver), reboot, then run this again." "$NY_E_PRECONDITION"
    fi
    if ! command -v nvidia-container-runtime >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || ny_die "Installing NVIDIA's container toolkit is only automated on Debian and Ubuntu." \
            "See https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html" "$NY_E_PRECONDITION"
        ny_confirm "Install NVIDIA's container toolkit (from NVIDIA's apt repository) and restart k3s on this machine?" y || return 0
        ny_step "Adding NVIDIA's package repository"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            ny_plan_add run "add NVIDIA's container toolkit apt repository, apt-get install nvidia-container-toolkit"
        else
            local key=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            if command -v gpg >/dev/null 2>&1; then
                curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o "$key" ||
                    ny_die "Couldn't get NVIDIA's signing key."
            else
                # no gpg (a minimal Debian): apt reads the key as it is, by its .asc name
                key=/usr/share/keyrings/nvidia-container-toolkit-keyring.asc
                curl -fsSL -o "$key" https://nvidia.github.io/libnvidia-container/gpgkey || ny_die "Couldn't get NVIDIA's signing key."
            fi
            local list from="deb https://" to="deb [signed-by=${key}] https://"
            list="$(curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list)" ||
                ny_die "Couldn't get NVIDIA's package list."
            printf '%s\n' "${list//"$from"/"$to"}" >/etc/apt/sources.list.d/nvidia-container-toolkit.list
            ny_step "Installing nvidia-container-toolkit"
            DEBIAN_FRONTEND=noninteractive apt-get update -qq || ny_die "apt-get update failed."
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit >/dev/null || ny_die "Installing nvidia-container-toolkit failed."
        fi
    else
        ny_ok "NVIDIA's container toolkit is already installed."
    fi
    # k3s looks for nvidia-container-runtime when it starts and adds the 'nvidia' runtime
    local svc=k3s-agent
    systemctl is-active --quiet k3s 2>/dev/null && svc=k3s
    ny_step "Restarting ${svc} so it picks up the NVIDIA runtime"
    ny_run systemctl restart "$svc" || ny_die "Restarting ${svc} failed."
    ny_ok "Done on this machine. Now, on the server: sudo nodeyard ai gpu enable $(hostname)   (use the node's name in the cluster)"
    return 0
}

ai_gpu_enable() {
    ny_need_kube
    [[ $# -eq 1 ]] || ny_usage_error "Usage: nodeyard ai gpu enable NODE"
    local node="$1" pod out name vram ns="nodeyard-system"
    kctl get node "$node" >/dev/null 2>&1 || ny_die "No node called ${node}."
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add run "test nvidia-smi in a container on ${node}, then label it nodeyard.io/gpu=nvidia"
        return 0
    fi
    pod="gpu-check-$(ny_k8s_name "$node")"
    kctl create namespace "$ns" >/dev/null 2>&1 || true
    kctl -n "$ns" delete pod "$pod" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    ny_step "Checking that a container on ${node} can use the NVIDIA card"
    kctl -n "$ns" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: ${pod}, namespace: ${ns}, labels: {app.kubernetes.io/managed-by: nodeyard}}
spec:
  restartPolicy: Never
  runtimeClassName: nvidia
  nodeSelector: {kubernetes.io/hostname: ${node}}
  tolerations: [{operator: Exists}]
  containers:
  - name: c
    image: ${HW_IMAGE_GLIBC:-python:3.12-slim}
    env: [{name: NVIDIA_VISIBLE_DEVICES, value: all}, {name: NVIDIA_DRIVER_CAPABILITIES, value: utility}]
    command: ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
YAML
    kctl -n "$ns" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/${pod}" --timeout=240s >/dev/null 2>&1 || true
    out="$(kctl -n "$ns" logs "$pod" 2>/dev/null | head -1 || true)"
    if [[ -z "$out" || "$out" != *,* ]]; then
        local why
        why="$(kctl -n "$ns" get events --field-selector "involvedObject.name=${pod}" -o jsonpath='{range .items[*]}{.message}{"\n"}{end}' 2>/dev/null | tail -1 || true)"
        kctl -n "$ns" delete pod "$pod" --wait=false >/dev/null 2>&1 || true
        ny_die "A container on ${node} couldn't use the NVIDIA card${why:+: ${why}}" "On ${node} run: sudo nodeyard ai gpu setup" "$NY_E_PRECONDITION"
    fi
    kctl -n "$ns" delete pod "$pod" --wait=false >/dev/null 2>&1 || true
    name="${out%%,*}"
    vram="$(tr -dc '0-9' <<<"${out##*,}")"
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl label node "$node" nodeyard.io/gpu=nvidia "nodeyard.io/gpu-vram-mib=${vram}" --overwrite >/dev/null ||
        ny_die "Couldn't label ${node}."
    ny_ok "${node}: ${name} with $((vram / 1024)) GiB of video memory is ready for models."
    if kctl -n "$ns" get daemonset nodeyard-agent >/dev/null 2>&1; then
        ny_step "Updating the node agents (GPU usage on the dashboard)"
        dashboard_agent_install_cmd >/dev/null 2>&1 || ny_warn "Couldn't update the node agents; run: sudo nodeyard dashboard agent install"
    fi
    ny_hint "Split models whose main node is ${node} now put their fastest part on the GPU (redeploy to use it): nodeyard ai split plan"
    return 0
}

ai_gpu_status() {
    ny_need_kube
    local out
    out="$(kctl get nodes -l nodeyard.io/gpu=nvidia -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels.nodeyard\.io/gpu-vram-mib}{"\n"}{end}' 2>/dev/null || true)"
    if [[ -z "$out" ]]; then
        echo "No GPU nodes are set up. On a machine with an NVIDIA card: sudo nodeyard ai gpu setup"
        return 0
    fi
    awk '{printf "  %-18s NVIDIA, %d GiB video memory\n", $1, $2 / 1024}' <<<"$out"
    return 0
}

ai_split_unload() {
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    kctl get namespace "$SPLIT_NS" >/dev/null 2>&1 || ny_die "No split model is deployed." "Plan one with: nodeyard ai split plan" "$NY_E_PRECONDITION"
    ny_confirm "Unload the split model? Every node gets its memory back; the downloaded file stays, so 'ai split load' is quick." y || return 0
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl -n "$SPLIT_NS" scale deployment --all --replicas=0 >/dev/null ||
        ny_die "Couldn't stop the model's servers." "Check: nodeyard ai split status"
    ny_ok "The split model is unloaded and its memory is free. Load it again with: sudo nodeyard ai split load"
}

ai_split_load() {
    ny_need_kube
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    kctl get namespace "$SPLIT_NS" >/dev/null 2>&1 || ny_die "No split model is deployed." "Plan one with: nodeyard ai split plan" "$NY_E_PRECONDITION"
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl -n "$SPLIT_NS" scale deployment --all --replicas=1 >/dev/null ||
        ny_die "Couldn't start the model's servers." "Check: nodeyard ai split status"
    ny_ok "Loading the split model. It takes a minute or two while each node reads its share; follow it with: nodeyard ai split status"
}

ai_split_undeploy() {
    ny_need_kube
    local purge=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge)
                purge=1
                shift
                ;;
            --yes | -y)
                NY_YES=1
                shift
                ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai split undeploy [--purge]" ;;
        esac
    done
    local deployed=0
    kctl get namespace "$SPLIT_NS" >/dev/null 2>&1 && deployed=1
    if [[ $deployed -eq 0 && $purge -eq 0 ]]; then
        echo "No split model is deployed. (Leftover files? nodeyard ai split undeploy --purge, or: nodeyard ai split clean --models)"
        return 0
    fi
    ny_confirm "Remove the split model$([[ $purge -eq 1 ]] && echo ' AND delete every downloaded model, unfinished download and weight cache from every node' || true)?" n || {
        echo "Cancelled."
        return 0
    }
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add delete "Delete namespace ${SPLIT_NS}$([[ $purge -eq 1 ]] && echo ' and all model files and caches on every node' || true)"
        return 0
    fi
    # Unload first: every model server stops and its memory is back before
    # anything is deleted, so nothing is left running half-removed.
    if [[ $deployed -eq 1 ]]; then
        echo "Unloading the model first..."
        split_stop_servers
    fi
    local -a nodes=()
    # Purge looks at EVERY usable node, not only the ones running the model
    # now: a failed deploy or an earlier undeploy leaves files behind too.
    [[ $purge -eq 1 ]] && mapfile -t nodes < <(split_ai_nodes)
    local purge_script="rm -rf /d/models /d/rpc-cache; rm -f /m/*.gguf /m/*.gguf.part* /m/*.gguf.joining /m/*.gguf.copying; sync; echo cleaned"
    local out=""
    if [[ $purge -eq 1 && "${#nodes[@]}" -gt 0 ]]; then
        # (runs before the namespace goes, so the model folder is still known)
        kctl -n "$SPLIT_NS" delete job,deployment --all --wait=true >/dev/null 2>&1 || true
        out="$(split_on_nodes rw "$purge_script" "${nodes[@]}")"
    fi
    [[ $deployed -eq 1 ]] && { kctl delete namespace "$SPLIT_NS" --wait=true --timeout=300s >/dev/null || ny_warn "The ${SPLIT_NS} namespace is still being deleted; check: nodeyard ai split status"; }
    if [[ -n "$out" ]]; then
        if grep -q '^ERROR' <<<"$out"; then
            ny_warn "Couldn't clean some nodes:"
            awk '/^== / {n = $2} /^ERROR/ {print "  " n}' <<<"$out" >&2
        fi
        ny_ok "Removed the model files and caches from: $(awk '/^== / {n = $2} /^cleaned/ {printf "%s ", n}' <<<"$out")"
    fi
    [[ $deployed -eq 1 ]] && ny_ok "Split model removed."
    return 0
}

# split_stop_servers -- unloads: stops the model servers (main + one per node)
# and waits until their pods are really gone, so every node has its memory
# back. Forces any that hang. Downloads and the gate keep running.
split_stop_servers() {
    kctl get namespace "$SPLIT_NS" >/dev/null 2>&1 || return 0
    kctl -n "$SPLIT_NS" scale deployment --all --replicas=0 >/dev/null 2>&1 || true
    local w=0 left
    while ((w < 60)); do
        left="$(split_server_pods)"
        [[ -z "$left" ]] && return 0
        sleep 2
        w=$((w + 1))
    done
    ny_warn "Some model servers didn't stop within 2 minutes; forcing them: ${left//$'\n'/ }"
    # shellcheck disable=SC2086 # one pod name per word
    kctl -n "$SPLIT_NS" delete pod ${left} --grace-period=0 --force >/dev/null 2>&1 || true
    return 0
}

# split_server_pods -- the model-server pods (made by a Deployment) still there
split_server_pods() {
    kctl -n "$SPLIT_NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.ownerReferences[0].kind}{"\n"}{end}' 2>/dev/null |
        awk '$2 == "ReplicaSet" {print $1}'
    return 0
}

ai_split_switch_help() {
    cat <<'HELP'
Usage: nodeyard ai split switch --model owner/repo:file.gguf [--keep-old] [deploy options] [--yes]

Changes the running split model to another one and cleans up after the old one:
  1. unloads the old model: its servers stop and every node gets its memory back
  2. deletes the old model's file, unfinished parts and weight caches from every
     node (--keep-old keeps them, so switching back is quick)
  3. downloads the new model if it isn't on disk yet, then loads it

It takes the same options as 'nodeyard ai split deploy' (--ctx, --alias,
--nodes, --main, --no-gpu, ...). With nothing running it is the same as deploy.
HELP
}

ai_split_switch() {
    ny_need_kube
    local keep_old=0 a
    local -a rest=()
    for a in "$@"; do
        if [[ "$a" == "--keep-old" ]]; then keep_old=1; else rest+=("$a"); fi
    done
    split_parse_flags "${rest[@]}"
    split_pick_model
    split_print_plan
    local old
    old="$(split_in_use)"
    old="${old%%|*}"
    if [[ -n "$old" && "$old" != "$SPLIT_FILE" ]]; then
        echo "Running now: ${old}. It is unloaded first$([[ $keep_old -eq 1 ]] && echo '; its files stay on disk' || echo ', then its file and weight caches are deleted from every node')."
    elif [[ "$old" == "$SPLIT_FILE" ]]; then
        echo "${old} is already the running model: it is restarted with these settings."
    fi
    ny_confirm "Switch to ${SPLIT_FILE}?" y || {
        echo "Cancelled."
        return 0
    }
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        [[ -z "$old" ]] || ny_plan_add delete "Unload ${old}$([[ $keep_old -eq 0 && "$old" != "$SPLIT_FILE" ]] && echo ' and delete its files on every node' || true)"
        ny_plan_add apply "Run ${SPLIT_FILE} (main node ${PLAN_NAMES[0]})"
        return 0
    fi
    if [[ -n "$old" ]]; then
        echo "Unloading ${old}..."
        split_stop_servers
        ny_ok "Unloaded: every node has its memory back."
        if [[ "$old" != "$SPLIT_FILE" && $keep_old -eq 0 ]]; then
            echo "Deleting ${old} and its weight caches..."
            split_delete_files "$old"
        fi
        # (no new plan here: the memory figures lag a minute behind the unload,
        # and the plan above already counted the old model's memory as free)
    fi
    split_disk_check
    split_apply
}
