#!/usr/bin/env bash
set -euo pipefail

# Pull requests build and smoke-test the Linux wheel only. macOS and Windows run
# after merge (main pushes, the weekly schedule, manual runs), and publish.yml
# re-runs them on every v* tag before release. To check a branch on every OS
# before merging, dispatch CI on it: gh workflow run CI --ref <branch>
if [[ "$GITHUB_EVENT_NAME" == pull_request ]]; then
  os='["ubuntu-latest"]'
else
  os='["ubuntu-latest","macos-latest","windows-latest"]'
fi

printf 'os=%s\n' "$os" >> "$GITHUB_OUTPUT"
