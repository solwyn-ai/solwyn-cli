#!/usr/bin/env bash
set -euo pipefail

all_os='["ubuntu-latest","macos-latest","windows-latest"]'
os="$all_os"

if [[ "$GITHUB_EVENT_NAME" == pull_request ]]; then
  os='["ubuntu-latest"]'
  changed_files=$(mktemp)
  trap 'rm -f "$changed_files"' EXIT
  # Match the PR's merge-base diff, not changes made only on the base branch.
  # Disabling rename detection also catches moves away from sensitive paths.
  git diff --name-only --no-renames -z \
    "${PR_BASE_SHA:?}...${PR_HEAD_SHA:?}" > "$changed_files"
  while IFS= read -r -d '' path; do
    case "$path" in
      # Build inputs, acceptance clients/locks, and the CI policy itself.
      .github/*|scripts/*|examples/mcp/*|docs/registry/*|\
      pyproject.toml|uv.lock|hatch*|build*|setup.*|MANIFEST*|\
      requirements*.txt|constraints*.txt|.python-version|.gitattributes|\
      README.md|LICENSE*)
        os="$all_os" ;;
      # New platform helpers and shell scripts should default to cross-OS.
      *windows*|*macos*|*darwin*|*posix*|*.ps1|*.bat|*.cmd|*.sh)
        os="$all_os" ;;
      # Commands that launch MCP/browsers, use keychains, or handle paths.
      src/solwyn_cli/commands/mcp*|src/solwyn_cli/commands/config.py|\
      src/solwyn_cli/commands/init.py|src/solwyn_cli/commands/login.py|\
      src/solwyn_cli/commands/doctor.py|src/solwyn_cli/commands/agent_context.py|\
      src/solwyn_cli/commands/budget.py|src/solwyn_cli/commands/record.py|\
      src/solwyn_cli/commands/projects.py|src/solwyn_cli/commands/plan.py|\
      src/solwyn_cli/commands/invite.py)
        os="$all_os" ;;
      # Pure API commands/contracts retain all Python tests and the Linux wheel.
      src/solwyn_cli/commands/*|src/solwyn_cli/_vendor/*|src/solwyn_cli/spec/*)
        ;;
      # All other shared/new source includes startup, config, auth, MCP and
      # native atomic artifacts. Their regression tests must also opt in.
      src/*|tests/*mcp*|tests/fixtures/*|tests/conftest.py|tests/test_ci_*|\
      tests/test_config*|tests/test_auth*|tests/test_runtime.py|\
      tests/test_foundation.py|tests/test_smoke.py|tests/test_doctor*|\
      tests/test_init*|tests/test_atomic*|tests/test_agent_artifacts.py|\
      tests/test_budget*|tests/test_record*|tests/test_shared_command_auth.py|\
      tests/test_projects*|tests/test_plan*|tests/test_invite*|\
      tests/test_billing*|tests/test_team*|tests/test_reliability*)
        os="$all_os" ;;
    esac
    if [[ "$os" == "$all_os" ]]; then
      break
    fi
  done < "$changed_files"
fi

printf 'os=%s\n' "$os" >> "$GITHUB_OUTPUT"
