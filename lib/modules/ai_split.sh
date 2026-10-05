# shellcheck shell=bash
# EXPERIMENTAL: one model too big for any single node, split across several
# with llama.cpp RPC (ported unchanged in behaviour from k3s-manager 3.2).
# Every generated token passes through every node over the network, so it
# is slow over ethernet and every node must stay on while it runs.

ny_cmd "ai split plan" ai_split_plan "AI" "Experimental: plan how one big model would be split across nodes" ai
ny_cmd "ai split deploy" ai_split_deploy "AI" "Experimental: deploy one model split across several nodes" ai
ny_cmd "ai split status" ai_split_status "AI" "Experimental: download/load progress of the split model" ai
ny_cmd "ai split test" ai_split_test "AI" "Experimental: send the split model a prompt and time it" ai
ny_cmd "ai split undeploy" ai_split_undeploy "AI" "Experimental: remove the split model" ai
ny_cmd_alias "ai split remove" "ai split undeploy"

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
  --nodes a,b,c            Only use these nodes
  --reserve NODE=GiB       Keep extra memory free on a node (repeatable)
  --threads NODE=N         CPU threads on a node (repeatable)
  --main-only              The main node coordinates and holds no layers
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
SPLIT_LLAMA_BUILD="${NODEYARD_LLAMA_BUILD:-b11160}"
# Default (--model auto): the biggest of these Qwen3.6-35B-A3B quants that fits
# in the cluster's free memory right now, best first.
SPLIT_AUTO_REPO="unsloth/Qwen3.6-35B-A3B-GGUF"
SPLIT_AUTO_PREFIX="Qwen3.6-35B-A3B-"
SPLIT_AUTO_QUANTS=(Q8_0 UD-Q6_K_XL UD-Q5_K_XL UD-Q4_K_XL UD-IQ4_XS UD-Q3_K_XL UD-IQ3_S UD-Q2_K_XL)
SPLIT_DEFAULT_MODEL="auto"
SPLIT_DEFAULT_MODEL_DIR="/var/lib/nodeyard/models"

# split_parse_model SPEC -> SPLIT_REPO SPLIT_REV SPLIT_FILE SPLIT_URL
# SPEC is "owner/repo:file.gguf" or a huggingface.co .../resolve|blob/<rev>/<file> URL.
split_parse_model() {
    local spec="$1"
    if [[ "$spec" =~ ^https?://huggingface\.co/([^/]+/[^/]+)/(resolve|blob)/([^/]+)/(.+)$ ]]; then
        SPLIT_REPO="${BASH_REMATCH[1]}"; SPLIT_REV="${BASH_REMATCH[3]}"; SPLIT_FILE="${BASH_REMATCH[4]}"
    elif [[ "$spec" =~ ^([^/:]+/[^/:]+):(.+)$ ]]; then
        SPLIT_REPO="${BASH_REMATCH[1]}"; SPLIT_REV="main"; SPLIT_FILE="${BASH_REMATCH[2]}"
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
    local hdr
    hdr="$(curl -sI --max-time 30 "$SPLIT_URL" 2>/dev/null || true)"
    SPLIT_SIZE="$(grep -i '^x-linked-size:' <<<"$hdr" | tr -dc '0-9' || true)"
    SPLIT_SHA="$(grep -i '^x-linked-etag:' <<<"$hdr" | grep -oE '[0-9a-f]{64}' || true)"
    [[ -n "$SPLIT_SIZE" ]] || ny_die "Couldn't get the size of $SPLIT_URL -- check the repo/file name and that this machine is online."
    if [[ -z "$SPLIT_SHA" ]]; then
        ny_warn "Hugging Face didn't return a sha256 for this file; the download won't be verified."
    fi
    return 0
}

# split_node_table -> "name<TAB>capMiB<TAB>usedMiB<TAB>cpus<TAB>controlPlane<TAB>arch<TAB>ready" per node
split_node_table() {
    local tops name cap cpu arch ready cp pressure used mine podnodes podtops
    tops="$(kctl top nodes --no-headers 2>/dev/null || true)"
    # Memory held by an existing split deployment is freed when it is
    # replaced, so it doesn't count as "in use" (MiB per node).
    podnodes="$(kctl -n "$SPLIT_NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\n"}{end}' 2>/dev/null || true)"
    podtops="$(kctl -n "$SPLIT_NS" top pods --no-headers 2>/dev/null || true)"
    # (the possibly-empty control-plane label must stay last: read merges empty tab fields)
    while IFS=$'\t' read -r name cap cpu arch ready pressure cp; do
        [[ -n "$name" ]] || continue
        cap="${cap%Ki}"; [[ "$cap" =~ ^[0-9]+$ ]] || cap=0
        # a node under disk/memory pressure rejects new pods: report it as not ready
        if [[ "$pressure" == *True* ]]; then ready="Pressure"; fi
        used="$(awk -v n="$name" '$1 == n {print $4}' <<<"$tops")"
        case "$used" in
            *Mi) used="${used%Mi}" ;;
            *Gi) used=$(( ${used%Gi} * 1024 )) ;;
            *Ki) used=$(( ${used%Ki} / 1024 )) ;;
            *) used="" ;;
        esac
        mine="$(awk -v n="$name" 'NR == FNR { if ($2 == n) on[$1] = 1; next }
            ($1 in on) { v = $3; m = 0
                if (v ~ /Gi$/) m = v * 1024; else if (v ~ /Mi$/) m = v + 0; else if (v ~ /Ki$/) m = v / 1024
                s += m }
            END { printf "%d", s }' <(printf '%s\n' "$podnodes") <(printf '%s\n' "$podtops"))"
        if [[ -n "$used" && "$mine" =~ ^[0-9]+$ ]] && (( mine > 0 && mine < used )); then
            used=$(( used - mine ))
        fi
        [[ "$cp" == "true" ]] || cp="false"
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$(( cap / 1024 ))" "$used" "$cpu" "$cp" "$arch" "$ready"
    done < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.capacity.memory}{"\t"}{.status.capacity.cpu}{"\t"}{.status.nodeInfo.architecture}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.conditions[?(@.type=="DiskPressure")].status}{.status.conditions[?(@.type=="MemoryPressure")].status}{"\t"}{.metadata.labels.node-role\.kubernetes\.io/control-plane}{"\n"}{end}' 2>/dev/null)
    return 0
}

# split_plan -- fills PLAN_* arrays (main node first) from SPLIT_SIZE,
# SPLIT_NODES (optional filter), SPLIT_MAIN (optional) and SPLIT_RESERVE
# (per-node overrides). Dies, saying by how much, if the model won't fit.
SPLIT_RES_WORKER=1024     # MiB kept free on every node for the OS and k3s
SPLIT_RES_CP=1536         # control-plane nodes also run the API server & co.
SPLIT_RES_MAIN=1024       # extra on the main node: KV cache + prompt cache
SPLIT_WORK=256            # compute buffers each RPC server allocates
split_plan() {
    local size_mib=$(( SPLIT_SIZE / 1048576 ))
    local -a names=() avails=() cpus=() cps=() reserves=() frees=()
    local name cap used cpu cp arch ready reserve avail ov

    while IFS=$'\t' read -r name cap used cpu cp arch ready; do
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
        case "$arch" in amd64|arm64) ;; *) ny_warn "Skipping ${name}: architecture ${arch} isn't supported."; continue ;; esac
        if [[ -z "$used" ]]; then
            used=$(( cap * 30 / 100 ))
            ny_warn "No live memory stats for ${name} (is metrics-server working? try 'nodeyard nettest'); assuming ${used} MiB in use."
        fi
        reserve=$SPLIT_RES_WORKER
        if [[ "$cp" == "true" ]]; then reserve=$SPLIT_RES_CP; fi
        ov="${SPLIT_RESERVE[$name]:-}"
        if [[ -n "$ov" ]]; then reserve="$ov"; fi
        avail=$(( cap - used - reserve - SPLIT_WORK ))
        names+=("$name"); avails+=("$avail"); cpus+=("${cpu%%m*}"); cps+=("$cp"); reserves+=("$reserve"); frees+=("$(( cap - used ))")
    done <<<"${SPLIT_NODE_CACHE:-$(split_node_table)}"

    [[ "${#names[@]}" -gt 0 ]] || ny_die "No usable nodes."

    # main node: as given, else the non-control-plane node with the most room
    local i best=-1
    if [[ -n "${SPLIT_MAIN:-}" ]]; then
        for i in "${!names[@]}"; do
            if [[ "${names[i]}" == "$SPLIT_MAIN" ]]; then best=$i; fi
        done
        [[ $best -ge 0 ]] || ny_die "--main ${SPLIT_MAIN} isn't one of the usable nodes."
    else
        for i in "${!names[@]}"; do
            [[ "${cps[i]}" == "true" ]] && continue
            if [[ $best -lt 0 ]] || (( avails[i] > avails[best] )); then best=$i; fi
        done
        [[ $best -ge 0 ]] || best=0
        SPLIT_MAIN="${names[best]}"
    fi
    reserves[best]=$(( reserves[best] + SPLIT_RES_MAIN ))
    avails[best]=$(( avails[best] - SPLIT_RES_MAIN ))
    if (( frees[best] < SPLIT_RES_MAIN + 256 )); then
        ny_die "The main node ${names[best]} doesn't have enough free memory even to coordinate (${frees[best]} MiB free)."
    fi
    # A main node short on memory (but with a good disk/network) can just
    # coordinate: it reads the model and sends every layer to the others.
    # (Under 1 GiB a slice isn't worth the extra network hop per token.)
    if [[ "$SPLIT_MAIN_ONLY" -ne 1 ]] && (( avails[best] < 1024 )); then
        ny_warn "${names[best]} is low on memory, so it will only coordinate and hold no part of the model."
        SPLIT_MAIN_ONLY=1
    fi
    if [[ "$SPLIT_MAIN_ONLY" -eq 1 ]]; then avails[best]=0; fi

    local total=0
    for i in "${!names[@]}"; do
        if [[ $i -eq $best && "$SPLIT_MAIN_ONLY" -eq 1 ]]; then continue; fi
        if (( avails[i] < 512 )); then
            if [[ $i -eq $best ]]; then ny_die "The main node ${names[i]} doesn't have enough free memory (${frees[i]} MiB free)."; fi
            ny_warn "Leaving out ${names[i]}: only ${frees[i]} MiB free and ${reserves[i]} MiB is kept in reserve."
            avails[i]=0
            continue
        fi
        total=$(( total + avails[i] ))
    done

    local need=$(( size_mib * 103 / 100 ))

    if (( total < need )); then
        [[ "${SPLIT_PROBE:-0}" -eq 1 ]] && return 1
        ny_die "This model needs ~$(awk -v m="$need" 'BEGIN{printf "%.1f", m/1024}') GiB but only ~$(awk -v m="$total" 'BEGIN{printf "%.1f", m/1024}') GiB is free across the nodes after reserves. Pick a smaller quant, add nodes, or lower a node's reserve (--reserve NODE=GiB)."
    fi

    PLAN_NAMES=(); PLAN_SHARE=(); PLAN_CPU=(); PLAN_CP=(); PLAN_FREE=(); PLAN_RESERVE=()
    local order=("$best") share
    for i in "${!names[@]}"; do
        if [[ $i -ne $best ]] && (( avails[i] > 0 )); then order+=("$i"); fi
    done
    for i in "${order[@]}"; do
        share=0
        if (( avails[i] > 0 )); then
            share=$(( size_mib * avails[i] / total ))
            (( share < 1 )) && share=1
        fi
        PLAN_NAMES+=("${names[i]}"); PLAN_SHARE+=("$share"); PLAN_CPU+=("${cpus[i]}")
        PLAN_CP+=("${cps[i]}"); PLAN_FREE+=("${frees[i]}"); PLAN_RESERVE+=("${reserves[i]}")
    done
    return 0
}

# split_threads INDEX -- threads for a plan node: physical-ish cores (big SMT
# CPUs gain nothing from hyperthreads on memory-bound decode, and a laptop stays
# responsive), one fewer on control-plane nodes, or the --threads override.
split_threads() {
    local i="$1" t="${PLAN_CPU[$1]}" ov
    ov="${SPLIT_THREADS[${PLAN_NAMES[i]}]:-}"
    if [[ -n "$ov" ]]; then printf '%s\n' "$ov"; return 0; fi
    if (( t >= 8 )); then t=$(( t / 2 )); fi
    if [[ "${PLAN_CP[i]}" == "true" ]]; then t=$(( t > 1 ? t - 1 : 1 )); fi
    printf '%s\n' "$t"
    return 0
}

split_print_plan() {
    local size_gib i threads left extra
    size_gib=$(awk -v b="$SPLIT_SIZE" 'BEGIN{printf "%.1f", b/1073741824}')
    echo "Model: ${SPLIT_REPO} : ${SPLIT_FILE}  (${size_gib} GiB)"
    echo
    printf '  %-18s %-13s %9s %12s %10s %8s\n' "NODE" "ROLE" "FREE NOW" "MODEL SHARE" "LEFT FREE" "THREADS"
    for i in "${!PLAN_NAMES[@]}"; do
        threads="$(split_threads "$i")"
        extra=$SPLIT_WORK
        if [[ "${PLAN_SHARE[i]}" -eq 0 ]]; then extra=0; fi
        if [[ $i -eq 0 ]]; then extra=$(( extra + SPLIT_RES_MAIN )); fi
        left=$(( PLAN_FREE[i] - PLAN_SHARE[i] - extra ))
        printf '  %-18s %-13s %8.1fG %7.1fG %2d%% %9.1fG %8s\n' "${PLAN_NAMES[i]}" \
            "$(if [[ $i -eq 0 && "${PLAN_SHARE[i]}" -eq 0 ]]; then echo 'main only'; elif [[ $i -eq 0 ]]; then echo 'main + share'; else echo 'share'; fi)" \
            "$(awk -v m="${PLAN_FREE[i]}" 'BEGIN{print m/1024}')" \
            "$(awk -v m="${PLAN_SHARE[i]}" 'BEGIN{print m/1024}')" \
            "$(( PLAN_SHARE[i] * 100 * 1048576 / SPLIT_SIZE ))" \
            "$(awk -v m="$left" 'BEGIN{print m/1024}')" "$threads"
    done
    echo
    if [[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]]; then
        echo "Model file on ${PLAN_NAMES[0]}: ${SPLIT_MODEL_DIR}/${SPLIT_FILE}"
    fi
    echo "Every generated word passes through all of these nodes: they must all stay on."
    echo "Keep more free on a node (e.g. a laptop you use): --reserve NODE=GiB"
    return 0
}

# split_manifest FILE -- writes the full set of Kubernetes objects
split_manifest() {
    local out="$1" i n share mem_req mem_lim threads cache_arg rpc_list="" ts_list=""
    local fetch_script
    fetch_script="$(cat <<FETCH
          set -e
          [ -x /opt/llama/ggml-rpc-server ] && [ -x /opt/llama/llama-server ] && exit 0
          case "\$(uname -m)" in x86_64) P=x64;; aarch64) P=arm64;; *) echo "unsupported arch"; exit 1;; esac
          curl -fsSL --retry 5 -o /tmp/l.tgz "https://github.com/ggml-org/llama.cpp/releases/download/${SPLIT_LLAMA_BUILD}/llama-${SPLIT_LLAMA_BUILD}-bin-ubuntu-\$P.tar.gz"
          tar -xzf /tmp/l.tgz -C /opt/llama --strip-components=1
FETCH
)"

    {
        cat <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${SPLIT_NS}
  labels: {app.kubernetes.io/managed-by: nodeyard}
  annotations: {nodeyard/main: "${PLAN_NAMES[0]}", nodeyard/model-dir: "${SPLIT_MODEL_DIR}"}
---
apiVersion: batch/v1
kind: Job
metadata: {name: model-download, namespace: ${SPLIT_NS}}
spec:
  backoffLimit: 30
  template:
    spec:
      restartPolicy: OnFailure
      nodeSelector: {kubernetes.io/hostname: ${PLAN_NAMES[0]}}
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
        command:
        - sh
        - -c
        - |
          set -e
          cd /models
          if [ -f "\$F" ]; then echo "already downloaded"; exit 0; fi
          N=4; CH=\$(( (SIZE + N - 1) / N )); i=0
          # Disk check: the parts are joined in place, so the peak is the model
          # plus one part. Keep 12% of the disk free on top, or the kubelet
          # starts evicting pods (DiskPressure). Failing here retries later,
          # so freeing space lets it carry on by itself.
          have=0; for p in "\$F".part* "\$F.joining"; do [ -f "\$p" ] && have=\$(( have + \$(stat -c %s "\$p") )); done
          set -- \$(df -Pk /models | tail -1); cap=\$(( \$2 * 1024 )); free=\$(( \$4 * 1024 ))
          need=\$(( SIZE - have + CH + cap * 12 / 100 ))
          if [ "\$free" -lt "\$need" ]; then
            echo "NOT ENOUGH DISK: \$(( free / 1073741824 )) GiB free, need \$(( need / 1073741824 )) GiB (model + one part + 12% for the kubelet). Free space or pick another --main node."
            exit 1
          fi
          # Once joining has started the parts are being consumed: never refetch.
          [ -f "\$F.joining" ] && i=\$N
          while [ \$i -lt \$N ]; do
            (
              start=\$(( i * CH )); end=\$(( start + CH - 1 )); [ \$end -ge \$SIZE ] && end=\$(( SIZE - 1 ))
              want=\$(( end - start + 1 ))
              while :; do
                have=\$(stat -c %s "\$F.part\$i" 2>/dev/null || echo 0)
                [ "\$have" -ge "\$want" ] && break
                curl -fsL --connect-timeout 20 -r \$(( start + have ))-\$end "\$URL" >> "\$F.part\$i" || sleep 5
              done
            ) &
            i=\$(( i + 1 ))
          done
          wait
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
            [[ "$share" -gt 0 ]] || continue    # a main-only coordinator runs no RPC server
            cache_arg=', "-c"'
            if [[ "${#SPLIT_NO_CACHE[@]}" -gt 0 ]] && ny_in_list "${PLAN_NAMES[i]}" "${SPLIT_NO_CACHE[@]}"; then cache_arg=""; fi
            mem_req=$(( share + 256 ))
            mem_lim=$(( share + share * 15 / 100 + 512 ))
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
      containers:
      - name: rpc
        image: ghcr.io/ggml-org/llama.cpp:server
        command: ["/opt/llama/ggml-rpc-server", "-H", "0.0.0.0", "-p", "50052", "-t", "${threads}"${cache_arg}]
        env: [{name: LD_LIBRARY_PATH, value: /opt/llama}, {name: HOME, value: /cache}]
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
        if [[ "$SPLIT_NODEPORT" == "0" ]]; then svc_type="ClusterIP"; np_line=""; fi
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
metadata: {name: llama-main, namespace: ${SPLIT_NS}}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector: {matchLabels: {app: llama-main}}
  template:
    metadata: {labels: {app: llama-main}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${PLAN_NAMES[0]}}
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
      - name: wait-for-model
        image: busybox:1.36
        command: ["sh", "-c", "until [ -f '/models/${SPLIT_FILE}' ]; do echo 'waiting for the model download...'; sleep 30; done"]
        volumeMounts: [{name: models, mountPath: /models}]
      containers:
      - name: server
        image: ghcr.io/ggml-org/llama.cpp:server
        command: ["/opt/llama/llama-server"]
        args:
        - -m
        - "/models/${SPLIT_FILE}"
        - --alias
        - "${SPLIT_ALIAS}"
        - --rpc
        - "${rpc_list}"
        - -ngl
        - "999"
        - -sm
        - layer
        - -ts
        - "${ts_list}"
        - -c
        - "${SPLIT_CTX}"
        - -np
        - "1"
        - -t
        - "2"
        - -cram
        - "512"${think_args}${key_args}
        - --host
        - 0.0.0.0
        - --port
        - "8080"
        env: [{name: LD_LIBRARY_PATH, value: /opt/llama}]
        ports: [{containerPort: 8080}]
        readinessProbe:
          httpGet: {path: /health, port: 8080}
          periodSeconds: 10
        volumeMounts:
        - {name: bin, mountPath: /opt/llama}
        - {name: models, mountPath: /models}$( [[ -n "$SPLIT_API_KEY" ]] && printf '\n        - {name: api-key, mountPath: /secrets, readOnly: true}' || true )
      volumes:
      - {name: bin, hostPath: {path: /var/lib/nodeyard/llama.cpp/${SPLIT_LLAMA_BUILD}, type: DirectoryOrCreate}}
      - {name: models, hostPath: {path: ${SPLIT_MODEL_DIR}, type: DirectoryOrCreate}}$( [[ -n "$SPLIT_API_KEY" ]] && printf '\n      - {name: api-key, secret: {secretName: llama-api-key}}' || true )
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
    } > "$out"
    return 0
}

split_parse_flags() {
    SPLIT_MODEL_SPEC="$SPLIT_DEFAULT_MODEL"; SPLIT_MAIN=""; SPLIT_NODES=(); SPLIT_NODEPORT=31435
    SPLIT_CTX=16384; SPLIT_THINK="off"; SPLIT_API_KEY=""; SPLIT_ALIAS=""
    SPLIT_MODEL_DIR="$SPLIT_DEFAULT_MODEL_DIR"; SPLIT_MAIN_ONLY=0; SPLIT_NO_CACHE=()
    declare -gA SPLIT_RESERVE=()
    declare -gA SPLIT_THREADS=()
    local rn rg
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --reserve)
                [[ $# -ge 2 && "$2" == *=* ]] || ny_usage_error "--reserve needs NODE=GiB (e.g. --reserve archlinux=4)"
                rn="${2%%=*}"; rg="${2#*=}"
                [[ "$rg" =~ ^[0-9]+([.][0-9]+)?$ ]] || ny_usage_error "--reserve ${2}: the amount must be a number of GiB"
                SPLIT_RESERVE[$rn]="$(awk -v g="$rg" 'BEGIN{printf "%d", g*1024}')"
                shift 2 ;;
            --threads)
                [[ $# -ge 2 && "$2" =~ ^[^=]+=[0-9]+$ ]] || ny_usage_error "--threads needs NODE=N (e.g. --threads archlinux=6)"
                SPLIT_THREADS[${2%%=*}]="${2#*=}"
                shift 2 ;;
            --model) [[ $# -ge 2 ]] || ny_usage_error "--model needs owner/repo:file.gguf"; SPLIT_MODEL_SPEC="$2"; shift 2 ;;
            --main) [[ $# -ge 2 ]] || ny_usage_error "--main needs a node name"; SPLIT_MAIN="$2"; shift 2 ;;
            --nodes) [[ $# -ge 2 ]] || ny_usage_error "--nodes needs a comma-separated list"; IFS=',' read -r -a SPLIT_NODES <<<"$2"; shift 2 ;;
            --nodeport) [[ $# -ge 2 ]] || ny_usage_error "--nodeport needs a port (0 = cluster-only)"; SPLIT_NODEPORT="$2"; shift 2 ;;
            --ctx) [[ $# -ge 2 ]] || ny_usage_error "--ctx needs a number of tokens"; SPLIT_CTX="$2"; shift 2 ;;
            --think) [[ $# -ge 2 ]] || ny_usage_error "--think needs on|off"; SPLIT_THINK="$2"; shift 2 ;;
            --api-key) [[ $# -ge 2 ]] || ny_usage_error "--api-key needs a value"; SPLIT_API_KEY="$2"; ny_secret_register "$2"
                ny_warn "--api-key puts the key in your shell history; next time use --api-key-file."; shift 2 ;;
            --api-key-file)
                [[ $# -ge 2 && -r "$2" ]] || ny_usage_error "--api-key-file needs a readable file"
                SPLIT_API_KEY="$(ny_trim "$(<"$2")")"; ny_secret_register "$SPLIT_API_KEY"; shift 2 ;;
            --alias) [[ $# -ge 2 ]] || ny_usage_error "--alias needs a name"; SPLIT_ALIAS="$2"; shift 2 ;;
            --model-dir)
                [[ $# -ge 2 && "$2" =~ ^/[A-Za-z0-9._/-]+$ && "$2" != *..* ]] || ny_usage_error "--model-dir needs an absolute path on the main node (e.g. /srv/models)"
                SPLIT_MODEL_DIR="${2%/}"; shift 2 ;;
            --main-only) SPLIT_MAIN_ONLY=1; shift ;;
            --no-cache) [[ $# -ge 2 ]] || ny_usage_error "--no-cache needs a comma-separated list of nodes"; IFS=',' read -r -a SPLIT_NO_CACHE <<<"$2"; shift 2 ;;
            --yes|-y) NY_YES=1; shift ;;
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
        local q picked="" skipped=""
        SPLIT_NODE_CACHE="$(split_node_table)"
        for q in "${SPLIT_AUTO_QUANTS[@]}"; do
            split_parse_model "${SPLIT_AUTO_REPO}:${SPLIT_AUTO_PREFIX}${q}.gguf"
            split_model_info
            if ( SPLIT_PROBE=1 split_plan >/dev/null 2>&1 ); then picked="$q"; break; fi
            skipped+="${skipped:+, }${q} ($(awk -v b="$SPLIT_SIZE" 'BEGIN{printf "%.1f", b/1073741824}') GiB)"
        done
        [[ -n "$picked" ]] || { split_plan; ny_die "Not even the smallest version fits."; }
        ny_ok "Picked ${SPLIT_FILE}: the biggest version of Qwen3.6-35B-A3B that fits in the free memory right now."
        [[ -z "$skipped" ]] || echo "   Too big right now: ${skipped}"
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
    echo "Deploy it with: nodeyard ai split deploy --model ${SPLIT_MODEL_SPEC} --main ${SPLIT_MAIN}$( [[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]] && printf ' --model-dir %q' "$SPLIT_MODEL_DIR" || true )"
    return 0
}

# Early warning from the kubelet's own disk stats: the main node needs room for
# the model plus one download part, and must stay above the kubelet's eviction
# threshold. Only a warning, since the model may already be on that disk.
split_disk_check() {
    local node="${PLAN_NAMES[0]}" fs avail cap need
    if [[ "$SPLIT_MODEL_DIR" != "$SPLIT_DEFAULT_MODEL_DIR" ]]; then
        echo "(Disk space in ${SPLIT_MODEL_DIR} is checked by the download itself.)"
        return 0
    fi
    fs="$(kctl get --raw "/api/v1/nodes/${node}/proxy/stats/summary" 2>/dev/null | tr -d ' \n' | grep -o '"fs":{[^}]*}' | head -1 || true)"
    avail="$(printf '%s' "$fs" | grep -o '"availableBytes":[0-9]*' | cut -d: -f2 || true)"
    cap="$(printf '%s' "$fs" | grep -o '"capacityBytes":[0-9]*' | cut -d: -f2 || true)"
    [[ "$avail" =~ ^[0-9]+$ && "$cap" =~ ^[0-9]+$ ]] || return 0
    need=$(( SPLIT_SIZE + SPLIT_SIZE / 4 + cap * 12 / 100 ))
    if (( avail < need )); then
        ny_warn "${node} has $(( avail / 1073741824 )) GiB free disk; downloading needs about $(( need / 1073741824 )) GiB (model + one part + 12% the kubelet keeps free)."
        ny_warn "Fine if the model is already downloaded there; otherwise free space or use --main <node with more disk>."
    else
        ny_ok "Disk on ${node}: $(( avail / 1073741824 )) GiB free (needs about $(( need / 1073741824 )) GiB)."
    fi
    return 0
}

ai_split_deploy() {
    ny_need_kube
    split_parse_flags "$@"
    split_pick_model
    split_print_plan
    split_disk_check
    if kctl get namespace "$SPLIT_NS" >/dev/null 2>&1; then
        ny_warn "A split model is already deployed; this replaces it (the downloaded model file and weight caches are kept)."
    fi
    ny_confirm "Deploy this?" y || { echo "Cancelled."; return 0; }

    local manifest
    manifest="$(ny_mktemp)"
    split_manifest "$manifest"
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add apply "Apply the split-model deployment (main node ${PLAN_NAMES[0]})"
        ny_info "[dry-run] would apply:"; ny_redact "$(cat "$manifest")" >&2; printf '\n' >&2; return 0
    fi
    if kctl get namespace "$SPLIT_NS" >/dev/null 2>&1; then
        # Jobs are immutable, so replace the download Job too; the download
        # resumes from the part files already on disk.
        kctl -n "$SPLIT_NS" delete job,deployment,service,networkpolicy,secret --all --wait=true >/dev/null 2>&1 || true
    fi
    kctl apply --dry-run=server -f "$manifest" >/dev/null || { rm -f "$manifest"; ny_die "Kubernetes rejected the generated manifest."; }
    kctl apply -f "$manifest" || { rm -f "$manifest"; ny_die "Applying the manifest failed."; }
    rm -f "$manifest"

    ny_ok "Deployed. The main node (${PLAN_NAMES[0]}) downloads the model, then loads it and sends each node its share."
    echo "Watch progress:  nodeyard ai split status"
    echo "Try it:          nodeyard ai split test"
    if [[ "$SPLIT_NODEPORT" != "0" ]]; then
        echo "Chat page + API: http://<any node IP>:${SPLIT_NODEPORT}   (OpenAI-compatible API under /v1)"
    fi
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

    local dl phase got size
    dl="$(kctl -n "$SPLIT_NS" get pods -l job-name=model-download -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    phase="$(kctl -n "$SPLIT_NS" get pods -l job-name=model-download -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)"
    if [[ "$phase" == "Running" ]]; then
        size="$(kctl -n "$SPLIT_NS" get job model-download -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SIZE")].value}' 2>/dev/null || true)"
        got="$(kctl -n "$SPLIT_NS" exec "$dl" -- sh -c 'n=0; for f in /models/*.part* /models/*.joining; do [ -f "$f" ] && n=$((n + $(stat -c %s "$f"))); done; echo $n' 2>/dev/null || true)"
        local step
        step="$(kctl -n "$SPLIT_NS" logs "$dl" --tail=1 2>/dev/null || kctl -n "$SPLIT_NS" logs "$dl" --previous --tail=1 2>/dev/null || true)"
        if [[ "$step" == *joining* || "$step" == *verifying* ]]; then
            echo "Model download: finished; now ${step#* } (then the model loads)"
        elif [[ "$step" == *"NOT ENOUGH DISK"* ]]; then
            ny_warn "$step"
        elif [[ "$got" =~ ^[0-9]+$ && "$size" =~ ^[0-9]+$ && "$size" -gt 0 ]]; then
            echo "Model download: $(( got * 100 / size ))%  ($(( got / 1048576 )) of $(( size / 1048576 )) MiB)"
        else
            echo "Model download: running"
        fi
    elif [[ "$phase" == "Succeeded" ]]; then
        echo "Model download: done"
    fi

    local ready cip
    ready="$(kctl -n "$SPLIT_NS" get deploy llama-main -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    if [[ "$ready" == "1" ]]; then
        ny_ok "The model is loaded and serving."
        cip="$(kctl -n "$SPLIT_NS" get svc llama -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
        [[ -n "$cip" ]] && echo "In-cluster: http://${cip}:8080"
        local np
        np="$(kctl -n "$SPLIT_NS" get svc llama -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
        [[ -n "$np" ]] && echo "From your network: http://<any node IP>:${np}  (chat page; OpenAI API under /v1)"
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
            --prompt) [[ $# -ge 2 ]] || ny_usage_error "--prompt needs text"; prompt="$2"; shift 2 ;;
            --api-key) [[ $# -ge 2 ]] || ny_usage_error "--api-key needs a value"; key="$2"; ny_secret_register "$2"; shift 2 ;;
            --api-key-file) [[ $# -ge 2 && -r "$2" ]] || ny_usage_error "--api-key-file needs a readable file"; key="$(ny_trim "$(<"$2")")"; ny_secret_register "$key"; shift 2 ;;
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

ai_split_undeploy() {
    ny_need_kube
    local purge=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge) purge=1; shift ;;
            --yes|-y) NY_YES=1; shift ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard ai split undeploy [--purge]" ;;
        esac
    done
    kctl get namespace "$SPLIT_NS" >/dev/null 2>&1 || { echo "No split model is deployed."; return 0; }
    ny_confirm "Remove the split model$( [[ $purge -eq 1 ]] && echo ' AND delete the downloaded model + caches from every node' || true )?" n || { echo "Cancelled."; return 0; }

    local -a nodes=()
    local n main mdir
    while read -r n; do [[ -n "$n" ]] && nodes+=("$n"); done < <(kctl -n "$SPLIT_NS" get deploy -o jsonpath='{range .items[*]}{.spec.template.spec.nodeSelector.kubernetes\.io/hostname}{"\n"}{end}' 2>/dev/null | sort -u)
    main="$(kctl get namespace "$SPLIT_NS" -o jsonpath='{.metadata.annotations.nodeyard/main}' 2>/dev/null || true)"
    mdir="$(kctl get namespace "$SPLIT_NS" -o jsonpath='{.metadata.annotations.nodeyard/model-dir}' 2>/dev/null || true)"
    [[ "$mdir" =~ ^/[A-Za-z0-9._/-]+$ && "$mdir" != *..* ]] || mdir="$SPLIT_DEFAULT_MODEL_DIR"
    kctl delete namespace "$SPLIT_NS" --wait=true || true

    if [[ $purge -eq 1 && "${#nodes[@]}" -gt 0 ]]; then
        kctl create namespace nodeyard-cleanup >/dev/null 2>&1 || true
        for n in "${nodes[@]}"; do
            kctl -n nodeyard-cleanup apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata: {name: purge-$(ny_k8s_name "$n"), namespace: nodeyard-cleanup}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: ${n}}
  tolerations: [{operator: Exists}]
  containers:
  - name: c
    image: busybox:1.36
    command: ["sh", "-c", "rm -rf /data/models /data/rpc-cache /data/llama.cpp; rm -f /models/*.gguf /models/*.gguf.part* /models/*.gguf.joining /models/*.gguf.copying; echo cleaned"]
    volumeMounts: [{name: d, mountPath: /data}, {name: m, mountPath: /models}]
  volumes:
  - {name: d, hostPath: {path: /var/lib/nodeyard}}
  - {name: m, hostPath: {path: $( [[ "$n" == "$main" ]] && echo "$mdir" || echo "$SPLIT_DEFAULT_MODEL_DIR" ), type: DirectoryOrCreate}}
YAML
        done
        kctl -n nodeyard-cleanup wait --for=jsonpath='{.status.phase}'=Succeeded pod --all --timeout=180s >/dev/null 2>&1 || ny_warn "Some cleanup pods didn't finish; check: kubectl -n nodeyard-cleanup get pods"
        kctl delete namespace nodeyard-cleanup --wait=false >/dev/null 2>&1 || true
        ny_ok "Removed the model files and caches from: $(ny_join " " "${nodes[@]}")"
    fi
    ny_ok "Split model removed."
    return 0
}

