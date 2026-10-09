#!/usr/bin/env bash
# Keep the fork's Linux CLI/Telegram contracts in every push and PR.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec bash scripts/run_tests.sh \
  tests/agent/test_redact.py \
  tests/tools/test_reboot_approval.py \
  tests/tools/test_hardline_escaped_quote_state.py \
  tests/gateway/test_update_command.py \
  tests/gateway/test_update_handoff.py \
  tests/gateway/test_telegram_approval_buttons.py \
  tests/hermes_cli/test_cmd_update.py \
  tests/hermes_cli/test_update_completion_routing.py \
  tests/hermes_cli/test_update_channel.py \
  tests/hermes_cli/test_update_release_channel.py \
  tests/hermes_cli/test_source_check.py \
  tests/hermes_cli/test_update_autostash.py \
  tests/hermes_cli/test_update_host_obligation.py \
  tests/hermes_cli/test_update_fleet_restart_pending.py \
  tests/hermes_cli/test_fleet_matrix_self_restart_pending.py \
  tests/scripts/install/test_install_fork_channel.py \
  tests/scripts/install/test_install_diverged_rescue_ref.py \
  "$@"
