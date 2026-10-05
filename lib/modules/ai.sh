# shellcheck shell=bash
# AI workloads with Ollama (ported from k3s-manager 3.2):
#   ai install / uninstall  -- Ollama as a systemd service on this node only
#   ai deploy / undeploy    -- Ollama as a DaemonSet on chosen nodes behind one
#                              Service; concurrent requests are spread across
#                              machines by kube-proxy. Every node keeps its own
#                              full copy of each model (it does not split one
#                              model; see 'ai split' for that).
# The model-aware router, API keys and GPU setup come in a later phase.

ny_cmd "ai install" ai_install_cmd "AI" "Install Ollama on this node only" ai
ny_cmd "ai uninstall" ai_uninstall_cmd "AI" "Remove this node's local Ollama" ai
ny_cmd "ai deploy" ai_deploy_cmd "AI" "Run Ollama on chosen cluster nodes behind one service" ai
ny_cmd "ai undeploy" ai_undeploy_cmd "AI" "Remove the cluster-wide Ollama deployment" ai
ny_cmd "ai status" ai_status_cmd "AI" "Show AI deployments and the models on each node" ai
ny_cmd "ai nodes" ai_nodes_cmd "AI" "Show each node's architecture, memory and AI label" ai
ny_cmd "ai model install" ai_model_install_cmd "AI" "Download a model onto every AI node (or one with --node)" ai
ny_cmd "ai model list" ai_model_list_cmd "AI" "List the models on each AI node" ai
ny_cmd "ai model rm" ai_model_rm_cmd "AI" "Delete a model from every AI node (or one with --node)" ai
ny_cmd_alias "ai model pull" "ai model install"
ny_cmd_alias "ai model ls" "ai model list"
ny_cmd_alias "ai model remove" "ai model rm"
ny_cmd_alias "ai model delete" "ai model rm"

AI_NAMESPACE="ai-inference"
AI_OLLAMA_PORT=11434
AI_OLLAMA_INSTALLER="https://ollama.com/install.sh"
AI_NODE_LABEL="k3smgr.io/ai"

ai_arch_supported() {
    ny_detect_arch
    [[ "$NY_ARCH" == amd64 || "$NY_ARCH" == arm64 ]]
}

ai_install_cmd_help() {
    cat <<'HELP'
Usage: nodeyard ai install [--force]

Installs Ollama on this machine as a systemd service (API on port 11434),
using Ollama's official installer. For the whole cluster use 'ai deploy'.
HELP
}

ai_install_cmd() {
    ny_need_root
    ny_detect_all
    local force=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) force=1; shift ;;
            *) ny_usage_error "Unknown option for 'ai install': $1" ;;
        esac
    done
    if ! ai_arch_supported; then
        ny_warn "Ollama doesn't officially support ${NY_ARCH_RAW}."
        ny_offer "Try installing anyway?" n || ny_die "Ollama needs an amd64 or arm64 machine." "" "$NY_E_PRECONDITION"
    fi
    if have ollama && [[ "$force" -eq 0 ]]; then
        ny_ok "Ollama is already installed ($(ollama --version 2>/dev/null | head -n1))."
    else
        ny_confirm "Install Ollama on this machine (official installer from ollama.com)?" y || return 0
        local tmp
        tmp="$(ny_mktemp)"
        ny_download "$AI_OLLAMA_INSTALLER" "$tmp"
        ny_run sh "$tmp" || ny_die "The Ollama installer failed." "See its output above."
    fi
    [[ "$NY_INIT" == systemd ]] && { ny_service_enable ollama --now || ny_warn "Could not start the ollama service."; }
    firewall_open_ports "ollama API" "${AI_OLLAMA_PORT}/tcp"
    if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
        ny_ok "NVIDIA GPU found: Ollama will use it."
    else
        ny_info "No NVIDIA GPU found; Ollama runs on the CPU (fine for small models)."
    fi
    ny_ok "Ollama is installed (API on port ${AI_OLLAMA_PORT})."
    ny_hint "Download a model: sudo nodeyard ai model install llama3.2"
}

ai_uninstall_cmd() {
    ny_need_root
    local purge=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge-models) purge=1; shift ;;
            *) ny_usage_error "Unknown option for 'ai uninstall': $1" ;;
        esac
    done
    ny_confirm "Remove the local Ollama installation from this node?" n || return 0
    ny_run systemctl disable --now ollama >/dev/null 2>&1 || true
    ny_remove_file /etc/systemd/system/ollama.service
    ny_service_daemon_reload
    ny_remove_file /usr/local/bin/ollama
    ny_remove_file /usr/bin/ollama
    if [[ "$purge" -eq 1 ]]; then
        ny_run rm -rf /usr/share/ollama/.ollama /root/.ollama
        ny_ok "Removed the downloaded models."
    else
        ny_info "Downloaded models were kept (usually /usr/share/ollama/.ollama); --purge-models removes them."
    fi
    getent passwd ollama >/dev/null 2>&1 && { ny_run userdel ollama 2>/dev/null || true; }
    ny_ok "Ollama removed from this node."
}

ai_deploy_cmd_help() {
    cat <<'HELP'
Usage: nodeyard ai deploy [options]

Runs Ollama as a DaemonSet on the chosen nodes behind one Service. Run it on
a server. Each node keeps its own copy of every model, so models must fit on
the smallest node; concurrent requests are spread across the machines.

Options:
  --only NODE           Only this node (repeatable)
  --exclude NODE        Skip this node (repeatable)
  --min-memory-gb N     Skip nodes with less allocatable memory
  --memory-limit 6Gi    Memory limit per Ollama pod
  --nodeport PORT       Also expose the API on PORT of every node's address
  --image IMAGE         Ollama image (default ollama/ollama:latest)
HELP
}

ai_deploy_cmd() {
    ny_need_root
    ny_need_kube
    local -a only=() exclude=()
    local min_gb=0 mem_limit="" nodeport="" image="ollama/ollama:latest"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --only) ny_need_value "$1" $#; only+=("$2"); shift 2 ;;
            --exclude) ny_need_value "$1" $#; exclude+=("$2"); shift 2 ;;
            --min-memory-gb) ny_need_value "$1" $#; ny_valid_int "$2" 0 4096 || ny_usage_error "$NY_VALID_MSG"; min_gb="$2"; shift 2 ;;
            --memory-limit) ny_need_value "$1" $#; [[ "$2" =~ ^[0-9]+(Mi|Gi)$ ]] || ny_usage_error "--memory-limit must look like 6Gi or 512Mi."; mem_limit="$2"; shift 2 ;;
            --nodeport) ny_need_value "$1" $#; ny_valid_int "$2" 30000 32767 || ny_usage_error "--nodeport must be between 30000 and 32767."; nodeport="$2"; shift 2 ;;
            --image) ny_need_value "$1" $#; image="$2"; shift 2 ;;
            *) ny_usage_error "Unknown option for 'ai deploy': $1" ;;
        esac
    done

    local -a candidates=() selected=()
    local name arch mem_ki mem_gb skip
    mapfile -t candidates < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
    [[ "${#candidates[@]}" -gt 0 ]] || ny_die "No cluster nodes found." "Is k3s running? sudo nodeyard status"
    for name in "${candidates[@]}"; do
        [[ -n "$name" ]] || continue
        skip=0
        if [[ "${#only[@]}" -gt 0 ]] && ! ny_in_list "$name" "${only[@]}"; then skip=1; fi
        ny_in_list "$name" "${exclude[@]+"${exclude[@]}"}" && skip=1
        arch="$(kctl get node "$name" -o jsonpath='{.status.nodeInfo.architecture}' 2>/dev/null || true)"
        if [[ "$skip" -eq 0 && "$arch" != amd64 && "$arch" != arm64 ]]; then
            ny_warn "Skipping ${name}: the Ollama image doesn't support ${arch}."
            skip=1
        fi
        if [[ "$skip" -eq 0 && "$min_gb" != 0 ]]; then
            mem_ki="$(kctl get node "$name" -o jsonpath='{.status.allocatable.memory}' 2>/dev/null | sed 's/Ki$//' || true)"
            if [[ "$mem_ki" =~ ^[0-9]+$ ]]; then
                mem_gb=$((mem_ki / 1024 / 1024))
                if ((mem_gb < min_gb)); then
                    ny_warn "Skipping ${name}: about ${mem_gb} GiB allocatable, below --min-memory-gb ${min_gb}."
                    skip=1
                fi
            fi
        fi
        [[ "$skip" -eq 0 ]] && selected+=("$name")
    done
    [[ "${#selected[@]}" -gt 0 ]] || ny_die "No nodes matched." "Loosen --only/--exclude/--min-memory-gb, or check: nodeyard ai nodes"

    ny_info "Ollama will run on: $(ny_join ', ' "${selected[@]}")"
    local resources="" svc_type="ClusterIP" np_line=""
    [[ -n "$mem_limit" ]] && resources=$'\n        resources:\n          limits:\n            memory: "'"${mem_limit}"'"'
    if [[ -n "$nodeport" ]]; then
        svc_type="NodePort"
        np_line=$'\n    nodePort: '"${nodeport}"
    fi
    local manifest
    manifest="$(cat <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${AI_NAMESPACE}
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: ollama
  namespace: ${AI_NAMESPACE}
  labels: {app: ollama}
spec:
  selector:
    matchLabels: {app: ollama}
  template:
    metadata:
      labels: {app: ollama}
    spec:
      nodeSelector:
        ${AI_NODE_LABEL}: "true"
      tolerations:
      - {key: node-role.kubernetes.io/control-plane, effect: NoSchedule, operator: Exists}
      - {key: node-role.kubernetes.io/master, effect: NoSchedule, operator: Exists}
      containers:
      - name: ollama
        image: ${image}
        ports:
        - {containerPort: ${AI_OLLAMA_PORT}, name: http}
        env:
        - {name: OLLAMA_HOST, value: "0.0.0.0:${AI_OLLAMA_PORT}"}
        volumeMounts:
        - {name: models, mountPath: /root/.ollama}${resources}
      volumes:
      - name: models
        hostPath: {path: /var/lib/nodeyard/ollama, type: DirectoryOrCreate}
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: ${AI_NAMESPACE}
  labels: {app: ollama}
spec:
  type: ${svc_type}
  selector: {app: ollama}
  ports:
  - port: ${AI_OLLAMA_PORT}
    targetPort: ${AI_OLLAMA_PORT}
    protocol: TCP${np_line}
YAML
)"
    if [[ "$NY_DRY_RUN" -eq 1 ]]; then
        ny_plan_add apply "Label $(ny_join ', ' "${selected[@]}") and apply the Ollama DaemonSet/Service"
        ny_info "[dry-run] would label the nodes and apply:"
        printf '%s\n' "$manifest" >&2
        return 0
    fi
    ny_confirm "Deploy Ollama to these nodes?" y || return 0
    for name in "${candidates[@]}"; do
        if ny_in_list "$name" "${selected[@]}"; then
            ny_run "$(ny_path "$NY_K3S_BIN")" kubectl label node "$name" "${AI_NODE_LABEL}=true" --overwrite >/dev/null
        else
            ny_run "$(ny_path "$NY_K3S_BIN")" kubectl label node "$name" "${AI_NODE_LABEL}-" >/dev/null 2>&1 || true
        fi
    done
    if ny_simulating; then
        ny_plan_add apply "Apply the Ollama DaemonSet and Service"
    else
        printf '%s\n' "$manifest" | kctl apply -f - || ny_die "Applying the Ollama DaemonSet/Service failed." "Check: sudo nodeyard ai status"
        ny_step "Waiting for Ollama to start on each node (the first run downloads the image, which can take a while)"
        kctl rollout status daemonset/ollama -n "$AI_NAMESPACE" --timeout=300s || ny_warn "Not finished yet; check: sudo nodeyard ai status"
    fi
    local cip
    cip="$(kctl get svc ollama -n "$AI_NAMESPACE" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
    ny_ok "Ollama deployed on: $(ny_join ', ' "${selected[@]}")"
    ny_hint "In-cluster API (spread across those nodes): http://${cip:-<cluster-ip>}:${AI_OLLAMA_PORT}"
    [[ -n "$nodeport" ]] && ny_hint "From your network: http://<any node address>:${nodeport}"
    ny_hint "Download a model everywhere: sudo nodeyard ai model install llama3.2"
}

ai_undeploy_cmd() {
    ny_need_root
    ny_need_kube
    local keep_labels=0 keep_data=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --keep-labels) keep_labels=1; shift ;;
            --keep-data) keep_data=1; shift ;;
            *) ny_usage_error "Unknown option for 'ai undeploy': $1" ;;
        esac
    done
    ny_confirm "Remove the cluster-wide Ollama deployment (namespace ${AI_NAMESPACE})?" n || return 0
    ny_run "$(ny_path "$NY_K3S_BIN")" kubectl delete namespace "$AI_NAMESPACE" --ignore-not-found >/dev/null || true
    if [[ "$keep_labels" -eq 0 ]]; then
        local name
        while read -r name; do
            [[ -n "$name" ]] && { ny_run "$(ny_path "$NY_K3S_BIN")" kubectl label node "$name" "${AI_NODE_LABEL}-" >/dev/null 2>&1 || true; }
        done < <(kctl get nodes -l "${AI_NODE_LABEL}=true" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
    fi
    if [[ "$keep_data" -eq 0 ]]; then
        ny_info "Downloaded models are still on each node in /var/lib/nodeyard/ollama (they live on the host, not in the cluster)."
        ny_hint "To reclaim the space, on each node: sudo rm -rf /var/lib/nodeyard/ollama"
    fi
    ny_ok "Cluster-wide Ollama removed."
}

# ai_pods -- "pod<TAB>node" for every running Ollama pod.
ai_pods() {
    kctl_available || return 0
    kctl get pods -n "$AI_NAMESPACE" -l app=ollama --field-selector=status.phase=Running \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.nodeName}{"\n"}{end}' 2>/dev/null || true
}

ai_status_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    local shown=0 pod node
    if kctl_available && kctl get namespace "$AI_NAMESPACE" >/dev/null 2>&1; then
        shown=1
        printf '%s\n' "$(ny_color bold "Cluster AI deployment (namespace ${AI_NAMESPACE})")"
        kctl get pods -n "$AI_NAMESPACE" -o wide
        printf '\n'
        kctl get svc -n "$AI_NAMESPACE"
        printf '\n%s\n' "$(ny_color bold "Models per node")"
        while IFS=$'\t' read -r pod node; do
            [[ -n "$pod" ]] || continue
            printf -- '-- %s (%s) --\n' "$node" "$pod"
            kctl exec -n "$AI_NAMESPACE" "$pod" -- ollama list 2>/dev/null || printf '  (could not query; the pod may still be starting)\n'
        done < <(ai_pods)
    fi
    if have ollama || systemctl cat ollama.service >/dev/null 2>&1; then
        shown=1
        printf '\n%s\n' "$(ny_color bold "Local Ollama (this node)")"
        systemctl --no-pager status ollama 2>/dev/null | head -n 5 || true
        have ollama && { ollama list 2>/dev/null || true; }
    fi
    if [[ "$shown" -eq 0 ]]; then
        ny_info "No AI features are installed yet."
        ny_hint "One node:      sudo nodeyard ai install"
        ny_hint "Whole cluster: sudo nodeyard ai deploy   (on a server)"
    fi
    if kctl_available && kctl get namespace ai-split >/dev/null 2>&1; then
        printf '\n%s\n' "$(ny_color bold "Split model (experimental)")"
        printf '  Deployed; see: nodeyard ai split status\n'
    fi
    return 0
}

ai_nodes_cmd() {
    [[ $# -eq 0 ]] || ny_usage_error "Unexpected argument: $1"
    ny_need_kube
    local name arch ready mem label
    {
        printf 'NODE\tARCH\tREADY\tMEMORY\tAI\n'
        while IFS=$'\t' read -r name arch ready mem label; do
            [[ -n "$name" ]] || continue
            mem="${mem%Ki}"
            [[ "$mem" =~ ^[0-9]+$ ]] && mem="$(awk -v k="$mem" 'BEGIN{printf "%.1f GiB", k/1024/1024}')" || mem="?"
            [[ "$ready" == True ]] && ready="Ready" || ready="NotReady"
            printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$arch" "$ready" "$mem" "${label:-no}"
        done < <(kctl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.nodeInfo.architecture}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.status.allocatable.memory}{"\t"}{.metadata.labels.k3smgr\.io/ai}{"\n"}{end}' 2>/dev/null)
    } | ny_table --status READY
}

# ai_model_targets NODE -- fills AI_T_PODS / AI_T_NODES.
ai_model_targets() {
    local only="$1" pod node
    AI_T_PODS=()
    AI_T_NODES=()
    while IFS=$'\t' read -r pod node; do
        [[ -n "$pod" ]] || continue
        [[ -n "$only" && "$node" != "$only" ]] && continue
        AI_T_PODS+=("$pod")
        AI_T_NODES+=("$node")
    done < <(ai_pods)
    return 0
}

ai_model_parse() {
    AI_M_NAME="" AI_M_NODE=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --node) ny_need_value "$1" $#; AI_M_NODE="$2"; shift 2 ;;
            -*) ny_usage_error "Unknown option: $1" ;;
            *)
                [[ -z "$AI_M_NAME" ]] || ny_usage_error "Unexpected argument: $1"
                [[ "$1" =~ ^[A-Za-z0-9._:/-]+$ ]] || ny_usage_error "'$1' doesn't look like a model name (e.g. llama3.2 or qwen2.5:7b)."
                AI_M_NAME="$1"
                shift
                ;;
        esac
    done
    return 0
}

ai_model_install_cmd() {
    ny_need_root
    ai_model_parse "$@"
    [[ -n "$AI_M_NAME" ]] || ny_usage_error "Say which model to download." "nodeyard ai model install MODEL [--node NODE]"
    ai_model_targets "$AI_M_NODE"
    local i
    if [[ "${#AI_T_PODS[@]}" -gt 0 ]]; then
        ny_step "Downloading ${AI_M_NAME} onto ${#AI_T_PODS[@]} node(s)"
        for ((i = 0; i < ${#AI_T_PODS[@]}; i++)); do
            printf -- '-- %s --\n' "${AI_T_NODES[i]}"
            ny_run "$(ny_path "$NY_K3S_BIN")" kubectl exec -n "$AI_NAMESPACE" "${AI_T_PODS[i]}" -- ollama pull "$AI_M_NAME" ||
                ny_warn "Download failed on ${AI_T_NODES[i]}."
        done
        ny_ok "Done. Check with: nodeyard ai model list"
        return 0
    fi
    if have ollama; then
        ny_run ollama pull "$AI_M_NAME"
        ny_ok "Downloaded ${AI_M_NAME} into the local Ollama."
        return 0
    fi
    ny_die "There is no Ollama here to download into." "Install it first: sudo nodeyard ai install (this node) or sudo nodeyard ai deploy (cluster)." "$NY_E_PRECONDITION"
}

ai_model_list_cmd() {
    ai_model_parse "$@"
    ai_model_targets "$AI_M_NODE"
    local i
    if [[ "${#AI_T_PODS[@]}" -gt 0 ]]; then
        for ((i = 0; i < ${#AI_T_PODS[@]}; i++)); do
            printf -- '-- %s --\n' "${AI_T_NODES[i]}"
            kctl exec -n "$AI_NAMESPACE" "${AI_T_PODS[i]}" -- ollama list 2>/dev/null || printf '  (could not query)\n'
        done
        return 0
    fi
    have ollama && {
        ollama list
        return 0
    }
    ny_die "There is no Ollama on this node or in the cluster." "Install it: sudo nodeyard ai install / ai deploy" "$NY_E_PRECONDITION"
}

ai_model_rm_cmd() {
    ny_need_root
    ai_model_parse "$@"
    [[ -n "$AI_M_NAME" ]] || ny_usage_error "Say which model to delete." "nodeyard ai model rm MODEL [--node NODE]"
    ai_model_targets "$AI_M_NODE"
    ny_confirm "Delete ${AI_M_NAME} from ${AI_M_NODE:-every AI node}?" y || return 0
    local i
    if [[ "${#AI_T_PODS[@]}" -gt 0 ]]; then
        for ((i = 0; i < ${#AI_T_PODS[@]}; i++)); do
            ny_run "$(ny_path "$NY_K3S_BIN")" kubectl exec -n "$AI_NAMESPACE" "${AI_T_PODS[i]}" -- ollama rm "$AI_M_NAME" ||
                ny_warn "Delete failed on ${AI_T_NODES[i]}."
        done
        return 0
    fi
    have ollama && {
        ny_run ollama rm "$AI_M_NAME"
        return 0
    }
    ny_die "There is no Ollama on this node or in the cluster." "" "$NY_E_PRECONDITION"
}
