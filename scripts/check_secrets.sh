#!/bin/bash
# Refuse to commit anything that looks like a credential.
#
# This repo is public, and a leaked API token is not protected by the account's
# passkey or MFA -- the token *is* the authentication. Once pushed it is public
# immediately and stays in the history after any later fix, so the check has to
# happen before the commit, not in CI afterwards.
#
# Installed as a pre-commit hook by scripts/install_hooks.sh. Run directly to
# check the whole working tree instead of just what is staged.
#
# Never prints a matched value: the point is to stop a secret from spreading,
# and terminal scrollback and CI logs are places it would spread to.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

STAGED=${1:-staged}
fail=0

if [ "$STAGED" = "staged" ]; then
    files=$(git diff --cached --name-only --diff-filter=ACM)
else
    files=$(git ls-files)
fi
[ -z "$files" ] && exit 0

# 1. Files that should never be committed at all, whatever is in them.
while IFS= read -r file; do
    case "$(basename "$file")" in
        .env.example) ;;
        .env|.env.*|*.pem|*.key|id_rsa|id_ed25519)
            echo "BLOCKED: $file should not be committed"
            fail=1
            ;;
    esac
done <<< "$files"

# 2. Credential-shaped strings. Deliberately narrow: a pattern that fires on
#    ordinary code gets switched off, and then it protects nothing.
PATTERNS='ntn_[A-Za-z0-9]{25,}|secret_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}|[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}'

while IFS= read -r file; do
    [ -f "$file" ] || continue
    [ "$(basename "$file")" = "check_secrets.sh" ] && continue
    if grep -aInE "$PATTERNS" "$file" | grep -qv "xxxx"; then
        line=$(grep -anE "$PATTERNS" "$file" | grep -v "xxxx" | head -1 | cut -d: -f1)
        echo "BLOCKED: $file:$line looks like a credential"
        fail=1
    fi
done <<< "$files"

# 3. Personal data. Not a credential, but this repo is public and an agenda
#    is a record of where someone is and when. Real item names, addresses and
#    phone numbers belong in Notion, not in an example or a test fixture.
PERSONAL='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+?[0-9]{2,3}-[0-9]{3,4}-[0-9]{4}|\b[0-9]{17,20}\b'
ALLOWED_EMAIL='noreply@anthropic|example\.(com|org)|\.example\b|your@|user@|smtp\.gmail\.com'

while IFS= read -r file; do
    [ -f "$file" ] || continue
    case "$(basename "$file")" in check_secrets.sh|uv.lock|*.lock) continue ;; esac
    if grep -aInE "$PERSONAL" "$file" | grep -qvE "$ALLOWED_EMAIL"; then
        line=$(grep -anE "$PERSONAL" "$file" | grep -vE "$ALLOWED_EMAIL" | head -1 | cut -d: -f1)
        echo "BLOCKED: $file:$line looks like personal data (email, phone, or a Discord/Notion id)"
        fail=1
    fi
done <<< "$files"

# 4. The strongest check, and only possible locally: the real values. A secret
#    that matches no known pattern still must not appear in a tracked file.
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        case "$key" in ''|\#*) continue ;; esac
        value=${value%%#*}
        value=$(echo "$value" | tr -d '"'"'"' ' | tr -d '\r')
        # Short values are words like a calendar name, not credentials, and
        # matching them would flag ordinary code.
        [ ${#value} -lt 16 ] && continue
        while IFS= read -r file; do
            [ -f "$file" ] || continue
            if grep -aqF "$value" "$file" 2>/dev/null; then
                echo "BLOCKED: $file contains the value of $key from .env"
                fail=1
            fi
        done <<< "$files"
    done < .env
fi

if [ "$fail" -ne 0 ]; then
    echo
    echo "Nothing was committed. Credentials and personal data go in .env"
    echo "(gitignored) or stay in Notion; the repo keeps key names and"
    echo "invented examples only."
    exit 1
fi
exit 0
