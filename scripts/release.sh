#!/bin/bash
# Cut a release: bump the version, write a changelog entry, tag it, publish it.
#
#   scripts/release.sh 1.1.0
#
# The Mac mini follows main rather than tags, so this does not deploy anything
# -- it names a state that worked, so there is something to go back to. See
# scripts/rollback.sh for going back to one.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"
[ -n "$VERSION" ] || { echo "usage: scripts/release.sh <version>   e.g. 1.1.0"; exit 1; }
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "version should look like 1.1.0, not '$VERSION'"; exit 1
fi
TAG="v$VERSION"

# Releasing from a dirty or stale tree produces a tag that points at code
# nobody has, which is the one thing a rollback point must never be.
[ -z "$(git status --porcelain)" ] || { echo "working tree has uncommitted changes"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "releases are cut from main"; exit 1; }
git fetch --quiet origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || {
    echo "main and origin/main differ; push or pull first"; exit 1
}
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && { echo "$TAG already exists"; exit 1; }

# The tests that guard the mini's own updates guard this too: a tagged state
# that fails them is worse than no tag, because it looks safe to return to.
echo "running tests..."
PYTHONPATH=src uv run --no-sync pytest -q >/tmp/alliegent-release-tests.log 2>&1 || {
    echo "tests failed; not releasing"; tail -20 /tmp/alliegent-release-tests.log; exit 1
}

PREVIOUS=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [ -n "$PREVIOUS" ]; then
    NOTES=$(git log --reverse --pretty='- %s' "$PREVIOUS..HEAD")
    RANGE="since $PREVIOUS"
else
    NOTES=$(git log --reverse --pretty='- %s')
    RANGE="all of it"
fi
[ -n "$NOTES" ] || { echo "nothing new $RANGE"; exit 1; }

echo
echo "$TAG ($RANGE):"
echo "$NOTES"
echo
read -r -p "cut this release? [y/N] " reply
[ "$reply" = "y" ] || { echo "nothing done"; exit 0; }

# Version lives in pyproject.toml; keep it and the tag from drifting apart.
# Anchored to the start of the line, which only [project]'s own version is --
# a dependency pin is always indented or inline.
grep -qE '^version = ".*"' pyproject.toml || { echo "no version line in pyproject.toml"; exit 1; }
/usr/bin/sed -i '' -E "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml

# uv.lock records the project's own version too. Left out of the release
# commit, the server's `uv sync` rewrites it, and a dirty tree there stops
# every later update -- which is how v1.0.0 went.
uv lock --quiet

# Spliced in above the newest existing entry, so the file's preamble -- which
# explains the format -- stays at the top.
FIRST=$(grep -n '^## ' CHANGELOG.md | head -1 | cut -d: -f1)
{
    if [ -n "$FIRST" ]; then head -n "$((FIRST - 1))" CHANGELOG.md; else cat CHANGELOG.md; echo; fi
    echo "## $TAG — $(date '+%Y-%m-%d')"
    echo
    echo "$NOTES"
    echo
    [ -n "$FIRST" ] && tail -n "+$FIRST" CHANGELOG.md
} > CHANGELOG.md.new && mv CHANGELOG.md.new CHANGELOG.md

${EDITOR:-nano} CHANGELOG.md

git add pyproject.toml uv.lock CHANGELOG.md
git commit --quiet -m "Release $TAG"
git tag -a "$TAG" -m "$TAG"
git push --quiet origin main
git push --quiet origin "$TAG"

# --notes-file -, so the notes are the changelog entry as edited, not a second
# version of it written from the same commits.
if command -v gh >/dev/null 2>&1; then
    awk '/^## /{ n++ } n == 1 && !/^## /' CHANGELOG.md |
        gh release create "$TAG" --title "$TAG" --notes-file -
else
    echo "gh not installed; tag pushed, GitHub release not created"
fi

echo "released $TAG"
