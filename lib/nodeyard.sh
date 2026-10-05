# shellcheck shell=bash
# Loads all of nodeyard. Sourced by bin/nodeyard (and by the tests) after
# NY_HOME is set. Libraries only define functions and variables; nothing
# runs until ny_main is called.

# "printf ... | ny_write_file" must run ny_write_file in this shell, so the
# --dry-run plan and the journal see every change (needs job control off,
# which it is for scripts).
shopt -s lastpipe

# shellcheck source=core/base.sh
. "${NY_HOME}/lib/core/base.sh"
# shellcheck source=core/term.sh
. "${NY_HOME}/lib/core/term.sh"
# shellcheck source=core/log.sh
. "${NY_HOME}/lib/core/log.sh"
# shellcheck source=core/json.sh
. "${NY_HOME}/lib/core/json.sh"
# shellcheck source=core/errors.sh
. "${NY_HOME}/lib/core/errors.sh"
# shellcheck source=core/journal.sh
. "${NY_HOME}/lib/core/journal.sh"
# shellcheck source=core/run.sh
. "${NY_HOME}/lib/core/run.sh"
# shellcheck source=core/validate.sh
. "${NY_HOME}/lib/core/validate.sh"
# shellcheck source=core/config.sh
. "${NY_HOME}/lib/core/config.sh"
# shellcheck source=core/detect.sh
. "${NY_HOME}/lib/core/detect.sh"
# shellcheck source=core/deps.sh
. "${NY_HOME}/lib/core/deps.sh"
# shellcheck source=core/secrets.sh
. "${NY_HOME}/lib/core/secrets.sh"
# shellcheck source=core/ui.sh
. "${NY_HOME}/lib/core/ui.sh"
# shellcheck source=core/registry.sh
. "${NY_HOME}/lib/core/registry.sh"
# shellcheck source=core/wizard.sh
. "${NY_HOME}/lib/core/wizard.sh"
# shellcheck source=core/kube.sh
. "${NY_HOME}/lib/core/kube.sh"
# shellcheck source=core/ssh.sh
. "${NY_HOME}/lib/core/ssh.sh"
# shellcheck source=core/demo.sh
. "${NY_HOME}/lib/core/demo.sh"

# Feature modules, in the order their commands appear in help.
# shellcheck source=modules/menu.sh
. "${NY_HOME}/lib/modules/menu.sh"
# shellcheck source=modules/info.sh
. "${NY_HOME}/lib/modules/info.sh"
# shellcheck source=modules/host.sh
. "${NY_HOME}/lib/modules/host.sh"
# shellcheck source=modules/firewall.sh
. "${NY_HOME}/lib/modules/firewall.sh"
# shellcheck source=modules/k3s.sh
. "${NY_HOME}/lib/modules/k3s.sh"
# shellcheck source=modules/cluster.sh
. "${NY_HOME}/lib/modules/cluster.sh"
# shellcheck source=modules/ai.sh
. "${NY_HOME}/lib/modules/ai.sh"
# shellcheck source=modules/ai_split.sh
. "${NY_HOME}/lib/modules/ai_split.sh"
# shellcheck source=modules/doctor.sh
. "${NY_HOME}/lib/modules/doctor.sh"
# shellcheck source=modules/backup.sh
. "${NY_HOME}/lib/modules/backup.sh"
# shellcheck source=modules/config_cmd.sh
. "${NY_HOME}/lib/modules/config_cmd.sh"
# shellcheck source=modules/changes.sh
. "${NY_HOME}/lib/modules/changes.sh"
# shellcheck source=modules/tool.sh
. "${NY_HOME}/lib/modules/tool.sh"
