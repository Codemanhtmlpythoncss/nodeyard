"""Run a short shell script on one node through a throwaway helper pod, and get its output back.

The output comes back as the pod's termination message, which the node's kubelet reports to the API server itself, so
it arrives even when the control node can't reach that kubelet (`kubectl logs` can't); the logs are the fallback.
Only the first ~4 KB fits in a termination message, so scripts print their most useful lines first.

host=False: the node's root filesystem read-only at /host (scans).
host=True:  privileged, in the node's own namespaces (nsenter into PID 1) as root: for the Tuning page's changes, which
            the page shows and asks about before running. Nothing else uses it.
"""
import json
import os
import re
import shutil
import subprocess
import time

NS = "nodeyard-shell"
IMAGE = "busybox:1.36"


class NodeExecError(Exception):
    pass


def kubectl_argv():
    k3s = shutil.which("k3s") or ("/usr/local/bin/k3s" if os.path.exists("/usr/local/bin/k3s") else "")
    if k3s:
        return [k3s, "kubectl"]
    kc = shutil.which("kubectl")
    return [kc] if kc else None


def _kubectl(argv, kubeconfig, args, stdin=None, timeout=30):
    if not argv:
        raise NodeExecError("kubectl isn't available on the dashboard's server")
    p = subprocess.run(argv + ["--kubeconfig", kubeconfig] + args, input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def run(kubeconfig, node, script, host=False, deadline=300, kind="helper"):
    """The script's output (text). Raises NodeExecError with a plain reason."""
    argv = kubectl_argv()
    pod = "%s-%s-%s" % (kind, re.sub(r"[^a-z0-9-]+", "-", node.lower()).strip("-")[:30] or "node", os.urandom(3).hex())
    # the answer goes to the termination message (and the logs)
    wrapper = 'out=$(%s 2>&1); printf "%%s\\n" "$out" | head -c 3900 > /dev/termination-log; printf "%%s\\n" "$out"' % (
        'nsenter -t 1 -m -u -i -n -p -- sh -c "$0"' if host else 'nice -n 15 sh -c "$0" scan /host')
    container = {"name": kind, "image": IMAGE, "imagePullPolicy": "IfNotPresent", "command": ["sh", "-c", wrapper, script],
                 "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}, "limits": {"memory": "128Mi"}}}
    spec = {"nodeName": node, "restartPolicy": "Never", "activeDeadlineSeconds": deadline, "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 0, "tolerations": [{"operator": "Exists"}], "containers": [container]}
    if host:
        spec["hostPID"] = True
        container["securityContext"] = {"privileged": True}
    else:
        container["volumeMounts"] = [{"name": "host", "mountPath": "/host", "readOnly": True}]
        spec["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
    manifest = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "namespace": NS, "labels": {"app": "nodeyard-" + kind}}, "spec": spec}
    _kubectl(argv, kubeconfig, ["create", "namespace", NS])      # (already there is fine)
    rc, _out, err = _kubectl(argv, kubeconfig, ["apply", "-f", "-"], stdin=json.dumps(manifest).encode())
    if rc:
        raise NodeExecError("couldn't start the helper on %s: %s" % (node, err.strip()[-200:]))
    try:
        end = time.time() + deadline + 30
        while time.time() < end:
            time.sleep(2)
            rc, out, _err = _kubectl(argv, kubeconfig, ["-n", NS, "get", "pod", pod, "-o", "json"])
            if rc:
                continue
            st = json.loads(out).get("status") or {}
            phase = st.get("phase")
            if phase in ("Succeeded", "Failed"):
                term = (((st.get("containerStatuses") or [{}])[0].get("state") or {}).get("terminated") or {})
                text = term.get("message") or ""
                if text.count("\n") < 2:          # nothing came back that way: the logs need the kubelet reachable
                    rc, logs, _ = _kubectl(argv, kubeconfig, ["-n", NS, "logs", pod], timeout=20)
                    text = logs if rc == 0 and logs.strip() else text
                if not text.strip():
                    raise NodeExecError("the helper on %s %s without an answer" % (node, "failed" if phase == "Failed" else "finished"))
                return text
            reason = st.get("reason") or ""
            if reason.startswith("Evicted") or reason in ("OutOfpods", "OutOfDisk", "NodeAffinity"):
                raise NodeExecError("%s refused the helper (%s): its disk or memory may be full" % (node, reason))
        raise NodeExecError("the helper on %s didn't finish in time (is the node up?)" % node)
    finally:
        _kubectl(argv, kubeconfig, ["-n", NS, "delete", "pod", pod, "--ignore-not-found", "--wait=false"])
