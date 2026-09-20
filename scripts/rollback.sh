#!/bin/bash
# Put the server back on an earlier release, and stop the updater undoing it.
#
#   ssh <server> 'bash ~/work/alliegent/scripts/rollback.sh v1.0.0'
#   ssh <server> 'bash ~/work/alliegent/scripts/rollback.sh --resume'
#
# The updater fast-forwards to origin/main every hour, so a checkout on its own
# would last until the next run. This unloads it, and --resume returns to main
# and loads it again once main has the fix.
set -euo pipefail

REPO="${ALLIEGENT_REPO:-$HOME/work/alliegent}"
LABEL="${ALLIEGENT_LABEL:-com.alliegent.bot}"
UPDATER="${ALLIEGENT_UPDATE_LABEL:-com.alliegent.update}"
PLIST="$HOME/Library/LaunchAgents/$UPDATER.plist"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$REPO"
TARGET="${1:-}"
[ -n "$TARGET" ] || { echo "usage: rollback.sh <tag> | --resume"; git tag | tail -5; exit 1; }

[ -z "$(git status --porcelain)" ] || { echo "uncommitted changes in $REPO"; exit 1; }
git fetch --quiet --tags origin main

if [ "$TARGET" = "--resume" ]; then
    git checkout --quiet main
    git merge --ff-only --quiet origin/main
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    echo "back on main at $(git rev-parse --short HEAD); updater running again"
else
    git rev-parse -q --verify "refs/tags/$TARGET" >/dev/null || { echo "no tag $TARGET"; exit 1; }
    launchctl bootout "gui/$(id -u)/$UPDATER" 2>/dev/null || true
    git checkout --quiet "$TARGET"
    echo "updater stopped; on $TARGET — run with --resume once main is fixed"
fi

uv sync --quiet
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "bot restarted on $(git describe --tags --always)"
