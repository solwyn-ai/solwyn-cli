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
      .github/workflows/*|.github/scripts/*|examples/mcp/*|tests/fixtures/*|\
      scripts/mcp_wheel_smoke.py|scripts/inspect_wheel.py|scripts/locks/*|\
      pyproject.toml|uv.lock|hatch*|build*|setup.*|MANIFEST*|\
      requirements*.txt|constraints*.txt|.python-version|.gitattributes)
        os="$all_os" ;;
      # Platform helpers in source should default to cross-OS.
      src/*windows*|src/*macos*|src/*darwin*|src/*posix*|\
      src/*.ps1|src/*.bat|src/*.cmd|src/*.sh)
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
      # native atomic artifacts. Wheel acceptance does not run pytest; test-only
      # changes retain all Python tests and the Linux wheel leg.
      src/*)
        os="$all_os" ;;
    esac
    if [[ "$os" == "$all_os" ]]; then
      break
    fi
  done < "$changed_files"
fi

printf 'os=%s\n' "$os" >> "$GITHUB_OUTPUT"
