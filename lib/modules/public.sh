# shellcheck shell=bash
# shellcheck disable=SC2034 # NY_YES is read by the core
# Public access through Tailscale Funnel: the dashboard and the model's API
# reachable from anywhere on the internet (no Tailscale needed on the device
# you use), at https://<this machine>.<tailnet>.ts.net with a real certificate.
#
#   dashboard   https://NAME.ts.net:8443/      -> 127.0.0.1:<dashboard port>
#   model API   https://NAME.ts.net:10000/v1   -> the gate's public port,
#               127.0.0.1:31436, where the API key is ALWAYS needed
#
# Funnel delivers internet traffic from 127.0.0.1, so it must never point at
# anything that trusts loopback without a password or key.

ny_cmd "public on" public_on_cmd "Network" "Reach the dashboard and model API from anywhere on the internet (Tailscale Funnel)" public
ny_cmd "public off" public_off_cmd "Network" "Stop public access (Tailscale Funnel)" public
ny_cmd "public status" public_status_cmd "Network" "Show what is reachable from the internet, and its addresses" public json

PUBLIC_DASH_PORT=8443
PUBLIC_API_PORT=10000

public_on_cmd_help() {
    cat <<'HELP'
Usage: nodeyard public on [--dashboard] [--api] [--yes]
       nodeyard public off [--dashboard] [--api]
       nodeyard public status [--json]

Makes the dashboard and/or the split model's API reachable from anywhere on
the internet through Tailscale Funnel: from a phone on mobile data, a work
laptop or any server, with no Tailscale app needed there. You get HTTPS
addresses with a real certificate:

  dashboard   https://<this machine>.<your tailnet>.ts.net:8443/
  model API   https://<this machine>.<your tailnet>.ts.net:10000/v1

Without --dashboard or --api, both are turned on.

Safety:
  - The dashboard always asks for its password. It must be a strong one
    (a generated one, or 16+ characters): sudo nodeyard dashboard password --random
    Wrong passwords from the internet are counted per visitor and can't lock
    you out on Tailscale or your own network.
  - The model API always needs the model's API key from the internet, even
    though the model gate lets your own network in without one.
  - Anyone can see that the address exists. Turn it off when you don't need it:
    sudo nodeyard public off

Funnel has to be allowed for this machine in your tailnet's access policy
(Tailscale admin console > Access controls: the "funnel" node attribute).
HELP
}
public_off_cmd_help() { public_on_cmd_help; }
public_status_cmd_help() { public_on_cmd_help; }

# public_ts_json -- this machine's Tailscale status, or dies explaining why not
public_ts_json() {
    command -v tailscale >/dev/null 2>&1 || ny_die "Tailscale isn't installed on this machine." "Install it: https://tailscale.com/download, then: sudo tailscale up" "$NY_E_PRECONDITION"
    local st
    st="$(tailscale status --json 2>/dev/null || true)"
    [[ "$(jq -r '.BackendState // empty' <<<"$st" 2>/dev/null)" == "Running" ]] ||
        ny_die "Tailscale isn't connected on this machine." "Connect it: sudo tailscale up" "$NY_E_PRECONDITION"
    printf '%s\n' "$st"
    return 0
}

# public_name -- this machine's name on the internet (NAME.tailnet.ts.net)
public_name() {
    jq -r '.Self.DNSName // empty' <<<"$1" | sed 's/\.$//'
    return 0
}

public_parse() {
    PUB_DASH=0
    PUB_API=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dashboard) PUB_DASH=1 ;;
            --api) PUB_API=1 ;;
            --yes | -y) NY_YES=1 ;;
            *) ny_usage_error "Unknown option: $1" "nodeyard public on|off [--dashboard] [--api]" ;;
        esac
        shift
    done
    if [[ $PUB_DASH -eq 0 && $PUB_API -eq 0 ]]; then
        PUB_DASH=1
        PUB_API=1
    fi
    return 0
}

# public_password_strong -- the dashboard password is generated or 16+ characters
public_password_strong() {
    local pw
    ny_secret_exists "$DASH_PW_NAME" || return 1
    pw="$(ny_secret_get "$DASH_PW_NAME")"
    [[ "$pw" =~ ^[0-9A-F]{4}(-[0-9A-F]{4}){5}$ || ${#pw} -ge 16 ]]
}

public_on_cmd() {
    ny_need_root
    public_parse "$@"
    local st name dport listen
    st="$(public_ts_json)"
    name="$(public_name "$st")"
    [[ -n "$name" ]] || ny_die "Tailscale didn't say what this machine is called."
    jq -e '(.Self.CapMap // {}) | has("funnel")' <<<"$st" >/dev/null 2>&1 ||
        ny_die "Funnel isn't allowed for this machine in your tailnet." \
            "Allow it in the Tailscale admin console (Access controls: add the \"funnel\" node attribute for this machine), then run this again." "$NY_E_PRECONDITION"

    local -a todo=()
    if [[ $PUB_DASH -eq 1 ]]; then
        dashboard_is_active || ny_die "The dashboard isn't running." "Start it: sudo nodeyard dashboard start" "$NY_E_PRECONDITION"
        dport="$(dashboard_unit_value port)"
        listen="$(dashboard_unit_value listen)"
        [[ "$dport" =~ ^[0-9]+$ ]] || dport=9092
        [[ ",${listen}," == *,local,* || ",${listen}," == *,127.0.0.1,* || ",${listen}," == *,all,* ]] ||
            ny_die "The dashboard doesn't listen on this machine (127.0.0.1), which Funnel needs." "Restart it with: sudo nodeyard dashboard start --listen auto" "$NY_E_PRECONDITION"
        if dashboard_weak_ok; then
            public_password_strong || ny_warn "The dashboard password is weak and dashboard.weak-password is on: someone on the internet could guess it. (Wrong guesses are limited to 5 per visitor and 30 in total every 5 minutes.)"
        else
            public_password_strong || ny_die "The dashboard password is too weak to put on the internet." \
                "Make a strong one first: sudo nodeyard dashboard password --random   (or 16+ characters of your own)
  Or allow it anyway: sudo nodeyard config set dashboard.weak-password true" "$NY_E_PRECONDITION"
        fi
        todo+=("dashboard|${PUBLIC_DASH_PORT}|http://127.0.0.1:${dport}")
    fi
    if [[ $PUB_API -eq 1 ]]; then
        ny_need_kube
        kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" >/dev/null 2>&1 ||
            ny_die "The model gate isn't installed; the public model API goes through it." "Install it: sudo nodeyard ai gate install" "$NY_E_PRECONDITION"
        [[ -n "$(kctl -n "$SPLIT_NS" get daemonset "$SPLIT_GATE_DS" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_PORT")].value}' 2>/dev/null || true)" ]] ||
            ny_die "The model gate is from an older nodeyard without a public port." "Update it: sudo nodeyard ai gate install --yes" "$NY_E_PRECONDITION"
        kctl -n "$SPLIT_NS" get secret llama-api-key >/dev/null 2>&1 ||
            ny_die "The model has no API key, so it can't be put on the internet safely." "Deploy it with a key (the dashboard does this), or: sudo nodeyard ai split key --rotate" "$NY_E_PRECONDITION"
        todo+=("model API|${PUBLIC_API_PORT}|http://127.0.0.1:${SPLIT_GATE_PUBLIC_PORT}")
    fi

    echo "This makes the following reachable by ANYONE on the internet (password/key still required):"
    local t what port target
    for t in "${todo[@]}"; do
        IFS='|' read -r what port target <<<"$t"
        printf '  %-10s https://%s:%s/%s\n' "$what" "$name" "$port" "$([[ "$what" == "model API" ]] && echo v1 || true)"
    done
    ny_confirm "Turn on public access?" n || {
        echo "Cancelled."
        return 0
    }
    for t in "${todo[@]}"; do
        IFS='|' read -r what port target <<<"$t"
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            ny_plan_add run "tailscale funnel --bg --https=${port} ${target}"
            continue
        fi
        ny_run tailscale funnel --bg --https="$port" "$target" >/dev/null ||
            ny_die "Tailscale refused to publish the ${what}." "See: tailscale funnel status"
        ny_ok "${what} is public: https://${name}:${port}/$([[ "$what" == "model API" ]] && echo v1 || true)"
    done
    [[ "$NY_DRY_RUN" -eq 1 ]] && return 0
    ny_hint "The first visit can take a few seconds while Tailscale gets the HTTPS certificate."
    ny_hint "Turn it off any time: sudo nodeyard public off"
    return 0
}

public_off_cmd() {
    ny_need_root
    public_parse "$@"
    public_ts_json >/dev/null
    local port
    for port in $([[ $PUB_DASH -eq 1 ]] && echo "$PUBLIC_DASH_PORT") $([[ $PUB_API -eq 1 ]] && echo "$PUBLIC_API_PORT"); do
        if [[ "$NY_DRY_RUN" -eq 1 ]]; then
            ny_plan_add run "tailscale funnel --https=${port} off"
            continue
        fi
        ny_run tailscale funnel --https="$port" off >/dev/null 2>&1 || true
    done
    [[ "$NY_DRY_RUN" -eq 1 ]] || ny_ok "Public access is off$([[ $PUB_DASH -eq 1 && $PUB_API -eq 0 ]] && echo ' for the dashboard' || true)$([[ $PUB_API -eq 1 && $PUB_DASH -eq 0 ]] && echo ' for the model API' || true)."
    return 0
}

# public_state -> JSON {"name":..., "dashboard": url|null, "api": url|null}
public_state() {
    local st name cfg
    st="$(tailscale status --json 2>/dev/null || true)"
    name="$(public_name "$st")"
    cfg="$(tailscale funnel status --json 2>/dev/null || true)"
    jq -e . >/dev/null 2>&1 <<<"$cfg" || cfg='{}'
    jq -cn --arg n "$name" --argjson c "$cfg" --arg dp "$PUBLIC_DASH_PORT" --arg ap "$PUBLIC_API_PORT" '
        def on(p): (($c.AllowFunnel // {}) | has($n + ":" + p));
        {name: $n,
         dashboard: (if on($dp) then "https://\($n):\($dp)/" else null end),
         api: (if on($ap) then "https://\($n):\($ap)/v1" else null end)}' 2>/dev/null || printf '{"name":"%s","dashboard":null,"api":null}\n' "$name"
    return 0
}

public_status_cmd() {
    ny_need_root
    public_ts_json >/dev/null
    local s
    s="$(public_state)"
    if [[ "$NY_JSON" -eq 1 ]]; then
        ny_json_out "$(jq -c '. + {ok: true}' <<<"$s")"
        return 0
    fi
    local d a
    d="$(jq -r '.dashboard // empty' <<<"$s")"
    a="$(jq -r '.api // empty' <<<"$s")"
    if [[ -z "$d" && -z "$a" ]]; then
        echo "Nothing is public. Turn it on: sudo nodeyard public on"
        return 0
    fi
    [[ -n "$d" ]] && echo "Dashboard:  ${d}   (password needed)"
    [[ -n "$a" ]] && echo "Model API:  ${a}   (API key needed)"
    echo "Turn off: sudo nodeyard public off"
    return 0
}
