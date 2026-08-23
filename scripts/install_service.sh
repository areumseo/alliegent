#!/bin/bash
# Install the launchd jobs for the current user.
#
# The plists are templates: launchd expands neither ~ nor $HOME, so a
# committed plist would have to hardcode one person's home directory and name
# them in a public repo. __HOME__ is filled in here instead.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/Library/Logs/alliegent"

install_one() {
    local label=$1 template=$2
    sed "s|__HOME__|$HOME|g" "$template" > "$AGENTS/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
    echo "installed $label"
}

install_one com.alliegent.update scripts/com.alliegent.update.plist
echo
echo "The bot itself is installed separately, as $\{ALLIEGENT_LABEL:-com.alliegent.bot\}."
