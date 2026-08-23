#!/bin/bash
# Point git at the hooks kept in this repo.
#
# Hooks live in .git/hooks, which is not version-controlled, so every clone
# starts without them -- including the one on the server. This makes the
# repo's own hooks the ones that run.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x scripts/check_secrets.sh .githooks/*
git config core.hooksPath .githooks
echo "Hooks enabled: $(git config core.hooksPath)"
