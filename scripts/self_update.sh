#!/bin/bash
# Pull main on the server and restart the bot -- but only if the new commit's
# tests pass. A push from the laptop shouldn't be able to take the bot down,
# so a failing commit is rolled back and the running version is left alone.
#
# Run from launchd (see scripts/com.areumseo.alliegent-update.plist).
set -uo pipefail

REPO="${ALLIEGENT_REPO:-$HOME/work/alliegent}"
LABEL="com.areumseo.alliegent"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

cd "$REPO" || { log "no repo at $REPO"; exit 1; }

# Local edits on the server win. Overwriting someone's in-progress work to
# apply an update is the one failure here that isn't recoverable by rerunning.
if [ -n "$(git status --porcelain)" ]; then
    log "skipped: uncommitted changes in $REPO"
    exit 0
fi

git fetch --quiet origin main || { log "fetch failed"; exit 1; }

before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)
[ "$before" = "$after" ] && exit 0

log "updating ${before:0:7} -> ${after:0:7}"
git merge --ff-only --quiet origin/main || { log "not a fast-forward; left alone"; exit 1; }
uv sync --quiet

if ! PYTHONPATH=src uv run --no-sync pytest -q >/tmp/alliegent-update-tests.log 2>&1; then
    log "tests failed on ${after:0:7}; rolling back to ${before:0:7}"
    tail -20 /tmp/alliegent-update-tests.log
    git reset --hard --quiet "$before"
    uv sync --quiet
    exit 1
fi

launchctl kickstart -k "gui/$(id -u)/$LABEL" && log "restarted on ${after:0:7}"
