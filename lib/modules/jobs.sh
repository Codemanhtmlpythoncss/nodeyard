# shellcheck shell=bash
# Remote model training (nodeyard jobs): trains the same model on several machines
# over the tailnet (or on this machine), keeps the best copy, and manages the
# training SSH service on the master (key-only, bound to the tailnet address).
# The runner is share/nodeyard/jobs/nyjobs.py; the master script is
# share/nodeyard/jobs/training-access.sh. See docs/remote-training.md.

NY_JOBS_DIR="${NY_HOME}/share/nodeyard/jobs"

# NODEYARD_JOBS_PYTHON selects the Python that has torch and numpy (default: python3).
jobs_python() { "${NODEYARD_JOBS_PYTHON:-python3}" "${NY_JOBS_DIR}/nyjobs.py" "$@"; }
jobs_hosts_cmd() { jobs_python hosts "$@"; }
jobs_probe_cmd() { jobs_python probe "$@"; }
jobs_plan_cmd() { jobs_python plan "$@"; }
jobs_run_cmd() { jobs_python run "$@"; }
jobs_add_host_cmd() { jobs_python add-host "$@"; }
jobs_remove_host_cmd() { jobs_python remove-host "$@"; }
jobs_access_key_cmd() { jobs_python access-key "$@"; }
jobs_access_cmd() { sh "${NY_JOBS_DIR}/training-access.sh" "$@"; }

ny_cmd "jobs hosts" jobs_hosts_cmd "Jobs" "List training machines and their free capacity" jobs
ny_cmd "jobs probe" jobs_probe_cmd "Jobs" "Check each training machine (architecture, CPUs, memory, torch); read-only" jobs
ny_cmd "jobs plan" jobs_plan_cmd "Jobs" "Show where the training copies would run" jobs
ny_cmd "jobs run" jobs_run_cmd "Jobs" "Train copies in parallel across machines and keep the best" jobs
ny_cmd "jobs add-host" jobs_add_host_cmd "Jobs" "Add a training machine (name, tailnet address, user, port)" jobs
ny_cmd "jobs remove-host" jobs_remove_host_cmd "Jobs" "Remove a training machine" jobs
ny_cmd "jobs access-key" jobs_access_key_cmd "Jobs" "Print this Mac's SSH public key for the master" jobs
ny_cmd "jobs access" jobs_access_cmd "Jobs" "Set up, check, or remove the master's key-only training SSH (setup, status, remove, runtime)" jobs
