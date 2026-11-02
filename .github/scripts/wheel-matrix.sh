#!/usr/bin/env bash
set -euo pipefail

# Pull requests and main pushes smoke-test the Linux wheel only. macOS and
# Windows run on the weekly schedule and manual runs, and publish.yml re-runs
# them on every v* tag before release, so no release skips them. To check a
# branch on every OS before merging, dispatch CI on it:
#   gh workflow run CI --ref <branch>
case "$GITHUB_EVENT_NAME" in
  pull_request | push)
    os='["ubuntu-latest"]'
    ;;
  *)
    os='["ubuntu-latest","macos-latest","windows-latest"]'
    ;;
esac

printf 'os=%s\n' "$os" >> "$GITHUB_OUTPUT"
