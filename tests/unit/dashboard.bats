#!/usr/bin/env bats
# The dashboard: its commands, its menu entry, and the web server (demo data).

setup() {
    load ../helpers/common
    ny_cmd_setup
    SERVER="${NY_REPO_ROOT}/share/nodeyard/dashboard/server.py"
    SRV_PID=""
}

teardown() {
    [[ -z "$SRV_PID" ]] || kill "$SRV_PID" 2>/dev/null || true
}

# start_server [ARGS...] -- run the demo server on a free port; sets PORT.
start_server() {
    python3 "$SERVER" --demo --port 0 --interval 1 "$@" >"${BATS_TEST_TMPDIR}/server.log" 2>&1 &
    SRV_PID=$!
    local i
    PORT=""
    for ((i = 0; i < 100; i++)); do
        PORT="$(sed -n 's/.*listening on http:\/\/[^:]*:\([0-9][0-9]*\).*/\1/p' "${BATS_TEST_TMPDIR}/server.log" | head -n1)"
        [[ -n "$PORT" ]] && return 0
        sleep 0.1
    done
    cat "${BATS_TEST_TMPDIR}/server.log" >&2
    return 1
}

# --- commands ---------------------------------------------------------------------

@test "dashboard start --dry-run shows a hardened service listening on this machine and Tailscale, with a sign-in password" {
    run ny_cmd_run dashboard start --dry-run --yes
    assert_success
    assert_output --partial "nodeyard-dashboard.service"
    assert_output --partial "server.py --port 9092 --listen local,100.101.102.103"
    assert_output --partial "--password-file /etc/nodeyard/secrets/dashboard-password"
    assert_output --partial "After=network-online.target k3s.service tailscaled.service"
    assert_output --partial "NoNewPrivileges=true"
    assert_output --partial "ProtectSystem=strict"
    assert_output --partial "RestrictAddressFamilies=AF_INET AF_INET6"
    assert_output --partial "Open it in your browser: http://100.101.102.103:9092"
    assert_output --partial "sudo nodeyard dashboard password"
}

@test "dashboard start --listen local only listens here and explains the SSH tunnel" {
    run ny_cmd_run dashboard start --listen local --dry-run --yes
    assert_success
    assert_output --partial "--listen local "
    refute_output --partial "100.101.102.103"
    assert_output --partial "listens on this machine only"
    assert_output --partial "ssh -L 9092:localhost:9092"
}

@test "dashboard start --port puts the port in the service and the address" {
    run ny_cmd_run dashboard start --port 9100 --dry-run --yes
    assert_success
    assert_output --partial "--port 9100"
    assert_output --partial "http://100.101.102.103:9100"
}

@test "dashboard start --listen takes addresses and refuses nonsense" {
    run ny_cmd_run dashboard start --listen local,192.168.1.10 --dry-run --yes
    assert_success
    assert_output --partial "--listen local,192.168.1.10"
    assert_output --partial "http://192.168.1.10:9092"
    run ny_cmd_run dashboard start --listen banana --dry-run --yes
    assert_failure 2
    assert_output --partial "isn't local, tailscale, all or an IPv4 address"
}

@test "the dashboard password: none before start; start makes a random one and keeps it private" {
    run ny_cmd_run dashboard password --show
    assert_failure 3
    assert_output --partial "no dashboard password yet"
    run ny_cmd_run dashboard start --yes
    assert_success
    run --separate-stderr ny_cmd_run dashboard password --show
    assert_success
    [[ "$output" =~ ^[0-9A-F]{4}(-[0-9A-F]{4}){5}$ ]] || fail "unexpected password format: $output"
    local pwfile="${DEMO_ROOT}/etc/nodeyard/secrets/dashboard-password"
    assert_equal "$(stat -c '%a' "$pwfile" 2>/dev/null || stat -f '%Lp' "$pwfile")" 600
}

@test "a dashboard key from an earlier version carries over as the password" {
    mkdir -p "${DEMO_ROOT}/etc/nodeyard/secrets"
    printf '%s' "ABCD-1234-ABCD-1234-ABCD-1234" >"${DEMO_ROOT}/etc/nodeyard/secrets/dashboard-key"
    chmod 600 "${DEMO_ROOT}/etc/nodeyard/secrets/dashboard-key"
    run ny_cmd_run dashboard start --yes
    assert_success
    run --separate-stderr ny_cmd_run dashboard password --show
    assert_output "ABCD-1234-ABCD-1234-ABCD-1234"
    [[ ! -e "${DEMO_ROOT}/etc/nodeyard/secrets/dashboard-key" ]] || fail "the old key file was left behind"
}

@test "you can choose your own password, and starting again keeps it" {
    run ny_cmd_run dashboard start --yes
    assert_success
    run bash -c 'printf "%s" huskiboi | "$0" --demo dashboard password --stdin' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    assert_output --partial "Password saved"
    run --separate-stderr ny_cmd_run dashboard password --show
    assert_output huskiboi
    run ny_cmd_run dashboard start --yes
    assert_success
    run --separate-stderr ny_cmd_run dashboard password --show
    assert_output huskiboi
    run --separate-stderr ny_cmd_run dashboard password --show --json
    printf '%s' "$output" | jq -e '.ok == true and .password == "huskiboi"' >/dev/null
}

@test "passwords that are too short or contradictory options are refused" {
    run bash -c 'printf "%s" abc | "$0" --demo dashboard password --stdin' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_failure 2
    assert_output --partial "at least 6 characters"
    run ny_cmd_run dashboard password --show --random
    assert_failure 2
    run bash -c 'printf "" | "$0" --demo dashboard password --stdin' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_failure 2
}

@test "without a terminal or --stdin, setting a password explains how" {
    run bash -c 'NODEYARD_UI=none "$0" --demo dashboard password </dev/null' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_failure 3
    assert_output --partial "--stdin"
}

@test "--random makes a new generated password and prints it once" {
    run ny_cmd_run dashboard start --yes
    run --separate-stderr ny_cmd_run dashboard password --show
    local first="$output"
    run --separate-stderr ny_cmd_run dashboard password --random
    assert_success
    [[ "$output" =~ ^[0-9A-F]{4}(-[0-9A-F]{4}){5}$ && "$output" != "$first" ]] || fail "not a new generated password: $output"
}

@test "passwords never appear in nodeyard's log" {
    run ny_cmd_run dashboard start --yes
    assert_success
    run bash -c 'printf "%s" huskiboi | "$0" --demo dashboard password --stdin' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    run --separate-stderr ny_cmd_run dashboard password --show
    run grep -r -F -e huskiboi "${DEMO_ROOT}/var/log/nodeyard"
    assert_failure
}

@test "dashboard run refuses a port that is already taken" {
    start_server
    run ny_cmd_run dashboard run --port "$PORT"
    assert_failure 3
    assert_output --partial "already listening"
}

@test "dashboard start rejects a bad port and a bad interval" {
    run ny_cmd_run dashboard start --port 99999 --dry-run
    assert_failure 2
    assert_output --partial "not a valid port"
    run ny_cmd_run dashboard start --interval 0 --dry-run
    assert_failure 2
    run ny_cmd_run dashboard start --bogus --dry-run
    assert_failure 2
}

@test "dashboard stop with nothing installed says so" {
    run ny_cmd_run dashboard stop --yes
    assert_success
    assert_output --partial "isn't installed"
}

@test "dashboard status --json reports the state" {
    run --separate-stderr ny_cmd_run dashboard status --json
    assert_success
    printf '%s' "$output" | jq -e '.ok == true and .running == false and .installed == false and .port == 9092 and .url == "http://localhost:9092"' >/dev/null
}

@test "plain 'dashboard' is the same as 'dashboard status'" {
    run ny_cmd_run dashboard
    assert_success
    assert_output --partial "isn't running"
}

@test "dashboard status --json reports where it listens once installed" {
    run ny_cmd_run dashboard start --yes
    assert_success
    run --separate-stderr ny_cmd_run dashboard status --json
    printf '%s' "$output" | jq -e '.installed == true and .listen == "local,100.101.102.103" and .url == "http://100.101.102.103:9092"' >/dev/null
}

@test "dashboard run --dry-run in demo mode uses simulated data" {
    run ny_cmd_run dashboard run --dry-run
    assert_success
    assert_output --partial "server.py"
    assert_output --partial "--demo"
}

@test "the dashboard has its own group in help" {
    run ny_cmd_run help
    assert_success
    assert_output --regexp 'Dashboard
  dashboard status'
}

# --- menu -------------------------------------------------------------------------

@test "the main menu offers the dashboard on a server and its submenu works" {
    run bash -c 'printf "dashboard\nstatus\n\nback\nq\n" | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain NODEYARD_COLOR=never "$0" --demo menu' "${NY_REPO_ROOT}/bin/nodeyard"
    assert_success
    assert_output --partial "Web dashboard"
    assert_output --partial "port 9092"
    assert_output --partial "isn't running"
}

# --- the server -------------------------------------------------------------------

@test "server: listening beyond this machine needs a sign-in key" {
    run python3 "$SERVER" --demo --listen 0.0.0.0 --port 0
    assert_failure
    assert_output --partial "needs a sign-in password"
    run python3 "$SERVER" --demo --listen banana
    assert_failure
    assert_output --partial "isn't local, all or an IP address"
}

@test "server: the demo never listens beyond this machine, even with a password" {
    printf 'ABCD-EF01-2345-6789-ABCD-EF01\n' >"${BATS_TEST_TMPDIR}/key"
    run python3 "$SERVER" --demo --listen 0.0.0.0 --password-file "${BATS_TEST_TMPDIR}/key" --port 0
    assert_failure
    assert_output --partial "only listens on this machine"
}

@test "server: health, state, history and the page itself" {
    start_server --nodeyard-version 9.9.9
    run curl -fsS "http://127.0.0.1:${PORT}/api/health"
    assert_success
    printf '%s' "$output" | jq -e '.ok == true' >/dev/null
    run curl -fsS "http://127.0.0.1:${PORT}/api/state"
    printf '%s' "$output" | jq -e '.ok == true and .mode == "demo" and .version == "9.9.9" and .auth == false and (.state.nodes | length) == 4 and .totals.nodes_ready == 4
        and (.state.nodes[0].internal_ip | test("^192[.]168")) and (.alerts | length) > 0' >/dev/null
    run curl -fsS "http://127.0.0.1:${PORT}/api/history?points=10"
    printf '%s' "$output" | jq -e '(.points | length) == 10' >/dev/null
    run curl -fsS "http://127.0.0.1:${PORT}/"
    assert_output --partial "nodeyard dashboard"
    run curl -fsS "http://127.0.0.1:${PORT}/app.js"
    assert_success
}

@test "server: nothing but sign-in and AI accepts POST/PUT/DELETE" {
    start_server
    local m
    for m in POST PUT DELETE PATCH; do
        run curl -s -o /dev/null -w '%{http_code}' -X "$m" "http://127.0.0.1:${PORT}/api/state"
        assert_output 405
    done
}

@test "server: only answers requests addressed to localhost (DNS rebinding)" {
    start_server
    run curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "http://127.0.0.1:${PORT}/api/state"
    assert_output 403
    run curl -s -o /dev/null -w '%{http_code}' -H "Host: localhost:${PORT}" "http://127.0.0.1:${PORT}/api/health"
    assert_output 200
}

@test "server: cannot read files outside its web folder" {
    start_server
    run curl -s --path-as-is -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/../server.py"
    assert_output 404
    run curl -s --path-as-is -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/%2e%2e/server.py"
    assert_output 404
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/kube.py"
    assert_output 404
}

@test "server: sends security headers and no CORS" {
    start_server
    run curl -sI "http://127.0.0.1:${PORT}/api/state"
    assert_output --partial "X-Content-Type-Options: nosniff"
    assert_output --partial "X-Frame-Options: DENY"
    assert_output --regexp "Cache-Control: no-store"
    refute_output --partial "Access-Control-Allow-Origin"
    run curl -sI "http://127.0.0.1:${PORT}/"
    assert_output --partial "Content-Security-Policy: default-src 'none'; script-src 'self'"
}

@test "server: logs are validated and returned" {
    start_server
    run curl -s "http://127.0.0.1:${PORT}/api/logs?ns=home&pod=zigbee2mqtt-0&lines=5"
    printf '%s' "$output" | jq -e '.ok == true and (.text | contains("Zigbee2MQTT"))' >/dev/null
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/logs?ns=..%2Fx&pod=p"
    assert_output 400
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/logs?ns=home&pod=a;b"
    assert_output 400
}

KEY="ABCD-EF01-2345-6789-ABCD-EF01"
JSON_HDRS=(-H 'Content-Type: application/json' -H 'X-Nodeyard: 1')

start_signin_server() {
    printf '%s\n' "$KEY" >"${BATS_TEST_TMPDIR}/key"
    start_server --password-file "${BATS_TEST_TMPDIR}/key" "$@"
}

login() { # login KEY -> cookie jar
    curl -s -c "${BATS_TEST_TMPDIR}/jar" -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/login" "${JSON_HDRS[@]}" -d "{\"password\":\"$1\"}"
}

# --- AI features and node agents (demo back end) ----------------------------------------

post() { # post PATH JSON -> response body
    curl -s -X POST "http://127.0.0.1:${PORT}$1" "${JSON_HDRS[@]}" -d "$2"
}

@test "ai: models to chat with, search results, file lists and Ollama models" {
    start_server
    run curl -s "http://127.0.0.1:${PORT}/api/ai/targets"
    printf '%s' "$output" | jq -e '.ok and .can_run and (.targets | map(.id) | index("split")) != null and (.targets | map(select(.kind == "ollama")) | length) > 0' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/ai/search?q=qwen&sort=likes"
    printf '%s' "$output" | jq -e '.ok and (.results | length) >= 2 and all(.results[]; .id | test("(?i)qwen"))' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/ai/files?repo=Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"
    printf '%s' "$output" | jq -e '.ok and (.files | length) == 6 and (.files[0].size < .files[5].size) and .files[2].ollama == "hf.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/ai/files?repo=../etc"
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/ai/ollama"
    printf '%s' "$output" | jq -e '.ok and (.pods | length) == 3 and (.pods[0].models | map(.loaded) | any)' >/dev/null
}

@test "ai: chat streams the model's answer as server-sent events" {
    start_server
    run post /api/ai/chat '{"target":"split","messages":[{"role":"user","content":"hello"}]}'
    assert_success
    assert_output --partial 'data: {"choices"'
    assert_output --partial 'data: [DONE]'
}

@test "ai: chat refuses bad input and unknown models" {
    start_server
    run post /api/ai/chat '{"target":"split","messages":[]}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run post /api/ai/chat '{"target":"split","messages":[{"role":"root","content":"x"}]}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run post /api/ai/chat '{"target":"split","temperature":"hot","messages":[{"role":"user","content":"x"}]}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run post /api/ai/chat '{"target":"http://evil.example/","messages":[{"role":"user","content":"x"}]}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
}

@test "ai: a task starts, shows its output and finishes; bad requests and a second task are refused" {
    start_server
    run post /api/run '{"action":"split-unload"}'
    local job
    job="$(printf '%s' "$output" | jq -r '.job')"
    [[ -n "$job" && "$job" != null ]] || fail "no task started: $output"
    run post /api/run '{"action":"split-load"}'
    printf '%s' "$output" | jq -e '.ok == false and (.error | test("still running"))' >/dev/null
    local i status=""
    for ((i = 0; i < 50; i++)); do
        status="$(curl -s "http://127.0.0.1:${PORT}/api/job?id=${job}&since=0" | jq -r '.status')"
        [[ "$status" == ok ]] && break
        sleep 0.2
    done
    assert_equal "$status" ok
    run curl -s "http://127.0.0.1:${PORT}/api/job?id=${job}&since=1"
    printf '%s' "$output" | jq -e '.ok and (.lines | length) >= 1 and .next >= 2' >/dev/null
    run post /api/run '{"action":"deploy","repo":"../x","file":"a.gguf"}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run post /api/run '{"action":"rm -rf"}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/job?id=nope"
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
}

@test "ai: loading and unloading an Ollama model" {
    start_server
    run post /api/ai/ollama-load '{"pod":"ollama-demo-yard-2","model":"llama3.2:3b","load":true}'
    printf '%s' "$output" | jq -e '.ok == true' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/ai/ollama"
    printf '%s' "$output" | jq -e '[.pods[] | select(.pod == "ollama-demo-yard-2") | .models[] | select(.name == "llama3.2:3b")][0].loaded == true' >/dev/null
    run post /api/ai/ollama-load '{"pod":"ollama-demo-yard-2","model":"llama3.2:3b","load":false}'
    run curl -s "http://127.0.0.1:${PORT}/api/ai/ollama"
    printf '%s' "$output" | jq -e '[.pods[] | select(.pod == "ollama-demo-yard-2") | .models[] | select(.name == "llama3.2:3b")][0].loaded == false' >/dev/null
    run post /api/ai/ollama-load '{"pod":"nope","model":"x","load":true}'
    printf '%s' "$output" | jq -e '.ok == false' >/dev/null
}

@test "ai: POSTs without the dashboard's own headers or from another site are refused" {
    start_server
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/run" -H 'Content-Type: application/json' -d '{"action":"status"}'
    assert_output 403
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/run" "${JSON_HDRS[@]}" -H 'Origin: http://evil.example' -d '{"action":"status"}'
    assert_output 403
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/ai/chat" -H 'Content-Type: text/plain' -H 'X-Nodeyard: 1' -d '{}'
    assert_output 403
}

@test "ai: everything on the AI page needs a sign-in when there's a password" {
    start_signin_server
    local p
    for p in /api/ai/targets "/api/ai/search?q=x" /api/ai/ollama "/api/job?id=x" /api/agents; do
        run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}${p}"
        assert_output 401
    done
    for p in /api/ai/chat /api/run /api/ai/ollama-load /api/ai/reveal-key; do
        run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}${p}" "${JSON_HDRS[@]}" -d '{}'
        assert_output 401
    done
    run login "$KEY"
    assert_output 200
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" "http://127.0.0.1:${PORT}/api/ai/targets"
    printf '%s' "$output" | jq -e '.ok' >/dev/null
}

@test "agents: per-node processes, clocks and temperatures, with pods named" {
    start_server
    run curl -s "http://127.0.0.1:${PORT}/api/state"
    printf '%s' "$output" | jq -e '.state.agents.installed and .state.agents.ready == 4 and all(.state.nodes[]; .hw.freq_mhz > 0 and .hw.temp_c > 0 and (.hw.load | length) == 3)' >/dev/null
    run curl -s "http://127.0.0.1:${PORT}/api/agents?node=yard-3"
    printf '%s' "$output" | jq -e '.ok and (.nodes | keys) == ["yard-3"] and (.nodes["yard-3"].processes | length) > 5 and (.nodes["yard-3"].cpu.cores | length) == 8
        and any(.nodes["yard-3"].processes[]; .group.kind == "pod" and (.group.name | test("/")))' >/dev/null
}

@test "dashboard agent install --dry-run shows a locked-down read-only DaemonSet" {
    run ny_cmd_run dashboard agent install --dry-run --yes
    assert_success
    assert_output --partial "kind: DaemonSet"
    assert_output --partial "hostPID: true"
    assert_output --partial "readOnlyRootFilesystem: true"
    assert_output --partial "allowPrivilegeEscalation: false"
    assert_output --partial "drop: [ALL]"
    assert_output --partial "runAsNonRoot: true"
    assert_output --partial 'token: "***"'
    assert_output --partial "Nothing was installed"
}

@test "dashboard agent status with no agents says how to install them" {
    run ny_cmd_run dashboard agent status
    assert_success
    assert_output --partial "dashboard agent install"
}

@test "the service may write nodeyard's own state and nothing else" {
    run ny_cmd_run dashboard start --dry-run --yes
    assert_success
    assert_output --partial "ReadWritePaths=-/var/lib/nodeyard -/var/log/nodeyard -/etc/nodeyard"
    assert_output --partial "ProtectSystem=strict"
    assert_output --partial "--nodeyard-bin"
    assert_output --partial "--agent-token-file /etc/nodeyard/secrets/agent-token"
}

# --- real time ----------------------------------------------------------------------

@test "real time: the stream pushes a fresh snapshot every time the cluster is read" {
    start_server --interval 1
    run bash -c 'curl -s -N --max-time 4 "http://127.0.0.1:$0/api/stream" || true' "$PORT"
    local n
    n="$(grep -c '^event: state' <<<"$output")"
    ((n >= 3)) || fail "expected several state events in 4 seconds, got ${n}"
    grep -m1 '^data: ' <<<"$output" | sed 's/^data: //' | jq -e '.ok == true and (.state.nodes | length) == 4 and .totals.nodes_ready == 4 and .mode == "demo"' >/dev/null
}

@test "real time: the agents' data rides along only when asked for" {
    start_server --interval 1
    run bash -c 'curl -s -N --max-time 2 "http://127.0.0.1:$0/api/stream?agents=1" || true' "$PORT"
    grep -m1 '^data: ' <<<"$output" | sed 's/^data: //' | jq -e '.agents_full.ok == true and (.agents_full.nodes | keys | length) == 4' >/dev/null
    run bash -c 'curl -s -N --max-time 2 "http://127.0.0.1:$0/api/stream" || true' "$PORT"
    grep -m1 '^data: ' <<<"$output" | sed 's/^data: //' | jq -e 'has("agents_full") | not' >/dev/null
}

@test "real time: the stream needs a sign-in too" {
    start_signin_server
    run curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/api/stream"
    assert_output 401
    run login "$KEY"
    run bash -c 'curl -s -N --max-time 2 -b "$1" "http://127.0.0.1:$0/api/stream" || true' "$PORT" "${BATS_TEST_TMPDIR}/jar"
    assert_output --partial "event: state"
}

# --- sign-in -----------------------------------------------------------------------

@test "sign-in: without a session the API says 401 and pages go to the sign-in page" {
    start_signin_server
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/state"
    assert_output 401
    run curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://127.0.0.1:${PORT}/"
    assert_output "302 http://127.0.0.1:${PORT}/login"
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/app.js"
    assert_output 302
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login"
    assert_output 200
    run curl -s "http://127.0.0.1:${PORT}/api/auth"
    printf '%s' "$output" | jq -e '.enabled == true and .authenticated == false' >/dev/null
}

@test "sign-in: the right password (a generated one: any case, dashes optional) gives a session cookie that works, then logs out" {
    start_signin_server
    run login "abcd ef01-2345-6789-abcd-ef01"
    assert_output 200
    run grep -E 'HttpOnly' "${BATS_TEST_TMPDIR}/jar"
    assert_success
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" "http://127.0.0.1:${PORT}/api/state"
    printf '%s' "$output" | jq -e '.ok == true and .auth == true' >/dev/null
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/"
    assert_output 200
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" -X POST "http://127.0.0.1:${PORT}/api/logout" "${JSON_HDRS[@]}" -d '{}'
    assert_success
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/state"
    assert_output 401
}

@test "sign-in: a wrong password is refused, and the cookie is SameSite=Strict" {
    start_signin_server
    run login "WRONG-KEY"
    assert_output 401
    run curl -si -X POST "http://127.0.0.1:${PORT}/api/login" "${JSON_HDRS[@]}" -d "{\"password\":\"$KEY\"}"
    assert_output --partial "SameSite=Strict"
    assert_output --partial "HttpOnly"
}

@test "sign-in: five wrong passwords lock the address out for a while" {
    start_signin_server
    local i
    for i in 1 2 3 4 5; do
        run login "nope-$i"
        assert_output 401
    done
    run login "$KEY"
    assert_output 429
}

@test "sign-in: a password you chose must match exactly (case counts)" {
    printf 'huskiboi' >"${BATS_TEST_TMPDIR}/key"
    start_server --password-file "${BATS_TEST_TMPDIR}/key"
    run login "Huskiboi"
    assert_output 401
    run login "huskiboi"
    assert_output 200
}

@test "sign-in: an empty password file stops the server from starting" {
    printf '  ' >"${BATS_TEST_TMPDIR}/key"
    run python3 "$SERVER" --demo --password-file "${BATS_TEST_TMPDIR}/key" --port 0
    assert_failure
    assert_output --partial "empty"
}

@test "sign-in: a request that didn't come from the dashboard's own page is refused" {
    start_signin_server
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/login" -H 'Content-Type: application/json' -d "{\"password\":\"$KEY\"}"
    assert_output 403
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/login" "${JSON_HDRS[@]}" -H 'Origin: http://evil.example' -d "{\"password\":\"$KEY\"}"
    assert_output 403
    run curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/api/login" -H 'Content-Type: text/plain' -H 'X-Nodeyard: 1' -d "{\"password\":\"$KEY\"}"
    assert_output 403
}

@test "sign-in: a second address works too (this machine's 127.0.0.2 stands in for Tailscale)" {
    [[ "$(uname)" == Linux ]] || skip "needs Linux's whole 127/8 loopback"
    start_signin_server --listen local,127.0.0.2
    run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.2:${PORT}/api/state"
    assert_output 401
    run curl -s -c "${BATS_TEST_TMPDIR}/jar" -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.2:${PORT}/api/login" "${JSON_HDRS[@]}" -d "{\"password\":\"$KEY\"}"
    assert_output 200
    run curl -s -b "${BATS_TEST_TMPDIR}/jar" "http://127.0.0.2:${PORT}/api/state"
    printf '%s' "$output" | jq -e '.ok == true' >/dev/null
}

@test "the web page's scripts are valid JavaScript" {
    command -v node >/dev/null || skip "node isn't installed"
    local f
    for f in app.js charts.js theme.js login.js; do
        run node --check "${NY_REPO_ROOT}/share/nodeyard/dashboard/web/${f}"
        assert_success
    done
}

@test "the dashboard's Python unit tests pass" {
    run python3 -m unittest discover -s "${NY_REPO_ROOT}/tests/dashboard"
    assert_success
}
